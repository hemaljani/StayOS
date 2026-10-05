"""EMF delivery-latency metric emitter and tracing helpers (Requirement 17).

This module is the single place that knows how to record the alert
generation->delivery latency metric that the observability dashboard renders as
p50/p90/p99 per tier:

    * **Metric:** ``AlertDeliveryLatencyMs`` (milliseconds).
    * **Namespace:** ``PULSE/Delivery``.
    * **Dimension:** ``Tier`` (``CRITICAL`` / ``WARNING`` / ``INFO``).

The metric represents the elapsed milliseconds between when the alert was
generated (its ``createdAt``) and when it was delivered, emitted within 60 s of
delivery (Requirement 17.1). Emission is **best-effort** (Requirement 17.4): a
failure to emit -- a malformed timestamp, a serialization error, anything --
never interrupts delivery; it is caught and logged, and delivery proceeds.

Design points (PYQUALITY):
    * :func:`compute_latency_ms` is a **pure** function (no I/O, no clock): it
      takes the ``createdAt`` string and an explicit ``now`` and returns the
      clamped elapsed milliseconds, so it is trivially unit-testable.
    * The actual EMF write sits behind the injectable :data:`EmitterFn` seam.
      The default emitter uses AWS Lambda Powertools' ``single_metric`` (EMF)
      so the metric is serialized to stdout in the CloudWatch-parseable format
      without a ``print`` (PYQUALITY-03). Tests inject a fake emitter and never
      touch Powertools or stdout.
    * :func:`record_delivery_latency` composes the pure computation with the
      emitter and wraps the whole thing so it can never raise into the caller.

Tracing (Requirement 17.3): :func:`trace_subsegment` is a best-effort context
manager that opens a named X-Ray subsegment (via Powertools' tracer provider
when the X-Ray SDK is present) so a trace can carry distinct *generation* and
*delivery* segments. When tracing is unavailable it is a transparent no-op, so
importing or calling it never requires the X-Ray SDK. Beyond this helper,
``TracingConfig: Active`` on every Lambda (set in the pipeline/api templates)
plus a Powertools ``Tracer`` at each handler (built via
:func:`pulse.common.tracing.get_tracer` and applied as
``@tracer.capture_lambda_handler``, with an ``alertId`` annotation on the
single-alert triage/delivery/resolve hops) is what actually captures and
correlates the end-to-end trace; this helper only makes an explicit sub-segment
convenient.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Optional

from pulse.common.logging import get_logger

logger = get_logger("pulse-push-service")

# Custom metric namespace, metric name, and dimension (design Observability).
# The namespace is a PULSE-owned custom namespace (NAMING-02: ``${StackPrefix}``
# maps to ``PULSE`` for custom metric namespaces); the metric/dimension names
# are camelCase/PascalCase as appropriate for CloudWatch.
METRIC_NAMESPACE = "PULSE/Delivery"
METRIC_ALERT_DELIVERY_LATENCY = "AlertDeliveryLatencyMs"
DIMENSION_TIER = "Tier"

# The item attribute carrying the alert generation timestamp (camelCase,
# NAMING-05).
CREATED_AT_ATTR = "createdAt"

# An emitter takes the tier value and the latency in milliseconds and writes the
# metric, raising on failure. Injectable so tests never touch Powertools/stdout.
EmitterFn = Callable[[str, int], None]


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a timezone-aware ``datetime`` (UTC).

    Accepts a trailing ``Z`` (Zulu) suffix -- the form PULSE writes -- as well
    as an explicit offset. A naive timestamp is assumed to be UTC.

    Args:
        value: The ISO 8601 timestamp string (e.g. ``2026-08-17T14:30:00Z``).

    Returns:
        A timezone-aware ``datetime`` in UTC.

    Raises:
        ValueError: If ``value`` is not a parseable ISO 8601 timestamp.
    """
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def compute_latency_ms(created_at: str, now: datetime) -> int:
    """Compute delivery latency in milliseconds from ``createdAt`` to ``now``.

    Pure function: no clock, no I/O. The result is clamped to be non-negative,
    so minor clock skew that makes ``now`` appear before ``createdAt`` yields
    ``0`` rather than a negative latency.

    Args:
        created_at: The alert generation timestamp (ISO 8601).
        now: The delivery time as a ``datetime`` (naive is treated as UTC).

    Returns:
        The elapsed milliseconds, clamped to ``>= 0``.

    Raises:
        ValueError: If ``created_at`` is not a parseable ISO 8601 timestamp.
    """
    created = _parse_iso8601(created_at)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    elapsed_ms = (now_utc - created).total_seconds() * 1000.0
    return max(0, int(round(elapsed_ms)))


def _default_emitter(tier_value: str, latency_ms: int) -> None:
    """Emit ``AlertDeliveryLatencyMs`` as an EMF metric via Powertools.

    Uses Powertools' ``single_metric`` context manager, which serializes a
    single EMF metric blob to stdout on exit in the format CloudWatch parses
    into a custom metric -- no ``print`` required (PYQUALITY-03).

    Args:
        tier_value: The alert tier value used as the ``Tier`` dimension.
        latency_ms: The generation->delivery latency in milliseconds.
    """
    from aws_lambda_powertools.metrics import MetricUnit, single_metric

    with single_metric(
        name=METRIC_ALERT_DELIVERY_LATENCY,
        unit=MetricUnit.Milliseconds,
        value=latency_ms,
        namespace=METRIC_NAMESPACE,
    ) as metric:
        metric.add_dimension(name=DIMENSION_TIER, value=tier_value)


def record_delivery_latency(
    item: Mapping[str, Any],
    tier: str,
    *,
    now: Optional[datetime] = None,
    emitter: Optional[EmitterFn] = None,
) -> Optional[int]:
    """Record the delivery-latency metric for an alert (best-effort).

    Computes the generation->delivery latency from the alert's ``createdAt`` and
    ``now`` (default: the current UTC time) and emits it as
    ``AlertDeliveryLatencyMs`` dimensioned by ``Tier``. The whole operation is
    wrapped so it can never raise into the delivery path: on any failure (a
    missing/malformed ``createdAt``, an emitter error) it logs and returns
    ``None`` while delivery proceeds (Requirement 17.4).

    Args:
        item: The ``pulse-alerts`` item; its ``createdAt`` is the generation
            time.
        tier: The alert tier value (the ``Tier`` dimension).
        now: The delivery time; defaults to the current UTC time. Injectable so
            the elapsed value is deterministic in tests.
        emitter: The EMF emitter seam; the Powertools ``single_metric`` emitter
            is used when omitted.

    Returns:
        The emitted latency in milliseconds, or ``None`` when emission failed
        (and was logged) so delivery continues uninterrupted.
    """
    active_emitter = emitter if emitter is not None else _default_emitter
    try:
        created_at = item.get(CREATED_AT_ATTR)
        if not created_at:
            raise ValueError(f"alert item has no {CREATED_AT_ATTR!r}")
        latency_ms = compute_latency_ms(
            str(created_at), now if now is not None else datetime.now(UTC)
        )
        active_emitter(tier, latency_ms)
        return latency_ms
    except Exception as exc:  # noqa: BLE001 - best-effort, must never block delivery
        # Requirement 17.4: emission failure never interrupts delivery; record
        # the error identifying the affected alert and continue.
        logger.error(
            "Delivery latency metric emission failed; delivery unaffected",
            extra={
                "alertId": item.get("alertId"),
                "tier": tier,
                "error": str(exc),
            },
        )
        return None


@contextmanager
def trace_subsegment(name: str) -> Iterator[None]:
    """Open a best-effort X-Ray subsegment for a named span (Requirement 17.3).

    Lets a trace carry distinct *generation* and *delivery* subsegments. When
    the X-Ray SDK is present (Powertools tracing enabled) a subsegment named
    ``name`` is opened and closed around the block; when it is unavailable the
    context manager is a transparent no-op, so callers never depend on the SDK
    being installed and tests never require it.

    Args:
        name: The subsegment name (e.g. ``"generation"`` or ``"delivery"``).

    Yields:
        ``None``. The block runs whether or not tracing is active.
    """
    recorder = None
    try:
        from aws_xray_sdk.core import xray_recorder as recorder  # type: ignore

        recorder.begin_subsegment(name)
    except Exception:  # noqa: BLE001 - tracing is optional and never blocks work
        recorder = None
    try:
        yield
    finally:
        if recorder is not None:
            try:
                recorder.end_subsegment()
            except Exception:  # noqa: BLE001 - closing a subsegment is best-effort
                logger.debug("Failed to close X-Ray subsegment", extra={"name": name})


__all__ = [
    "METRIC_NAMESPACE",
    "METRIC_ALERT_DELIVERY_LATENCY",
    "DIMENSION_TIER",
    "CREATED_AT_ATTR",
    "EmitterFn",
    "compute_latency_ms",
    "record_delivery_latency",
    "trace_subsegment",
]
