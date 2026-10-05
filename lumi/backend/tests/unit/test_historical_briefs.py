"""Unit tests for LUMI historical briefs seeder module.

Tests the KPI generation, action item selection, narrative generation,
record building, and DynamoDB seeding functions in historical_briefs.py.
Validates REQ-HIST-1 through REQ-HIST-6 acceptance criteria.
"""

import re
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from historical_briefs import (
    PROPERTY_PROFILES,
    _build_historical_brief_record,
    _generate_daily_kpis,
    _generate_narrative,
    _select_action_items,
    seed_historical_briefs,
)
from seed_data import GM_SEED_DATA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Sample GM dict for unit tests on individual functions
SAMPLE_GM: Dict[str, Any] = GM_SEED_DATA[0]  # Chicago - jsmith

# Sample profile for unit tests
SAMPLE_PROFILE: Dict[str, Any] = PROPERTY_PROFILES["ALOHA-CHI-001"]

SAMPLE_DATE: str = "2025-01-15"


# ---------------------------------------------------------------------------
# Tests: _generate_daily_kpis - Occupancy within range
# ---------------------------------------------------------------------------


def test_generate_daily_kpis_occupancy_within_range() -> None:
    """Verify occupancy stays within occ_min/occ_max for all 5 profiles across all 7 days.

    REQ-HIST-2: Each property's occupancy must remain within its configured bounds.
    """
    for property_id, profile in PROPERTY_PROFILES.items():
        for day_index in range(7):
            kpis = _generate_daily_kpis(profile, day_index, SAMPLE_DATE)
            occupancy = kpis["occupancy"]["current"]
            assert profile["occ_min"] <= occupancy <= profile["occ_max"], (
                f"Property {property_id} day {day_index}: occupancy {occupancy} "
                f"outside range [{profile['occ_min']}, {profile['occ_max']}]"
            )


# ---------------------------------------------------------------------------
# Tests: _generate_daily_kpis - RevPAR calculation
# ---------------------------------------------------------------------------


def test_generate_daily_kpis_revpar_calculation() -> None:
    """Verify RevPAR = round(occ * adr / 100) for all profiles and days.

    REQ-HIST-2: RevPAR is calculated from occupancy and ADR, not independently randomized.
    """
    for property_id, profile in PROPERTY_PROFILES.items():
        for day_index in range(7):
            kpis = _generate_daily_kpis(profile, day_index, SAMPLE_DATE)
            occupancy = kpis["occupancy"]["current"]
            adr = kpis["adr"]["current"]
            expected_revpar = round(occupancy * adr / 100)
            actual_revpar = kpis["revPAR"]["current"]
            assert actual_revpar == expected_revpar, (
                f"Property {property_id} day {day_index}: RevPAR {actual_revpar} "
                f"!= round({occupancy} * {adr} / 100) = {expected_revpar}"
            )


# ---------------------------------------------------------------------------
# Tests: _generate_daily_kpis - No large jumps
# ---------------------------------------------------------------------------


def test_generate_daily_kpis_no_large_jumps() -> None:
    """Verify no day-over-day occupancy jump > 4 points for all profiles.

    REQ-HIST-2: Max day-over-day swing is 4 percentage points for occupancy.
    """
    for property_id, profile in PROPERTY_PROFILES.items():
        occupancies: List[int] = []
        for day_index in range(7):
            kpis = _generate_daily_kpis(profile, day_index, SAMPLE_DATE)
            occupancies.append(kpis["occupancy"]["current"])

        for i in range(1, len(occupancies)):
            jump = abs(occupancies[i] - occupancies[i - 1])
            assert jump <= 4, (
                f"Property {property_id} day {i-1}->{i}: occupancy jump "
                f"{jump} points ({occupancies[i-1]} -> {occupancies[i]}) exceeds max of 4"
            )


# ---------------------------------------------------------------------------
# Tests: _select_action_items - Excludes overbooking below 88
# ---------------------------------------------------------------------------


def test_select_action_items_excludes_overbooking_below_88() -> None:
    """Verify no OVERBOOKING_RISK action item when occupancy is 85%.

    REQ-HIST-3: Overbooking risk action item only appears when occupancy exceeds 88%.
    """
    actions = _select_action_items(
        profile=SAMPLE_PROFILE,
        occupancy=85,
        vip_count=5,
        day_index=3,
        property_id="ALOHA-CHI-001",
        brief_date=SAMPLE_DATE,
    )

    action_types = [action["type"] for action in actions]
    assert "OVERBOOKING_RISK" not in action_types, (
        "OVERBOOKING_RISK should not appear when occupancy is below 88%"
    )


# ---------------------------------------------------------------------------
# Tests: _select_action_items - Includes overbooking at 88+
# ---------------------------------------------------------------------------


def test_select_action_items_includes_overbooking_at_88() -> None:
    """Verify OVERBOOKING_RISK appears when occupancy is 90%.

    REQ-HIST-3: Overbooking risk action item appears when occupancy exceeds 88%.
    """
    actions = _select_action_items(
        profile=SAMPLE_PROFILE,
        occupancy=90,
        vip_count=5,
        day_index=3,
        property_id="ALOHA-MIA-001",
        brief_date=SAMPLE_DATE,
    )

    action_types = [action["type"] for action in actions]
    assert "OVERBOOKING_RISK" in action_types, (
        "OVERBOOKING_RISK should appear when occupancy is at 90%"
    )


# ---------------------------------------------------------------------------
# Tests: _select_action_items - Count range 3-6
# ---------------------------------------------------------------------------


def test_select_action_items_count_range() -> None:
    """Verify action item count is between 3-6 for all day indices (0-6).

    REQ-HIST-3: Each day has 3-6 action items.
    """
    for day_index in range(7):
        actions = _select_action_items(
            profile=SAMPLE_PROFILE,
            occupancy=85,
            vip_count=5,
            day_index=day_index,
            property_id="ALOHA-MIA-001",
            brief_date=SAMPLE_DATE,
        )
        count = len(actions)
        assert 3 <= count <= 6, (
            f"Day {day_index}: action item count {count} outside range [3, 6]"
        )


# ---------------------------------------------------------------------------
# Tests: _select_action_items - Chicago today mock data
# ---------------------------------------------------------------------------


def test_select_action_items_chicago_today() -> None:
    """Verify day_index=6 + property_id ALOHA-CHI-001 returns exactly 5 items matching mock.

    REQ-HIST-3: Today's action items match the existing mock data for demo continuity.
    """
    actions = _select_action_items(
        profile=SAMPLE_PROFILE,
        occupancy=89,
        vip_count=7,
        day_index=6,
        property_id="ALOHA-CHI-001",
        brief_date=SAMPLE_DATE,
    )

    assert len(actions) == 5, f"Expected 5 items for Chicago today, got {len(actions)}"

    # Verify expected titles from mock data
    expected_titles = [
        "Overbooking Risk - +6 Rooms",
        "4 Rooms Out of Order",
        "Ambassador VIP - David Chen",
        "Upsell Opportunity - 28 Eligible Arrivals",
        "F&B Staffing Confirmed",
    ]
    actual_titles = [action["title"] for action in actions]
    assert actual_titles == expected_titles, (
        f"Chicago today titles mismatch.\nExpected: {expected_titles}\nActual: {actual_titles}"
    )


# ---------------------------------------------------------------------------
# Tests: _generate_narrative - No unfilled placeholders
# ---------------------------------------------------------------------------


def test_generate_narrative_no_unfilled_placeholders() -> None:
    """Verify no {placeholder} patterns remain in narrative output for all profiles.

    REQ-HIST-1: Narrative text must be fully interpolated with no template artifacts.
    """
    for property_id, profile in PROPERTY_PROFILES.items():
        # Find the GM matching this property
        gm = next(gm for gm in GM_SEED_DATA if gm["propertyId"] == property_id)

        for day_index in range(7):
            kpis = _generate_daily_kpis(profile, day_index, SAMPLE_DATE)
            occupancy = kpis["occupancy"]["current"]
            vip_count = kpis["arrivals"]["vipCount"]

            actions = _select_action_items(
                profile=profile,
                occupancy=occupancy,
                vip_count=vip_count,
                day_index=day_index,
                property_id=property_id,
                brief_date=SAMPLE_DATE,
            )

            narrative = _generate_narrative(
                profile=profile,
                gm=gm,
                kpis=kpis,
                actions=actions,
                day_index=day_index,
                brief_date=SAMPLE_DATE,
            )

            # Check for any unfilled {placeholder} patterns
            unfilled = re.findall(r"\{[^}]+\}", narrative)
            assert not unfilled, (
                f"Property {property_id} day {day_index}: unfilled placeholders "
                f"found in narrative: {unfilled}"
            )


# ---------------------------------------------------------------------------
# Tests: _generate_narrative - Rotation produces different narratives
# ---------------------------------------------------------------------------


def test_generate_narrative_rotation() -> None:
    """Verify day 0, 1, 2 produce different narratives.

    REQ-HIST-1: Narrative text varies by day (not identical across all 7 days).
    """
    profile = SAMPLE_PROFILE
    gm = SAMPLE_GM
    narratives: List[str] = []

    for day_index in range(3):
        kpis = _generate_daily_kpis(profile, day_index, SAMPLE_DATE)
        occupancy = kpis["occupancy"]["current"]
        vip_count = kpis["arrivals"]["vipCount"]

        actions = _select_action_items(
            profile=profile,
            occupancy=occupancy,
            vip_count=vip_count,
            day_index=day_index,
            property_id="ALOHA-CHI-001",
            brief_date=SAMPLE_DATE,
        )

        narrative = _generate_narrative(
            profile=profile,
            gm=gm,
            kpis=kpis,
            actions=actions,
            day_index=day_index,
            brief_date=SAMPLE_DATE,
        )
        narratives.append(narrative)

    # All 3 narratives should be distinct (different templates + different KPI values)
    assert len(set(narratives)) == 3, (
        "Days 0, 1, 2 should produce different narratives due to template rotation"
    )


# ---------------------------------------------------------------------------
# Tests: _build_historical_brief_record - Status field
# ---------------------------------------------------------------------------


def test_build_historical_brief_record_status() -> None:
    """Verify status is DELIVERED for day 0-5, GENERATED for day 6.

    REQ-HIST-1: Status field uses DELIVERED for historical, GENERATED for today.
    """
    profile = SAMPLE_PROFILE
    gm = SAMPLE_GM

    for day_index in range(6):
        record = _build_historical_brief_record(gm, profile, SAMPLE_DATE, day_index)
        assert record["status"] == "DELIVERED", (
            f"Day {day_index}: expected status DELIVERED, got {record['status']}"
        )

    # Day 6 (today) should be GENERATED
    record = _build_historical_brief_record(gm, profile, SAMPLE_DATE, 6)
    assert record["status"] == "GENERATED", (
        f"Day 6: expected status GENERATED, got {record['status']}"
    )


# ---------------------------------------------------------------------------
# Tests: _build_historical_brief_record - Required fields
# ---------------------------------------------------------------------------


def test_build_historical_brief_record_required_fields() -> None:
    """Verify all required top-level fields exist in the brief record.

    REQ-HIST-1: Each brief record must contain all required fields matching
    the orchestrator schema.
    """
    profile = SAMPLE_PROFILE
    gm = SAMPLE_GM

    record = _build_historical_brief_record(gm, profile, SAMPLE_DATE, 3)

    required_fields = [
        "propertyId",
        "briefDate",
        "dailyKPIs",
        "narrative",
        "audioBrief",
        "status",
        "ttl",
        "gmAlias",
    ]

    for field in required_fields:
        assert field in record, f"Missing required field: {field}"

    # Verify types for key fields
    assert isinstance(record["propertyId"], str)
    assert isinstance(record["briefDate"], str)
    assert isinstance(record["dailyKPIs"], dict)
    assert isinstance(record["narrative"], str)
    assert isinstance(record["audioBrief"], dict)
    assert isinstance(record["status"], str)
    assert isinstance(record["ttl"], int)
    assert isinstance(record["gmAlias"], str)


# ---------------------------------------------------------------------------
# Tests: seed_historical_briefs - Writes 35 records
# ---------------------------------------------------------------------------


def test_seed_historical_briefs_writes_35_records() -> None:
    """Verify seed function writes 35 PutItem records (5 GMs x 7 days).

    REQ-HIST-1: 7 records are seeded per GM (5 GMs x 7 days = 35 total records).
    """
    table_name = "stayos-briefs-test"

    # Mock the DynamoDB Table's put_item to track calls without actual writes
    # (avoids DynamoDB float serialization issue in moto with round() values)
    mock_table = MagicMock()
    mock_table.put_item.return_value = {}

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with patch("historical_briefs._dynamodb_resource", mock_dynamodb):
        records_written = seed_historical_briefs(
            table_name=table_name,
            gm_list=GM_SEED_DATA,
            days=7,
        )

    assert records_written == 35, (
        f"Expected 35 records written, got {records_written}"
    )

    # Verify put_item was called 35 times
    assert mock_table.put_item.call_count == 35, (
        f"Expected 35 put_item calls, got {mock_table.put_item.call_count}"
    )


# ---------------------------------------------------------------------------
# Tests: seed_historical_briefs - Idempotent on ConditionalCheckFailed
# ---------------------------------------------------------------------------


def test_seed_historical_briefs_idempotent() -> None:
    """Verify function returns 0 and does not raise when all PutItem calls fail with ConditionalCheckFailedException.

    REQ-HIST-1: Seeding is idempotent - re-running does not duplicate or overwrite.
    """
    from botocore.exceptions import ClientError

    table_name = "stayos-briefs-test"

    # Mock DynamoDB Table where every put_item raises ConditionalCheckFailedException
    mock_table = MagicMock()
    mock_table.put_item.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "The conditional request failed",
            }
        },
        operation_name="PutItem",
    )

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with patch("historical_briefs._dynamodb_resource", mock_dynamodb):
        records_written = seed_historical_briefs(
            table_name=table_name,
            gm_list=GM_SEED_DATA,
            days=7,
        )

    # All 35 writes should be skipped (not errored), returning 0
    assert records_written == 0, (
        f"Expected 0 records on idempotent re-run, got {records_written}"
    )

    # Verify put_item was still called 35 times (attempted for each record)
    assert mock_table.put_item.call_count == 35
