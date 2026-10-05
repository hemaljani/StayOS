"""Property test for the Remediation_Plan authoring determinism boundary.

Covers ``forecast.narrative.author_remediation_plan``: the pure Forecast Engine
owns every NUMBER (``rooms_oversold``, ``anticipated_date``, ``confidence``, and
the overflow option), and the Narrative_Model authors ONLY the prose and steps
around those fixed numbers. The determinism boundary is absolute -- whether the
model is present and well-behaved, absent, or failing, the returned plan's
numeric fields ALWAYS equal the INPUTS, never the model's text (Req 3.9). The
``authored`` marker records ``"MODEL"`` on success and ``"TEMPLATE"`` on the
deterministic fallback (Req 3.10), and the plan is a reviewable proposal that is
never marked executed or applied (Req 5.3).

Three model conditions are injected via the conftest seams and the boundary is
asserted for all of them under Hypothesis.

Validates: Requirements 3.4, 3.7, 3.8, 3.9, 3.10, 5.3.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.narrative import RemediationPlan, author_remediation_plan

from .conftest import make_failing_invoker, make_json_invoker

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# A well-behaved model whose authored text carries BOGUS numbers on purpose:
# 9999 rooms, 1999-01-01, 3% confidence. The determinism boundary means none of
# these ever leak into the returned plan's numeric fields.
_BOGUS_MODEL_JSON = (
    '{"narrative": "MODEL SAYS 9999 rooms on 1999-01-01 at 3% confidence", '
    '"steps": ["s1", "s2"]}'
)

# Base date the ISO ``anticipated_date`` is generated from (base + offset days).
_BASE_DATE = date(2025, 1, 1)


# ---------------------------------------------------------------------------
# Property 29: Model authoring never alters the deterministic numbers
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 29: Model authoring never alters the deterministic numbers
@PROPERTY_SETTINGS
@given(
    rooms_oversold=st.integers(min_value=1, max_value=500),
    confidence=st.integers(min_value=0, max_value=100),
    date_offset=st.integers(min_value=0, max_value=3650),
    # Overflow option: either a full (sister id + available rooms) pair, or both
    # None -- matching the engine's "present only when retrieved" contract.
    overflow=st.one_of(
        st.none(),
        st.tuples(
            st.text(
                alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789",
                min_size=3,
                max_size=16,
            ),
            st.integers(min_value=0, max_value=200),
        ),
    ),
)
def test_property_29_model_authoring_never_alters_numbers(
    rooms_oversold: int,
    confidence: int,
    date_offset: int,
    overflow: Optional[tuple[str, int]],
) -> None:
    """The authored plan's numbers always equal the inputs, for every model state.

    Injects three model conditions -- a present well-behaved model returning
    BOGUS numbers, an absent model, and a failing model -- and asserts for each
    that the returned plan reproduces the INPUT numbers exactly (Req 3.9), that
    ``authored`` reflects MODEL vs TEMPLATE (Req 3.10), and that the plan carries
    no executed/applied flag (Req 5.3, a proposal only).
    """
    # Derive the deterministic inputs from the generated values.
    anticipated_date = (_BASE_DATE + timedelta(days=date_offset)).isoformat()
    overflow_sister_property_id = overflow[0] if overflow is not None else None
    overflow_available_rooms = overflow[1] if overflow is not None else None

    # Condition 1: PRESENT well-behaved model. Its text names 9999/1999/3% but
    # the plan must carry the INPUT numbers; content comes from the model.
    plan_model = author_remediation_plan(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        overflow_sister_property_id=overflow_sister_property_id,
        overflow_available_rooms=overflow_available_rooms,
        model_id="anthropic.claude-sonnet-test",
        invoke_model=make_json_invoker(_BOGUS_MODEL_JSON),
    )

    # Condition 2: ABSENT model -> deterministic template fallback.
    plan_absent = author_remediation_plan(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        overflow_sister_property_id=overflow_sister_property_id,
        overflow_available_rooms=overflow_available_rooms,
        model_id=None,
        invoke_model=None,
    )

    # Condition 3: FAILING model -> deterministic template fallback.
    plan_failing = author_remediation_plan(
        rooms_oversold=rooms_oversold,
        anticipated_date=anticipated_date,
        confidence=confidence,
        overflow_sister_property_id=overflow_sister_property_id,
        overflow_available_rooms=overflow_available_rooms,
        model_id="anthropic.claude-sonnet-test",
        invoke_model=make_failing_invoker(),
    )

    # The determinism boundary holds identically for all three conditions.
    for plan in (plan_model, plan_absent, plan_failing):
        assert isinstance(plan, RemediationPlan)
        # Numeric fields equal the INPUTS, never the model text (Req 3.9).
        assert plan.rooms_oversold == rooms_oversold
        assert plan.anticipated_date == anticipated_date
        assert plan.confidence == confidence
        assert plan.overflow_sister_property_id == overflow_sister_property_id
        assert plan.overflow_available_rooms == overflow_available_rooms
        # (Req 5.3) The plan is a proposal only: it carries no field/flag marking
        # it executed or applied. RemediationPlan defines no such field, so any
        # such attribute must be absent (and never truthy if somehow present).
        assert not getattr(plan, "applied", False)
        assert not getattr(plan, "executed", False)
        assert not hasattr(plan, "applied")
        assert not hasattr(plan, "executed")

    # authored: MODEL for the well-behaved model, TEMPLATE for the fallbacks.
    assert plan_model.authored == "MODEL"
    assert plan_absent.authored == "TEMPLATE"
    assert plan_failing.authored == "TEMPLATE"

    # Content on the MODEL-authored plan comes from the model, not the template.
    assert plan_model.narrative == (
        "MODEL SAYS 9999 rooms on 1999-01-01 at 3% confidence"
    )
    assert plan_model.steps == ("s1", "s2")
