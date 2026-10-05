"""Property test for the Forecasting Agent invocation gate (Req 1.4, 1.5).

Covers ``forecast.invocation.validate_invocation``: the ``propertyId`` gate the
thin agent entrypoint calls before any operational read. For any enabled id the
gate returns that id (whitespace-stripped); for a missing/empty/blank/non-string
id it raises :class:`InvalidInvocationError` with reason
``REASON_MISSING_PROPERTY_ID``; for a present but not-enabled id it raises with
reason ``REASON_UNKNOWN_PROPERTY_ID``. Because the gate is meant to reject
BEFORE any read or write, this test encodes that structurally: a recording tool
caller (and a recording alerts writer) that would register a call if ever
touched are asserted to have zero calls after every reject, and no exception
other than ``InvalidInvocationError`` is allowed to escape a reject path.

Since ``validate_invocation`` takes only ``session_input`` + ``enabled_ids`` and
accepts no I/O collaborator, the "before any read/write" property holds by
construction: the gate has no seam to touch. The recording fakes make that
observable by proving they stay untouched across the reject.

Validates: Requirements 1.4, 1.5.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

# conftest.py puts the forecast-agent service dir on sys.path.
from forecast.invocation import (  # noqa: E402
    PROPERTY_ID_KEY,
    REASON_MISSING_PROPERTY_ID,
    REASON_UNKNOWN_PROPERTY_ID,
    InvalidInvocationError,
    validate_invocation,
)

from .conftest import RecordingToolCaller

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# A pool of plausible enabled ids to sample the enabled set from.
_ID_POOL = [
    "ALOHA-CHI-001",
    "ALOHA-MIA-001",
    "ALOHA-TYO-001",
    "ALOHA-MAD-001",
    "ALOHA-BOM-001",
    "ALOHA-NYC-001",
    "ALOHA-LON-001",
]

# Non-string values that must all be rejected as missing-property-id.
_NON_STRING_VALUES = [None, 0, 1, 42, 3.14, True, False, [], {}, ("x",)]


def _fresh_seams() -> tuple[RecordingToolCaller, list[tuple[str, dict[str, Any]]]]:
    """Return an untouched recording tool caller and a recording alerts writer.

    Both seams would register a call the instant they were used. The gate takes
    neither of them, so after any reject they must remain empty - which is how
    this test observes "no read / no write before rejection".
    """
    tool_caller = RecordingToolCaller(results={})
    alerts_writes: list[tuple[str, dict[str, Any]]] = []
    return tool_caller, alerts_writes


def _assert_no_io(
    tool_caller: RecordingToolCaller,
    alerts_writes: list[tuple[str, dict[str, Any]]],
) -> None:
    """Assert neither the tool caller (read) nor alerts writer (write) was used."""
    assert tool_caller.calls == []
    assert alerts_writes == []


# ---------------------------------------------------------------------------
# Property 2: Invalid invocation is rejected before any read or write
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 2: Invalid invocation is rejected before any read or write
@PROPERTY_SETTINGS
@given(
    enabled_ids=st.frozensets(st.sampled_from(_ID_POOL), min_size=1, max_size=len(_ID_POOL)),
    padding=st.sampled_from(["", " ", "  ", "\t", "\n", " \t "]),
    extra_input=st.dictionaries(
        st.text(min_size=1, max_size=8).filter(lambda k: k != PROPERTY_ID_KEY),
        st.integers(),
        max_size=3,
    ),
)
def test_property_2_invalid_invocation_rejected_before_read_or_write(
    enabled_ids: frozenset[str],
    padding: str,
    extra_input: dict[str, int],
) -> None:
    """Accept enabled ids (stripped); reject missing/unknown before any I/O.

    For a sampled enabled id, ``validate_invocation`` returns the stripped id.
    For missing key / None / empty / whitespace-only / non-string values it
    raises with reason ``REASON_MISSING_PROPERTY_ID``; for a present, non-empty
    id that is not enabled it raises with reason ``REASON_UNKNOWN_PROPERTY_ID``.
    On every reject the recording tool caller and alerts writer stay untouched
    and no exception other than ``InvalidInvocationError`` escapes.
    """
    enabled_id = sorted(enabled_ids)[0]

    # --- Accept: an enabled id (with surrounding whitespace) is returned stripped.
    accept_input: dict[str, Any] = dict(extra_input)
    accept_input[PROPERTY_ID_KEY] = f"{padding}{enabled_id}{padding}"
    tool_caller, alerts_writes = _fresh_seams()
    result = validate_invocation(accept_input, enabled_ids)
    assert result == enabled_id
    # A successful accept performs no I/O either - the gate only validates.
    _assert_no_io(tool_caller, alerts_writes)

    # --- Reject (missing-property-id): key entirely absent.
    missing_key_input = dict(extra_input)
    _assert_reject(
        missing_key_input, enabled_ids, REASON_MISSING_PROPERTY_ID, expected_received=None
    )

    # --- Reject (missing-property-id): None value.
    none_input = dict(extra_input)
    none_input[PROPERTY_ID_KEY] = None
    _assert_reject(
        none_input, enabled_ids, REASON_MISSING_PROPERTY_ID, expected_received=None
    )

    # --- Reject (missing-property-id): empty / whitespace-only strings.
    for blank in ("", padding if padding.strip() == "" else "   "):
        blank_input = dict(extra_input)
        blank_input[PROPERTY_ID_KEY] = blank
        _assert_reject(
            blank_input, enabled_ids, REASON_MISSING_PROPERTY_ID, expected_received=blank
        )

    # --- Reject (missing-property-id): non-string values.
    for non_string in _NON_STRING_VALUES:
        non_string_input = dict(extra_input)
        non_string_input[PROPERTY_ID_KEY] = non_string
        _assert_reject(
            non_string_input,
            enabled_ids,
            REASON_MISSING_PROPERTY_ID,
            expected_received=non_string,
        )

    # --- Reject (unknown-property-id): present, non-empty id NOT in enabled_ids.
    unknown_id = "NOT-ENABLED-999"
    assert unknown_id not in enabled_ids
    unknown_input = dict(extra_input)
    unknown_input[PROPERTY_ID_KEY] = f"{padding}{unknown_id}{padding}"
    _assert_reject(
        unknown_input,
        enabled_ids,
        REASON_UNKNOWN_PROPERTY_ID,
        expected_received=unknown_id,
    )


def _assert_reject(
    session_input: dict[str, Any],
    enabled_ids: frozenset[str],
    expected_reason: str,
    *,
    expected_received: Any,
) -> None:
    """Assert the call rejects with the given reason and touches no I/O seam.

    Wraps the gate call between fresh recording seams so that, after the reject,
    the tool caller (a read seam) and alerts writer (a write seam) are proven to
    have zero calls - encoding "rejected before any read or write". Also asserts
    only ``InvalidInvocationError`` escapes and that ``.received_value`` /
    ``.reason`` are captured on the error.
    """
    tool_caller, alerts_writes = _fresh_seams()
    raised: InvalidInvocationError | None = None
    try:
        validate_invocation(session_input, enabled_ids)
    except InvalidInvocationError as error:
        raised = error
    # No exception other than InvalidInvocationError may escape (any other type
    # would have propagated out of the try and failed the test).
    assert raised is not None, "expected InvalidInvocationError, but none was raised"
    assert raised.reason == expected_reason
    assert raised.received_value == expected_received
    # Before any read/write: the recording seams were never touched.
    _assert_no_io(tool_caller, alerts_writes)
