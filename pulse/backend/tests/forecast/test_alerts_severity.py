"""Property test for the pure Alert Writer severity tiering (Req 4.2).

Covers the severity-tier boundary of the pure Alert Writer
(``forecast.alerts.severity_for_confidence``): within the writable band, a
prediction's severity tier is a pure function of confidence versus a single
``WARNING_CONFIDENCE_FLOOR`` (85). A confidence at or above the floor tiers to
``SEVERITY_WARNING``; a confidence below it tiers to ``SEVERITY_INFO``. Because
the mapping is pure (no I/O, no clients), this is exercised directly with
Hypothesis over a confidence and a threshold each spanning ``[0, 100]``, with
the property focused on the 85 boundary (inclusive at 85, exclusive at 84).

Validates: Requirements 4.2.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.alerts import (
    SEVERITY_INFO,
    SEVERITY_WARNING,
    WARNING_CONFIDENCE_FLOOR,
    severity_for_confidence,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)


# ---------------------------------------------------------------------------
# Property 15: Severity tier boundary at 85
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 15: Severity tier boundary at 85
@PROPERTY_SETTINGS
@given(
    # Span the full confidence range so both sides of the 85 floor are covered.
    confidence=st.integers(min_value=0, max_value=100),
    # Threshold varies across the same range; severity is a pure function of
    # confidence vs the WARNING floor and must not depend on the threshold.
    threshold=st.integers(min_value=0, max_value=100),
)
def test_property_15_severity_tier_boundary_at_85(
    confidence: int, threshold: int
) -> None:
    """Severity tiers at the 85 boundary: >= 85 -> WARNING, < 85 -> INFO.

    Asserts, for any confidence in ``[0, 100]`` and any threshold in
    ``[0, 100]``:
    - the boundary constant is exactly 85;
    - ``confidence >= 85`` maps to ``SEVERITY_WARNING`` (inclusive at 85);
    - ``confidence < 85`` maps to ``SEVERITY_INFO``;
    - the result is always one of the two known tier tokens.
    """
    # The single boundary lives in one place and is the spec's value.
    assert WARNING_CONFIDENCE_FLOOR == 85

    tier = severity_for_confidence(confidence, threshold)

    # Result is always exactly one of the two tier tokens.
    assert tier in (SEVERITY_INFO, SEVERITY_WARNING)

    # The 85 boundary: inclusive above/at 85 is WARNING, strictly below is INFO.
    if confidence >= WARNING_CONFIDENCE_FLOOR:
        assert tier == SEVERITY_WARNING
    else:
        assert tier == SEVERITY_INFO


# Feature: predictive-forecasting-agent, Property 15: Severity tier boundary at 85
def test_property_15_exact_boundary_examples() -> None:
    """Pin the exact 85/84 boundary: 85 -> WARNING, 84 -> INFO.

    A focused example alongside the property to nail the inclusive boundary at
    the WARNING floor and its exclusive neighbor just below.
    """
    # Threshold is irrelevant to tiering; use a low value so both examples sit
    # within the writable band.
    threshold = 0

    # confidence == 85 -> WARNING (inclusive boundary).
    assert severity_for_confidence(85, threshold) == SEVERITY_WARNING
    # confidence == 84 -> INFO (just below the boundary).
    assert severity_for_confidence(84, threshold) == SEVERITY_INFO
