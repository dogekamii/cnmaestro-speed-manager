from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_MAC = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


@dataclass(frozen=True, slots=True)
class Rates:
    downlink: int
    uplink: int

    def __post_init__(self) -> None:
        if type(self.downlink) is not int or type(self.uplink) is not int:
            raise ValueError("rates must be integers")
        if self.downlink < 0 or self.uplink < 0:
            raise ValueError("rates must be non-negative")


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    mac: str
    name: str
    rates: Rates
    package: str | None
    network: str
    tower: str
    ap: str
    online: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if not _MAC.fullmatch(self.mac):
            raise ValueError(f"invalid MAC address: {self.mac}")


@dataclass(frozen=True, slots=True)
class PlanItem:
    mac: str
    name: str
    before: Rates
    target: Rates
    before_package: str | None
    target_package: str
    template: str
    network: str
    tower: str
    ap: str
    online: bool
    observed_at: datetime
    rollback_template: str | None
    approximate_acknowledged: bool


@dataclass(frozen=True, slots=True)
class BatchPlan:
    plan_id: str
    created_at: datetime
    target_package: str
    items: tuple[PlanItem, ...]
    inventory_fingerprint: str
    connection_identity: str
