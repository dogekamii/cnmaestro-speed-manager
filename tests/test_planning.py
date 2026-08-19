from dataclasses import replace
from datetime import UTC, datetime

import pytest

from operations_toolkit.modules.cnmaestro.catalog import Catalog, Package
from operations_toolkit.modules.cnmaestro.models import DeviceSnapshot, Rates
from operations_toolkit.modules.cnmaestro.planning import PlanBuilder, PlanInvalidated

NOW = datetime(2026, 8, 18, tzinfo=UTC)


def catalog() -> Catalog:
    return Catalog(
        schema_version=1,
        packages=(
            Package("10 Mbps", "10mbps Package", Rates(10752, 1075)),
            Package("50 Mbps", "50mbps Package", Rates(53760, 10750)),
        ),
        protected_scopes=("network:Core",),
        max_batch_size=2,
    )


def device(
    mac: str = "0A:00:3E:80:42:EC", *, dl: int = 10752, ul: int = 1075, network: str = "Access"
) -> DeviceSnapshot:
    return DeviceSnapshot(
        mac=mac,
        name="Customer A",
        rates=Rates(dl, ul),
        package="10 Mbps",
        network=network,
        tower="North",
        ap="AA:BB:CC:DD:EE:FF",
        online=True,
        observed_at=NOW,
    )


def test_target_changed_after_preview_invalidates_immutable_plan() -> None:
    builder = PlanBuilder(catalog())
    plan = builder.build((device(),), "50 Mbps", connection_identity="test")
    assert plan.items[0].before == Rates(10752, 1075)
    with pytest.raises(PlanInvalidated, match="target changed"):
        builder.assert_current(plan, (device(),), "10 Mbps")


def test_unmatched_rate_drift_is_detected_by_exact_rates_not_label() -> None:
    builder = PlanBuilder(catalog())
    plan = builder.build((device(),), "50 Mbps", connection_identity="test")
    drifted = replace(device(), rates=Rates(10753, 1075), package="10 Mbps")
    with pytest.raises(PlanInvalidated, match="inventory changed"):
        builder.assert_current(plan, (drifted,), "50 Mbps")


def test_package_matching_uses_both_rates() -> None:
    c = catalog()
    assert c.exact_match(Rates(10752, 1075)).name == "10 Mbps"
    assert c.exact_match(Rates(10752, 9999)) is None


def test_approximate_match_requires_explicit_per_device_acknowledgement() -> None:
    builder = PlanBuilder(catalog())
    unmatched = replace(device(), rates=Rates(10700, 1060), package=None)
    with pytest.raises(ValueError, match="acknowledgement"):
        builder.build((unmatched,), "50 Mbps", connection_identity="test")
    plan = builder.build((unmatched,), "50 Mbps", connection_identity="test", approximate_acknowledgements={unmatched.mac})
    assert plan.items[0].approximate_acknowledged is True


def test_max_batch_and_protected_scope_rules() -> None:
    builder = PlanBuilder(catalog())
    with pytest.raises(ValueError, match="maximum batch"):
        builder.build(
            (device(), device("0A:00:3E:80:42:ED"), device("0A:00:3E:80:42:EE")), "50 Mbps", connection_identity="test"
        )
    with pytest.raises(ValueError, match="protected scope"):
        builder.build((device(network="Core"),), "50 Mbps", connection_identity="test")


def test_catalog_rejects_invalid_failure_and_canary_limits() -> None:
    with pytest.raises(ValueError, match="limits"):
        replace(catalog(), failure_threshold=0)
    with pytest.raises(ValueError, match="canary"):
        replace(catalog(), canary_size=3, max_batch_size=2)


def test_scanned_inventory_is_classified_by_exact_both_rate_match() -> None:
    from operations_toolkit.modules.cnmaestro.planning import classify_inventory

    scanned = replace(device(), package=None)
    classified = classify_inventory((scanned,), catalog())
    assert classified[0].package == "10 Mbps"


def test_duplicate_device_macs_are_rejected_before_plan_creation() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        PlanBuilder(replace(catalog(), max_batch_size=3)).build(
            (device(), device()), "50 Mbps", connection_identity="test"
        )


def test_catalog_target_policy_change_invalidates_existing_plan() -> None:
    original = catalog()
    plan = PlanBuilder(original).build((device(),), "50 Mbps", connection_identity="test")
    changed_target = replace(
        original.packages[1], template="new-template", rates=Rates(60000, 12000)
    )
    builder = PlanBuilder(replace(original, packages=(original.packages[0], changed_target)))

    with pytest.raises(PlanInvalidated, match="catalog policy"):
        builder.assert_current(plan, (device(),), "50 Mbps")
