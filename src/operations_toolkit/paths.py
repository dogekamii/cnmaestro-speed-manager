from __future__ import annotations

import os
import shutil
from importlib.resources import files
from pathlib import Path

from platformdirs import user_data_path


def data_dir() -> Path:
    override = os.environ.get("OPERATIONS_TOOLKIT_DATA_DIR")
    return (
        Path(override) if override else Path(user_data_path("OperationsToolkit", appauthor=False))
    )


def prepare_data_dir(path: Path | None = None) -> Path:
    target = path or data_dir()
    target.mkdir(parents=True, exist_ok=True)
    catalog = target / "packages.json"
    if not catalog.exists():
        source = files("operations_toolkit.modules.cnmaestro.config").joinpath("packages.json")
        with source.open("rb") as source_handle, catalog.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle)
    return target
