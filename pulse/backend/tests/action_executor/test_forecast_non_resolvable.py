"""Property test: FORECAST_OVERSELL is non-resolvable by the Action Executor.

The predictive forecasting agent emits ``FORECAST_OVERSELL`` alerts as
advisory-only heads-up: they are intentionally excluded from the executor's
``RESOLVABLE_TYPES`` and, like the existing INFO advisory types, are never
executed as a write-back. Submitting one to the write-back dispatcher raises a
:class:`WriteBackError` tagged ``detail="non-resolvable-type"`` and produces no
write-back, so the originating alert is left unchanged (Requirement 5.2).

This lives in the executor test package (mirroring the existing executor tests'
imports and construction style) rather than the forecast test package, since it
exercises the existing executor and does not need the forecast conftest.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.action_executor.executor import (
    RESOLVABLE_TYPES,
    build_operational_writeback,
)
from pulse.common.errors import WriteBackError
from pulse.common.models import AlertType

# The approved-option shape mirrored from the existing executor tests; the
# dispatcher rejects the type before the option is ever consulted.
_APPROVED_OPTION = {"label": "A", "rank": 1, "title": "Advisory heads-up"}

# Bounded, DynamoDB-key-safe identifier alphabet (matches the existing tests).
_IDENT = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=90),
    min_size=1,
    max_size=20,
)


def test_forecast_oversell_not_in_resolvable_types() -> None:
    """Structural guard: FORECAST_OVERSELL is not a resolvable write-back type.

    Validates: Requirement 5.2
    """
    assert AlertType.FORECAST_OVERSELL not in RESOLVABLE_TYPES


# Feature: predictive-forecasting-agent, Property 24: A predictive alert is rejected as non-resolvable by the executor and left unchanged
@settings(max_examples=100)
@given(
    property_id=_IDENT,
    alert_id=_IDENT,
    entity_key=_IDENT,
    user_id=_IDENT,
)
def test_property_24_forecast_alert_rejected_as_non_resolvable(
    property_id: str,
    alert_id: str,
    entity_key: str,
    user_id: str,
) -> None:
    """A FORECAST_OVERSELL alert is rejected non-resolvable; alert unchanged.

    Mirrors the existing INFO/non-resolvable-type behavior: the write-back
    dispatcher raises :class:`WriteBackError` with ``detail`` equal to
    ``"non-resolvable-type"`` and produces no write-back, so the originating
    alert item is preserved unchanged.

    Validates: Requirement 5.2
    """
    # A FORECAST_OVERSELL alert item constructed like an existing executor test.
    alert_item = {
        "alertId": alert_id,
        "propertyId": property_id,
        "type": AlertType.FORECAST_OVERSELL.value,
        "title": "Forecast oversell heads-up",
        "detail": "predicted confirmed may exceed available",
        "status": "UNACKNOWLEDGED",
        "sourceEntityRef": {
            "table": "stayos-reservations",
            "propertyId": property_id,
            "entityKey": entity_key,
            "ruleType": AlertType.FORECAST_OVERSELL.value,
        },
    }
    # Snapshot before dispatch so we can assert the alert is left unchanged.
    original = dict(alert_item)

    with pytest.raises(WriteBackError) as exc_info:
        build_operational_writeback(alert_item, _APPROVED_OPTION, user_id)

    # Rejected with the exact non-resolvable detail the existing types use.
    assert exc_info.value.detail == "non-resolvable-type"
    # No write-back produced and the alert item is preserved unchanged.
    assert alert_item == original
