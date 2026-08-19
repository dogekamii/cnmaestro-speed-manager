from __future__ import annotations

from concurrent.futures import Future
from contextlib import suppress
from tkinter import messagebox
from typing import Any

import ttkbootstrap as tb
from ttkbootstrap.constants import DISABLED, EW, LEFT, NSEW, W, X

from operations_toolkit import __version__
from operations_toolkit.core.async_worker import AsyncWorker
from operations_toolkit.core.modules import ModuleContext
from operations_toolkit.core.security import redact
from operations_toolkit.modules.cnmaestro.adapters import LiveCnMaestroAdapter
from operations_toolkit.modules.cnmaestro.ui import AuditRecoveryView
from operations_toolkit.paths import prepare_data_dir
from operations_toolkit.registry import first_party_modules


class Application(tb.Window):
    def __init__(self, *, demo: bool = False) -> None:
        super().__init__(themename="darkly")
        self.demo = demo
        self._async_worker = AsyncWorker()
        self.title(f"Operations Toolkit — {__version__}")
        self.geometry("1380x860")
        self.minsize(1120, 700)
        self.context = ModuleContext(
            prepare_data_dir(), demo=demo, operation_changed=self._operation_changed
        )
        self.context.services["async_worker"] = self._async_worker
        self.connection_status = tb.StringVar(
            value="● DEMO · Network disabled" if demo else "● Disconnected"
        )
        self.module_titles = ("Overview", "Bulk Speed Changes", "Audit & Recovery")
        self._views: dict[str, Any] = {}
        self._connecting_adapter: LiveCnMaestroAdapter | None = None
        self._connection_future: Future[str] | None = None
        self._shutting_down = False
        self._build_shell()
        self.protocol("WM_DELETE_WINDOW", self._shutdown)
        self.show_view("Overview")

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._connecting_adapter is not None:
            self._close_provisional(self._connecting_adapter)
        with suppress(Exception):
            self.context.session_gate.shutdown()
        self._async_worker.close()
        for view in tuple(self._views.values()):
            close = getattr(view, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        self.context.services.pop("cnmaestro.adapter", None)
        self.context.services.pop("async_worker", None)
        self.client_id.set("")
        self.client_secret.set("")
        self.destroy()

    def _build_shell(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        sidebar = tb.Frame(self, padding=20, bootstyle="dark")
        sidebar.grid(row=0, column=0, rowspan=2, sticky=NSEW)
        sidebar.configure(width=250)
        sidebar.grid_propagate(False)
        tb.Label(sidebar, text="OPERATIONS", font=("Segoe UI", 10, "bold"), bootstyle="info").pack(
            anchor=W
        )
        tb.Label(sidebar, text="Toolkit", font=("Segoe UI", 24, "bold")).pack(
            anchor=W, pady=(0, 30)
        )
        tb.Label(
            sidebar, text="WORKSPACE", font=("Segoe UI", 9, "bold"), bootstyle="secondary"
        ).pack(anchor=W, pady=(0, 8))
        for title in self.module_titles:
            tb.Button(
                sidebar,
                text=f"  {title}",
                command=lambda name=title: self.show_view(name),
                bootstyle="dark",
                width=27,
            ).pack(fill=X, pady=3)
        tb.Separator(sidebar).pack(fill=X, pady=22)
        tb.Label(sidebar, text=f"v{__version__} · Beta", bootstyle="secondary").pack(anchor=W)
        top = tb.Frame(self, padding=(22, 14))
        top.grid(row=0, column=1, sticky=EW)
        top.columnconfigure(8, weight=1)
        self.endpoint = tb.StringVar(value="https://cloud.cambiumnetworks.com")
        self.client_id = tb.StringVar()
        self.client_secret = tb.StringVar()
        tb.Label(top, text="Endpoint", bootstyle="secondary").grid(row=0, column=0, sticky=W)
        self.endpoint_entry = tb.Entry(top, textvariable=self.endpoint, width=29)
        self.endpoint_entry.grid(row=1, column=0, padx=(0, 8))
        tb.Label(top, text="Client ID", bootstyle="secondary").grid(row=0, column=1, sticky=W)
        self.id_entry = tb.Entry(top, textvariable=self.client_id, width=18)
        self.id_entry.grid(row=1, column=1, padx=(0, 8))
        tb.Label(top, text="Client secret", bootstyle="secondary").grid(row=0, column=2, sticky=W)
        self.secret_entry = tb.Entry(top, textvariable=self.client_secret, show="•", width=18)
        self.secret_entry.grid(row=1, column=2, padx=(0, 8))
        self.connection_button = tb.Button(
            top, text="Connect", command=self.connect, bootstyle="info"
        )
        self.connection_button.grid(row=1, column=3)
        tb.Label(
            top,
            textvariable=self.connection_status,
            bootstyle="success" if self.demo else "secondary",
        ).grid(row=1, column=8, sticky="e")
        if self.demo:
            for widget in (
                self.endpoint_entry,
                self.id_entry,
                self.secret_entry,
                self.connection_button,
            ):
                widget.configure(state=DISABLED)
        self.content = tb.Frame(self)
        self.content.grid(row=1, column=1, sticky=NSEW)
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

    def _operation_changed(self, active: bool) -> None:
        if not self.demo:
            self.connection_button.configure(state=DISABLED if active else "normal")
        self.connection_status.set(
            f"● {self.context.session_gate.operation.title()} active · connection frozen"
            if active and self.context.session_gate.operation
            else ("● DEMO · Network disabled" if self.demo else "● Connected")
        )

    def connect(self) -> None:
        if self.demo:
            raise RuntimeError("live connections are disabled in demo mode")
        if not self.client_id.get() or not self.client_secret.get():
            messagebox.showerror("Credentials required", "Enter client ID and client secret.")
            return
        if self._connecting_adapter is not None:
            raise RuntimeError("a connection attempt is already in progress")
        adapter = LiveCnMaestroAdapter(
            self.endpoint.get(),
            self.client_id.get(),
            self.client_secret.get(),
            approved_redirect_hosts={"api.cambiumnetworks.com", "cloud.cambiumnetworks.com"},
        )
        self._connecting_adapter = adapter
        self.connection_button.configure(state=DISABLED)
        self.connection_status.set("● Connecting…")

        future = self._async_worker.submit(adapter.connect())
        self._connection_future = future
        self._watch_connection(future, adapter)

    def _watch_connection(
        self, future: Future[str], adapter: LiveCnMaestroAdapter
    ) -> None:
        if self._shutting_down:
            self._close_provisional(adapter)
            return
        if not future.done():
            self.after(20, self._watch_connection, future, adapter)
            return
        try:
            base = future.result()
            self._connected(adapter, base)
        except Exception as exc:
            self._connection_failed(exc, adapter)

    def _connected(self, adapter: LiveCnMaestroAdapter, base: str) -> None:
        if adapter is not self._connecting_adapter:
            return
        self.context.session_gate.replace(
            adapter, lambda: self._async_worker.run(adapter.close(), timeout=15)
        )
        self._connecting_adapter = None
        self._connection_future = None
        self.context.services["cnmaestro.adapter"] = adapter
        self.client_secret.set("")
        self.connection_status.set(f"● Connected · {base}")
        self.connection_button.configure(state="normal")
        view = self._views.get("Bulk Speed Changes")
        if view and hasattr(view, "set_adapter"):
            view.set_adapter(adapter)

    def _close_provisional(self, adapter: LiveCnMaestroAdapter) -> None:
        if adapter is not self._connecting_adapter:
            return
        future = self._connection_future
        self._connecting_adapter = None
        self._connection_future = None
        if future is not None and not future.done():
            future.cancel()
        with suppress(Exception):
            self._async_worker.run(adapter.close(), timeout=15)

    def _connection_failed(self, exc: Exception, adapter: LiveCnMaestroAdapter) -> None:
        self._close_provisional(adapter)
        self.client_secret.set("")
        self.connection_status.set("● Connection failed")
        self.connection_button.configure(state="normal")
        messagebox.showerror("Connection failed", str(redact(str(exc))))

    def show_view(self, name: str) -> None:
        for view in self._views.values():
            view.grid_remove()
        if name not in self._views:
            if name == "Overview":
                view = self._overview()
            elif name == "Audit & Recovery":
                view = AuditRecoveryView(self.content, self.context)
            else:
                view = first_party_modules()[0].create_view(self.content, self.context)
            self._views[name] = view
            view.grid(row=0, column=0, sticky=NSEW)
        self._views[name].grid()

    def _overview(self) -> tb.Frame:
        frame = tb.Frame(self.content, padding=28)
        frame.columnconfigure((0, 1, 2), weight=1)
        tb.Label(frame, text="Operations Toolkit", font=("Segoe UI", 26, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=W
        )
        tb.Label(
            frame,
            text="A modular, safety-first desktop for deliberate infrastructure operations.",
            bootstyle="secondary",
            font=("Segoe UI", 12),
        ).grid(row=1, column=0, columnspan=3, sticky=W, pady=(3, 26))
        cards = [
            (
                "Bulk Speed Changes",
                "cnMaestro",
                "Immutable previews, canary publishing, exact verification",
                "info",
            ),
            (
                "Recovery queue",
                "Durable state",
                "Ambiguous outcomes stay UNKNOWN until reconciled",
                "warning",
            ),
            (
                "Audit exports",
                "CSV + JSON",
                "Exact rates, templates, jobs, and scope metadata",
                "success",
            ),
        ]
        for index, (title, badge, detail, style) in enumerate(cards):
            card = tb.Labelframe(frame, text=badge, padding=20, bootstyle=style)
            card.grid(row=2, column=index, sticky=NSEW, padx=(0 if index == 0 else 8, 0), ipady=20)
            tb.Label(card, text=title, font=("Segoe UI", 15, "bold")).pack(anchor=W)
            tb.Label(card, text=detail, wraplength=270, justify=LEFT, bootstyle="secondary").pack(
                anchor=W, pady=(8, 0)
            )
        notice = tb.Labelframe(frame, text="Operator safety", padding=18, bootstyle="secondary")
        notice.grid(row=3, column=0, columnspan=3, sticky=EW, pady=(22, 0))
        tb.Label(
            notice,
            text="Writes are never blindly retried. A timeout around PUT is UNKNOWN, not failed. In-flight writes cannot be cancelled. Review the canary and durable audit evidence before proceeding.",
            wraplength=950,
            justify=LEFT,
        ).pack(anchor=W)
        return frame
