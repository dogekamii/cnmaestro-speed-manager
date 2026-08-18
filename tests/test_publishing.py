import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from operations_toolkit.modules.cnmaestro.adapters import (
    DemoCnMaestroAdapter,
    JobResult,
    Submission,
)
from operations_toolkit.modules.cnmaestro.api import AmbiguousWrite
from operations_toolkit.modules.cnmaestro.models import Rates
from operations_toolkit.modules.cnmaestro.persistence import OperationStore
from operations_toolkit.modules.cnmaestro.planning import PlanBuilder, PlanInvalidated
from operations_toolkit.modules.cnmaestro.publishing import PublishService, validate_job
from tests.test_planning import catalog, device


class ScriptedAdapter:
    def __init__(
        self,
        pulls: list[Rates],
        *,
        job: JobResult | None = None,
        submit_error: Exception | None = None,
        inventory_result=None,
        connection_identity: str = "test",
    ) -> None:
        self.pulls = pulls
        self.job_result = job or JobResult("completed", 1, 0, 0, 0, None, None)
        self.submit_error = submit_error
        self.submit_calls = 0
        self.pull_calls = 0
        self.inventory_result = inventory_result or (
            device(),
            device("0A:00:3E:80:42:ED"),
            device("0A:00:3E:80:42:EE"),
        )
        self.connection_identity = connection_identity

    async def inventory(self):
        return self.inventory_result

    async def pull_rates(self, mac: str) -> Rates:
        self.pull_calls += 1
        return self.pulls.pop(0)

    async def submit_template(self, mac: str, template: str) -> Submission:
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        return Submission("job-1")

    async def job_status(self, job_id: str) -> JobResult:
        return self.job_result

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("job", "category"),
    [
        (JobResult("completed", 1, 0, 0, 1, None, None), "skipped"),
        (JobResult("completed", 0, 1, 0, 0, None, None), "failed"),
        (JobResult("completed", 1, 0, 1, 0, None, None), "unknown"),
        (JobResult("timed_out", 0, 0, 1, 0, None, None), "timeout"),
    ],
)
def test_job_validation_distinguishes_non_success_outcomes(job: JobResult, category: str) -> None:
    with pytest.raises(Exception) as error:
        validate_job(job, mac="0A:00:3E:80:42:EC", template="50mbps Package")
    assert error.value.category == category


def test_job_validation_checks_intended_mac_and_template_when_available() -> None:
    job = JobResult("completed", 1, 0, 0, 0, "0A:00:3E:80:42:ED", "other")
    with pytest.raises(Exception) as error:
        validate_job(job, mac="0A:00:3E:80:42:EC", template="50mbps Package")
    assert error.value.category == "target_mismatch"


@pytest.mark.asyncio
async def test_delayed_verification_eventually_records_exact_rates(tmp_path: Path) -> None:
    before, target = Rates(10752, 1075), Rates(53760, 10750)
    adapter = ScriptedAdapter([before, before, target])
    store = OperationStore(tmp_path / "publish.db")
    plan = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")
    result = await PublishService(
        adapter, store, catalog(), verification_attempts=2, sleep=lambda _: None
    ).publish(plan, run_id="run-ok", approve_remainder=lambda _result: True)
    assert result.verified == 1
    record = store.device("run-ok", device().mac)
    assert record["state"] == "verified" and record["verified_dl"] == 53760
    assert adapter.submit_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_submit_is_unknown_and_never_resubmitted(tmp_path: Path) -> None:
    adapter = ScriptedAdapter(
        [Rates(10752, 1075)], submit_error=AmbiguousWrite("unknown; reconciliation required")
    )
    store = OperationStore(tmp_path / "unknown.db")
    plan = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")
    result = await PublishService(adapter, store, catalog(), sleep=lambda _: None).publish(
        plan, run_id="run-unknown", approve_remainder=lambda _result: True
    )
    assert result.unknown == 1 and adapter.submit_calls == 1
    assert store.device("run-unknown", device().mac)["state"] == "unknown"


@pytest.mark.asyncio
async def test_canary_requires_explicit_approval_before_remainder(tmp_path: Path) -> None:
    second = device("0A:00:3E:80:42:ED")
    adapter = ScriptedAdapter([Rates(10752, 1075), Rates(53760, 10750)])
    store = OperationStore(tmp_path / "canary.db")
    plan = PlanBuilder(catalog()).build((device(), second), "50 Mbps", connection_identity="test")
    result = await PublishService(
        adapter, store, catalog(), verification_attempts=1, sleep=lambda _: None
    ).publish(plan, run_id="run-canary", approve_remainder=lambda _result: False)
    assert result.verified == 1 and result.planned == 1
    assert store.device("run-canary", second.mac)["state"] == "planned"


@pytest.mark.asyncio
async def test_cancellation_is_checked_after_semaphore_acquisition(tmp_path: Path) -> None:
    c = replace(catalog(), max_batch_size=3)
    devices = (device(), device("0A:00:3E:80:42:ED"), device("0A:00:3E:80:42:EE"))
    entered_second = asyncio.Event()
    release_second = asyncio.Event()

    class BlockingAdapter(ScriptedAdapter):
        async def pull_rates(self, mac: str) -> Rates:
            if self.pull_calls == 2:
                entered_second.set()
                await release_second.wait()
            return await super().pull_rates(mac)

    adapter = BlockingAdapter(
        [Rates(10752, 1075), Rates(53760, 10750), Rates(10752, 1075), Rates(53760, 10750)]
    )
    store = OperationStore(tmp_path / "cancel.db")
    plan = PlanBuilder(c).build(devices, "50 Mbps", connection_identity="test")
    service = PublishService(
        adapter, store, c, concurrency=1, verification_attempts=1, sleep=lambda _: None
    )
    task = asyncio.create_task(service.publish(plan, run_id="run-cancel", approve_remainder=lambda _result: True))
    await entered_second.wait()
    service.stop_before_next()
    release_second.set()
    result = await task
    assert result.planned == 1
    assert adapter.submit_calls == 2
    assert store.device("run-cancel", devices[2].mac)["state"] == "planned"


def test_demo_adapter_is_network_impossible_and_matches_contract() -> None:
    demo = DemoCnMaestroAdapter()
    assert demo.network_enabled is False
    for name in ("inventory", "pull_rates", "submit_template", "job_status", "close"):
        assert callable(getattr(demo, name))


@pytest.mark.asyncio
async def test_configurable_two_device_canary_runs_before_remainder_approval(
    tmp_path: Path,
) -> None:
    c = replace(catalog(), max_batch_size=3, canary_size=2)
    devices = (device(), device("0A:00:3E:80:42:ED"), device("0A:00:3E:80:42:EE"))
    adapter = ScriptedAdapter([Rates(10752, 1075), Rates(53760, 10750)] * 2)
    store = OperationStore(tmp_path / "canary-two.db")
    plan = PlanBuilder(c).build(devices, "50 Mbps", connection_identity="test")
    result = await PublishService(
        adapter, store, c, canary_size=2, verification_attempts=1, sleep=lambda _: None
    ).publish(plan, run_id="run-canary-two", approve_remainder=lambda _result: False)
    assert result.verified == 2 and result.planned == 1


@pytest.mark.asyncio
async def test_failed_canary_never_requests_approval_or_writes_remainder(tmp_path: Path) -> None:
    second = device("0A:00:3E:80:42:ED")
    adapter = ScriptedAdapter([Rates(10752, 1075), Rates(10752, 1075)])
    store = OperationStore(tmp_path / "canary-failed.db")
    plan = PlanBuilder(catalog()).build((device(), second), "50 Mbps", connection_identity="test")
    approvals: list[int] = []

    def approve(result) -> bool:
        approvals.append(result.verified)
        return True

    result = await PublishService(
        adapter,
        store,
        catalog(),
        verification_attempts=1,
        failure_threshold=2,
        stop_on_first_issue=False,
        sleep=lambda _: None,
    ).publish(plan, run_id="run-canary-failed", approve_remainder=approve)

    assert result.failed == 1 and result.planned == 1
    assert adapter.submit_calls == 1
    assert approvals == []


@pytest.mark.asyncio
async def test_remainder_approval_is_requested_only_after_entire_canary_verifies(tmp_path: Path) -> None:
    c = replace(catalog(), max_batch_size=3, canary_size=2)
    devices = (device(), device("0A:00:3E:80:42:ED"), device("0A:00:3E:80:42:EE"))
    adapter = ScriptedAdapter([Rates(10752, 1075), Rates(53760, 10750)] * 3)
    store = OperationStore(tmp_path / "canary-order.db")
    plan = PlanBuilder(c).build(devices, "50 Mbps", connection_identity="test")
    approval_observations: list[tuple[int, int]] = []

    def approve(result) -> bool:
        approval_observations.append((result.verified, adapter.submit_calls))
        return True

    result = await PublishService(
        adapter, store, c, canary_size=2, verification_attempts=1, sleep=lambda _: None
    ).publish(plan, run_id="run-canary-order", approve_remainder=approve)

    assert approval_observations == [(2, 2)]
    assert result.verified == 3
    assert adapter.submit_calls == 3


@pytest.mark.asyncio
async def test_publish_rejects_different_connection_identity_before_any_write(tmp_path: Path) -> None:
    adapter = ScriptedAdapter([], connection_identity="tenant-b")
    store = OperationStore(tmp_path / "tenant-drift.db")
    plan = PlanBuilder(catalog()).build(
        (device(),), "50 Mbps", connection_identity="tenant-a"
    )

    with pytest.raises(PlanInvalidated, match="connection"):
        await PublishService(adapter, store, catalog()).publish(
            plan, run_id="run-tenant-drift"
        )

    assert adapter.submit_calls == 0


@pytest.mark.asyncio
async def test_publish_revalidates_full_selected_inventory_scope_before_write(tmp_path: Path) -> None:
    drifted = replace(device(), tower="Other")
    adapter = ScriptedAdapter([], inventory_result=(drifted,))
    store = OperationStore(tmp_path / "scope-drift.db")
    plan = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")

    with pytest.raises(PlanInvalidated, match="inventory"):
        await PublishService(adapter, store, catalog()).publish(
            plan, run_id="run-scope-drift"
        )

    assert adapter.submit_calls == 0


@pytest.mark.asyncio
async def test_persisted_submission_errors_redact_bearer_tokens(tmp_path: Path) -> None:
    secret = "eyJ.super.secret.token"  # pragma: allowlist secret
    adapter = ScriptedAdapter(
        [Rates(10752, 1075)],
        submit_error=RuntimeError(f"Authorization: Bearer {secret}"),
    )
    store = OperationStore(tmp_path / "redaction.db")
    plan = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")

    await PublishService(adapter, store, catalog()).publish(plan, run_id="run-redact")

    detail = store.device("run-redact", device().mac)["error_detail"]
    assert secret not in detail
    assert "[REDACTED]" in detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job", "submit_error", "expected_state"),
    [
        (JobResult("completed", 1, 0, 0, 1, None, None), None, "failed"),
        (JobResult("timed_out", 0, 0, 1, 0, None, None), None, "unknown"),
        (None, AmbiguousWrite("unknown"), "unknown"),
    ],
)
async def test_nonverified_canary_outcomes_never_offer_or_write_remainder(
    tmp_path: Path,
    job: JobResult | None,
    submit_error: Exception | None,
    expected_state: str,
) -> None:
    second = device("0A:00:3E:80:42:ED")
    adapter = ScriptedAdapter(
        [Rates(10752, 1075)], job=job, submit_error=submit_error
    )
    store = OperationStore(tmp_path / f"canary-{expected_state}-{bool(submit_error)}.db")
    plan = PlanBuilder(catalog()).build(
        (device(), second), "50 Mbps", connection_identity="test"
    )
    approvals: list[bool] = []

    await PublishService(adapter, store, catalog()).publish(
        plan,
        run_id="run-canary-nonverified",
        approve_remainder=lambda _result: approvals.append(True) or True,
    )

    assert store.device("run-canary-nonverified", device().mac)["state"] == expected_state
    assert store.device("run-canary-nonverified", second.mac)["state"] == "planned"
    assert adapter.submit_calls == 1
    assert approvals == []
