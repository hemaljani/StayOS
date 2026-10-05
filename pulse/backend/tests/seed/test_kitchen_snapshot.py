"""Unit tests for the per-property kitchen snapshot builder.

Confirms :func:`pulse.seed.kitchen_snapshot.build_kitchen_snapshot`:

    * yields the exact curated prototype values for ``ALOHA-CHI-001`` (no
      regression of the demo's canonical property),
    * is deterministic (same property id -> identical snapshot, so a reseed
      never makes the demo flicker and tests are stable),
    * produces distinct data across properties (each demo GM sees a fresh
      kitchen, not an obvious clone),
    * always emits the full snapshot shape for any property id.

# Feature: initial-pulse-project - per-property kitchen snapshot
"""

from __future__ import annotations

from pulse.seed.kitchen_snapshot import DEMO_PROPERTY_ID, build_kitchen_snapshot

_PILOT_PROPERTIES = (
    "ALOHA-CHI-001",
    "ALOHA-MIA-001",
    "ALOHA-TYO-001",
    "ALOHA-MAD-001",
    "ALOHA-BOM-001",
)

_REQUIRED_KEYS = {
    "propertyId",
    "banquetCountdown",
    "fbStats",
    "deliverySla",
    "kitchenOrders",
    "channelMix",
    "channelMixNote",
}


def test_chi_snapshot_matches_curated_prototype_values() -> None:
    """The canonical demo property keeps the exact curated prototype values."""
    snapshot = build_kitchen_snapshot(DEMO_PROPERTY_ID)

    countdown = snapshot["banquetCountdown"]
    assert countdown["title"] == "Meridian Corp Breakfast \u00b7 82 Covers"
    assert countdown["minutesRemaining"] == 18
    assert countdown["progressPct"] == 72
    assert snapshot["fbStats"][0]["value"] == "47"
    assert snapshot["deliverySla"]["pct"] == 90
    assert len(snapshot["kitchenOrders"]) == 5
    assert snapshot["channelMix"][0] == {"label": "Room Svc", "pct": 62}
    assert snapshot["channelMix"][-1] == {
        "label": "3rd Party",
        "pct": 5,
        "warning": True,
    }
    assert "commission leakage" in snapshot["channelMixNote"]


def test_snapshot_is_deterministic_per_property() -> None:
    """A given property id always produces an identical snapshot."""
    for property_id in _PILOT_PROPERTIES:
        first = build_kitchen_snapshot(property_id)
        second = build_kitchen_snapshot(property_id)
        assert first == second


def test_snapshots_are_distinct_across_properties() -> None:
    """Different properties get distinct kitchen data (not a clone of CHI)."""
    titles = {
        build_kitchen_snapshot(pid)["banquetCountdown"]["title"]
        for pid in _PILOT_PROPERTIES
    }
    # All five banquet titles differ (distinct events + cover counts).
    assert len(titles) == len(_PILOT_PROPERTIES)

    # A non-CHI property differs from CHI in more than just the propertyId.
    chi = build_kitchen_snapshot(DEMO_PROPERTY_ID)
    mia = build_kitchen_snapshot("ALOHA-MIA-001")
    assert chi["banquetCountdown"]["title"] != mia["banquetCountdown"]["title"]
    assert chi != mia


def test_snapshot_shape_complete_for_any_property() -> None:
    """Every property snapshot carries the full expected attribute set."""
    for property_id in (*_PILOT_PROPERTIES, "SOME-OTHER-PROP-999"):
        snapshot = build_kitchen_snapshot(property_id)
        assert set(snapshot) == _REQUIRED_KEYS
        assert snapshot["propertyId"] == property_id
        assert len(snapshot["fbStats"]) == 3
        # channelMix percentages are a plausible 100% split.
        assert sum(slice_["pct"] for slice_ in snapshot["channelMix"]) == 100
        # A banquet setup order is always present in the feed.
        assert any(order["kind"] == "banquet" for order in snapshot["kitchenOrders"])
