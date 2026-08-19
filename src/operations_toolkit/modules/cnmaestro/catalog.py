from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Rates

_SCOPE = re.compile(r"^(network|tower|ap):.+$")


@dataclass(frozen=True, slots=True)
class Package:
    name: str
    template: str
    rates: Rates

    def __post_init__(self) -> None:
        if not self.name or not self.template:
            raise ValueError("package name and template must be non-empty")


@dataclass(frozen=True, slots=True)
class Catalog:
    schema_version: int
    packages: tuple[Package, ...]
    protected_scopes: tuple[str, ...] = ()
    max_batch_size: int = 50
    canary_size: int = 1
    failure_threshold: int = 1
    stop_on_first_issue: bool = True

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported catalog schema_version")
        limits = (self.max_batch_size, self.canary_size, self.failure_threshold)
        if not self.packages or any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("catalog limits must be positive integers")
        if type(self.stop_on_first_issue) is not bool:
            raise ValueError("stop_on_first_issue must be boolean")
        if self.canary_size > self.max_batch_size:
            raise ValueError("canary size cannot exceed maximum batch size")
        if any(not isinstance(scope, str) or not _SCOPE.fullmatch(scope) for scope in self.protected_scopes):
            raise ValueError("protected scopes must use network:, tower:, or ap:")
        names = [item.name for item in self.packages]
        templates = [item.template for item in self.packages]
        if len(names) != len(set(names)) or len(templates) != len(set(templates)):
            raise ValueError("package names and templates must be unique")

    def named(self, name: str) -> Package:
        try:
            return next(item for item in self.packages if item.name == name)
        except StopIteration as exc:
            raise ValueError(f"unknown package: {name}") from exc

    def exact_match(self, rates: Rates) -> Package | None:
        return next((item for item in self.packages if item.rates == rates), None)

    def scope_denied(self, *, network: str, tower: str, ap: str) -> str | None:
        scopes = {f"network:{network}", f"tower:{tower}", f"ap:{ap}"}
        return next((rule for rule in self.protected_scopes if rule in scopes), None)


def _strict_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def load_catalog(path: Path) -> Catalog:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("catalog root must be an object")
    allowed = {
        "schema_version",
        "packages",
        "protected_scopes",
        "max_batch_size",
        "canary_size",
        "failure_threshold",
        "stop_on_first_issue",
    }
    required = {"schema_version", "packages", "max_batch_size"}
    unknown = set(raw) - allowed
    missing = required - set(raw)
    if unknown or missing:
        raise ValueError(f"invalid catalog keys; unknown={sorted(unknown)}, missing={sorted(missing)}")
    package_rows = raw["packages"]
    if not isinstance(package_rows, list) or not package_rows:
        raise ValueError("packages must be a non-empty array")
    packages: list[Package] = []
    package_keys = {"name", "template", "downlink", "uplink"}
    for index, item in enumerate(package_rows):
        if not isinstance(item, dict) or set(item) != package_keys:
            raise ValueError(f"package {index} must contain exactly {sorted(package_keys)}")
        name, template = item["name"], item["template"]
        if not isinstance(name, str) or not name or not isinstance(template, str) or not template:
            raise ValueError(f"package {index} name and template must be non-empty strings")
        packages.append(
            Package(
                name,
                template,
                Rates(
                    _strict_int(item["downlink"], "downlink"),
                    _strict_int(item["uplink"], "uplink"),
                ),
            )
        )
    scopes = raw.get("protected_scopes", [])
    if not isinstance(scopes, list):
        raise ValueError("protected_scopes must be an array")
    stop = raw.get("stop_on_first_issue", True)
    if type(stop) is not bool:
        raise ValueError("stop_on_first_issue must be boolean")
    return Catalog(
        schema_version=_strict_int(raw["schema_version"], "schema_version", minimum=1),
        packages=tuple(packages),
        protected_scopes=tuple(scopes),
        max_batch_size=_strict_int(raw["max_batch_size"], "max_batch_size", minimum=1),
        canary_size=_strict_int(raw.get("canary_size", 1), "canary_size", minimum=1),
        failure_threshold=_strict_int(
            raw.get("failure_threshold", 1), "failure_threshold", minimum=1
        ),
        stop_on_first_issue=stop,
    )
