"""Unit tests for the dataset_generator.guests_generator module.

Tests VIP guest profile generation including loyalty tier distribution,
preferences assignment, special occasion and sensitive notes assignment,
corporate account derivation, email/phone generation, and the full
generate_guests function.
"""

from datetime import date, timedelta
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from dataset_generator.config import (
    CORPORATE_ACCOUNTS,
    GUEST_PREFERENCES_POOL,
    LOYALTY_TIER_DISTRIBUTION,
    PROPERTY_PROFILES,
    SEED_DAYS,
    SENSITIVE_NOTES_POOL,
    SPECIAL_OCCASIONS_POOL,
    STAY_COUNT_RANGES,
    VIP_NAME_POOL,
)
from dataset_generator.guests_generator import (
    PROPERTY_AREA_CODES,
    PROPERTY_SHORT_CODES,
    _assign_corporate_account,
    _assign_preferences,
    _assign_sensitive_notes,
    _assign_special_occasion,
    _build_tier_sequence,
    _compute_last_stay_date,
    _compute_total_stays,
    _derive_email,
    _generate_guest_id,
    _generate_phone,
    generate_guests,
)


# ---------------------------------------------------------------------------
# _build_tier_sequence tests
# ---------------------------------------------------------------------------


class TestBuildTierSequence:
    """Tests for loyalty tier sequence generation."""

    def test_sequence_length_is_50(self) -> None:
        """Tier sequence must contain exactly 50 entries."""
        sequence = _build_tier_sequence()
        assert len(sequence) == 50

    def test_ambassador_count(self) -> None:
        """Sequence must contain exactly 15 AMBASSADOR entries."""
        sequence = _build_tier_sequence()
        assert sequence.count("AMBASSADOR") == 15

    def test_titanium_count(self) -> None:
        """Sequence must contain exactly 20 TITANIUM entries."""
        sequence = _build_tier_sequence()
        assert sequence.count("TITANIUM") == 20

    def test_platinum_count(self) -> None:
        """Sequence must contain exactly 15 PLATINUM entries."""
        sequence = _build_tier_sequence()
        assert sequence.count("PLATINUM") == 15

    def test_order_ambassador_first(self) -> None:
        """First 15 entries are all AMBASSADOR."""
        sequence = _build_tier_sequence()
        assert all(t == "AMBASSADOR" for t in sequence[:15])

    def test_order_titanium_middle(self) -> None:
        """Entries 15-34 are all TITANIUM."""
        sequence = _build_tier_sequence()
        assert all(t == "TITANIUM" for t in sequence[15:35])

    def test_order_platinum_last(self) -> None:
        """Entries 35-49 are all PLATINUM."""
        sequence = _build_tier_sequence()
        assert all(t == "PLATINUM" for t in sequence[35:50])


# ---------------------------------------------------------------------------
# _generate_guest_id tests
# ---------------------------------------------------------------------------


class TestGenerateGuestId:
    """Tests for guest ID generation."""

    def test_format_chicago_first(self) -> None:
        """First Chicago guest produces G-CHI-001."""
        assert _generate_guest_id("ALOHA-CHI-001", 0) == "G-CHI-001"

    def test_format_miami_last(self) -> None:
        """Last Miami guest produces G-MIA-050."""
        assert _generate_guest_id("ALOHA-MIA-001", 49) == "G-MIA-050"

    def test_format_tokyo(self) -> None:
        """Tokyo guest at index 9 produces G-TYO-010."""
        assert _generate_guest_id("ALOHA-TYO-001", 9) == "G-TYO-010"

    def test_all_properties_have_short_codes(self) -> None:
        """Every property in PROPERTY_PROFILES has a short code mapping."""
        for profile in PROPERTY_PROFILES:
            assert profile["propertyId"] in PROPERTY_SHORT_CODES


# ---------------------------------------------------------------------------
# _compute_total_stays tests
# ---------------------------------------------------------------------------


class TestComputeTotalStays:
    """Tests for deterministic total stays calculation."""

    def test_ambassador_within_range(self) -> None:
        """Ambassador stays must be between 25 and 80."""
        for i in range(50):
            stays = _compute_total_stays("AMBASSADOR", i)
            assert 25 <= stays <= 80

    def test_titanium_within_range(self) -> None:
        """Titanium stays must be between 10 and 40."""
        for i in range(50):
            stays = _compute_total_stays("TITANIUM", i)
            assert 10 <= stays <= 40

    def test_platinum_within_range(self) -> None:
        """Platinum stays must be between 5 and 20."""
        for i in range(50):
            stays = _compute_total_stays("PLATINUM", i)
            assert 5 <= stays <= 20

    def test_deterministic(self) -> None:
        """Same inputs always produce same stay count."""
        stays_a = _compute_total_stays("AMBASSADOR", 7)
        stays_b = _compute_total_stays("AMBASSADOR", 7)
        assert stays_a == stays_b

    def test_varied_distribution(self) -> None:
        """Different indices produce different stay values (not all the same)."""
        stays = {_compute_total_stays("TITANIUM", i) for i in range(20)}
        assert len(stays) > 1


# ---------------------------------------------------------------------------
# _assign_preferences tests
# ---------------------------------------------------------------------------


class TestAssignPreferences:
    """Tests for preference assignment logic."""

    def test_returns_1_to_5_preferences(self) -> None:
        """Each guest gets between 1 and 5 preferences."""
        for i in range(50):
            prefs = _assign_preferences(i)
            assert 1 <= len(prefs) <= 5

    def test_preference_count_cycles(self) -> None:
        """Preference count cycles through 1, 2, 3, 4, 5."""
        assert len(_assign_preferences(0)) == 1
        assert len(_assign_preferences(1)) == 2
        assert len(_assign_preferences(2)) == 3
        assert len(_assign_preferences(3)) == 4
        assert len(_assign_preferences(4)) == 5
        assert len(_assign_preferences(5)) == 1

    def test_all_from_pool(self) -> None:
        """All assigned preferences must come from GUEST_PREFERENCES_POOL."""
        for i in range(50):
            prefs = _assign_preferences(i)
            for pref in prefs:
                assert pref in GUEST_PREFERENCES_POOL

    def test_no_duplicates_within_guest(self) -> None:
        """A single guest should not have duplicate preferences."""
        for i in range(50):
            prefs = _assign_preferences(i)
            assert len(prefs) == len(set(prefs))

    def test_deterministic(self) -> None:
        """Same index always produces same preferences."""
        prefs_a = _assign_preferences(12)
        prefs_b = _assign_preferences(12)
        assert prefs_a == prefs_b


# ---------------------------------------------------------------------------
# _assign_special_occasion tests
# ---------------------------------------------------------------------------


class TestAssignSpecialOccasion:
    """Tests for special occasion assignment."""

    def test_every_10th_gets_occasion(self) -> None:
        """Guests at indices 0, 10, 20, 30, 40 get a special occasion."""
        for i in [0, 10, 20, 30, 40]:
            assert _assign_special_occasion(i) is not None

    def test_others_get_none(self) -> None:
        """Guests not at index % 10 == 0 get None."""
        for i in [1, 2, 5, 9, 11, 15, 19, 25, 33, 49]:
            assert _assign_special_occasion(i) is None

    def test_returns_valid_occasion(self) -> None:
        """Assigned occasions come from SPECIAL_OCCASIONS_POOL."""
        for i in range(0, 50, 10):
            occasion = _assign_special_occasion(i)
            assert occasion in SPECIAL_OCCASIONS_POOL

    def test_approximately_10_percent(self) -> None:
        """Roughly 10% of 50 guests (5 guests) get an occasion."""
        count = sum(1 for i in range(50) if _assign_special_occasion(i) is not None)
        assert count == 5


# ---------------------------------------------------------------------------
# _assign_sensitive_notes tests
# ---------------------------------------------------------------------------


class TestAssignSensitiveNotes:
    """Tests for sensitive notes assignment."""

    def test_every_20th_gets_note(self) -> None:
        """Guests at indices 0, 20, 40 get sensitive notes."""
        for i in [0, 20, 40]:
            assert _assign_sensitive_notes(i) is not None

    def test_others_get_none(self) -> None:
        """Guests not at index % 20 == 0 get None."""
        for i in [1, 5, 10, 15, 19, 21, 30, 39, 49]:
            assert _assign_sensitive_notes(i) is None

    def test_returns_valid_note(self) -> None:
        """Assigned notes come from SENSITIVE_NOTES_POOL."""
        for i in range(0, 50, 20):
            note = _assign_sensitive_notes(i)
            assert note in SENSITIVE_NOTES_POOL

    def test_approximately_5_percent(self) -> None:
        """Roughly 5% of 50 guests (2-3 guests) get a note."""
        count = sum(1 for i in range(50) if _assign_sensitive_notes(i) is not None)
        # Indices 0, 20, 40 qualify -> 3 guests out of 50 = 6%
        assert 2 <= count <= 4


# ---------------------------------------------------------------------------
# _assign_corporate_account tests
# ---------------------------------------------------------------------------


class TestAssignCorporateAccount:
    """Tests for corporate account assignment."""

    def test_returns_valid_account(self) -> None:
        """All assigned accounts come from CORPORATE_ACCOUNTS pool."""
        for i in range(50):
            account = _assign_corporate_account(i)
            assert account in CORPORATE_ACCOUNTS

    def test_deterministic(self) -> None:
        """Same index always produces same account."""
        account_a = _assign_corporate_account(7)
        account_b = _assign_corporate_account(7)
        assert account_a == account_b

    def test_rotates_through_pool(self) -> None:
        """Multiple different accounts are assigned across 50 guests."""
        accounts = {_assign_corporate_account(i) for i in range(50)}
        assert len(accounts) > 1


# ---------------------------------------------------------------------------
# _derive_email tests
# ---------------------------------------------------------------------------


class TestDeriveEmail:
    """Tests for email derivation from name and company."""

    def test_basic_format(self) -> None:
        """Simple name + company produces expected email format."""
        email = _derive_email("David Chen", "Meridian Corp")
        assert email == "david.chen@meridian-corp.com"

    def test_multi_part_name(self) -> None:
        """Multi-part names use dots between all parts."""
        email = _derive_email("Ana Lucia Vargas", "Pacific Ventures")
        assert email == "ana.lucia.vargas@pacific-ventures.com"

    def test_special_characters_stripped(self) -> None:
        """Apostrophes and hyphens in names are stripped."""
        email = _derive_email("Michael O'Brien", "Atlas Technologies")
        assert email == "michael.obrien@atlas-technologies.com"

    def test_ampersand_stripped_from_company(self) -> None:
        """Special characters in company names are stripped."""
        email = _derive_email("John Smith", "Sterling & Associates")
        assert email == "john.smith@sterling--associates.com"

    def test_hyphenated_name(self) -> None:
        """Hyphenated names have the hyphen stripped."""
        email = _derive_email("Laura Chen-Ramirez", "Nexus Global")
        assert email == "laura.chenramirez@nexus-global.com"


# ---------------------------------------------------------------------------
# _generate_phone tests
# ---------------------------------------------------------------------------


class TestGeneratePhone:
    """Tests for deterministic phone number generation."""

    def test_chicago_first_guest(self) -> None:
        """First Chicago guest gets phone starting with area code."""
        phone = _generate_phone("ALOHA-CHI-001", 0)
        assert phone == "+1-312-555-0101"

    def test_miami_tenth_guest(self) -> None:
        """10th Miami guest (index 9) gets 0110 suffix."""
        phone = _generate_phone("ALOHA-MIA-001", 9)
        assert phone == "+1-305-555-0110"

    def test_tokyo_format(self) -> None:
        """Tokyo phone uses international format."""
        phone = _generate_phone("ALOHA-TYO-001", 0)
        assert phone == "+81-3-5555-0101"

    def test_all_properties_have_area_codes(self) -> None:
        """Every property has a defined area code."""
        for profile in PROPERTY_PROFILES:
            assert profile["propertyId"] in PROPERTY_AREA_CODES


# ---------------------------------------------------------------------------
# _compute_last_stay_date tests
# ---------------------------------------------------------------------------


class TestComputeLastStayDate:
    """Tests for deterministic last stay date calculation."""

    def test_index_zero_is_today(self) -> None:
        """Guest index 0 has lastStayDate equal to base_date."""
        base = date(2026, 8, 7)
        result = _compute_last_stay_date(0, base)
        assert result == "2026-08-07"

    def test_index_one_is_yesterday(self) -> None:
        """Guest index 1 has lastStayDate one day before base."""
        base = date(2026, 8, 7)
        result = _compute_last_stay_date(1, base)
        assert result == "2026-08-06"

    def test_wraps_at_30(self) -> None:
        """Index 30 wraps back to base_date (30 % 30 == 0)."""
        base = date(2026, 8, 7)
        result = _compute_last_stay_date(30, base)
        assert result == "2026-08-07"

    def test_within_30_days(self) -> None:
        """All generated dates are within the last 30 days."""
        base = date(2026, 8, 7)
        earliest_allowed = base - timedelta(days=SEED_DAYS - 1)
        for i in range(50):
            result_date = date.fromisoformat(_compute_last_stay_date(i, base))
            assert earliest_allowed <= result_date <= base

    def test_returns_iso_format(self) -> None:
        """Returned string is valid ISO date format."""
        base = date(2026, 8, 7)
        result = _compute_last_stay_date(15, base)
        # Should not raise
        parsed = date.fromisoformat(result)
        assert isinstance(parsed, date)


# ---------------------------------------------------------------------------
# generate_guests tests (integration of all helpers)
# ---------------------------------------------------------------------------


class TestGenerateGuests:
    """Tests for the full guest generation function."""

    def setup_method(self) -> None:
        """Create a mock writer for each test."""
        self.mock_writer = MagicMock()
        self.mock_writer.write_items.return_value = {
            "success": 25,
            "failed": 0,
            "skipped": 0,
        }

    def test_returns_all_properties(self) -> None:
        """Lookup dict contains all 5 properties."""
        result = generate_guests(self.mock_writer)
        expected_ids = {p["propertyId"] for p in PROPERTY_PROFILES}
        assert set(result.keys()) == expected_ids

    def test_50_guests_per_property(self) -> None:
        """Each property has exactly 50 guest profiles."""
        result = generate_guests(self.mock_writer)
        for property_id, guests in result.items():
            assert len(guests) == 50, f"{property_id} has {len(guests)} guests"

    def test_total_250_guests(self) -> None:
        """Total across all properties is 250 guests."""
        result = generate_guests(self.mock_writer)
        total = sum(len(guests) for guests in result.values())
        assert total == 250

    def test_tier_distribution_per_property(self) -> None:
        """Each property has 15 Ambassador, 20 Titanium, 15 Platinum."""
        result = generate_guests(self.mock_writer)
        for property_id, guests in result.items():
            tiers = [g["loyaltyTier"] for g in guests]
            assert tiers.count("AMBASSADOR") == 15, f"{property_id} Ambassador count"
            assert tiers.count("TITANIUM") == 20, f"{property_id} Titanium count"
            assert tiers.count("PLATINUM") == 15, f"{property_id} Platinum count"

    def test_guest_item_has_all_required_fields(self) -> None:
        """Each guest item contains all required DynamoDB attributes."""
        required_fields = {
            "propertyId", "guestId", "loyaltyTierGuestId", "name",
            "loyaltyTier", "totalStays", "preferences", "specialOccasion",
            "sensitiveNotes", "corporateAccount", "email", "phone",
            "lastStayDate",
        }
        result = generate_guests(self.mock_writer)
        first_guest = result["ALOHA-CHI-001"][0]
        assert required_fields.issubset(set(first_guest.keys()))

    def test_unique_guest_ids_per_property(self) -> None:
        """No duplicate guestIds within a single property."""
        result = generate_guests(self.mock_writer)
        for property_id, guests in result.items():
            guest_ids = [g["guestId"] for g in guests]
            assert len(guest_ids) == len(set(guest_ids)), (
                f"Duplicate guest IDs in {property_id}"
            )

    def test_unique_guest_ids_globally(self) -> None:
        """No duplicate guestIds across all properties."""
        result = generate_guests(self.mock_writer)
        all_ids: List[str] = []
        for guests in result.values():
            all_ids.extend(g["guestId"] for g in guests)
        assert len(all_ids) == len(set(all_ids))

    def test_loyalty_tier_guest_id_format(self) -> None:
        """loyaltyTierGuestId follows TIER#guestId pattern."""
        result = generate_guests(self.mock_writer)
        for guests in result.values():
            for guest in guests:
                expected = f"{guest['loyaltyTier']}#{guest['guestId']}"
                assert guest["loyaltyTierGuestId"] == expected

    def test_names_from_vip_pool(self) -> None:
        """Guest names come from the VIP_NAME_POOL for that property."""
        result = generate_guests(self.mock_writer)
        for property_id, guests in result.items():
            pool = VIP_NAME_POOL[property_id]
            for guest in guests:
                assert guest["name"] in pool

    def test_writer_called_for_each_property(self) -> None:
        """Writer.write_items is called once per property."""
        generate_guests(self.mock_writer)
        assert self.mock_writer.write_items.call_count == 5

    def test_writer_receives_50_items_per_call(self) -> None:
        """Each writer call receives exactly 50 guest items."""
        generate_guests(self.mock_writer)
        for call in self.mock_writer.write_items.call_args_list:
            items = call[0][0]
            assert len(items) == 50

    def test_special_occasion_10_percent(self) -> None:
        """Approximately 10% of guests per property have a specialOccasion."""
        result = generate_guests(self.mock_writer)
        for property_id, guests in result.items():
            with_occasion = [g for g in guests if g["specialOccasion"] is not None]
            # 5 out of 50 = 10%
            assert len(with_occasion) == 5, f"{property_id} occasion count"

    def test_sensitive_notes_approximately_5_percent(self) -> None:
        """Approximately 5% of guests per property have sensitiveNotes."""
        result = generate_guests(self.mock_writer)
        for property_id, guests in result.items():
            with_notes = [g for g in guests if g["sensitiveNotes"] is not None]
            # 3 out of 50 = 6% (indices 0, 20, 40)
            assert 2 <= len(with_notes) <= 4, f"{property_id} notes count"

    def test_total_stays_within_tier_range(self) -> None:
        """Each guest's totalStays is within their tier's configured range."""
        result = generate_guests(self.mock_writer)
        for guests in result.values():
            for guest in guests:
                tier = guest["loyaltyTier"]
                min_stays, max_stays = STAY_COUNT_RANGES[tier]
                assert min_stays <= guest["totalStays"] <= max_stays, (
                    f"{guest['guestId']} has {guest['totalStays']} stays "
                    f"(expected {min_stays}-{max_stays})"
                )

    def test_email_contains_at_sign(self) -> None:
        """All generated emails contain an @ sign."""
        result = generate_guests(self.mock_writer)
        for guests in result.values():
            for guest in guests:
                assert "@" in guest["email"]
                assert guest["email"].endswith(".com")

    def test_phone_starts_with_plus(self) -> None:
        """All generated phone numbers start with a + sign."""
        result = generate_guests(self.mock_writer)
        for guests in result.values():
            for guest in guests:
                assert guest["phone"].startswith("+")

    def test_last_stay_date_is_valid_iso(self) -> None:
        """All lastStayDate values are valid ISO date strings."""
        result = generate_guests(self.mock_writer)
        for guests in result.values():
            for guest in guests:
                # Should not raise
                parsed = date.fromisoformat(guest["lastStayDate"])
                assert isinstance(parsed, date)

    def test_deterministic_output(self) -> None:
        """Running generate_guests twice produces identical results."""
        result_a = generate_guests(self.mock_writer)
        result_b = generate_guests(self.mock_writer)
        for property_id in result_a:
            for i in range(50):
                guest_a = result_a[property_id][i]
                guest_b = result_b[property_id][i]
                assert guest_a["guestId"] == guest_b["guestId"]
                assert guest_a["name"] == guest_b["name"]
                assert guest_a["loyaltyTier"] == guest_b["loyaltyTier"]
                assert guest_a["totalStays"] == guest_b["totalStays"]
                assert guest_a["preferences"] == guest_b["preferences"]
