from dataclasses import replace
from pathlib import Path

from operations_toolkit.modules.cnmaestro.models import Rates
from operations_toolkit.modules.cnmaestro.persistence import OperationState, OperationStore
from operations_toolkit.modules.cnmaestro.planning import PlanBuilder, build_rollback_plan
from tests.test_planning import catalog, device


def test_rollback_only_includes_verified_devices_with_known_exact_previous_template(
    tmp_path: Path,
) -> None:
    unmatched = replace(device("0A:00:3E:80:42:ED"), rates=Rates(10700, 1060), package=None)
    plan = PlanBuilder(catalog()).build(
        (device(), unmatched), "50 Mbps", connection_identity="test", approximate_acknowledgements={unmatched.mac}
    )
    store = OperationStore(tmp_path / "rollback.db")
    store.create_run("run-rb", plan)
    for item in plan.items:
        store.transition("run-rb", item.mac, OperationState.SUBMITTING)
        store.transition("run-rb", item.mac, OperationState.SUBMITTED)
        store.transition(
            "run-rb", item.mac, OperationState.JOB_KNOWN, job_id=f"job-{item.mac[-2:]}"
        )
        store.transition(
            "run-rb",
            item.mac,
            OperationState.VERIFIED,
            verified_dl=item.target.downlink,
            verified_ul=item.target.uplink,
        )
    rollback = build_rollback_plan(store, "run-rb", catalog())
    assert [(item.mac, item.template) for item in rollback.items] == [
        (device().mac, "10mbps Package")
    ]
    assert rollback.blocked == (
        (unmatched.mac, "previous rates do not map to a verified known template"),
    )
