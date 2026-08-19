import os
import subprocess
import sys
from pathlib import Path

from operations_toolkit import __version__
from operations_toolkit.core.modules import ModuleContext, ModuleProvider
from operations_toolkit.registry import first_party_modules


def test_product_identity_and_small_static_module_contract(tmp_path: Path) -> None:
    assert __version__ == "2.0.0-beta.1"
    modules = first_party_modules()
    assert len(modules) == 1
    provider = modules[0]
    assert isinstance(provider, ModuleProvider)
    assert provider.module_id == "cnmaestro.bulk_speed_changes"
    assert provider.title == "Bulk Speed Changes"
    assert "cnMaestro" in provider.description
    assert ModuleContext(tmp_path, demo=True).demo is True


def test_smoke_test_initializes_database_and_config_without_gui_or_network(tmp_path: Path) -> None:
    env = {**os.environ, "OPERATIONS_TOOLKIT_DATA_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "operations_toolkit", "--smoke-test"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Operations Toolkit 2.0.0-beta.1 smoke test: PASS" in result.stdout
    assert "network=disabled" in result.stdout
    assert (tmp_path / "operations.db").exists()
    assert (tmp_path / "packages.json").exists()


def test_demo_mode_adapter_is_selected_without_live_adapter_construction(tmp_path: Path) -> None:
    provider = first_party_modules()[0]
    context = ModuleContext(tmp_path, demo=True)
    adapter = provider.create_adapter(context)
    assert adapter.network_enabled is False


def test_artifact_workflows_gate_packaging_on_full_quality_checks() -> None:
    for name in ("windows-build.yml", "release-artifacts.yml"):
        workflow = Path(".github/workflows", name).read_text(encoding="utf-8")
        positions = [
            workflow.index("pytest"),
            workflow.index("ruff check"),
            workflow.index("mypy"),
            workflow.index("pyinstaller"),
        ]
        assert positions == sorted(positions), name


def test_release_workflow_validates_input_against_runtime_version() -> None:
    workflow = Path(".github/workflows/release-artifacts.yml").read_text(encoding="utf-8")
    assert "inputs.version" in workflow
    assert "Version(" in workflow
    assert "operations_toolkit.__version__" in workflow
