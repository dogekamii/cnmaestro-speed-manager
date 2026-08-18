from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any
from uuid import uuid4

import ttkbootstrap as tb
from ttkbootstrap.constants import DISABLED, EW, LEFT, NORMAL, NSEW, W

from operations_toolkit.core.async_worker import AsyncWorker
from operations_toolkit.core.modules import ModuleContext
from operations_toolkit.core.security import redact
from operations_toolkit.core.session import OperationToken

from .adapters import CnMaestroAdapter
from .catalog import load_catalog
from .models import BatchPlan, DeviceSnapshot
from .persistence import OperationStore
from .planning import PlanBuilder, classify_inventory
from .publishing import PublishResult, PublishService


class BulkSpeedChangesView(tb.Frame):
    def __init__(
        self, parent: object, context: ModuleContext, adapter: CnMaestroAdapter | None
    ) -> None:
        super().__init__(parent, padding=24)
        self.context = context
        self.adapter = adapter
        self.catalog = load_catalog(context.data_dir / "packages.json")
        self.store = OperationStore(context.data_dir / "operations.db")
        self.worker: AsyncWorker = context.services["async_worker"]
        self._closed = False
        self._approval_requests: queue.Queue[tuple[PublishResult, Future[bool]]] = queue.Queue()
        self.devices: tuple[DeviceSnapshot, ...] = ()
        self.plan: BatchPlan | None = None
        self.target = tb.StringVar(value="50 Mbps")
        self.summary = tb.StringVar(value="Select devices, then create an immutable preview.")
        self.status = tb.StringVar(
            value="DEMO inventory ready"
            if context.demo
            else "Connect to cnMaestro to scan inventory"
        )
        self._build()
        self.target.trace_add(
            "write", lambda *_: self.invalidate_preview("Target changed — preview invalidated.")
        )
        if adapter is not None:
            if context.session_gate.connection is None:
                context.session_gate.replace(
                    adapter, lambda: self.worker.run(adapter.close(), timeout=15)
                )
            self.scan_inventory()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        tb.Label(self, text="cnMaestro / Bulk Speed Changes", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky=W
        )
        tb.Label(
            self,
            text="Exact-rate planning with durable write-ahead evidence, canary gating, and verification.",
            bootstyle="secondary",
        ).grid(row=1, column=0, sticky=W, pady=(2, 18))
        toolbar = tb.Frame(self)
        toolbar.grid(row=2, column=0, sticky=EW, pady=(0, 12))
        toolbar.columnconfigure(7, weight=1)
        self.scan_button = tb.Button(
            toolbar, text="Scan inventory", bootstyle="info-outline", command=self.scan_inventory
        )
        self.scan_button.grid(row=0, column=0, padx=(0, 8))
        tb.Label(toolbar, text="Target package").grid(row=0, column=1, padx=(8, 6))
        tb.Combobox(
            toolbar,
            textvariable=self.target,
            values=[p.name for p in self.catalog.packages],
            state="readonly",
            width=16,
        ).grid(row=0, column=2)
        tb.Button(
            toolbar, text="Create preview", bootstyle="primary", command=self.create_preview
        ).grid(row=0, column=3, padx=8)
        self.publish_button = tb.Button(
            toolbar,
            text="Publish canary + verify",
            bootstyle="success",
            command=self.publish,
            state=DISABLED,
        )
        self.publish_button.grid(row=0, column=4)
        tb.Label(toolbar, textvariable=self.status, bootstyle="info").grid(
            row=0, column=7, sticky="e"
        )
        columns = ("name", "mac", "package", "rates", "scope", "online")
        self.tree = tb.Treeview(
            self, columns=columns, show="headings", selectmode="extended", bootstyle="dark"
        )
        widths = (190, 160, 120, 150, 240, 80)
        for column, width in zip(columns, widths, strict=True):
            self.tree.heading(column, text=column.replace("_", " ").title())
            self.tree.column(column, width=width, anchor=W)
        self.tree.grid(row=3, column=0, sticky=NSEW)
        self.tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self.invalidate_preview("Selection changed — preview invalidated."),
        )
        panel = tb.Labelframe(self, text="Immutable batch plan", padding=14, bootstyle="secondary")
        panel.grid(row=4, column=0, sticky=EW, pady=(14, 0))
        panel.columnconfigure(0, weight=1)
        tb.Label(panel, textvariable=self.summary, wraplength=900, justify=LEFT).grid(
            row=0, column=0, sticky=W
        )
        self.progress = tb.Progressbar(
            panel, bootstyle="success-striped", mode="determinate", maximum=100
        )
        self.progress.grid(row=1, column=0, sticky=EW, pady=(10, 0))

    def set_adapter(self, adapter: CnMaestroAdapter) -> None:
        self.invalidate_preview("Connection changed — preview invalidated.")
        self.devices = ()
        self.tree.delete(*self.tree.get_children())
        self.adapter = adapter
        self.status.set("Connected — scan inventory")

    def invalidate_preview(self, reason: str = "Inventory changed — preview invalidated.") -> None:
        if self.plan is not None:
            self.plan = None
            self.publish_button.configure(state=DISABLED)
            self.summary.set(reason)

    def scan_inventory(self) -> None:
        if self.adapter is None:
            messagebox.showwarning("Connection required", "Connect to cnMaestro before scanning.")
            return
        token = self.context.session_gate.begin("scan")
        self.context.operation_changed(True)
        self.scan_button.configure(state=DISABLED)
        future = self.worker.submit(self.adapter.inventory())
        self._watch_future(future, self._scan_succeeded, "Scan failed", token)

    def _scan_succeeded(self, inventory: object) -> None:
        self.devices = classify_inventory(tuple(inventory), self.catalog)  # type: ignore[arg-type]
        self.invalidate_preview()
        self.tree.delete(*self.tree.get_children())
        for item in self.devices:
            self.tree.insert(
                "",
                "end",
                iid=item.mac,
                values=(
                    item.name,
                    item.mac,
                    item.package or "Unmatched",
                    f"{item.rates.downlink} / {item.rates.uplink}",
                    f"{item.network} / {item.tower} / {item.ap}",
                    "Online" if item.online else "Offline",
                ),
            )
        self.status.set(f"{len(self.devices)} devices loaded")

    def _watch_future(
        self,
        future: Future[Any],
        on_success: Callable[[Any], None],
        error_title: str,
        token: OperationToken,
    ) -> None:
        if self._closed:
            return
        self._service_approval_requests()
        if not future.done():
            self.after(20, self._watch_future, future, on_success, error_title, token)
            return
        try:
            result = future.result()
            on_success(result)
        except Exception as exc:
            messagebox.showerror(error_title, str(redact(str(exc))))
        finally:
            if self.context.session_gate.operation is not None:
                self.context.session_gate.end(token)
            self.context.operation_changed(False)
            self.scan_button.configure(state=NORMAL)

    def _service_approval_requests(self) -> None:
        while True:
            try:
                result, decision = self._approval_requests.get_nowait()
            except queue.Empty:
                return
            approved = messagebox.askyesno(
                "Canary approval",
                f"Canary verified: {result.verified} verified, {result.failed} failed, "
                f"{result.unknown} unknown. Publish the remaining planned devices?",
            )
            decision.set_result(approved)

    async def _approve_remainder(self, result: PublishResult) -> bool:
        decision: Future[bool] = Future()
        self._approval_requests.put((result, decision))
        return await asyncio.wrap_future(decision)

    def create_preview(self) -> None:
        selected = set(self.tree.selection())
        snapshots = tuple(item for item in self.devices if item.mac in selected)
        try:
            if self.adapter is None:
                raise ValueError("connection is unavailable")
            self.plan = PlanBuilder(self.catalog).build(
                snapshots,
                self.target.get(),
                connection_identity=self.adapter.connection_identity,
            )
        except ValueError as exc:
            messagebox.showerror("Preview blocked", str(exc))
            return
        rollbackable = sum(item.rollback_template is not None for item in self.plan.items)
        self.summary.set(
            f"Plan {self.plan.plan_id}: {len(self.plan.items)} device(s) → {self.plan.target_package}. Exact before/target rates and scope frozen at {self.plan.created_at.astimezone(UTC).isoformat()}. {rollbackable} automatically rollbackable."
        )
        self.publish_button.configure(state=NORMAL)

    def publish(self) -> None:
        if self.plan is None:
            return
        phrase = tb.dialogs.Querybox.get_string(
            prompt="Type APPLY SPEED CHANGES to confirm the immutable plan.",
            title="Confirmation required",
            parent=self,
        )
        if phrase != "APPLY SPEED CHANGES":
            messagebox.showwarning("Publish blocked", "Confirmation phrase did not match.")
            return
        if not messagebox.askyesno(
            "Final confirmation",
            f"Publish plan {self.plan.plan_id} with exact-rate stale checks? In-flight PUTs cannot be cancelled.",
        ):
            return
        token = self.context.session_gate.begin("publish")
        self.context.operation_changed(True)
        try:
            if self.adapter is None:
                raise RuntimeError("connection is unavailable")
            future = self.worker.submit(
                PublishService(
                    self.adapter,
                    self.store,
                    self.catalog,
                    canary_size=self.catalog.canary_size,
                    failure_threshold=self.catalog.failure_threshold,
                    stop_on_first_issue=self.catalog.stop_on_first_issue,
                ).publish(
                    self.plan,
                    run_id=uuid4().hex,
                    approve_remainder=self._approve_remainder,
                )
            )
            self._watch_future(future, self._publish_succeeded, "Publish error", token)
        except Exception:
            self.context.session_gate.end(token)
            self.context.operation_changed(False)
            raise

    def _publish_succeeded(self, value: object) -> None:
        result = value
        assert isinstance(result, PublishResult)
        assert self.plan is not None
        total = len(self.plan.items)
        self.progress.configure(value=(result.verified / total) * 100)
        self.summary.set(
            f"Run complete: {result.verified} verified, {result.failed} failed, "
            f"{result.unknown} unknown, {result.planned} awaiting action. "
            "Review Audit & Recovery."
        )
        self.publish_button.configure(state=DISABLED)
        self.plan = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while True:
            try:
                _result, decision = self._approval_requests.get_nowait()
            except queue.Empty:
                break
            decision.set_result(False)
        self.store.close()


class AuditRecoveryView(tb.Frame):
    def __init__(self, parent: object, context: ModuleContext) -> None:
        super().__init__(parent, padding=24)
        self.context = context
        self.store = OperationStore(context.data_dir / "operations.db")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        tb.Label(self, text="Audit & Recovery", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky=W
        )
        tb.Label(
            self,
            text="Operational evidence and reconciliation queue. SQLite is not tamper-proof compliance evidence.",
            bootstyle="warning",
        ).grid(row=1, column=0, sticky=W, pady=(3, 14))
        self.tree = tb.Treeview(
            self,
            columns=("run", "mac", "state", "before", "target", "verified", "job", "error"),
            show="headings",
            bootstyle="dark",
        )
        for column in ("run", "mac", "state", "before", "target", "verified", "job", "error"):
            self.tree.heading(column, text=column.title())
            self.tree.column(column, width=125)
        self.tree.grid(row=2, column=0, sticky=NSEW)
        actions = tb.Frame(self)
        actions.grid(row=3, column=0, sticky=EW, pady=(12, 0))
        tb.Button(actions, text="Refresh", command=self.refresh, bootstyle="info-outline").pack(
            side=LEFT
        )
        tb.Button(
            actions, text="Export CSV", command=lambda: self.export("csv"), bootstyle="secondary"
        ).pack(side=LEFT, padx=8)
        tb.Button(
            actions, text="Export JSON", command=lambda: self.export("json"), bootstyle="secondary"
        ).pack(side=LEFT)
        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in self.store.audit_rows():
            self.tree.insert(
                "",
                "end",
                values=(
                    row["run_id"],
                    row["mac"],
                    row["state"],
                    f"{row['before_dl']}/{row['before_ul']}",
                    f"{row['target_dl']}/{row['target_ul']}",
                    f"{row['verified_dl'] or '—'}/{row['verified_ul'] or '—'}",
                    row["job_id"] or "—",
                    row["error_category"] or "—",
                ),
            )

    def export(self, kind: str) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=f".{kind}", filetypes=[(kind.upper(), f"*.{kind}")]
        )
        if path:
            (self.store.export_csv if kind == "csv" else self.store.export_json)(Path(path))

    def close(self) -> None:
        self.store.close()
