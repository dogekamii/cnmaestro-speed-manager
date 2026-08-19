import pytest
import ttkbootstrap as tb

from operations_toolkit.ui.app import Application


@pytest.fixture(autouse=True)
def reset_ttkbootstrap_singleton() -> None:
    tb.Style.instance = None
    yield
    tb.Style.instance = None



def test_dark_demo_shell_constructs_with_first_party_navigation() -> None:
    app = Application(demo=True)
    try:
        app.update_idletasks()
        assert app.title() == "Operations Toolkit — 2.0.0-beta.1"
        assert app.style.theme.name == "darkly"
        assert app.module_titles == ("Overview", "Bulk Speed Changes", "Audit & Recovery")
        assert app.connection_button.instate(["disabled"])
        assert "DEMO" in app.connection_status.get()
    finally:
        app.destroy()


def test_window_shutdown_clears_active_session_credentials() -> None:
    app = Application(demo=True)
    cleared: list[bool] = []
    app.context.session_gate.replace("live", lambda: cleared.append(True))
    app._shutdown()
    assert cleared == [True]


def test_window_close_handler_is_idempotent_and_closes_resources() -> None:
    app = Application(demo=True)
    closed: list[str] = []

    class Resource:
        def close(self) -> None:
            closed.append("store")

    app.client_id.set("client-id")
    app.client_secret.set("client-secret")
    app._views["resource"] = Resource()
    app.context.services["cnmaestro.adapter"] = "live"
    app.context.session_gate.replace("live", lambda: closed.append("adapter"))

    assert app.protocol("WM_DELETE_WINDOW")
    app._shutdown()
    app._shutdown()

    assert closed == ["adapter", "store"]
    assert app.client_id.get() == ""
    assert app.client_secret.get() == ""
    assert "cnmaestro.adapter" not in app.context.services
    assert app.context.session_gate.connection is None


def test_inventory_scan_does_not_block_tk_main_thread() -> None:
    import asyncio
    import threading
    import time

    app = Application(demo=False)
    app.show_view("Bulk Speed Changes")
    view = app._views["Bulk Speed Changes"]
    release = threading.Event()

    class SlowAdapter:
        connection_identity = "slow-test"

        async def inventory(self):
            await asyncio.to_thread(release.wait)
            return ()

    view.set_adapter(SlowAdapter())
    timer = threading.Timer(0.35, release.set)
    timer.start()
    started = time.monotonic()
    try:
        view.scan_inventory()
        assert time.monotonic() - started < 0.1
    finally:
        release.set()
        timer.join()
        app._shutdown()


def test_replacing_adapter_invalidates_preview_and_inventory() -> None:
    from operations_toolkit.modules.cnmaestro.adapters import DemoCnMaestroAdapter
    from operations_toolkit.modules.cnmaestro.planning import PlanBuilder, classify_inventory

    app = Application(demo=False)
    app.show_view("Bulk Speed Changes")
    view = app._views["Bulk Speed Changes"]
    first = DemoCnMaestroAdapter()
    view.set_adapter(first)
    inventory = classify_inventory(app._async_worker.run(first.inventory()), view.catalog)
    view.devices = inventory
    view.plan = PlanBuilder(view.catalog).build(
        inventory[:1], "50 Mbps", connection_identity=first.connection_identity
    )
    view.publish_button.configure(state="normal")

    try:
        view.set_adapter(DemoCnMaestroAdapter())
        assert view.plan is None
        assert view.devices == ()
        assert view.publish_button.instate(["disabled"])
    finally:
        app._shutdown()


def test_ui_requests_remainder_approval_after_canary_submission(monkeypatch) -> None:
    import time

    from operations_toolkit.modules.cnmaestro import ui as cn_ui
    from operations_toolkit.modules.cnmaestro.adapters import DemoCnMaestroAdapter
    from operations_toolkit.modules.cnmaestro.planning import PlanBuilder, classify_inventory

    app = Application(demo=False)
    app.show_view("Bulk Speed Changes")
    view = app._views["Bulk Speed Changes"]
    adapter = DemoCnMaestroAdapter()
    view.set_adapter(adapter)
    app.context.session_gate.replace(
        adapter, lambda: app._async_worker.run(adapter.close(), timeout=15)
    )
    inventory = classify_inventory(app._async_worker.run(adapter.inventory()), view.catalog)
    view.devices = inventory
    view.plan = PlanBuilder(view.catalog).build(
        inventory[:2], "50 Mbps", connection_identity=adapter.connection_identity
    )
    view.publish_button.configure(state="normal")
    events: list[str] = []
    original_submit = adapter.submit_template

    async def submit(mac: str, template: str):
        events.append(f"submit:{mac}")
        return await original_submit(mac, template)

    adapter.submit_template = submit  # type: ignore[method-assign]
    monkeypatch.setattr(cn_ui.tb.dialogs.Querybox, "get_string", lambda **_kwargs: "APPLY SPEED CHANGES")

    def confirm(title: str, _message: str) -> bool:
        events.append(title)
        return True

    monkeypatch.setattr(cn_ui.messagebox, "askyesno", confirm)
    try:
        view.publish()
        deadline = time.monotonic() + 3
        while view.plan is not None and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        assert view.plan is None
        canary_submit = next(index for index, event in enumerate(events) if event.startswith("submit:"))
        assert canary_submit < events.index("Canary approval")
        assert sum(event.startswith("submit:") for event in events) == 2
    finally:
        app._shutdown()


def test_demo_connect_boundary_never_constructs_live_adapter(monkeypatch) -> None:
    from operations_toolkit.ui import app as app_module

    events: list[str] = []

    class ForbiddenLiveAdapter:
        def __init__(self, *_args, **_kwargs) -> None:
            events.append("constructed")

        async def connect(self) -> str:
            events.append("connect-called")
            return "forbidden"

    monkeypatch.setattr(app_module, "LiveCnMaestroAdapter", ForbiddenLiveAdapter)
    app = Application(demo=True)
    app.client_id.set("should-never-be-used")
    app.client_secret.set("should-never-be-used")
    try:
        with pytest.raises(RuntimeError, match="disabled in demo mode"):
            app.connect()
        assert events == []
    finally:
        app._shutdown()


def test_failed_provisional_adapter_is_closed_and_cleared_exactly_once(monkeypatch) -> None:
    import time

    from operations_toolkit.ui import app as app_module

    events: list[str] = []

    class FailingLiveAdapter:
        def __init__(self, *_args, **_kwargs) -> None:
            events.append("constructed")

        async def connect(self) -> str:
            events.append("connect-called")
            raise RuntimeError("synthetic connect failure")

        async def close(self) -> None:
            events.append("close-called")

    monkeypatch.setattr(app_module, "LiveCnMaestroAdapter", FailingLiveAdapter)
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *_args: None)
    app = Application(demo=False)
    app.client_id.set("client")
    app.client_secret.set("secret")
    try:
        app.connect()
        deadline = time.monotonic() + 2
        while app.connection_status.get() != "● Connection failed" and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        assert app.connection_status.get() == "● Connection failed"
        assert events == ["constructed", "connect-called", "close-called"]
        assert app.context.session_gate.connection is None
        assert "cnmaestro.adapter" not in app.context.services
    finally:
        app._shutdown()
    assert events.count("close-called") == 1


def test_shutdown_cancels_and_closes_connecting_adapter_once_despite_late_watcher(monkeypatch) -> None:
    import asyncio
    import time

    from operations_toolkit.ui import app as app_module

    events: list[str] = []
    instances: list[object] = []

    class BlockingLiveAdapter:
        def __init__(self, *_args, **_kwargs) -> None:
            instances.append(self)
            events.append("constructed")

        async def connect(self) -> str:
            events.append("connect-called")
            await asyncio.Future()
            return "unreachable"

        async def close(self) -> None:
            events.append("close-called")

    monkeypatch.setattr(app_module, "LiveCnMaestroAdapter", BlockingLiveAdapter)
    app = Application(demo=False)
    app.client_id.set("client")
    app.client_secret.set("secret")
    app.connect()
    deadline = time.monotonic() + 2
    while "connect-called" not in events and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    assert events[:2] == ["constructed", "connect-called"]
    future = app._connection_future
    adapter = instances[0]
    assert future is not None

    app._shutdown()
    app._watch_connection(future, adapter)  # type: ignore[arg-type]
    app._shutdown()

    assert events.count("close-called") == 1
    assert future.cancelled()


def test_session_registration_race_closes_provisional_adapter_on_failure(monkeypatch) -> None:
    import time

    from operations_toolkit.ui import app as app_module

    events: list[str] = []
    instances: list[object] = []

    class SuccessfulLiveAdapter:
        def __init__(self, *_args, **_kwargs) -> None:
            instances.append(self)

        async def connect(self) -> str:
            return "https://api.cambiumnetworks.com"

        async def close(self) -> None:
            events.append("close-called")

    monkeypatch.setattr(app_module, "LiveCnMaestroAdapter", SuccessfulLiveAdapter)
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *_args: None)
    app = Application(demo=False)
    app.client_id.set("client")
    app.client_secret.set("secret")
    app.context.session_gate.begin("publish")
    try:
        app.connect()
        future = app._connection_future
        assert future is not None
        deadline = time.monotonic() + 2
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert future.done()
        app._watch_connection(future, instances[0])  # type: ignore[arg-type]
        assert app.connection_status.get() == "● Connection failed"
        assert events == ["close-called"]
        assert app.context.session_gate.connection is None
    finally:
        app._shutdown()
    assert events == ["close-called"]


def test_shutdown_during_pending_canary_approval_closes_worker_and_store(monkeypatch) -> None:
    import sqlite3
    import time

    from operations_toolkit.modules.cnmaestro import ui as cn_ui
    from operations_toolkit.modules.cnmaestro.adapters import DemoCnMaestroAdapter
    from operations_toolkit.modules.cnmaestro.planning import PlanBuilder, classify_inventory

    app = Application(demo=False)
    app.show_view("Bulk Speed Changes")
    view = app._views["Bulk Speed Changes"]
    adapter = DemoCnMaestroAdapter()
    view.set_adapter(adapter)
    app.context.session_gate.replace(
        adapter, lambda: app._async_worker.run(adapter.close(), timeout=15)
    )
    inventory = classify_inventory(app._async_worker.run(adapter.inventory()), view.catalog)
    view.devices = inventory
    view.plan = PlanBuilder(view.catalog).build(
        inventory[:2], "50 Mbps", connection_identity=adapter.connection_identity
    )
    view.publish_button.configure(state="normal")
    monkeypatch.setattr(
        cn_ui.tb.dialogs.Querybox,
        "get_string",
        lambda **_kwargs: "APPLY SPEED CHANGES",
    )
    monkeypatch.setattr(cn_ui.messagebox, "askyesno", lambda *_args, **_kwargs: True)

    view.publish()
    deadline = time.monotonic() + 3
    while view._approval_requests.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not view._approval_requests.empty()
    with view._approval_requests.mutex:
        decision = view._approval_requests.queue[0][1]

    app._shutdown()
    app._shutdown()

    assert decision.done()
    assert decision.cancelled() or decision.result() is False
    assert not app._async_worker._thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = view.store.schema_version


@pytest.mark.parametrize("decision_state", ["cancelled", "completed"])
def test_bulk_view_close_is_idempotent_with_finished_approval_decisions(
    decision_state: str, monkeypatch
) -> None:
    import sqlite3
    from concurrent.futures import Future

    from operations_toolkit.modules.cnmaestro.publishing import PublishResult

    app = Application(demo=False)
    app.show_view("Bulk Speed Changes")
    view = app._views["Bulk Speed Changes"]
    decision: Future[bool] = Future()
    if decision_state == "cancelled":
        decision.cancel()
    else:
        decision.set_result(True)
    view._approval_requests.put((PublishResult(1, 0, 0, 1), decision))
    close_calls = 0
    original_close = view.store.close

    def close_store() -> None:
        nonlocal close_calls
        close_calls += 1
        original_close()

    monkeypatch.setattr(view.store, "close", close_store)
    try:
        view.close()
        view.close()

        assert close_calls == 1
        assert decision.cancelled() if decision_state == "cancelled" else decision.result() is True
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            _ = view.store.schema_version
    finally:
        app._shutdown()


def test_bulk_view_close_closes_store_when_approval_cleanup_raises() -> None:
    import sqlite3
    from concurrent.futures import Future

    from operations_toolkit.modules.cnmaestro.publishing import PublishResult

    class BrokenDecision(Future[bool]):
        def set_result(self, result: bool) -> None:
            raise RuntimeError("synthetic approval cleanup failure")

    app = Application(demo=False)
    app.show_view("Bulk Speed Changes")
    view = app._views["Bulk Speed Changes"]
    view._approval_requests.put((PublishResult(1, 0, 0, 1), BrokenDecision()))
    try:
        with pytest.raises(RuntimeError, match="synthetic approval cleanup failure"):
            view.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            _ = view.store.schema_version
        view.close()
    finally:
        app._shutdown()
