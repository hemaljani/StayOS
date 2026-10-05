"""PULSE observability helpers (metrics and tracing).

This package holds the cross-cutting observability primitives used by the
delivery path and, potentially, other PULSE components:

    * :mod:`pulse.observability.metrics` -- the EMF latency-metric emitter that
      records ``AlertDeliveryLatencyMs`` (namespace ``PULSE/Delivery``,
      dimensioned by ``Tier``) within 60 s of delivery (Requirement 17.1), plus
      a pure latency-ms helper and a best-effort X-Ray subsegment utility for
      distinguishing the generation and delivery trace segments (Requirement
      17.3).

All emission here is best-effort: a failure to emit a metric or open a trace
subsegment never interrupts alert generation or delivery (Requirement 17.4).
"""

from __future__ import annotations

__all__: list[str] = []
