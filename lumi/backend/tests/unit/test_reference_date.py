"""Unit tests for the dataset_generator.reference_date resolver.

Covers the reference_date resolver helper added for the data-Orchestrator spec
(Task 1, Requirement 2.1): it accepts an explicit ISO ``YYYY-MM-DD`` string or a
``date``/``datetime``, defaults to UTC today when nothing is supplied, and
rejects malformed input. Centralising this resolution is what lets every
generator entry point derive its whole window from one explicit anchor instead
of an implicit ``date.today()``/``datetime.now()`` call.
"""

from datetime import date, datetime, timezone

import pytest

from dataset_generator.reference_date import ISO_DATE_FORMAT, resolve_reference_date


class TestResolveReferenceDate:
    """Tests for resolve_reference_date input handling and defaults."""

    def test_iso_string_is_parsed(self) -> None:
        """An ISO YYYY-MM-DD string resolves to the matching date."""
        assert resolve_reference_date("2026-08-15") == date(2026, 8, 15)

    def test_date_is_returned_unchanged(self) -> None:
        """A date instance is returned as-is."""
        anchor = date(2025, 1, 2)
        assert resolve_reference_date(anchor) == anchor

    def test_datetime_is_narrowed_to_date(self) -> None:
        """A datetime is narrowed to its calendar date (window is day-granular)."""
        moment = datetime(2027, 3, 4, 23, 59, 59, tzinfo=timezone.utc)
        assert resolve_reference_date(moment) == date(2027, 3, 4)

    def test_none_defaults_to_utc_today(self) -> None:
        """Omitting the argument defaults to the current UTC date."""
        expected_today = datetime.now(timezone.utc).date()
        assert resolve_reference_date(None) == expected_today

    def test_no_argument_defaults_to_utc_today(self) -> None:
        """Calling with no argument behaves the same as passing None."""
        expected_today = datetime.now(timezone.utc).date()
        assert resolve_reference_date() == expected_today

    def test_iso_format_constant_matches_parsing(self) -> None:
        """The exported ISO format constant round-trips a known date string."""
        parsed = datetime.strptime("2026-12-31", ISO_DATE_FORMAT).date()
        assert resolve_reference_date("2026-12-31") == parsed

    @pytest.mark.parametrize(
        "bad_value",
        ["not-a-date", "2026/08/15", "08-15-2026", "2026-13-01", ""],
    )
    def test_invalid_string_raises_value_error(self, bad_value: str) -> None:
        """A non-ISO or impossible date string raises ValueError."""
        with pytest.raises(ValueError):
            resolve_reference_date(bad_value)
