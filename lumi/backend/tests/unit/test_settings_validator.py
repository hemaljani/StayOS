"""Unit tests for LUMI settings input validator.

Tests each validation rule with valid and invalid inputs, combined
validation with multiple errors, and edge cases.
"""

from typing import Any, Dict, List

import pytest

from validators.settings_validator import validate_settings


# ---------------------------------------------------------------------------
# briefDeliveryTime validation
# ---------------------------------------------------------------------------


class TestBriefDeliveryTimeValidation:
    """Tests for briefDeliveryTime field validation."""

    def test_valid_times(self) -> None:
        """Valid HH:MM times should produce no errors."""
        valid_times = ["00:00", "06:30", "12:00", "23:59", "09:05"]
        for time_val in valid_times:
            errors = validate_settings({"briefDeliveryTime": time_val})
            assert errors == [], f"Expected no errors for {time_val}, got {errors}"

    def test_invalid_format_single_digit_hour(self) -> None:
        """Single-digit hour without leading zero should fail."""
        errors = validate_settings({"briefDeliveryTime": "6:30"})
        assert len(errors) == 1
        assert errors[0]["field"] == "briefDeliveryTime"

    def test_invalid_format_24_hour(self) -> None:
        """Hour value 24 should fail (max is 23)."""
        errors = validate_settings({"briefDeliveryTime": "24:00"})
        assert len(errors) == 1
        assert errors[0]["field"] == "briefDeliveryTime"

    def test_invalid_format_60_minutes(self) -> None:
        """Minute value 60 should fail (max is 59)."""
        errors = validate_settings({"briefDeliveryTime": "12:60"})
        assert len(errors) == 1
        assert errors[0]["field"] == "briefDeliveryTime"

    def test_invalid_type_integer(self) -> None:
        """Non-string type should fail."""
        errors = validate_settings({"briefDeliveryTime": 630})
        assert len(errors) == 1
        assert errors[0]["field"] == "briefDeliveryTime"

    def test_invalid_format_missing_colon(self) -> None:
        """Missing colon separator should fail."""
        errors = validate_settings({"briefDeliveryTime": "0630"})
        assert len(errors) == 1
        assert errors[0]["field"] == "briefDeliveryTime"

    def test_invalid_format_extra_chars(self) -> None:
        """Extra characters in time string should fail."""
        errors = validate_settings({"briefDeliveryTime": "06:30:00"})
        assert len(errors) == 1
        assert errors[0]["field"] == "briefDeliveryTime"


# ---------------------------------------------------------------------------
# alertToggles validation
# ---------------------------------------------------------------------------


class TestAlertTogglesValidation:
    """Tests for alertToggles field validation."""

    def test_valid_all_true(self) -> None:
        """All valid keys set to True should pass."""
        body = {
            "alertToggles": {
                "overbookingRisk": True,
                "roomsOutOfOrder": True,
                "vipArrivalAlert": True,
                "upsellOpportunity": True,
                "staffingConfirmed": True,
            }
        }
        errors = validate_settings(body)
        assert errors == []

    def test_valid_mixed_booleans(self) -> None:
        """Mix of True and False values should pass."""
        body = {
            "alertToggles": {
                "overbookingRisk": True,
                "roomsOutOfOrder": False,
                "vipArrivalAlert": True,
                "upsellOpportunity": False,
                "staffingConfirmed": True,
            }
        }
        errors = validate_settings(body)
        assert errors == []

    def test_invalid_non_boolean_value(self) -> None:
        """Non-boolean value should produce an error."""
        body = {"alertToggles": {"overbookingRisk": "yes"}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "boolean" in errors[0]["message"]

    def test_invalid_unknown_key(self) -> None:
        """Unknown toggle key should produce an error."""
        body = {"alertToggles": {"unknownToggle": True}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "Unknown" in errors[0]["message"]
        assert "unknownToggle" in errors[0]["message"]

    def test_invalid_not_a_dict(self) -> None:
        """Non-dict value should produce an error."""
        body = {"alertToggles": "not-a-dict"}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "object" in errors[0]["message"]


# ---------------------------------------------------------------------------
# kpiThresholds validation
# ---------------------------------------------------------------------------


class TestKpiThresholdsValidation:
    """Tests for kpiThresholds field validation."""

    def test_valid_thresholds(self) -> None:
        """Valid threshold values should pass."""
        body = {"kpiThresholds": {"occupancyAlertBelow": 70, "adrAlertBelow": 200}}
        errors = validate_settings(body)
        assert errors == []

    def test_valid_boundary_values(self) -> None:
        """Boundary values (0, 100, 1000) should pass."""
        body = {"kpiThresholds": {"occupancyAlertBelow": 0, "adrAlertBelow": 0}}
        errors = validate_settings(body)
        assert errors == []

        body = {"kpiThresholds": {"occupancyAlertBelow": 100, "adrAlertBelow": 1000}}
        errors = validate_settings(body)
        assert errors == []

    def test_invalid_occupancy_over_100(self) -> None:
        """Occupancy above 100 should fail."""
        body = {"kpiThresholds": {"occupancyAlertBelow": 101}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "occupancyAlertBelow" in errors[0]["field"]

    def test_invalid_occupancy_negative(self) -> None:
        """Negative occupancy should fail."""
        body = {"kpiThresholds": {"occupancyAlertBelow": -1}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "occupancyAlertBelow" in errors[0]["field"]

    def test_invalid_adr_over_1000(self) -> None:
        """ADR above 1000 should fail."""
        body = {"kpiThresholds": {"adrAlertBelow": 1001}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "adrAlertBelow" in errors[0]["field"]

    def test_invalid_adr_negative(self) -> None:
        """Negative ADR should fail."""
        body = {"kpiThresholds": {"adrAlertBelow": -50}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "adrAlertBelow" in errors[0]["field"]

    def test_invalid_float_value(self) -> None:
        """Float value should fail (must be integer)."""
        body = {"kpiThresholds": {"occupancyAlertBelow": 70.5}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "integer" in errors[0]["message"]

    def test_integral_float_is_accepted(self) -> None:
        """A whole-number float (70.0) is a valid integer threshold."""
        body = {"kpiThresholds": {"occupancyAlertBelow": 70.0, "adrAlertBelow": 200.0}}
        errors = validate_settings(body)
        assert errors == []

    def test_decimal_whole_value_is_accepted(self) -> None:
        """A Decimal whole number is accepted (documented Decimal support)."""
        from decimal import Decimal

        body = {"kpiThresholds": {"occupancyAlertBelow": Decimal("70")}}
        errors = validate_settings(body)
        assert errors == []

    def test_decimal_fractional_value_is_rejected(self) -> None:
        """A fractional Decimal (70.5) is rejected as non-integral."""
        from decimal import Decimal

        body = {"kpiThresholds": {"occupancyAlertBelow": Decimal("70.5")}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "occupancyAlertBelow" in errors[0]["field"]

    def test_integral_numeric_string_is_accepted(self) -> None:
        """An integral numeric string ("70") is accepted."""
        body = {"kpiThresholds": {"occupancyAlertBelow": "70", "adrAlertBelow": "200"}}
        errors = validate_settings(body)
        assert errors == []

    def test_fractional_numeric_string_is_rejected(self) -> None:
        """A fractional numeric string ("70.5") is rejected as non-integral."""
        body = {"kpiThresholds": {"adrAlertBelow": "70.5"}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "adrAlertBelow" in errors[0]["field"]

    def test_non_numeric_string_is_rejected(self) -> None:
        """A non-numeric string is rejected as not an integer threshold."""
        body = {"kpiThresholds": {"occupancyAlertBelow": "not-a-number"}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "occupancyAlertBelow" in errors[0]["field"]

    def test_boolean_value_is_rejected(self) -> None:
        """A boolean is rejected even though bool is an int subtype.

        int(True) == 1 would otherwise slip through as a valid threshold; the
        validator rejects bools explicitly (review finding F-2 hardening).
        """
        body = {"kpiThresholds": {"occupancyAlertBelow": True}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "occupancyAlertBelow" in errors[0]["field"]

    def test_none_value_is_rejected(self) -> None:
        """A None threshold is rejected rather than raising out of the validator."""
        body = {"kpiThresholds": {"adrAlertBelow": None}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "adrAlertBelow" in errors[0]["field"]

    def test_adr_fractional_float_is_rejected(self) -> None:
        """A fractional ADR float (199.99) is rejected as non-integral."""
        body = {"kpiThresholds": {"adrAlertBelow": 199.99}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "adrAlertBelow" in errors[0]["field"]

    def test_invalid_not_a_dict(self) -> None:
        """Non-dict value should produce an error."""
        body = {"kpiThresholds": "invalid"}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "object" in errors[0]["message"]


# ---------------------------------------------------------------------------
# audioPreferences validation
# ---------------------------------------------------------------------------


class TestAudioPreferencesValidation:
    """Tests for audioPreferences field validation."""

    def test_valid_preferences(self) -> None:
        """Valid language and briefLength should pass."""
        body = {"audioPreferences": {"language": "en-US", "briefLength": "standard"}}
        errors = validate_settings(body)
        assert errors == []

    def test_valid_all_languages(self) -> None:
        """All supported languages should pass."""
        for lang in ["en-US", "es-ES", "ja-JP", "zh-CN"]:
            body = {"audioPreferences": {"language": lang}}
            errors = validate_settings(body)
            assert errors == [], f"Expected no errors for language {lang}"

    def test_valid_all_brief_lengths(self) -> None:
        """All supported brief lengths should pass."""
        for length in ["brief", "standard", "detailed"]:
            body = {"audioPreferences": {"briefLength": length}}
            errors = validate_settings(body)
            assert errors == [], f"Expected no errors for briefLength {length}"

    def test_invalid_language(self) -> None:
        """Unsupported language should fail."""
        body = {"audioPreferences": {"language": "fr-FR"}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "language" in errors[0]["field"]

    def test_invalid_brief_length(self) -> None:
        """Unsupported brief length should fail."""
        body = {"audioPreferences": {"briefLength": "verbose"}}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "briefLength" in errors[0]["field"]

    def test_invalid_not_a_dict(self) -> None:
        """Non-dict value should produce an error."""
        body = {"audioPreferences": 42}
        errors = validate_settings(body)
        assert len(errors) == 1
        assert "object" in errors[0]["message"]


# ---------------------------------------------------------------------------
# Combined / edge case validation
# ---------------------------------------------------------------------------


class TestCombinedValidation:
    """Tests for combined validation with multiple errors."""

    def test_empty_body_is_valid(self) -> None:
        """Empty body (no fields to validate) should pass."""
        errors = validate_settings({})
        assert errors == []

    def test_multiple_errors_across_fields(self) -> None:
        """Multiple invalid fields should produce multiple errors."""
        body = {
            "briefDeliveryTime": "25:00",
            "kpiThresholds": {"occupancyAlertBelow": 200},
            "audioPreferences": {"language": "invalid"},
        }
        errors = validate_settings(body)
        assert len(errors) == 3

    def test_partial_valid_body(self) -> None:
        """Body with only some valid fields should pass for those fields."""
        body = {"briefDeliveryTime": "07:00", "audioPreferences": {"language": "ja-JP"}}
        errors = validate_settings(body)
        assert errors == []

    def test_unknown_top_level_keys_ignored(self) -> None:
        """Unknown top-level keys in body are ignored (not validated)."""
        body = {"somethingRandom": "value", "briefDeliveryTime": "06:30"}
        errors = validate_settings(body)
        assert errors == []
