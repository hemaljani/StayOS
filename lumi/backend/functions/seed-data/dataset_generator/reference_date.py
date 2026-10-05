"""Reference date resolver for window-relative dataset generation.

The Unified Data Orchestrator re-anchors the deterministic LUMI dataset to a
single ``reference_date`` (the "today" the generators build their 30-day window
around). Advancing this date rolls the window forward. This module centralises
how that date is resolved so every generator entry point derives its whole
window from one explicit value instead of an implicit ``date.today()`` /
``datetime.now()`` call.

Supports Requirement 2.1 (generators accept an explicit Reference_Date) and the
"add a reference_date resolver helper" sub-task of the data-Orchestrator spec.
"""

from datetime import date, datetime, timezone
from typing import Optional, Union

# ISO date format used across the orchestrator input contract (YYYY-MM-DD).
ISO_DATE_FORMAT: str = "%Y-%m-%d"


def resolve_reference_date(
    reference_date: Optional[Union[str, date]] = None,
) -> date:
    """Resolve the reference date the generators anchor their window to.

    Accepts an explicit date (as an ISO ``YYYY-MM-DD`` string or a ``date``
    object) and returns a concrete ``date``. When nothing is supplied, defaults
    to the current UTC date so a given caller always gets a deterministic,
    timezone-stable "today".

    Args:
        reference_date: Optional anchor date. May be an ISO ``YYYY-MM-DD``
            string, a ``datetime.date`` (a ``datetime`` is narrowed to its
            date), or ``None`` to default to UTC today.

    Returns:
        The resolved ``datetime.date`` to anchor generation to.

    Raises:
        ValueError: If a string is supplied that is not a valid ISO
            ``YYYY-MM-DD`` date.
    """
    if reference_date is None:
        # Default to UTC today for a timezone-stable, deterministic anchor.
        return datetime.now(timezone.utc).date()

    if isinstance(reference_date, datetime):
        # Narrow a datetime to its calendar date; the window is day-granular.
        return reference_date.date()

    if isinstance(reference_date, date):
        return reference_date

    # At this point reference_date is a string; parse strictly as ISO YYYY-MM-DD.
    try:
        return datetime.strptime(reference_date, ISO_DATE_FORMAT).date()
    except (ValueError, TypeError) as error:
        raise ValueError(
            f"reference_date must be an ISO YYYY-MM-DD string or a date, "
            f"got {reference_date!r}"
        ) from error
