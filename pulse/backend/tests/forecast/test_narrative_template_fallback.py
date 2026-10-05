"""Example tests for the deterministic template fallback of the Narrative_Model.

Covers ``forecast.narrative.author_remediation_plan``'s fallback path (Req 3.10):
when no Narrative_Model is configured, or when the injected model raises, the
authoring function MUST still return a usable :class:`RemediationPlan` marked
``authored == "TEMPLATE"`` that carries the IDENTICAL deterministic numbers it
was given (``rooms_oversold``, ``anticipated_date``, ``confidence``, and the
overflow option, Req 3.9) with a non-empty prose narrative and a non-empty tuple
of walk-plan steps. A plan is never blocked on the model.

These are plain example/unit tests (not property tests) using concrete fixtures,
mirroring the design Testing Strategy. The failing-model seam comes from the
shared ``make_failing_invoker`` helper in ``conftest``.

Validates: Requirement 3.10.
"""

from __future__ import annotations

from .conftest import make_failing_invoker

from forecast.narrative import RemediationPlan, author_remediation_plan

# Concrete, deterministic oversell facts reused across the example tests. These
# stand in for the pure engine's OversellPrediction numbers the model must
# reproduce and never recompute (Req 3.9).
ROOMS_OVERSOLD = 4
ANTICIPATED_DATE = "2025-07-14"
CONFIDENCE = 82
OVERFLOW_SISTER_PROPERTY_ID = "ALOHA-MIA-001"
OVERFLOW_AVAILABLE_ROOMS = 6


def _assert_numbers_preserved(plan: RemediationPlan) -> None:
    """Assert the plan carries the fixed input numbers unchanged (Req 3.9).

    Checks the deterministic numeric surface only; callers assert the authoring
    marker and prose/steps separately.

    Args:
        plan: The remediation plan returned by ``author_remediation_plan``.
    """
    assert plan.rooms_oversold == ROOMS_OVERSOLD
    assert plan.anticipated_date == ANTICIPATED_DATE
    assert plan.confidence == CONFIDENCE


def test_no_model_configured_returns_template_plan() -> None:
    """No model configured (model_id=None, invoke_model=None) -> TEMPLATE plan.

    Asserts the deterministic fallback fires when nothing is configured: the
    authoring marker is ``"TEMPLATE"``, every input number is preserved, and the
    prose/steps are populated so an alert is never blocked on the model.
    """
    plan = author_remediation_plan(
        rooms_oversold=ROOMS_OVERSOLD,
        anticipated_date=ANTICIPATED_DATE,
        confidence=CONFIDENCE,
        overflow_sister_property_id=OVERFLOW_SISTER_PROPERTY_ID,
        overflow_available_rooms=OVERFLOW_AVAILABLE_ROOMS,
        model_id=None,
        invoke_model=None,
    )

    assert plan.authored == "TEMPLATE"
    _assert_numbers_preserved(plan)
    # The overflow option is deterministic and preserved through the template.
    assert plan.overflow_sister_property_id == OVERFLOW_SISTER_PROPERTY_ID
    assert plan.overflow_available_rooms == OVERFLOW_AVAILABLE_ROOMS

    # Non-empty prose and a non-empty tuple of step strings.
    assert isinstance(plan.narrative, str) and plan.narrative.strip()
    assert isinstance(plan.steps, tuple) and len(plan.steps) > 0
    assert all(isinstance(step, str) and step.strip() for step in plan.steps)


def test_model_raises_falls_back_to_template_plan() -> None:
    """A raising model (with a model_id set) -> deterministic TEMPLATE plan.

    Injects ``make_failing_invoker`` alongside a configured ``model_id`` so the
    model seam is exercised and raises; the authoring function must swallow the
    error and fall back to the template carrying the same numbers (Req 3.10).
    """
    plan = author_remediation_plan(
        rooms_oversold=ROOMS_OVERSOLD,
        anticipated_date=ANTICIPATED_DATE,
        confidence=CONFIDENCE,
        overflow_sister_property_id=OVERFLOW_SISTER_PROPERTY_ID,
        overflow_available_rooms=OVERFLOW_AVAILABLE_ROOMS,
        model_id="anthropic.claude-sonnet-test",
        invoke_model=make_failing_invoker(),
    )

    assert plan.authored == "TEMPLATE"
    _assert_numbers_preserved(plan)
    assert plan.overflow_sister_property_id == OVERFLOW_SISTER_PROPERTY_ID
    assert plan.overflow_available_rooms == OVERFLOW_AVAILABLE_ROOMS
    assert isinstance(plan.narrative, str) and plan.narrative.strip()
    assert isinstance(plan.steps, tuple) and len(plan.steps) > 0


def test_template_includes_overflow_step_when_candidate_present() -> None:
    """The template surfaces an overflow walk step when a candidate is provided.

    With an overflow sister id and available-room count set, the deterministic
    steps must reference that candidate so the GM can pre-stage the walk target.
    """
    plan = author_remediation_plan(
        rooms_oversold=ROOMS_OVERSOLD,
        anticipated_date=ANTICIPATED_DATE,
        confidence=CONFIDENCE,
        overflow_sister_property_id=OVERFLOW_SISTER_PROPERTY_ID,
        overflow_available_rooms=OVERFLOW_AVAILABLE_ROOMS,
        model_id=None,
        invoke_model=None,
    )

    assert plan.authored == "TEMPLATE"
    # The overflow candidate id appears in at least one deterministic step.
    steps_text = " ".join(plan.steps)
    assert OVERFLOW_SISTER_PROPERTY_ID in steps_text
    assert str(OVERFLOW_AVAILABLE_ROOMS) in steps_text


def test_template_works_without_overflow_candidate() -> None:
    """The template is valid when no overflow candidate is provided (both None).

    Without a sister candidate, the overflow fields stay ``None`` and no overflow
    step is emitted, but the plan is still a usable TEMPLATE with prose and steps.
    """
    plan = author_remediation_plan(
        rooms_oversold=ROOMS_OVERSOLD,
        anticipated_date=ANTICIPATED_DATE,
        confidence=CONFIDENCE,
        overflow_sister_property_id=None,
        overflow_available_rooms=None,
        model_id=None,
        invoke_model=None,
    )

    assert plan.authored == "TEMPLATE"
    _assert_numbers_preserved(plan)
    assert plan.overflow_sister_property_id is None
    assert plan.overflow_available_rooms is None
    assert isinstance(plan.narrative, str) and plan.narrative.strip()
    assert isinstance(plan.steps, tuple) and len(plan.steps) > 0
