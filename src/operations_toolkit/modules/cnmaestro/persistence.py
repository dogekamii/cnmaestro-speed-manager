from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from operations_toolkit.core.security import redact

from .models import BatchPlan

CURRENT_SCHEMA_VERSION = 2


class OperationState(StrEnum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    JOB_KNOWN = "job_known"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


_ALLOWED: dict[OperationState, set[OperationState]] = {
    OperationState.PLANNED: {OperationState.SUBMITTING, OperationState.FAILED},
    OperationState.SUBMITTING: {
        OperationState.SUBMITTED,
        OperationState.UNKNOWN,
        OperationState.FAILED,
    },
    OperationState.SUBMITTED: {
        OperationState.JOB_KNOWN,
        OperationState.UNKNOWN,
        OperationState.FAILED,
    },
    OperationState.JOB_KNOWN: {
        OperationState.VERIFIED,
        OperationState.UNKNOWN,
        OperationState.FAILED,
    },
    OperationState.UNKNOWN: {OperationState.JOB_KNOWN, OperationState.FAILED},
    OperationState.VERIFIED: set(),
    OperationState.FAILED: set(),
}


class OperationStore:
    """SQLite WAL evidence store with transactional, recoverable migrations."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")

    @property
    def schema_version(self) -> int:
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def journal_mode(self) -> str:
        return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def _backup(self, version: int) -> None:
        backup_path = Path(f"{self.path}.v{version}.bak")
        destination = sqlite3.connect(backup_path)
        try:
            self._connection.backup(destination)
        finally:
            destination.close()

    def _migrate(self) -> None:
        version = self.schema_version
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this application")
        if version and version < CURRENT_SCHEMA_VERSION:
            self._backup(version)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if version < 1:
                self._connection.execute(
                    "CREATE TABLE operation_runs(run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, module_id TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL, approval_state TEXT NOT NULL, completed_at TEXT)"
                )
                self._connection.execute(
                    "CREATE TABLE cn_speed_operations(run_id TEXT NOT NULL REFERENCES operation_runs(run_id), mac TEXT NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL, before_dl INTEGER NOT NULL, before_ul INTEGER NOT NULL, target_dl INTEGER NOT NULL, target_ul INTEGER NOT NULL, before_package TEXT, target_package TEXT NOT NULL, template TEXT NOT NULL, rollback_template TEXT, network TEXT NOT NULL, tower TEXT NOT NULL, ap TEXT NOT NULL, online INTEGER NOT NULL, observed_at TEXT NOT NULL, job_id TEXT, error_detail TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(run_id,mac))"
                )
                self._connection.execute(
                    "CREATE INDEX idx_cn_speed_state ON cn_speed_operations(state)"
                )
                version = 1
            if version < 2:
                self._connection.execute(
                    "ALTER TABLE cn_speed_operations ADD COLUMN error_category TEXT"
                )
                self._connection.execute(
                    "ALTER TABLE cn_speed_operations ADD COLUMN verified_dl INTEGER"
                )
                self._connection.execute(
                    "ALTER TABLE cn_speed_operations ADD COLUMN verified_ul INTEGER"
                )
                version = 2
            self._connection.execute(f"PRAGMA user_version={version}")
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def create_run(self, run_id: str, plan: BatchPlan) -> None:
        now = datetime.now(UTC).isoformat()
        approval = "pending_canary" if len(plan.items) > 1 else "not_required"
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "INSERT INTO operation_runs VALUES(?,?,?,?,?,?,NULL)",
                (run_id, plan.plan_id, "cnmaestro.bulk_speed_changes", now, "active", approval),
            )
            self._connection.executemany(
                "INSERT INTO cn_speed_operations(run_id,mac,sequence,state,before_dl,before_ul,target_dl,target_ul,before_package,target_package,template,rollback_template,network,tower,ap,online,observed_at,job_id,error_detail,attempts,created_at,updated_at,error_category,verified_dl,verified_ul) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,?,?,NULL,NULL,NULL)",
                [
                    (
                        run_id,
                        item.mac,
                        index,
                        OperationState.PLANNED,
                        item.before.downlink,
                        item.before.uplink,
                        item.target.downlink,
                        item.target.uplink,
                        item.before_package,
                        item.target_package,
                        item.template,
                        item.rollback_template,
                        item.network,
                        item.tower,
                        item.ap,
                        int(item.online),
                        item.observed_at.isoformat(),
                        now,
                        now,
                    )
                    for index, item in enumerate(plan.items, 1)
                ],
            )
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def transition(self, run_id: str, mac: str, state: OperationState, **fields: Any) -> None:
        current_row = self._connection.execute(
            "SELECT state FROM cn_speed_operations WHERE run_id=? AND mac=?", (run_id, mac)
        ).fetchone()
        if current_row is None:
            raise KeyError((run_id, mac))
        current = OperationState(current_row[0])
        if state not in _ALLOWED[current]:
            raise ValueError(f"invalid state transition: {current} -> {state}")
        allowed = {
            "job_id",
            "error_category",
            "error_detail",
            "attempts",
            "verified_dl",
            "verified_ul",
        }
        if "error_detail" in fields:
            fields["error_detail"] = redact(fields["error_detail"])
        if set(fields) - allowed:
            raise ValueError("unsupported transition fields")
        assignments = ["state=?", "updated_at=?", *[f"{key}=?" for key in fields]]
        values: list[Any] = [state, datetime.now(UTC).isoformat(), *fields.values(), run_id, mac]
        with self._connection:
            self._connection.execute(
                f"UPDATE cn_speed_operations SET {','.join(assignments)} WHERE run_id=? AND mac=?",
                values,
            )

    def set_approval(self, run_id: str, state: str) -> None:
        if state not in {"approved", "declined", "canary_failed", "pending_canary", "not_required"}:
            raise ValueError("invalid approval state")
        with self._connection:
            self._connection.execute(
                "UPDATE operation_runs SET approval_state=? WHERE run_id=?", (state, run_id)
            )

    def device(self, run_id: str, mac: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM cn_speed_operations WHERE run_id=? AND mac=?", (run_id, mac)
        ).fetchone()
        if row is None:
            raise KeyError((run_id, mac))
        return dict(row)

    def reconciliation_queue(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM cn_speed_operations WHERE state IN ('submitting','unknown','submitted','job_known') ORDER BY updated_at"
            )
        ]

    def audit_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._connection.execute(
                "SELECT r.created_at AS run_created_at,r.approval_state,o.* FROM cn_speed_operations o JOIN operation_runs r USING(run_id) ORDER BY r.created_at,o.sequence"
            )
        ]

    def export_json(self, path: Path) -> Path:
        path.write_text(json.dumps(self.audit_rows(), indent=2), encoding="utf-8")
        return path

    def export_csv(self, path: Path) -> Path:
        rows = self.audit_rows()
        with path.open("w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        return path

    def raw(self, statement: str) -> list[sqlite3.Row]:
        if not statement.lstrip().upper().startswith("PRAGMA"):
            raise ValueError("raw queries are limited to PRAGMA inspection")
        return list(self._connection.execute(statement))

    def close(self) -> None:
        self._connection.close()
