"""Signal Reader for the PULSE Forecasting Agent (Req 2.1 - 2.5).

This module reads the forward-looking operational signals a per-property
forecasting run needs, by invoking the shared Gateway's READ-ONLY tools through
an injected ``ToolCaller`` seam. It never opens an MCP/Strands client itself:
the thin agent entrypoint (``app.py``) owns the Gateway wiring and passes a
``call_tool`` callable in, so the reader stays pure enough to unit/property test
on any host without an AgentCore Runtime session (PYQUALITY-05).

Scope of this module today (task 5.1 - core structure + read-only calls):

* Define the :data:`ToolCaller` seam type and the frozen :class:`SignalSet`.
* :func:`read_signals` ALWAYS reads occupancy and revenue, and reads sister
  availability ONLY when overflow evaluation is needed (the two-phase read,
  Req 2.1 - 2.3). Every tool call carries ``propertyId`` (Req 2.4), and the
  reader only ever invokes the three READ-ONLY tools in
  :data:`READ_ONLY_TOOLS` - never a write tool (Req 2.5).

Resilience behavior implemented here (task 5.2):

* Each tool call is bounded by a per-call 10s timeout
  (:data:`PER_CALL_TIMEOUT_SECONDS`) and retried up to
  :data:`MAX_ATTEMPTS` (3) total attempts with exponential backoff of 1s then
  2s between attempts (derived from :data:`BACKOFF_BASE_SECONDS`). A timeout is
  treated as a FAILED attempt and triggers retry/backoff (Req 8.5).
* On all-attempts failure for a single tool, the reader records a structured
  error (failed tool + ``propertyId``), adds the tool name to
  ``missing_signals``, and CONTINUES without terminating the run (Req 2.7, 8.1).
  The corresponding :class:`SignalSet` field is left ``None``.
* Partial-data flagging (Req 2.8): a missing required tool
  (``get_occupancy`` / ``get_revenue``) surfaces via ``missing_signals`` so a
  downstream consumer can derive ``based_on_partial_data``.

The timeout runner, backoff sleep, and monotonic clock are all injectable seams
(defaulting to real behavior) so tests can drive attempt counts and the backoff
sequence deterministically WITHOUT wall-clock delays.

Property-isolation filtering implemented here (task 5.3, Req 6.1, 6.3 - 6.6):

* :func:`filter_to_property` is a PURE, deterministic helper that, given the raw
  rows of a tool result and the bound run ``propertyId``, drops any record whose
  ``propertyId`` does not match the bound id, retains the matching records, and
  returns the kept records alongside a tuple of :class:`PropertyDiscrepancy`
  entries (one per distinct foreign ``propertyId`` plus a synthetic
  "no matching data" entry when zero records match) (Req 6.3, 6.4).
* :func:`project_sister_availability` is a PURE projection that keeps ONLY the
  availability values keyed by each sister ``propertyId`` and discards every
  other sister operational field - sister availability is the only cross-property
  data admitted (Req 6.5, 6.6).
* :func:`read_signals` wires the filter over the occupancy and revenue results so
  ``excluded_foreign_records`` is the total count of dropped foreign records and
  the recorded :class:`PropertyDiscrepancy` entries are surfaced on
  ``SignalSet.discrepancies`` (and logged structurally, PYQUALITY-03).

This module does NOT resolve the Gateway's bare -> ``tools___`` namespaced tool
names; that lives in the Triage Agent ``GatewayToolClient``. The reader calls
the seam with the bare tool names defined below and lets the injected caller
handle resolution.

Design reference: design.md -> Components -> ``signals.SignalReader``
(property-isolation filter) and Cross-cutting -> Property isolation;
app.py Gateway tool-name resolution note.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 6.1, 6.3, 6.4, 6.5, 6.6.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Optional

from forecast.engine import ForecastHorizon

# Module-level named logger (PYQUALITY-03). A library-style named logger lets the
# host application (the agent entrypoint / AgentCore Runtime) own handler and
# level configuration while this module emits structured records.
logger = logging.getLogger(__name__)

# The seam the entrypoint injects: given a bare tool name and an arguments
# mapping, invoke the corresponding Gateway MCP tool and return its raw result.
# Keeping this a plain callable (not a Strands/MCP client) is what keeps the
# reader host-testable - no import from strands/mcp (design: ToolCaller seam).
ToolCaller = Callable[[str, dict[str, Any]], Any]

# A thunk that performs one bounded tool invocation and returns its result. The
# TimeoutRunner seam receives one of these plus a timeout budget and is
# responsible for either returning the result or raising SignalToolTimeout.
ToolThunk = Callable[[], Any]

# The timeout-runner seam: run ``thunk`` and return its result, but raise
# :class:`SignalToolTimeout` if it does not complete within ``timeout_seconds``.
# Defaults to :func:`_run_with_timeout` (a real, thread-based timeout). Tests
# inject a deterministic runner so no real clock is involved.
TimeoutRunner = Callable[[ToolThunk, float], Any]

# The backoff-sleep seam: sleep for the given number of seconds between attempts.
# Defaults to ``time.sleep``; tests inject a no-op recorder to assert the backoff
# sequence without wall-clock delay.
SleepFn = Callable[[float], None]

# The monotonic-clock seam: return a monotonically increasing timestamp in
# seconds. Defaults to ``time.monotonic``; injectable so tests can drive timing
# deterministically. Kept available for callers/tests that need a clock seam.
MonotonicClock = Callable[[], float]

# The session-input key naming the single property this run is bound to. Every
# tool call must carry it so the Gateway scopes reads to that property (Req 2.4).
PROPERTY_ID_ARG = "propertyId"

# The camelCase key that identifies which property a raw tool record belongs to.
# The property-isolation filter (Req 6.3) matches each record's value at this key
# against the bound run id. camelCase matches the platform's on-the-wire record
# shape (NAMING: DynamoDB attributes / API payloads are camelCase).
RECORD_PROPERTY_ID_KEY = "propertyId"

# Candidate keys under which a raw tool result may nest its list of record rows
# when the result is a dict envelope rather than a bare list. The filter probes
# these in order and falls back to treating the value as a single record. Kept
# defensive because the reader must be robust to the raw tool shapes (task 5.3).
RECORD_LIST_KEYS: tuple[str, ...] = ("records", "items", "data", "results")

# The camelCase key under which a sister-availability record carries its bookable
# availability value. The sister projection (Req 6.5, 6.6) keeps ONLY this value
# per sister propertyId and discards every other operational field. Probed in
# order so the projection tolerates minor shape variation across the tool result.
SISTER_AVAILABILITY_VALUE_KEYS: tuple[str, ...] = (
    "availableRooms",
    "availability",
    "roomsAvailable",
)

# The reason string stamped on a "no matching data" discrepancy so a downstream
# consumer can distinguish a zero-match response (Req 6.4) from a foreign-record
# exclusion (Req 6.3) without inspecting counts.
NO_MATCHING_DATA_REASON = "no matching data"

# Per-call timeout budget in seconds (Req 8.5). A tool call exceeding this is
# treated as a failed attempt and triggers retry/backoff.
PER_CALL_TIMEOUT_SECONDS = 10

# Maximum total attempts per tool call, including the first (Req 2.6). After
# MAX_ATTEMPTS failures the tool is degraded gracefully (Req 2.7).
MAX_ATTEMPTS = 3

# Base for exponential backoff between attempts, in seconds (Req 2.6). With
# MAX_ATTEMPTS=3 this derives the sequence 1s (after attempt 1) then 2s (after
# attempt 2): BACKOFF_BASE_SECONDS * 2**(attempt_index) for attempt_index 0, 1.
BACKOFF_BASE_SECONDS = 1

# The precomputed backoff sequence between the (MAX_ATTEMPTS - 1) retry gaps:
# 1s then 2s. Exposed so tests can assert the exact sequence deterministically.
BACKOFF_SEQUENCE_SECONDS: tuple[float, ...] = tuple(
    float(BACKOFF_BASE_SECONDS * (2 ** attempt_index))
    for attempt_index in range(MAX_ATTEMPTS - 1)
)

# Bare Gateway tool names the reader invokes. The injected ToolCaller resolves
# these to their namespaced (``tools___``) forms; the reader never does.
TOOL_GET_OCCUPANCY = "get_occupancy"
TOOL_GET_REVENUE = "get_revenue"
TOOL_GET_SISTER_PROPERTY_AVAILABILITY = "get_sister_property_availability"

# The complete set of tools this reader is permitted to invoke. All three are
# READ-ONLY operational reads; the reader must NEVER call a write tool (Req 2.5).
# Kept as a frozenset so it is an immutable allowlist the reader guards against.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        TOOL_GET_OCCUPANCY,
        TOOL_GET_REVENUE,
        TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
    }
)


class NonReadOnlyToolError(Exception):
    """Raised if a caller attempts to invoke a tool outside :data:`READ_ONLY_TOOLS`.

    A domain-specific guard (PYQUALITY-02) enforcing the read-only posture of the
    Signal Reader (Req 2.5): the reader must only ever call the three read tools
    and never a write tool. Carries the offending tool name (a non-secret
    identifier) so the entrypoint can log the attempted violation.

    Attributes:
        tool_name: The bare tool name that was rejected for not being read-only.
    """

    def __init__(self, tool_name: str) -> None:
        """Initialize the error with the rejected tool name.

        Args:
            tool_name: The bare tool name that was not in :data:`READ_ONLY_TOOLS`.
        """
        # Keep the structured field on the instance for the entrypoint to log,
        # and build a human-readable message for the base Exception.
        self.tool_name = tool_name
        super().__init__(
            f"Refusing to call non-read-only tool {tool_name!r}; "
            f"the Signal Reader may only call {sorted(READ_ONLY_TOOLS)}"
        )


class SignalToolTimeout(Exception):
    """Raised when a single tool invocation exceeds the per-call timeout budget.

    A domain-specific timeout error (PYQUALITY-02) so the retry loop can treat a
    timeout as a distinct FAILED attempt (Req 8.5) rather than conflating it with
    an arbitrary seam error. Carries the failed tool name and the budget that was
    exceeded (both non-secret) for structured logging.

    Attributes:
        tool_name: The bare tool name whose invocation timed out.
        timeout_seconds: The per-call timeout budget that was exceeded.
    """

    def __init__(self, tool_name: str, timeout_seconds: float) -> None:
        """Initialize the timeout error with the tool name and budget.

        Args:
            tool_name: The bare tool name whose invocation timed out.
            timeout_seconds: The per-call timeout budget, in seconds, exceeded.
        """
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Tool {tool_name!r} did not complete within "
            f"{timeout_seconds}s (per-call timeout)"
        )


def _run_with_timeout(thunk: ToolThunk, timeout_seconds: float) -> Any:
    """Run ``thunk`` on a worker thread, enforcing a wall-clock timeout budget.

    The default :data:`TimeoutRunner` seam. It runs the thunk on a short-lived
    worker so a blocking tool call cannot hang the run past ``timeout_seconds``.
    Tests inject their own runner to avoid real threads and real waiting.

    Args:
        thunk: The zero-argument callable performing one tool invocation.
        timeout_seconds: The maximum time, in seconds, to allow the thunk to run.

    Returns:
        The value returned by ``thunk`` when it completes within the budget.

    Raises:
        TimeoutError: If ``thunk`` does not complete within ``timeout_seconds``.
            The retry loop maps this to :class:`SignalToolTimeout`.
    """
    # A single-worker executor bounds this one call; the ``with`` block ensures
    # the executor is always torn down (PYQUALITY-02 resource cleanup).
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(thunk)
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            # Normalize the futures timeout to a builtin TimeoutError so the
            # retry loop has a single timeout type to translate. We do not wait
            # for the abandoned worker; the interpreter reclaims it.
            raise TimeoutError(
                f"tool call exceeded {timeout_seconds}s"
            ) from exc


@dataclass(frozen=True)
class PropertyDiscrepancy:
    """A record of the property-isolation filter dropping non-matching data.

    Emitted by :func:`filter_to_property` when a raw tool result contains records
    that do not belong to the bound run ``propertyId``. Two situations produce a
    discrepancy (Req 6.3, 6.4):

    * Foreign records: one entry per distinct foreign ``propertyId`` seen, whose
      ``foreign_property_id`` is that non-matching id, ``excluded_count`` is how
      many of its records were dropped, and ``reason`` is ``None``.
    * No matching data: a single entry with ``foreign_property_id`` ``None`` and
      ``reason`` :data:`NO_MATCHING_DATA_REASON` when a non-empty response yields
      zero records matching the bound id (Req 6.4).

    The frozen dataclass is deterministic and comparable so property tests (task
    5.10) can assert on the recorded discrepancies. Its :meth:`to_payload` renders
    the camelCase ``{runPropertyId, foreignPropertyId, excludedCount}`` shape the
    design specifies for the surfaced discrepancy payload (NAMING: camelCase keys
    for data that may be surfaced downstream).

    Attributes:
        run_property_id: The bound run ``propertyId`` the records were filtered
            against (Req 6.1).
        foreign_property_id: The non-matching ``propertyId`` whose records were
            excluded, or ``None`` for a "no matching data" discrepancy (Req 6.4).
        excluded_count: The number of records excluded for this discrepancy.
        reason: An optional human-readable reason; set to
            :data:`NO_MATCHING_DATA_REASON` for the zero-match case, else ``None``.
    """

    run_property_id: str
    foreign_property_id: Optional[str]
    excluded_count: int
    reason: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        """Render the camelCase discrepancy payload for surfacing/logging.

        Returns:
            A dict with the design's ``runPropertyId``, ``foreignPropertyId``, and
            ``excludedCount`` keys (camelCase per NAMING), plus ``reason`` when a
            reason is set. Suitable for structured logging (PYQUALITY-03) and for
            attaching to a downstream consumer of the isolation discrepancies.
        """
        # camelCase keys because this payload may be surfaced beyond Python (e.g.
        # onto an alert / into logs), where the platform convention is camelCase.
        payload: dict[str, Any] = {
            "runPropertyId": self.run_property_id,
            "foreignPropertyId": self.foreign_property_id,
            "excludedCount": self.excluded_count,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _extract_records(raw_result: Any) -> list[Any]:
    """Normalize a raw tool result into a flat list of candidate record rows.

    The filter must be robust to the raw tool shapes (task 5.3): a tool may return
    a bare list of records, a dict envelope nesting the list under one of
    :data:`RECORD_LIST_KEYS`, or a single record dict. This pure helper flattens
    all of those to a list so :func:`filter_to_property` can iterate uniformly.

    Args:
        raw_result: The raw value returned by a tool call. May be a list, a dict
            envelope, a single record dict, or ``None``.

    Returns:
        A list of candidate record rows. ``None`` and an empty envelope yield an
        empty list; a bare list is returned as-is; a dict is treated as an
        envelope when it nests a list under a known key, otherwise as one record.
    """
    # No result at all -> no records to filter.
    if raw_result is None:
        return []

    # A bare list of rows is already in the shape we want.
    if isinstance(raw_result, list):
        return list(raw_result)

    # A dict may either envelope a list of rows under a known key, or itself be a
    # single record row. Probe the known envelope keys first (defensive ordering).
    if isinstance(raw_result, dict):
        for list_key in RECORD_LIST_KEYS:
            nested = raw_result.get(list_key)
            if isinstance(nested, list):
                return list(nested)
        # No envelope key held a list: treat the dict as a single record row.
        return [raw_result]

    # Any other scalar shape is treated as a single opaque record row.
    return [raw_result]


def _record_property_id(record: Any) -> Optional[str]:
    """Read the ``propertyId`` off one raw record row, if present.

    Args:
        record: A single candidate record row (typically a dict).

    Returns:
        The record's ``propertyId`` value as-is when the row is a dict carrying
        :data:`RECORD_PROPERTY_ID_KEY`; otherwise ``None`` (a row missing a
        ``propertyId`` is treated as foreign and excluded, Req 6.3).
    """
    # Only dict rows can carry a propertyId; anything else is treated as missing
    # (and therefore foreign) by the caller.
    if isinstance(record, dict):
        return record.get(RECORD_PROPERTY_ID_KEY)
    return None


def filter_to_property(
    raw_result: Any,
    run_property_id: str,
) -> tuple[list[Any], tuple[PropertyDiscrepancy, ...]]:
    """Drop records that do not belong to the bound run property (Req 6.3, 6.4).

    The PURE, deterministic core of the property-isolation filter. Given the raw
    result of a READ-ONLY tool call and the single ``propertyId`` bound to the run
    (Req 6.1), it retains only records whose ``propertyId`` equals the bound id
    and excludes every other record - including records missing a ``propertyId``
    or carrying a different one (Req 6.3). Being pure and side-effect free, it is
    safe to drive from property tests (task 5.10).

    Discrepancy recording (Req 6.3, 6.4):

    * For foreign records, one :class:`PropertyDiscrepancy` is recorded per
      distinct foreign ``propertyId`` (records missing a ``propertyId`` are
      grouped under a ``None`` foreign id), each carrying that id and its excluded
      count. Distinct foreign ids are recorded in first-seen order for
      deterministic output.
    * If the response contained records but NONE matched the bound id, an
      additional "no matching data" discrepancy (``reason`` =
      :data:`NO_MATCHING_DATA_REASON`) is recorded and the kept list is empty
      (Req 6.4). An empty/absent response records no discrepancy (there is simply
      nothing to isolate).

    Args:
        raw_result: The raw tool result to filter. May be a bare list, a dict
            envelope nesting rows under a known key, a single record dict, or
            ``None`` (see :func:`_extract_records`).
        run_property_id: The single bound run ``propertyId`` to retain records for
            (Req 6.1).

    Returns:
        A ``(kept_records, discrepancies)`` tuple. ``kept_records`` is the list of
        rows whose ``propertyId`` equals ``run_property_id`` (order preserved).
        ``discrepancies`` is a tuple of :class:`PropertyDiscrepancy` entries
        describing every exclusion, empty when nothing was excluded.
    """
    records = _extract_records(raw_result)

    kept_records: list[Any] = []
    # Count excluded records per distinct foreign propertyId, preserving first-seen
    # order so the recorded discrepancies are deterministic for property tests.
    foreign_counts: dict[Optional[str], int] = {}
    foreign_order: list[Optional[str]] = []

    for record in records:
        record_property_id = _record_property_id(record)
        if record_property_id == run_property_id:
            # Belongs to the bound property: retain it (Req 6.3).
            kept_records.append(record)
        else:
            # Foreign (or missing) propertyId: exclude and tally by its id.
            if record_property_id not in foreign_counts:
                foreign_counts[record_property_id] = 0
                foreign_order.append(record_property_id)
            foreign_counts[record_property_id] += 1

    discrepancies: list[PropertyDiscrepancy] = [
        PropertyDiscrepancy(
            run_property_id=run_property_id,
            foreign_property_id=foreign_property_id,
            excluded_count=foreign_counts[foreign_property_id],
        )
        for foreign_property_id in foreign_order
    ]

    # Zero-match response (had records, none matched): treat as empty and record a
    # dedicated "no matching data" discrepancy (Req 6.4). An empty response
    # (no records at all) records nothing - there is nothing to isolate.
    if records and not kept_records:
        discrepancies.append(
            PropertyDiscrepancy(
                run_property_id=run_property_id,
                foreign_property_id=None,
                excluded_count=len(records),
                reason=NO_MATCHING_DATA_REASON,
            )
        )

    return kept_records, tuple(discrepancies)


def project_sister_availability(
    raw_result: Any,
    run_property_id: str,
) -> dict[str, Any]:
    """Keep ONLY sister availability values, discarding other sister fields.

    The PURE cross-property projection (Req 6.5, 6.6). Sister-property availability
    is the sole cross-property data the run may admit for overflow evaluation, and
    even then only the availability *values* may cross the boundary - every other
    operational field belonging to a sister ``propertyId`` MUST be discarded.

    This helper extracts each sister record's availability value (probing
    :data:`SISTER_AVAILABILITY_VALUE_KEYS` in order) and returns a plain mapping of
    sister ``propertyId`` -> availability value. Records belonging to the bound run
    property are skipped (a property does not overflow to itself), as are records
    missing a ``propertyId`` or carrying no recognizable availability value. Being
    pure, it is safe to drive from property tests (task 5.10).

    Args:
        raw_result: The raw ``get_sister_property_availability`` result. May be a
            bare list, a dict envelope, a single record dict, or ``None``.
        run_property_id: The bound run ``propertyId`` (Req 6.1); records for it are
            skipped since sister availability is strictly cross-property.

    Returns:
        A mapping of sister ``propertyId`` to its availability value, containing
        only availability values and nothing else (Req 6.5, 6.6). Empty when there
        is no admissible sister availability.
    """
    records = _extract_records(raw_result)

    availability_by_property: dict[str, Any] = {}
    for record in records:
        record_property_id = _record_property_id(record)
        # A sister must have a propertyId and must not be the bound property.
        if record_property_id is None or record_property_id == run_property_id:
            continue
        if not isinstance(record, dict):
            continue
        # Keep ONLY the availability value; discard every other operational field
        # belonging to the sister property (Req 6.6).
        for value_key in SISTER_AVAILABILITY_VALUE_KEYS:
            if value_key in record:
                availability_by_property[record_property_id] = record[value_key]
                break

    return availability_by_property


@dataclass(frozen=True)
class SignalSet:
    """The raw operational signals retrieved for one per-property forecasting run.

    Bundles the read-only Gateway results the Forecast Engine consumes, all
    scoped to a single bound ``property_id`` (the data-isolation boundary,
    Req 6.1). Sister availability is present only when overflow evaluation was
    requested (Req 2.3). Fields carry the raw tool results; parsing into the
    engine's ``DailyDemand`` shape happens downstream.

    Attributes:
        property_id: The single bound run property every signal was read for
            (Req 2.4, 6.1).
        occupancy: The raw ``get_occupancy`` result, or ``None`` when the tool
            returned no data (Req 2.1).
        revenue: The raw ``get_revenue`` result, or ``None`` when the tool
            returned no data (Req 2.2).
        sister_availability: The raw ``get_sister_property_availability`` result
            when overflow evaluation was requested; ``None`` when overflow was
            not needed or the tool returned no data (Req 2.3).
        missing_signals: The bare tool names whose signal was missing after all
            retries were exhausted (Req 2.7). A missing required tool
            (``get_occupancy`` / ``get_revenue``) here is what a downstream
            consumer uses to derive ``based_on_partial_data`` (Req 2.8).
        excluded_foreign_records: The total count of records dropped by the
            property-isolation filter across the occupancy and revenue reads for
            carrying a foreign (or missing) ``propertyId`` (Req 6.3, 6.4). This is
            the sum of the ``excluded_count`` of every foreign-record discrepancy
            in :attr:`discrepancies` (the "no matching data" entries are not
            re-counted, as their records were already tallied as foreign).
        discrepancies: The property-isolation discrepancies recorded while
            filtering the reads to the bound ``property_id`` (Req 6.3, 6.4). Each
            entry names a foreign ``propertyId`` and its excluded count, or a
            "no matching data" outcome. Empty when nothing crossed the boundary.
            Surfaced so downstream consumers (and property tests) can assert on
            the isolation outcome.
    """

    property_id: str
    occupancy: Optional[Any]
    revenue: Optional[Any]
    sister_availability: Optional[Any]
    missing_signals: tuple[str, ...]
    excluded_foreign_records: int
    discrepancies: tuple[PropertyDiscrepancy, ...] = ()


def _call_read_only_tool(
    call_tool: ToolCaller,
    tool_name: str,
    property_id: str,
    *,
    timeout_runner: TimeoutRunner = _run_with_timeout,
    sleep: SleepFn = time.sleep,
    timeout_seconds: float = PER_CALL_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[bool, Any]:
    """Invoke one READ-ONLY Gateway tool for a single property, with resilience.

    Enforces the read-only posture (Req 2.5) and stamps ``propertyId`` onto every
    call (Req 2.4). The tool name is checked against :data:`READ_ONLY_TOOLS`
    BEFORE the seam is touched, so a write tool is never invoked even once.

    Each attempt is bounded by ``timeout_seconds`` (Req 8.5) via the injected
    ``timeout_runner`` seam. Up to ``max_attempts`` total attempts are made
    (Req 2.6); between attempts the loop sleeps the exponential backoff amount
    (1s then 2s, derived from :data:`BACKOFF_BASE_SECONDS`) via the injected
    ``sleep`` seam. A timeout is treated as a FAILED attempt just like any other
    failure (Req 8.5). If every attempt fails, the tool is degraded gracefully:
    a structured error is logged (failed tool + ``propertyId``) and the function
    returns ``(False, None)`` so the caller can add the tool to
    ``missing_signals`` and CONTINUE the run (Req 2.7, 8.1) - it does NOT raise.

    Args:
        call_tool: The injected :data:`ToolCaller` seam that resolves and invokes
            the Gateway tool.
        tool_name: The bare read-only tool name to invoke; must be a member of
            :data:`READ_ONLY_TOOLS`.
        property_id: The single bound run property to scope the read to; included
            in the tool arguments as ``propertyId`` (Req 2.4).
        timeout_runner: Seam that runs one attempt under a timeout budget.
            Defaults to :func:`_run_with_timeout`; injectable for deterministic
            tests. It must raise on timeout (a builtin ``TimeoutError`` or
            :class:`SignalToolTimeout`).
        sleep: Seam invoked with the backoff seconds between attempts. Defaults
            to ``time.sleep``; inject a recorder to assert backoff without delay.
        timeout_seconds: Per-call timeout budget passed to ``timeout_runner``.
            Defaults to :data:`PER_CALL_TIMEOUT_SECONDS`.
        max_attempts: Total attempts allowed, including the first. Defaults to
            :data:`MAX_ATTEMPTS`.

    Returns:
        A ``(succeeded, result)`` tuple. On success ``succeeded`` is ``True`` and
        ``result`` is the raw tool result. On all-attempts failure ``succeeded``
        is ``False`` and ``result`` is ``None`` (graceful degradation, Req 2.7).

    Raises:
        NonReadOnlyToolError: If ``tool_name`` is not in :data:`READ_ONLY_TOOLS`
            (Req 2.5). Raised before the seam is invoked and never swallowed -
            a read-only violation is a programming error, not a degradable read.
    """
    # Read-only guard (Req 2.5): reject any tool outside the allowlist before we
    # ever touch the seam, so a write tool can never be called - not even once.
    if tool_name not in READ_ONLY_TOOLS:
        logger.error(
            "Blocked attempt to call a non-read-only tool",
            extra={"tool_name": tool_name, "property_id": property_id},
        )
        raise NonReadOnlyToolError(tool_name)

    # Every call carries the bound propertyId so the Gateway scopes the read to
    # this single property (Req 2.4). Build the args once; they are identical
    # across attempts.
    tool_args = {PROPERTY_ID_ARG: property_id}

    # Bounded retry loop (Req 2.6). attempt_number is 1-based for logging; the
    # backoff after a failed attempt uses BACKOFF_BASE_SECONDS * 2**(index).
    last_error: Optional[BaseException] = None
    for attempt_index in range(max_attempts):
        attempt_number = attempt_index + 1
        logger.info(
            "Reading operational signal",
            extra={
                "tool_name": tool_name,
                "property_id": property_id,
                "attempt": attempt_number,
                "max_attempts": max_attempts,
            },
        )
        try:
            # Run this single attempt under the per-call timeout budget. A
            # timeout raises out of the runner and is caught below as a failed
            # attempt (Req 8.5), identical to any other tool failure.
            result = timeout_runner(
                lambda: call_tool(tool_name, tool_args), timeout_seconds
            )
            # Success: return immediately, no further attempts or backoff.
            return True, result
        except (TimeoutError, SignalToolTimeout) as exc:
            # A timeout is a FAILED attempt (Req 8.5). Log with context and fall
            # through to backoff/retry - never swallowed silently (PYQUALITY-03).
            last_error = exc
            logger.warning(
                "Signal tool call timed out",
                extra={
                    "tool_name": tool_name,
                    "property_id": property_id,
                    "attempt": attempt_number,
                    "timeout_seconds": timeout_seconds,
                },
            )
        except Exception as exc:  # noqa: BLE001 - degrade one tool, keep the run
            # Any other tool/seam failure is also a failed attempt. We catch
            # broadly here ONLY to implement single-tool graceful degradation
            # (Req 2.7): we log with context and retry/degrade rather than
            # terminating the whole run. This is not a silent swallow.
            last_error = exc
            logger.warning(
                "Signal tool call failed",
                extra={
                    "tool_name": tool_name,
                    "property_id": property_id,
                    "attempt": attempt_number,
                    "error_type": type(exc).__name__,
                },
            )

        # Backoff before the next attempt, but not after the final attempt.
        if attempt_index < max_attempts - 1:
            backoff_seconds = float(BACKOFF_BASE_SECONDS * (2 ** attempt_index))
            logger.info(
                "Backing off before retrying signal tool call",
                extra={
                    "tool_name": tool_name,
                    "property_id": property_id,
                    "next_attempt": attempt_number + 1,
                    "backoff_seconds": backoff_seconds,
                },
            )
            sleep(backoff_seconds)

    # All attempts exhausted: degrade this one tool gracefully (Req 2.7, 8.1).
    # Record a structured error identifying the failed tool + property, then
    # signal failure to the caller (which adds it to missing_signals) WITHOUT
    # raising, so the run continues.
    logger.error(
        "Signal tool failed after all attempts; degrading gracefully",
        extra={
            "tool_name": tool_name,
            "property_id": property_id,
            "attempts": max_attempts,
            "last_error_type": (
                type(last_error).__name__ if last_error is not None else None
            ),
        },
    )
    return False, None


def read_signals(
    property_id: str,
    horizon: ForecastHorizon,
    call_tool: ToolCaller,
    *,
    needs_overflow: bool = False,
    timeout_runner: TimeoutRunner = _run_with_timeout,
    sleep: SleepFn = time.sleep,
    timeout_seconds: float = PER_CALL_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> SignalSet:
    """Read the operational signals for one per-property forecasting run.

    The Signal Reader entry point (design: Components -> ``signals.SignalReader``).
    It performs the two-phase read over the injected READ-ONLY Gateway seam,
    scoped to a single bound ``property_id`` (Req 2.1 - 2.5):

    1. ALWAYS read ``get_occupancy`` and ``get_revenue`` (Req 2.1, 2.2).
    2. Read ``get_sister_property_availability`` ONLY when ``needs_overflow`` is
       ``True`` - the second phase is skipped otherwise (Req 2.3).

    Every call carries ``propertyId`` (Req 2.4) and only the three tools in
    :data:`READ_ONLY_TOOLS` are ever invoked; a write tool is never called
    (Req 2.5).

    Each tool call is bounded by a per-call timeout and retried up to
    ``max_attempts`` with 1s -> 2s exponential backoff (Req 2.6, 8.5). A tool
    that fails every attempt is degraded gracefully: its field is left ``None``,
    its bare name is added to ``missing_signals``, and the run CONTINUES (Req
    2.7, 8.1). A missing required tool (occupancy / revenue) surfaces via
    ``missing_signals`` so a downstream consumer can derive
    ``based_on_partial_data`` (Req 2.8).

    After the reads succeed, the property-isolation filter runs over the occupancy
    and revenue results (Req 6.3, 6.4): records carrying a foreign or missing
    ``propertyId`` are dropped, the surviving records replace the raw result, and
    the recorded :class:`PropertyDiscrepancy` entries are surfaced on
    ``SignalSet.discrepancies`` and logged structurally (PYQUALITY-03).
    ``excluded_foreign_records`` is the total count of dropped foreign records
    across both reads. Sister availability is filtered separately by the engine's
    overflow evaluation (only availability values cross the boundary, Req 6.5,
    6.6, via :func:`project_sister_availability`); the reader passes its raw
    result through unchanged.

    Args:
        property_id: The single bound run property to read every signal for (the
            data-isolation boundary, Req 6.1). Included as ``propertyId`` on every
            tool call (Req 2.4).
        horizon: The forecast horizon window this run evaluates. Passed for the
            two-phase read contract; the per-tool date-range arguments derived
            from it are wired in task 5.2, so it is accepted here but not yet
            expanded into tool arguments.
        call_tool: The injected :data:`ToolCaller` seam that resolves and invokes
            the Gateway tools. Keeping this a seam is what makes the reader
            host-testable without a Strands/MCP client.
        needs_overflow: When ``True``, also read sister-property availability for
            overflow evaluation (Req 2.3). Keyword-only; defaults to ``False``.
        timeout_runner: Seam that bounds each attempt by a timeout. Defaults to
            :func:`_run_with_timeout`; injectable for deterministic tests.
        sleep: Seam invoked with the backoff seconds between attempts. Defaults
            to ``time.sleep``; inject a recorder to assert backoff without delay.
        timeout_seconds: Per-call timeout budget. Defaults to
            :data:`PER_CALL_TIMEOUT_SECONDS`.
        max_attempts: Total attempts allowed per tool. Defaults to
            :data:`MAX_ATTEMPTS`.

    Returns:
        A :class:`SignalSet` bound to ``property_id`` carrying the raw occupancy
        and revenue results (or ``None`` for a tool degraded after retries), the
        sister-availability result (``None`` when overflow was not requested or
        the tool was degraded), the ``missing_signals`` tuple of bare tool names
        that failed all attempts, ``excluded_foreign_records`` equal to the total
        count of foreign records dropped by the property-isolation filter across
        the occupancy and revenue reads, and the ``discrepancies`` tuple recording
        each isolation exclusion (Req 6.3, 6.4).

    Raises:
        NonReadOnlyToolError: If a non-read-only tool is ever reached (a guard on
            Req 2.5). Not expected under normal use since the reader only names
            read-only tools.
    """
    logger.info(
        "Reading signals for forecasting run",
        extra={
            "property_id": property_id,
            "needs_overflow": needs_overflow,
            "horizon_start": horizon.start_date,
            "horizon_end": horizon.end_date,
        },
    )

    # Bundle the resilience seams once so every per-tool call is driven the same
    # way (and tests inject them in a single place via read_signals).
    call_kwargs = {
        "timeout_runner": timeout_runner,
        "sleep": sleep,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
    }

    # Accumulate the bare tool names that failed every attempt so downstream can
    # flag partial data (Req 2.7, 2.8). Order preserved for stable output.
    missing_signals: list[str] = []

    # Phase 1 - always read occupancy and revenue (Req 2.1, 2.2). A tool that
    # fails all attempts degrades to None and is recorded in missing_signals;
    # the run CONTINUES (Req 2.7, 8.1).
    occupancy_ok, occupancy = _call_read_only_tool(
        call_tool, TOOL_GET_OCCUPANCY, property_id, **call_kwargs
    )
    if not occupancy_ok:
        missing_signals.append(TOOL_GET_OCCUPANCY)

    revenue_ok, revenue = _call_read_only_tool(
        call_tool, TOOL_GET_REVENUE, property_id, **call_kwargs
    )
    if not revenue_ok:
        missing_signals.append(TOOL_GET_REVENUE)

    # Phase 2 - read sister availability ONLY when overflow is needed (Req 2.3).
    sister_availability: Optional[Any] = None
    if needs_overflow:
        sister_ok, sister_availability = _call_read_only_tool(
            call_tool,
            TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
            property_id,
            **call_kwargs,
        )
        if not sister_ok:
            missing_signals.append(TOOL_GET_SISTER_PROPERTY_AVAILABILITY)

    if missing_signals:
        logger.warning(
            "Forecasting run proceeding with partial signals",
            extra={
                "property_id": property_id,
                "missing_signals": missing_signals,
            },
        )

    # Property-isolation filter (Req 6.3, 6.4): defensively drop any record whose
    # propertyId != the bound run id from the occupancy and revenue reads, even
    # though the Gateway also scopes server-side. Sister availability is NOT
    # filtered here - it is the one cross-property signal, and only its
    # availability values are admitted downstream (Req 6.5, 6.6) via
    # project_sister_availability during overflow evaluation.
    all_discrepancies: list[PropertyDiscrepancy] = []
    excluded_foreign_records = 0

    if occupancy is not None:
        occupancy, occupancy_discrepancies = filter_to_property(
            occupancy, property_id
        )
        all_discrepancies.extend(occupancy_discrepancies)

    if revenue is not None:
        revenue, revenue_discrepancies = filter_to_property(revenue, property_id)
        all_discrepancies.extend(revenue_discrepancies)

    # excluded_foreign_records is the total dropped foreign records; the synthetic
    # "no matching data" entries are not re-counted (their records were already
    # tallied under a foreign/None id), so sum only the foreign-record entries.
    excluded_foreign_records = sum(
        discrepancy.excluded_count
        for discrepancy in all_discrepancies
        if discrepancy.reason is None
    )

    if all_discrepancies:
        # Structured log of the isolation outcome (PYQUALITY-03): emit the
        # camelCase payloads so a downstream consumer sees the same shape.
        logger.warning(
            "Property-isolation filter excluded non-matching records",
            extra={
                "property_id": property_id,
                "excluded_foreign_records": excluded_foreign_records,
                "discrepancies": [
                    discrepancy.to_payload() for discrepancy in all_discrepancies
                ],
            },
        )

    return SignalSet(
        property_id=property_id,
        occupancy=occupancy,
        revenue=revenue,
        sister_availability=sister_availability,
        missing_signals=tuple(missing_signals),
        excluded_foreign_records=excluded_foreign_records,
        discrepancies=tuple(all_discrepancies),
    )


__all__ = [
    "ToolCaller",
    "ToolThunk",
    "TimeoutRunner",
    "SleepFn",
    "MonotonicClock",
    "SignalSet",
    "PropertyDiscrepancy",
    "read_signals",
    "filter_to_property",
    "project_sister_availability",
    "NonReadOnlyToolError",
    "SignalToolTimeout",
    "READ_ONLY_TOOLS",
    "TOOL_GET_OCCUPANCY",
    "TOOL_GET_REVENUE",
    "TOOL_GET_SISTER_PROPERTY_AVAILABILITY",
    "PROPERTY_ID_ARG",
    "RECORD_PROPERTY_ID_KEY",
    "RECORD_LIST_KEYS",
    "SISTER_AVAILABILITY_VALUE_KEYS",
    "NO_MATCHING_DATA_REASON",
    "PER_CALL_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_SEQUENCE_SECONDS",
]
