from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .catalog import Catalog
from .models import BatchPlan, DeviceSnapshot, PlanItem, Rates


class PlanInvalidated(RuntimeError):
    pass


def _fingerprint(devices: tuple[DeviceSnapshot, ...], connection_identity: str) -> str:
    records = [
        {
            "mac": d.mac,
            "dl": d.rates.downlink,
            "ul": d.rates.uplink,
            "package": d.package,
            "network": d.network,
            "tower": d.tower,
            "ap": d.ap,
            "online": d.online,
        }
        for d in sorted(devices, key=lambda item: item.mac)
    ]
    return hashlib.sha256(
        json.dumps(
            {"connection_identity": connection_identity, "devices": records},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def classify_inventory(
    devices: tuple[DeviceSnapshot, ...], catalog: Catalog
) -> tuple[DeviceSnapshot, ...]:
    """Attach only exact DL+UL catalog matches; approximate matches remain unmatched."""
    return tuple(
        replace(
            device,
            package=(match.name if (match := catalog.exact_match(device.rates)) else None),
        )
        for device in devices
    )


class PlanBuilder:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def build(
        self,
        devices: tuple[DeviceSnapshot, ...],
        target_name: str,
        *,
        connection_identity: str,
        approximate_acknowledgements: set[str] | None = None,
        now: datetime | None = None,
    ) -> BatchPlan:
        if not devices:
            raise ValueError("select at least one device")
        if not connection_identity:
            raise ValueError("connection identity is required")
        macs = [device.mac for device in devices]
        if len(macs) != len(set(macs)):
            raise ValueError("duplicate device MAC in plan")
        if len(devices) > self.catalog.max_batch_size:
            raise ValueError(f"maximum batch size is {self.catalog.max_batch_size}")
        target = self.catalog.named(target_name)
        acknowledgements = approximate_acknowledgements or set()
        items: list[PlanItem] = []
        for device in devices:
            denied = self.catalog.scope_denied(
                network=device.network, tower=device.tower, ap=device.ap
            )
            if denied:
                raise ValueError(f"protected scope denied: {denied}")
            if not device.online:
                raise ValueError(f"device is offline: {device.mac}")
            exact_before = self.catalog.exact_match(device.rates)
            approximate = exact_before is None
            if approximate and device.mac not in acknowledgements:
                raise ValueError(f"explicit per-device acknowledgement required: {device.mac}")
            if device.rates == target.rates:
                raise ValueError(f"device already has exact target rates: {device.mac}")
            items.append(
                PlanItem(
                    mac=device.mac,
                    name=device.name,
                    before=device.rates,
                    target=target.rates,
                    before_package=exact_before.name if exact_before else None,
                    target_package=target.name,
                    template=target.template,
                    network=device.network,
                    tower=device.tower,
                    ap=device.ap,
                    online=device.online,
                    observed_at=device.observed_at,
                    rollback_template=exact_before.template if exact_before else None,
                    approximate_acknowledged=approximate,
                )
            )
        frozen = tuple(items)
        created = now or datetime.now(UTC)
        fingerprint = _fingerprint(devices, connection_identity)
        plan_id = hashlib.sha256(
            f"{created.isoformat()}:{target.name}:{fingerprint}".encode()
        ).hexdigest()[:20]
        return BatchPlan(plan_id, created, target.name, frozen, fingerprint, connection_identity)

    def assert_current(
        self, plan: BatchPlan, devices: tuple[DeviceSnapshot, ...], target_name: str
    ) -> None:
        if target_name != plan.target_package:
            raise PlanInvalidated("target changed after preview")
        target = self.catalog.named(target_name)
        if any(
            item.target != target.rates or item.template != target.template
            for item in plan.items
        ):
            raise PlanInvalidated("catalog policy changed after preview")
        for device in devices:
            denied = self.catalog.scope_denied(
                network=device.network, tower=device.tower, ap=device.ap
            )
            if denied or not device.online:
                raise PlanInvalidated("selected inventory is no longer eligible")
        if _fingerprint(devices, plan.connection_identity) != plan.inventory_fingerprint:
            raise PlanInvalidated("selected inventory changed after preview")


if TYPE_CHECKING:
    from .persistence import OperationStore


@dataclass(frozen=True, slots=True)
class RollbackItem:
    mac: str
    template: str
    restore_rates: Rates


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    source_run_id: str
    items: tuple[RollbackItem, ...]
    blocked: tuple[tuple[str, str], ...]


def build_rollback_plan(store: OperationStore, run_id: str, catalog: Catalog) -> RollbackPlan:
    """Build only; execution uses the same documented template API and normal safety pipeline."""
    items: list[RollbackItem] = []
    blocked: list[tuple[str, str]] = []
    rows = [row for row in store.audit_rows() if row["run_id"] == run_id]
    for row in rows:
        if row["state"] != "verified":
            blocked.append((row["mac"], "source operation is not verified"))
            continue
        previous = Rates(row["before_dl"], row["before_ul"])
        package = catalog.exact_match(previous)
        if (
            package is None
            or not row["rollback_template"]
            or row["rollback_template"] != package.template
        ):
            blocked.append((row["mac"], "previous rates do not map to a verified known template"))
            continue
        items.append(RollbackItem(row["mac"], package.template, previous))
    return RollbackPlan(run_id, tuple(items), tuple(blocked))
