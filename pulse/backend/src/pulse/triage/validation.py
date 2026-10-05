"""Pure parsing and validation of a Triage_Brief from raw model output.

The Triage Agent asks Amazon Bedrock to return a triage brief as strict JSON.
This module turns that raw, untrusted structure into a validated
:class:`~pulse.common.models.TriageBrief`, enforcing every well-formedness
invariant from Requirements 10.1 and 10.2 (Property 18):

    * ``summary`` is a string of 1-500 characters.
    * ``confidence`` is an integer percentage in 0-100 (booleans and non-integer
      numbers are rejected).
    * ``options`` contains 2-5 ranked options ordered from highest to lowest
      rank (rank 1 first), every option has a unique label and a unique rank,
      and at most one option is marked recommended.

This logic is intentionally I/O-free (no Bedrock, no DynamoDB) so it is
unit-testable and reusable at 100+ property iterations. Any violation raises
:class:`~pulse.common.errors.TriageFailure`; the Rule Engine treats that as a
signal to deliver the alert without a brief (Requirements 1.7, 10.6).

Raw JSON keys are camelCase (the model contract mirrors the ``triageBrief``
DynamoDB shape in design Data Models); Python attributes are snake_case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from pulse.common.errors import TriageFailure
from pulse.common.models import RankedOption, ReviewRisk, TriageBrief

# Summary length bounds (Requirement 10.1).
SUMMARY_MIN_LEN = 1
SUMMARY_MAX_LEN = 500

# Confidence percentage bounds (Requirement 10.1).
CONFIDENCE_MIN = 0
CONFIDENCE_MAX = 100

# Ranked-option count bounds (Requirement 10.1).
OPTIONS_MIN = 2
OPTIONS_MAX = 5


def _require_int_in_range(value: Any, low: int, high: int, field_name: str) -> int:
    """Return an integer confirmed to be within an inclusive range.

    Booleans are rejected (``bool`` is a subclass of ``int`` in Python) and only
    genuine integers are accepted, so a confidence of ``true`` or ``91.5`` fails.

    Args:
        value: The raw value to validate.
        low: The inclusive lower bound.
        high: The inclusive upper bound.
        field_name: The field name, for the error message.

    Returns:
        The validated integer.

    Raises:
        TriageFailure: If the value is not an integer within ``[low, high]``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TriageFailure(
            f"{field_name} must be an integer, got {value!r}",
            reason="invalid_schema",
        )
    if not (low <= value <= high):
        raise TriageFailure(
            f"{field_name} must be within {low}-{high}, got {value}",
            reason="invalid_schema",
        )
    return value


def _parse_review_risk(value: Any) -> Optional[ReviewRisk]:
    """Parse an optional review-risk level into a :class:`ReviewRisk`.

    Args:
        value: The raw review-risk value (``"Low"``/``"Medium"``/``"High"``) or
            ``None``.

    Returns:
        The parsed :class:`ReviewRisk`, or ``None`` when absent.

    Raises:
        TriageFailure: If the value is present but not a valid level.
    """
    if value is None:
        return None
    try:
        return ReviewRisk(value)
    except ValueError as exc:
        raise TriageFailure(
            f"reviewRisk must be one of Low/Medium/High, got {value!r}",
            reason="invalid_schema",
        ) from exc


def _parse_cost(value: Any) -> Optional[float]:
    """Parse an optional numeric estimated cost.

    Args:
        value: The raw cost value (number) or ``None``.

    Returns:
        The cost as a float, or ``None`` when absent.

    Raises:
        TriageFailure: If the value is present but not a non-boolean number.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TriageFailure(
            f"estimatedCost must be numeric, got {value!r}", reason="invalid_schema"
        )
    return float(value)


def _parse_option(raw: Any) -> RankedOption:
    """Parse a single raw ranked-option object.

    Args:
        raw: The raw option mapping.

    Returns:
        The parsed :class:`RankedOption`.

    Raises:
        TriageFailure: If required fields are missing or mistyped.
    """
    if not isinstance(raw, Mapping):
        raise TriageFailure("Each option must be an object", reason="invalid_schema")

    label = raw.get("label")
    if not isinstance(label, str) or not label:
        raise TriageFailure(
            f"Option label must be a non-empty string, got {label!r}",
            reason="invalid_schema",
        )
    rank = _require_int_in_range(raw.get("rank"), 1, OPTIONS_MAX, "Option rank")

    title = raw.get("title")
    detail = raw.get("detail")
    if not isinstance(title, str) or not title:
        raise TriageFailure(
            "Option title must be a non-empty string", reason="invalid_schema"
        )
    if not isinstance(detail, str) or not detail:
        raise TriageFailure(
            "Option detail must be a non-empty string", reason="invalid_schema"
        )

    recommended = raw.get("recommended", False)
    if not isinstance(recommended, bool):
        raise TriageFailure(
            "Option recommended must be a boolean", reason="invalid_schema"
        )

    return RankedOption(
        label=label,
        rank=rank,
        title=title,
        detail=detail,
        recommended=recommended,
        estimated_cost=_parse_cost(raw.get("estimatedCost")),
        review_risk=_parse_review_risk(raw.get("reviewRisk")),
    )


def _validate_option_set(options: Sequence[RankedOption]) -> None:
    """Validate the structural invariants across a set of ranked options.

    Args:
        options: The parsed ranked options.

    Raises:
        TriageFailure: If the count is out of range, labels or ranks are not
            unique, the options are not ordered highest-to-lowest rank, or more
            than one option is recommended.
    """
    if not (OPTIONS_MIN <= len(options) <= OPTIONS_MAX):
        raise TriageFailure(
            f"A triage brief must have {OPTIONS_MIN}-{OPTIONS_MAX} options, "
            f"got {len(options)}",
            reason="invalid_schema",
        )

    labels = [option.label for option in options]
    if len(set(labels)) != len(labels):
        raise TriageFailure("Option labels must be unique", reason="invalid_schema")

    ranks = [option.rank for option in options]
    if len(set(ranks)) != len(ranks):
        raise TriageFailure("Option ranks must be unique", reason="invalid_schema")

    # Ordered from highest to lowest rank: rank 1 first, strictly ascending.
    if ranks != sorted(ranks):
        raise TriageFailure(
            "Options must be ordered from highest to lowest rank",
            reason="invalid_schema",
        )

    recommended_count = sum(1 for option in options if option.recommended)
    if recommended_count > 1:
        raise TriageFailure(
            f"At most one option may be recommended, got {recommended_count}",
            reason="invalid_schema",
        )


def _coerce_summary(summary: Any) -> str:
    """Validate a raw ``summary`` and coerce an over-long one to the max length.

    The model must return a non-empty string summary. A genuinely malformed
    summary (missing, non-string, or empty) is rejected as ``invalid_schema``.
    However, the model occasionally returns an otherwise-valid summary that
    slightly exceeds ``SUMMARY_MAX_LEN`` (BUG-021): rather than throw away an
    otherwise-good brief for length alone, such a summary is defensively
    truncated to ``SUMMARY_MAX_LEN`` (with a trailing ellipsis) so the result
    still honors the 1-500 char contract (Requirement 10.1) and the brief can
    attach. The prompts instruct the model to stay well under the limit; this is
    the safety net for the rare overshoot.

    Args:
        summary: The raw ``summary`` value from the model output.

    Returns:
        A valid summary string of length ``SUMMARY_MIN_LEN``-``SUMMARY_MAX_LEN``.

    Raises:
        TriageFailure: If the summary is missing, not a string, or empty.
    """
    if not isinstance(summary, str) or len(summary) < SUMMARY_MIN_LEN:
        raise TriageFailure(
            f"summary must be a string of {SUMMARY_MIN_LEN}-{SUMMARY_MAX_LEN} "
            "characters",
            reason="invalid_schema",
        )
    if len(summary) <= SUMMARY_MAX_LEN:
        return summary
    # Over-long but valid text: truncate to the max, reserving one char for the
    # ellipsis so the returned string is never longer than SUMMARY_MAX_LEN.
    return summary[: SUMMARY_MAX_LEN - 1].rstrip() + "\u2026"


def parse_summary_and_confidence(raw: Any) -> tuple[str, int]:
    """Validate and extract the ``summary`` and ``confidence`` of a raw brief.

    Shared by :func:`parse_and_validate_brief` and by the type-specific triage
    assembly (Walk Risk, OOO Cluster) that constructs its own options while still
    requiring a well-formed summary (1-500 chars) and confidence (integer
    0-100). See Requirement 10.1.

    Args:
        raw: The raw brief structure.

    Returns:
        A ``(summary, confidence)`` tuple.

    Raises:
        TriageFailure: If the brief is not an object, the summary is missing /
            non-string / empty, or the confidence is missing or out of range. An
            over-long summary is defensively truncated (see
            :func:`_coerce_summary`), not rejected.
    """
    if not isinstance(raw, Mapping):
        raise TriageFailure(
            "Triage brief must be a JSON object", reason="invalid_schema"
        )

    summary = _coerce_summary(raw.get("summary"))

    confidence = _require_int_in_range(
        raw.get("confidence"), CONFIDENCE_MIN, CONFIDENCE_MAX, "confidence"
    )
    return summary, confidence


def parse_and_validate_brief(raw: Any) -> TriageBrief:
    """Parse and validate a raw triage-brief structure into a ``TriageBrief``.

    Enforces the Requirement 10.1/10.2 well-formedness invariants (Property 18).
    The optional ``executeLabel`` is carried through when present; the heavier
    ``walkStrategy`` is attached by the Walk Risk specialization, not here.

    Args:
        raw: The raw brief structure (typically ``json.loads`` of the model
            output).

    Returns:
        A validated :class:`TriageBrief`.

    Raises:
        TriageFailure: If any well-formedness invariant is violated.
    """
    summary, confidence = parse_summary_and_confidence(raw)

    raw_options = raw.get("options")
    if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
        raise TriageFailure("options must be a list", reason="invalid_schema")
    options = [_parse_option(item) for item in raw_options]
    _validate_option_set(options)

    execute_label = raw.get("executeLabel")
    if execute_label is not None and not isinstance(execute_label, str):
        raise TriageFailure("executeLabel must be a string", reason="invalid_schema")

    return TriageBrief(
        summary=summary,
        confidence=confidence,
        options=options,
        execute_label=execute_label,
    )


__all__ = [
    "SUMMARY_MIN_LEN",
    "SUMMARY_MAX_LEN",
    "CONFIDENCE_MIN",
    "CONFIDENCE_MAX",
    "OPTIONS_MIN",
    "OPTIONS_MAX",
    "parse_summary_and_confidence",
    "parse_and_validate_brief",
]
