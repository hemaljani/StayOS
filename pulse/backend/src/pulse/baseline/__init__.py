"""Curated deterministic PULSE baseline (Component 5, owned by PULSE).

The StayOS Unified Data Orchestrator primes a small, predictable set of
``pulse-alerts`` items per property after each Roll-Forward so a presenter
opening PULSE always sees a populated, story-ready feed (Requirement 6). This
package owns that baseline: the deterministic catalog and the reset-then-prime
builder. The orchestrator only *invokes* it through the
``pulse_baseline_shim`` seam, mirroring how it invokes the PULSE quiesce seam.

Implementation choice: the baseline is written by *directly seeding*
``pulse-alerts`` items (not by driving the demo simulator's ``run`` path). The
simulator writes to the LUMI operational tables and relies on the
DynamoDB Streams -> rule engine to *create* alerts, but that stream consumption
is exactly what the orchestrator quiesces during a Roll-Forward. Depending on
it would violate Requirement 6.4 ("SHALL NOT depend on the quiesced bulk stream
fallout") and would not be synchronous or deterministic during priming.
Directly seeding alert items sidesteps the stream entirely and is fully
deterministic and idempotent.
"""

from __future__ import annotations

from pulse.baseline.builder import (
    BASELINE_ID_PREFIX,
    CuratedAlertSpec,
    baseline_specs_for_property,
    build_baseline_items,
    prime_property_baseline,
    reset_property_baseline,
)

__all__ = [
    "BASELINE_ID_PREFIX",
    "CuratedAlertSpec",
    "baseline_specs_for_property",
    "build_baseline_items",
    "prime_property_baseline",
    "reset_property_baseline",
]
