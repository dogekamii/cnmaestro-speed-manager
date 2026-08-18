from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__
from .modules.cnmaestro.catalog import load_catalog
from .modules.cnmaestro.persistence import OperationStore
from .paths import prepare_data_dir
from .registry import first_party_modules


def smoke_test() -> int:
    directory = prepare_data_dir()
    catalog = load_catalog(directory / "packages.json")
    store = OperationStore(directory / "operations.db")
    try:
        assert store.schema_version == 2
        assert catalog.packages
        assert first_party_modules()
    finally:
        store.close()
    print(
        f"Operations Toolkit {__version__} smoke test: PASS | schema=2 | modules=1 | network=disabled"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="OperationsToolkit")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="initialize config/database without GUI or network",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="launch with deterministic, network-impossible sample data",
    )
    parser.add_argument("--version", action="store_true")
    options = parser.parse_args(argv)
    if options.version:
        print(f"Operations Toolkit {__version__}")
        return 0
    if options.smoke_test:
        return smoke_test()
    from .ui.app import Application

    app = Application(demo=options.demo)
    app.mainloop()
    return 0
