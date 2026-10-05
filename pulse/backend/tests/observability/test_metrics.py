"""Unit tests for the delivery-latency EMF emitter (Task 22 / Requirement 17).

Covers the pure latency-ms computation and the best-effort emission contract:
an emission failure must never interrupt delivery and must be recorded
(Requirement 17.4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pulse.observability import metrics as obs

# ---------------------------------------------------------------------------
# compute_latency_ms (pure)
# ---------------------------------------------------------------------------


def test_compute_latency_ms_basic_elapsed() -> None:
    """Elapsed milliseconds between createdAt (Z) and now are exact."""
    created = "2026-08-17T14:30:00Z"
    now = datetime(2026, 8, 17, 14, 30, 1, 500000, tzinfo=UTC)
    assert obs.compute_latency_ms(created, now) == 1500


def test_compute_latency_ms_clamped_to_non_negative() -> None:
    """A now earlier than createdAt (clock skew) clamps to 0, never negative."""
    created = "2026-08-17T14:30:05Z"
    now = datetime(2026, 8, 17, 14, 30, 0, tzinfo=UTC)
    assert obs.compute_latency_ms(created, now) == 0


def test_compute_latency_ms_parses_explicit_offset() -> None:
    """An explicit UTC offset is parsed equivalently to the Z suffix."""
    created = "2026-08-17T14:30:00+00:00"
    now = datetime(2026, 8, 17, 14, 30, 2, tzinfo=UTC)
    assert obs.compute_latency_ms(created, now) == 2000


def test_compute_latency_ms_treats_naive_now_as_utc() -> None:
    """A naive now datetime is treated as UTC (no crash, sane value)."""
    created = "2026-08-17T14:30:00Z"
    now = datetime(2026, 8, 17, 14, 30, 3)  # naive
    assert obs.compute_latency_ms(created, now) == 3000


# ---------------------------------------------------------------------------
# record_delivery_latency (best-effort emission)
# ---------------------------------------------------------------------------


def test_record_delivery_latency_emits_tier_and_ms() -> None:
    """The recorder passes the tier value and computed ms to the emitter."""
    captured: list[tuple[str, int]] = []

    def _emitter(tier_value: str, latency_ms: int) -> None:
        captured.append((tier_value, latency_ms))

    item: dict[str, Any] = {"alertId": "a1", "createdAt": "2026-08-17T14:30:00Z"}
    now = datetime(2026, 8, 17, 14, 30, 2, tzinfo=UTC)

    result = obs.record_delivery_latency(item, "CRITICAL", now=now, emitter=_emitter)

    assert result == 2000
    assert captured == [("CRITICAL", 2000)]


def test_record_delivery_latency_emission_failure_is_best_effort() -> None:
    """An emitter that raises is swallowed; delivery is not interrupted (17.4)."""

    def _raising_emitter(tier_value: str, latency_ms: int) -> None:
        raise RuntimeError("cloudwatch unavailable")

    item: dict[str, Any] = {"alertId": "a1", "createdAt": "2026-08-17T14:30:00Z"}
    now = datetime(2026, 8, 17, 14, 30, 2, tzinfo=UTC)

    # Must not raise; returns None to signal emission did not complete.
    result = obs.record_delivery_latency(
        item, "WARNING", now=now, emitter=_raising_emitter
    )
    assert result is None


def test_record_delivery_latency_missing_created_at_is_best_effort() -> None:
    """A missing createdAt yields None without raising and without emitting."""
    calls: list[tuple[str, int]] = []

    item: dict[str, Any] = {"alertId": "a1"}
    result = obs.record_delivery_latency(
        item, "INFO", emitter=lambda tier, ms: calls.append((tier, ms))
    )
    assert result is None
    assert calls == []


def test_record_delivery_latency_malformed_created_at_is_best_effort() -> None:
    """A malformed createdAt is caught; returns None, delivery unaffected."""
    item: dict[str, Any] = {"alertId": "a1", "createdAt": "not-a-timestamp"}
    result = obs.record_delivery_latency(
        item, "INFO", emitter=lambda tier, ms: None
    )
    assert result is None


# ---------------------------------------------------------------------------
# trace_subsegment (best-effort, no-op without the X-Ray SDK)
# ---------------------------------------------------------------------------


def test_trace_subsegment_is_transparent_noop_without_xray() -> None:
    """The subsegment context manager runs the block whether or not X-Ray is on."""
    ran = False
    with obs.trace_subsegment("delivery"):
        ran = True
    assert ran is True
