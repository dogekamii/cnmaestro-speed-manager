import csv
import json
import sqlite3
from pathlib import Path

import pytest

from operations_toolkit.modules.cnmaestro.persistence import OperationState, OperationStore
from operations_toolkit.modules.cnmaestro.planning import PlanBuilder
from tests.test_planning import catalog, device


def test_wal_state_machine_survives_restart_and_exposes_reconciliation(tmp_path: Path) -> None:
    db = tmp_path / "operations.db"
    plan = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")
    store = OperationStore(db)
    store.create_run("run-1", plan)
    store.transition("run-1", device().mac, OperationState.SUBMITTING)
    store.transition(
        "run-1",
        device().mac,
        OperationState.UNKNOWN,
        error_category="ambiguous_write",
        error_detail="timeout",
    )
    store.close()

    reopened = OperationStore(db)
    record = reopened.device("run-1", device().mac)
    assert record["before_dl"] == 10752 and record["before_ul"] == 1075
    assert record["target_dl"] == 53760 and record["target_ul"] == 10750
    assert record["template"] == "50mbps Package"
    assert record["network"] == "Access" and record["tower"] == "North"
    assert record["state"] == "unknown"
    assert reopened.reconciliation_queue()[0]["error_category"] == "ambiguous_write"
    assert reopened.journal_mode() == "wal"


def test_schema_migration_is_versioned_and_creates_recovery_backup(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE operation_runs(run_id TEXT PRIMARY KEY, plan_id TEXT, module_id TEXT, created_at TEXT, state TEXT, approval_state TEXT, completed_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE cn_speed_operations(run_id TEXT, mac TEXT, sequence INTEGER, state TEXT, before_dl INTEGER, before_ul INTEGER, target_dl INTEGER, target_ul INTEGER, before_package TEXT, target_package TEXT, template TEXT, rollback_template TEXT, network TEXT, tower TEXT, ap TEXT, online INTEGER, observed_at TEXT, job_id TEXT, error_detail TEXT, attempts INTEGER, created_at TEXT, updated_at TEXT, PRIMARY KEY(run_id,mac))"
    )
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    store = OperationStore(db)
    assert store.schema_version == 2
    columns = {row[1] for row in store.raw("PRAGMA table_info(cn_speed_operations)")}
    assert {"error_category", "verified_dl", "verified_ul"} <= columns
    assert Path(str(db) + ".v1.bak").exists()


def test_audit_export_contains_exact_rates_and_scope(tmp_path: Path) -> None:
    store = OperationStore(tmp_path / "audit.db")
    plan = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")
    store.create_run("run-export", plan)
    store.transition("run-export", device().mac, OperationState.SUBMITTING)
    store.transition("run-export", device().mac, OperationState.SUBMITTED, attempts=1)
    store.transition("run-export", device().mac, OperationState.JOB_KNOWN, job_id="job-7")
    store.transition(
        "run-export", device().mac, OperationState.VERIFIED, verified_dl=53760, verified_ul=10750
    )
    json_path = store.export_json(tmp_path / "audit.json")
    csv_path = store.export_csv(tmp_path / "audit.csv")
    data = json.loads(json_path.read_text())
    assert data[0]["before_dl"] == 10752 and data[0]["verified_ul"] == 10750
    with csv_path.open(newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["network"] == "Access" and row["job_id"] == "job-7"


def test_crash_window_submitting_state_is_in_reconciliation_queue(tmp_path: Path) -> None:
    db = tmp_path / "submitting.db"
    plan = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")
    store = OperationStore(db)
    store.create_run("run-submitting", plan)
    store.transition("run-submitting", device().mac, OperationState.SUBMITTING)
    store.close()

    reopened = OperationStore(db)
    assert [row["state"] for row in reopened.reconciliation_queue()] == ["submitting"]


def test_create_run_is_atomic_when_duplicate_rows_violate_primary_key(tmp_path: Path) -> None:
    from dataclasses import replace

    db = tmp_path / "atomic.db"
    original = PlanBuilder(catalog()).build((device(),), "50 Mbps", connection_identity="test")
    duplicate = replace(original, items=(original.items[0], original.items[0]))
    store = OperationStore(db)

    with pytest.raises(sqlite3.IntegrityError):
        store.create_run("run-duplicate", duplicate)

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM operation_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cn_speed_operations").fetchone()[0] == 0
    finally:
        connection.close()
