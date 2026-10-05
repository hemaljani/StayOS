"""Property test: predictive forecast alerts are advisory-only, never executed.

Covers the advisory-only invariant of the Alert Writer's record assembly
(``forecast.alerts.build_forecast_alert_item``): a forecast recommendation is a
reviewable proposal derived from a ``RemediationPlan`` and is NEVER marked
executed or applied. The assembled ``FORECAST_OVERSELL`` item must always carry
``applied == False`` (``NOT_APPLIED``) and an ``approval`` gate stuck at
``{"state": "PENDING"}`` (``PENDING_APPROVAL_STATE``), and neither the item nor
its nested ``forecast.recommendation`` may carry any truthy execution/applied
marker. Because assembly is pure (no I/O, no clients), this is exercised
directly with Hypothesis over the generated oversell facts, spanning both plan
authoring paths: the deterministic TEMPLATE plan (``author_remediation_plan``
with ``model_id=None``) and a MODEL-authored plan (via a ``make_json_invoker``
model seam from ``.conftest``). The invariant holds regardless of MODEL vs
TEMPLATE authoring.

Validates: Requirements 5.3.
"""

from __future__ import annotations

import json
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    NOT_APPLIED,
    PENDING_APPROVAL_STATE,
    build_forecast_alert_item,
    derive_dedupe_key,
)
from forecast.narrative import author_remediation_plan

from .conftest import make_json_invoker

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# Keys that would indicate an executed/applied recommendation. A forecast
# recommendation must never carry any of these set truthy (Req 5.3).
EXECUTION_MARKER_KEYS = ("applied", "executed", "isApplied", "isExecuted")

# A well-formed MODEL response so author_remediation_plan takes the MODEL path
# (authored == "MODEL") rather than falling back to the TEMPLATE.
_MODEL_JSON = json.dumps(
    {
        "narrative": "Pre-stage a walk plan for the predicted oversell night.",
        "steps": ["Confirm the shortfall.", "Brief the front desk."],
    }
)


def _has_truthy_marker(value: object) -> bool:
    """Return whether ``value`` is a mapping carrying a truthy execution marker.

    Checks only the top level of the given mapping for any of
    :data:`EXECUTION_MARKER_KEYS` set to a truthy value (e.g. ``True``). Used to
    assert a recommendation is never stamped as executed/applied (Req 5.3).

    Args:
        value: The object to inspect; typically the ``recommendation`` dict.

    Returns:
        ``True`` if ``value`` is a dict with a truthy execution marker key,
        ``False`` otherwise.
    """
    if not isinstance(value, dict):
        return False
    return any(bool(value.get(marker_key)) for marker_key in EXECUTION_MARKER_KEYS)


# ---------------------------------------------------------------------------
# Property 25: Recommendations are never marked executed
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 25: Recommendations are never marked executed
@PROPERTY_SETTINGS
@given(
    property_id=st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=90),
        min_size=1,
        max_size=24,
    ).filter(lambda token: token.strip() != ""),
    rooms_oversold=st.integers(min_value=1, max_value=500),
    confidence=st.integers(min_value=0, max_value=100),
    anticipated_date=st.dates(
        min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)
    ),
    # Optionally attach an overflow candidate so the recommendation's
    # overflowOption branch is exercised too.
    overflow_rooms=st.one_of(st.none(), st.integers(min_value=1, max_value=50)),
    based_on_partial_data=st.booleans(),
    # Author via the TEMPLATE path (model_id=None) or the MODEL path (injected
    # invoker); both must produce an advisory-only item.
    use_model=st.booleans(),
)
def test_property_25_recommendations_never_marked_executed(
    property_id: str,
    rooms_oversold: int,
    confidence: int,
    anticipated_date: date,
    overflow_rooms: int | None,
    based_on_partial_data: bool,
    use_model: bool,
) -> None:
    """Assembled forecast alerts are advisory-only for every generated case.

    Asserts, for any generated oversell fact set and either authoring path:
    - ``item["applied"]`` is exactly ``False`` (``NOT_APPLIED``), never ``True``;
    - ``item["approval"]["state"]`` is ``"PENDING"`` (``PENDING_APPROVAL_STATE``),
      never ``APPROVED`` / ``EXECUTED``;
    - the top-level item carries no truthy execution/applied marker beyond the
      required ``applied == False``;
    - ``item["forecast"]["recommendation"]`` carries no truthy applied/executed
      marker.
    This holds regardless of MODEL vs TEMPLATE authoring.
    """
    iso_date = anticipated_date.isoformat()
    overflow_sister_property_id = "SISTER-001" if overflow_rooms is not None else None

    # Author the plan via the requested path. TEMPLATE: model_id=None (no model
    # configured). MODEL: inject a well-formed invoker so authored == "MODEL".
    if use_model:
        plan = author_remediation_plan(
            rooms_oversold=rooms_oversold,
            anticipated_date=iso_date,
            confidence=confidence,
            overflow_sister_property_id=overflow_sister_property_id,
            overflow_available_rooms=overflow_rooms,
            model_id="anthropic.claude-sonnet-test",
            invoke_model=make_json_invoker(_MODEL_JSON),
        )
    else:
        plan = author_remediation_plan(
            rooms_oversold=rooms_oversold,
            anticipated_date=iso_date,
            confidence=confidence,
            overflow_sister_property_id=overflow_sister_property_id,
            overflow_available_rooms=overflow_rooms,
            model_id=None,
        )

    dedupe_key = derive_dedupe_key(property_id, "OVERSELL", iso_date)

    item = build_forecast_alert_item(
        property_id=property_id,
        plan=plan,
        rooms_oversold=rooms_oversold,
        anticipated_date=iso_date,
        confidence=confidence,
        dedupe_key=dedupe_key,
        based_on_partial_data=based_on_partial_data,
        threshold=50,
    )

    # applied is exactly False (never True) for every generated case (Req 5.3).
    assert item["applied"] is NOT_APPLIED
    assert item["applied"] is False

    # The approval gate is stuck at PENDING (never APPROVED/EXECUTED) (Req 5.3).
    assert item["approval"]["state"] == PENDING_APPROVAL_STATE
    assert item["approval"]["state"] == "PENDING"

    # The top-level item carries no OTHER truthy execution marker: applied is
    # present-and-False, and no executed/isApplied/isExecuted marker is truthy.
    for marker_key in EXECUTION_MARKER_KEYS:
        assert not bool(item.get(marker_key))

    # The nested recommendation is a reviewable proposal: it must not carry any
    # truthy applied/executed marker (Req 5.3).
    recommendation = item["forecast"]["recommendation"]
    assert not _has_truthy_marker(recommendation)
