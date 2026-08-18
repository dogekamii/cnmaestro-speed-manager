from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .adapters import CnMaestroAdapter, JobResult
from .api import AmbiguousWrite
from .catalog import Catalog
from .models import BatchPlan, PlanItem
from .persistence import OperationState, OperationStore
from .planning import PlanBuilder, PlanInvalidated, classify_inventory


class JobValidationError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def validate_job(job: JobResult, *, mac: str, template: str) -> None:
    state = job.state.lower()
    if state in {"timed_out", "timeout"}:
        raise JobValidationError("timeout", "job polling timed out")
    if state != "completed":
        raise JobValidationError("unknown", f"job ended in ambiguous state: {job.state}")
    if job.skipped:
        raise JobValidationError("skipped", f"job skipped {job.skipped} device(s)")
    if job.failed:
        raise JobValidationError("failed", f"job failed {job.failed} device(s)")
    if job.remaining:
        raise JobValidationError("unknown", f"job reports {job.remaining} remaining")
    if job.success != 1:
        raise JobValidationError("failed", f"expected success=1, got {job.success}")
    if job.intended_mac is not None and job.intended_mac != mac:
        raise JobValidationError("target_mismatch", "job MAC does not match intended device")
    if job.intended_template is not None and job.intended_template != template:
        raise JobValidationError("target_mismatch", "job template does not match intended template")


@dataclass(frozen=True, slots=True)
class PublishResult:
    verified: int
    failed: int
    unknown: int
    planned: int


RemainderApproval = Callable[[PublishResult], bool | Awaitable[bool]]


class PublishService:
    def __init__(
        self,
        adapter: CnMaestroAdapter,
        store: OperationStore,
        catalog: Catalog,
        *,
        concurrency: int = 1,
        canary_size: int = 1,
        verification_attempts: int = 4,
        failure_threshold: int = 1,
        stop_on_first_issue: bool = True,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.catalog = catalog
        self._semaphore = asyncio.Semaphore(concurrency)
        if canary_size < 1:
            raise ValueError("canary size must be positive")
        self._canary_size = canary_size
        self._verification_attempts = verification_attempts
        self._failure_threshold = failure_threshold
        self._stop_on_first_issue = stop_on_first_issue
        self._sleep = sleep
        self._stop = asyncio.Event()
        self._issues = 0

    def stop_before_next(self) -> None:
        """Stop queued work only. Any in-flight network write continues."""
        self._stop.set()

    async def _wait(self, seconds: float) -> None:
        result = self._sleep(seconds)
        if inspect.isawaitable(result):
            await result

    async def publish(
        self,
        plan: BatchPlan,
        *,
        run_id: str,
        approve_remainder: RemainderApproval | None = None,
    ) -> PublishResult:
        self._stop.clear()
        self._issues = 0
        await self._assert_plan_current(plan)
        self.store.create_run(run_id, plan)  # durable before any PUT or pull
        canary_count = min(self._canary_size, len(plan.items))
        canary = plan.items[:canary_count]
        for item in canary:
            await self._process_item(run_id, item)

        if any(self.store.device(run_id, item.mac)["state"] != "verified" for item in canary):
            if len(plan.items) > canary_count:
                self.store.set_approval(run_id, "canary_failed")
            return self._result(run_id, plan)

        remainder = plan.items[canary_count:]
        if not remainder:
            self.store.set_approval(run_id, "not_required")
            return self._result(run_id, plan)

        canary_result = self._result(run_id, plan)
        approved = False
        if approve_remainder is not None:
            decision = approve_remainder(canary_result)
            approved = bool(await decision) if inspect.isawaitable(decision) else bool(decision)
        if not approved:
            self.store.set_approval(run_id, "declined")
            return canary_result

        self.store.set_approval(run_id, "approved")
        await asyncio.gather(*(self._process_item(run_id, item) for item in remainder))
        return self._result(run_id, plan)

    async def _assert_plan_current(self, plan: BatchPlan) -> None:
        if self.adapter.connection_identity != plan.connection_identity:
            raise PlanInvalidated("connection changed after preview")
        inventory = classify_inventory(await self.adapter.inventory(), self.catalog)
        by_mac = {device.mac: device for device in inventory}
        if len(by_mac) != len(inventory):
            raise PlanInvalidated("inventory contains duplicate device MACs")
        try:
            selected = tuple(by_mac[item.mac] for item in plan.items)
        except KeyError as exc:
            raise PlanInvalidated("selected device is missing from current inventory") from exc
        PlanBuilder(self.catalog).assert_current(plan, selected, plan.target_package)

    async def _process_item(self, run_id: str, item: PlanItem) -> None:
        async with self._semaphore:
            if self._stop.is_set():  # intentionally after acquisition
                return
            live = await self.adapter.pull_rates(item.mac)
            if live != item.before:
                self.store.transition(
                    run_id,
                    item.mac,
                    OperationState.FAILED,
                    error_category="stale_state",
                    error_detail=f"previewed {item.before}; live {live}",
                )
                self._issue()
                return
            self.store.transition(run_id, item.mac, OperationState.SUBMITTING)
            try:
                submission = await self.adapter.submit_template(item.mac, item.template)
            except AmbiguousWrite as exc:
                self.store.transition(
                    run_id,
                    item.mac,
                    OperationState.UNKNOWN,
                    attempts=1,
                    error_category="ambiguous_write",
                    error_detail=str(exc),
                )
                self._issue()
                return
            except Exception as exc:
                self.store.transition(
                    run_id,
                    item.mac,
                    OperationState.FAILED,
                    attempts=1,
                    error_category="submission_failed",
                    error_detail=str(exc),
                )
                self._issue()
                return
            self.store.transition(run_id, item.mac, OperationState.SUBMITTED, attempts=1)
            if not submission.job_id:
                self.store.transition(
                    run_id,
                    item.mac,
                    OperationState.UNKNOWN,
                    error_category="job_unknown",
                    error_detail="submission returned no job ID",
                )
                self._issue()
                return
            self.store.transition(
                run_id, item.mac, OperationState.JOB_KNOWN, job_id=submission.job_id
            )
            try:
                validate_job(
                    await self.adapter.job_status(submission.job_id),
                    mac=item.mac,
                    template=item.template,
                )
            except JobValidationError as exc:
                state = (
                    OperationState.UNKNOWN
                    if exc.category in {"unknown", "timeout"}
                    else OperationState.FAILED
                )
                self.store.transition(
                    run_id, item.mac, state, error_category=exc.category, error_detail=str(exc)
                )
                self._issue()
                return
            verified = None
            for attempt in range(self._verification_attempts):
                verified = await self.adapter.pull_rates(item.mac)
                if verified == item.target:
                    self.store.transition(
                        run_id,
                        item.mac,
                        OperationState.VERIFIED,
                        verified_dl=verified.downlink,
                        verified_ul=verified.uplink,
                    )
                    return
                if attempt + 1 < self._verification_attempts:
                    await self._wait(min(2**attempt, 8))
            assert verified is not None
            self.store.transition(
                run_id,
                item.mac,
                OperationState.FAILED,
                verified_dl=verified.downlink,
                verified_ul=verified.uplink,
                error_category="verification_mismatch",
                error_detail=f"expected {item.target}; observed {verified}",
            )
            self._issue()

    def _issue(self) -> None:
        self._issues += 1
        if self._stop_on_first_issue or self._issues >= self._failure_threshold:
            self._stop.set()

    def _result(self, run_id: str, plan: BatchPlan) -> PublishResult:
        states = [self.store.device(run_id, item.mac)["state"] for item in plan.items]
        return PublishResult(
            states.count("verified"),
            states.count("failed"),
            states.count("unknown"),
            states.count("planned"),
        )
