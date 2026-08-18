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
