"""Property tests for triage-brief parsing and validation.

Covers Property 18: a validated triage brief is well-formed and rank-ordered -
summary 1-500 chars, integer confidence 0-100, 2-5 options ordered highest to
lowest rank with unique labels and ranks, at most one recommended.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.common.errors import TriageFailure
from pulse.triage.validation import parse_and_validate_brief

PROPERTY_SETTINGS = settings(max_examples=100)


def _well_formed_brief(num_options: int, recommended_index: int | None) -> dict:
    """Build a well-formed raw brief with ``num_options`` options."""
    options = []
    for i in range(num_options):
        options.append(
            {
                "label": chr(ord("A") + i),
                "rank": i + 1,
                "title": f"Option {i}",
                "detail": f"Detail for option {i}",
                "recommended": recommended_index == i,
            }
        )
    return {
        "summary": "A valid situation summary.",
        "confidence": 80,
        "options": options,
    }


# Feature: initial-pulse-project, Property 18: Triage briefs are well-formed and
# rank-ordered
@PROPERTY_SETTINGS
@given(
    summary=st.text(min_size=1, max_size=500),
    confidence=st.integers(min_value=0, max_value=100),
    num_options=st.integers(min_value=2, max_value=5),
    recommended_index=st.one_of(st.none(), st.integers(min_value=0, max_value=4)),
)
def test_property_18_well_formed_briefs_accepted(
    summary: str, confidence: int, num_options: int, recommended_index: int | None
) -> None:
    """Any brief meeting the invariants parses and preserves them.

    Validates: Requirements 10.1, 10.2
    """
    rec = recommended_index if (recommended_index or 0) < num_options else None
    raw = _well_formed_brief(num_options, rec)
    raw["summary"] = summary
    raw["confidence"] = confidence

    brief = parse_and_validate_brief(raw)

    assert 1 <= len(brief.summary) <= 500
    assert 0 <= brief.confidence <= 100
    assert 2 <= len(brief.options) <= 5
    ranks = [o.rank for o in brief.options]
    labels = [o.label for o in brief.options]
    assert ranks == sorted(ranks)  # highest (rank 1) to lowest
    assert len(set(ranks)) == len(ranks)  # unique ranks
    assert len(set(labels)) == len(labels)  # unique labels
    assert sum(1 for o in brief.options if o.recommended) <= 1


# Feature: initial-pulse-project, Property 18: Triage briefs are well-formed and
# rank-ordered
@PROPERTY_SETTINGS
@given(
    bad=st.sampled_from(
        [
            "empty_summary",
            "non_string_summary",
            "confidence_high",
            "confidence_negative",
            "confidence_bool",
            "confidence_float",
            "too_few_options",
            "too_many_options",
            "duplicate_labels",
            "duplicate_ranks",
            "unordered_ranks",
            "two_recommended",
        ]
    )
)
def test_property_18_malformed_briefs_rejected(bad: str) -> None:
    """Any brief violating an invariant is rejected as a TriageFailure.

    Validates: Requirements 10.1, 10.2
    """
    raw = _well_formed_brief(3, 0)
    if bad == "empty_summary":
        raw["summary"] = ""
    elif bad == "non_string_summary":
        raw["summary"] = 123
    elif bad == "confidence_high":
        raw["confidence"] = 101
    elif bad == "confidence_negative":
        raw["confidence"] = -1
    elif bad == "confidence_bool":
        raw["confidence"] = True
    elif bad == "confidence_float":
        raw["confidence"] = 80.5
    elif bad == "too_few_options":
        raw["options"] = raw["options"][:1]
    elif bad == "too_many_options":
        raw = _well_formed_brief(5, 0)
        raw["options"].append(
            {"label": "F", "rank": 6, "title": "x", "detail": "y", "recommended": False}
        )
    elif bad == "duplicate_labels":
        raw["options"][1]["label"] = raw["options"][0]["label"]
    elif bad == "duplicate_ranks":
        raw["options"][1]["rank"] = raw["options"][0]["rank"]
    elif bad == "unordered_ranks":
        raw["options"][0]["rank"], raw["options"][2]["rank"] = (
            raw["options"][2]["rank"],
            raw["options"][0]["rank"],
        )
    elif bad == "two_recommended":
        raw["options"][0]["recommended"] = True
        raw["options"][1]["recommended"] = True

    try:
        parse_and_validate_brief(raw)
    except TriageFailure:
        return
    raise AssertionError(f"Expected TriageFailure for malformed case {bad!r}")


# Feature: initial-pulse-project, Property 18: Triage briefs are well-formed and
# rank-ordered (BUG-021: an over-long but otherwise-valid summary is coerced to
# the max length rather than rejected, so a good brief still attaches).
@PROPERTY_SETTINGS
@given(overshoot=st.integers(min_value=1, max_value=600))
def test_property_18_over_long_summary_is_truncated_not_rejected(
    overshoot: int,
) -> None:
    """A summary exceeding 500 chars is truncated to <=500, not rejected.

    Validates: Requirement 10.1 (BUG-021 defensive truncation)
    """
    raw = _well_formed_brief(3, 0)
    raw["summary"] = "x" * (500 + overshoot)

    brief = parse_and_validate_brief(raw)

    # The brief still parses and honors the 1-500 char contract.
    assert 1 <= len(brief.summary) <= 500
    # An ellipsis marks the truncation.
    assert brief.summary.endswith("\u2026")

