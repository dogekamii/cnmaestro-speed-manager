import json
from copy import deepcopy
from pathlib import Path

import pytest

from operations_toolkit.modules.cnmaestro.catalog import load_catalog

BASE = {
    "schema_version": 1,
    "max_batch_size": 10,
    "canary_size": 1,
    "failure_threshold": 1,
    "stop_on_first_issue": True,
    "protected_scopes": [],
    "packages": [
        {"name": "10 Mbps", "template": "10mbps Package", "downlink": 10752, "uplink": 1075}
    ],
}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["packages"][0].update(name=""),
        lambda value: value["packages"][0].update(template=""),
        lambda value: value["packages"][0].update(downlink=1.9),
        lambda value: value["packages"][0].update(uplink=True),
        lambda value: value.update(protected_scopes=["invalid:scope"]),
        lambda value: value["packages"][0].update(unknown="value"),
        lambda value: value.update(stop_on_first_issue="false"),
    ],
)
def test_catalog_loader_rejects_values_outside_shipped_schema(tmp_path: Path, mutate) -> None:
    payload = deepcopy(BASE)
    mutate(payload)
    path = tmp_path / "packages.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_catalog(path)
