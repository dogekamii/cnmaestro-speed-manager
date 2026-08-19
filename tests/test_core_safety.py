import hashlib
from pathlib import Path

import pytest

from operations_toolkit.core.security import redact
from operations_toolkit.core.session import SessionBusy, SessionGate
from operations_toolkit.core.updates import UpdatePolicy, validate_manifest, verify_download
from operations_toolkit.core.validation import non_negative_finite
from operations_toolkit.modules.cnmaestro.adapters import (
    CnMaestroAdapter,
    DemoCnMaestroAdapter,
    LiveCnMaestroAdapter,
)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, "not-a-number"])
def test_offline_days_and_tolerances_require_finite_non_negative_values(value: object) -> None:
    with pytest.raises(ValueError):
        non_negative_finite(value, "offline days")


def test_secret_redaction_covers_nested_values_and_bearer_tokens() -> None:
    payload = {
        "client_secret": "super-secret",  # pragma: allowlist secret
        "nested": {"access_token": "abc123"},
        "message": "Authorization: Bearer eyJ.long.token",
        "safe": "customer",
    }
    result = redact(payload)
    rendered = repr(result)
    assert (
        "super-secret" not in rendered
        and "abc123" not in rendered
        and "eyJ.long.token" not in rendered
    )
    assert result["safe"] == "customer"


def test_connection_switching_is_frozen_during_operation_and_old_session_is_cleared() -> None:
    cleared: list[str] = []
    gate = SessionGate()
    gate.replace("session-a", lambda: cleared.append("a"))
    token = gate.begin("publish")
    with pytest.raises(SessionBusy, match="publish"):
        gate.replace("session-b", lambda: cleared.append("b"))
    gate.end(token)
    gate.replace("session-b", lambda: cleared.append("b"))
    assert cleared == ["a"]
    gate.disconnect()
    assert cleared == ["a", "b"]


def test_update_metadata_and_checksum_are_locked_to_approved_repo(tmp_path: Path) -> None:
    policy = UpdatePolicy("dogekamii", "cnmaestro-speed-manager")
    manifest = validate_manifest(
        {
            "version": "2.0.0-beta.2",
            "download_url": "https://github.com/dogekamii/cnmaestro-speed-manager/releases/download/v2/OperationsToolkit.exe",
            "sha256": "0" * 64,
        },
        policy,
    )
    assert manifest.version == "2.0.0-beta.2"
    with pytest.raises(ValueError, match="approved GitHub repository"):
        validate_manifest(
            {
                "version": "9",
                "download_url": "https://github.com/evil/repo/releases/a.exe",
                "sha256": "0" * 64,
            },
            policy,
        )
    artifact = tmp_path / "OperationsToolkit.exe"
    artifact.write_bytes(b"beta")
    digest = hashlib.sha256(b"beta").hexdigest()
    assert verify_download(artifact, digest) == artifact
    with pytest.raises(ValueError, match="checksum"):
        verify_download(artifact, "0" * 64)


def test_live_and_demo_adapters_satisfy_same_small_contract() -> None:
    assert isinstance(DemoCnMaestroAdapter(), CnMaestroAdapter)
    for method in ("inventory", "pull_rates", "submit_template", "job_status", "close"):
        assert callable(getattr(LiveCnMaestroAdapter, method))


def test_session_shutdown_clears_active_operation_and_is_idempotent() -> None:
    cleared: list[str] = []
    gate = SessionGate()
    gate.replace("session", lambda: cleared.append("session"))
    gate.begin("publish")

    gate.shutdown()
    gate.shutdown()

    assert cleared == ["session"]
    assert gate.connection is None
    assert gate.operation is None
