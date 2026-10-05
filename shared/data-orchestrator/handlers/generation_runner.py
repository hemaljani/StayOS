"""Re-anchor generation runner shared by the Generate and Reconcile steps.

This module is the single seam between the orchestrator step Lambdas and the
Task 1 LUMI ``dataset_generator`` package. It runs the deterministic generator
pipeline against a resolved ``reference_date`` using Idempotent_Upsert writes,
then exposes:

* :func:`run_generation` - executes rooms -> guests -> revenue -> reservations
  -> work_orders (all idempotent upserts) and returns the per-table write
  results plus the in-memory lookups/lists the reconcile step needs.
* :func:`run_reconcile` - applies ``reconcile_room_status`` for the resolved
  reference date against the generated reservations and work orders.

Table names are read exclusively from environment variables (PYQUALITY-06 /
NAMING); nothing is hardcoded. The generators themselves emit the full 5-property
estate deterministically, so a re-run with the same reference date is a no-op
(Requirements 2.3, 2.4); callers scope reported counts to the target properties
for a roll-forward (Requirements 1.4, 2.2).

Runtime note: the deployment packages the ``dataset_generator`` package
alongside these handlers (or points ``DATASET_GENERATOR_PATH`` at it); see
``dataset_generator_shim`` for how the import is resolved.

Satisfies: Requirements 1.3, 1.4, 2.2, 2.3. Supports Properties 1, 2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Tuple

from aws_lambda_powertools import Logger

# Importing the shim registers the dataset_generator package on sys.path and
# re-exports the generator entry points as typed symbols.
from dataset_generator_shim import (
    BatchWriter,
    generate_guests,
    generate_reservations,
    generate_revenue,
    generate_rooms,
    generate_work_orders,
    reconcile_room_status,
    resolve_reference_date,
)

logger = Logger(service="stayos-data-orchestrator")

# Logical table purpose -> environment variable holding its physical name.
# Names match lumi/backend/tests/conftest.py and the deployed LUMI stack.
TABLE_ENV_VARS: Dict[str, str] = {
    "rooms": "ROOMS_TABLE_NAME",
    "guests": "GUESTS_TABLE_NAME",
    "revenues": "REVENUES_TABLE_NAME",
    "reservations": "RESERVATIONS_TABLE_NAME",
    "work_orders": "WORK_ORDERS_TABLE_NAME",
}

# Ordered logical purposes so per-table counts always cover the 5 LUMI tables.
DATASET_PURPOSES: Tuple[str, ...] = (
    "rooms",
    "guests",
    "revenues",
    "reservations",
    "work_orders",
)


class MissingTableConfigError(RuntimeError):
    """Raised when a required table-name environment variable is unset.

    Using a domain-specific exception (PYQUALITY-02) lets the state machine
    distinguish a misconfiguration from a downstream DynamoDB failure.
    """


class _NullBatchWriter:
    """No-op stand-in for :class:`BatchWriter` that never touches DynamoDB.

    The generators build their full deterministic item lists in memory and then
    hand them to a writer. Reconcile needs only the built structures
    (``reservations`` / ``work_orders`` / ``rooms_lookup``), not another round
    of idempotent-upsert writes plus the ~50k-item ``BatchGetItem`` read-back
    that ``run_generation`` performs (review finding CR-5). Passing this null
    writer lets the pipeline build the window with zero DynamoDB write/read cost.

    It implements just the surface the generators use: ``write_items`` (a no-op
    returning zeroed counts) and the ``*_count`` attributes.
    """

    def __init__(self, table_name: str) -> None:
        """Record the table name; keep zeroed counters for interface parity.

        Args:
            table_name: The logical table this writer would target (unused).
        """
        self.table_name = table_name
        self.success_count: int = 0
        self.failure_count: int = 0
        self.skipped_count: int = 0
        self.readback_fallback_count: int = 0

    def write_items(
        self, items: List[Dict[str, Any]], idempotent: bool = False
    ) -> Dict[str, int]:
        """Discard the items; return zeroed counts without any DynamoDB call.

        Args:
            items: The generated items (intentionally not persisted).
            idempotent: Accepted for signature parity; ignored.

        Returns:
            A zeroed counts dict matching :meth:`BatchWriter.write_items`.
        """
        return {"success": 0, "failed": 0, "skipped": 0, "readback_fallback": 0}


@dataclass
class GenerationResult:
    """Outcome of a generation run, consumable by Generate and Reconcile.

    Attributes:
        reference_date: The resolved anchor date the window was built around.
        per_table_counts: Items actually WRITTEN per logical table on this run
            (new or changed items via Idempotent_Upsert). On an idempotent
            re-run with the same reference date every count is 0, which is how
            Property 2 (roll-forward idempotence) is observed. Deterministic
            generation emits the same estate each run, so unchanged items are
            skipped by the writer rather than rewritten (Requirements 2.3, 2.4).
        generated_counts: Total items GENERATED per table for the target
            properties this run, regardless of whether they were written. Useful
            to confirm a coherent dataset exists for the property even on a
            no-op re-run (Requirement 2.2).
        rooms_lookup: propertyId -> list of generated room items.
        reservations: All generated reservation items across the estate.
        work_orders: All generated work order items across the estate.
    """

    reference_date: date
    per_table_counts: Dict[str, int] = field(default_factory=dict)
    generated_counts: Dict[str, int] = field(default_factory=dict)
    rooms_lookup: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    reservations: List[Dict[str, Any]] = field(default_factory=list)
    work_orders: List[Dict[str, Any]] = field(default_factory=list)


def _resolve_table_name(purpose: str) -> str:
    """Resolve a physical table name from its environment variable.

    Args:
        purpose: The logical table purpose (a key of :data:`TABLE_ENV_VARS`).

    Returns:
        The physical DynamoDB table name.

    Raises:
        MissingTableConfigError: If the backing environment variable is unset
            or empty.
    """
    env_var = TABLE_ENV_VARS[purpose]
    table_name = os.environ.get(env_var)
    if not table_name:
        raise MissingTableConfigError(
            f"environment variable {env_var} for the {purpose} table is not set"
        )
    return table_name


def _scoped_count(
    items: List[Dict[str, Any]], target_properties: List[str]
) -> int:
    """Count items whose ``propertyId`` is in the target set.

    Args:
        items: Generated items, each carrying a ``propertyId`` attribute.
        target_properties: The properties this run is scoped to.

    Returns:
        Number of items belonging to a target property.
    """
    target = set(target_properties)
    return sum(1 for item in items if item.get("propertyId") in target)


def run_generation(
    reference_date_raw: str,
    target_properties: List[str],
    writer_factory: Any = BatchWriter,
) -> GenerationResult:
    """Run the deterministic generator pipeline as idempotent upserts.

    Executes the Task 1 generators in dependency order against table names read
    from environment variables. The generators emit the full 5-property estate
    deterministically; per-table counts in the result are scoped to
    ``target_properties`` so a roll-forward reports only its property's window
    (Requirement 1.4). Idempotent_Upsert means a re-run with the same
    ``reference_date`` writes nothing new (Requirements 2.3, 2.4).

    Args:
        reference_date_raw: The ISO ``YYYY-MM-DD`` reference date to anchor the
            window to.
        target_properties: Properties to scope reported counts to. For ``seed``
            this is the full pilot estate; for ``roll-forward`` a single
            property.
        writer_factory: Callable ``(table_name) -> writer`` used to build each
            per-table writer. Defaults to the real :class:`BatchWriter` (writes
            to DynamoDB). :func:`build_generation_window` passes
            :class:`_NullBatchWriter` to build the window in memory only, with
            no writes and no read-back (review finding CR-5).

    Returns:
        A :class:`GenerationResult` with per-table write counts and the
        in-memory lookups/lists the reconcile step consumes.

    Raises:
        MissingTableConfigError: If any required table env var is unset.
    """
    reference_date = resolve_reference_date(reference_date_raw)

    rooms_table = _resolve_table_name("rooms")
    guests_table = _resolve_table_name("guests")
    revenues_table = _resolve_table_name("revenues")
    reservations_table = _resolve_table_name("reservations")
    work_orders_table = _resolve_table_name("work_orders")

    logger.info(
        "running re-anchor generation pipeline",
        extra={
            "referenceDate": reference_date.isoformat(),
            "targetProperties": target_properties,
        },
    )

    # Hold each writer so we can read how many items were actually WRITTEN
    # (new or changed) versus skipped as unchanged. On an idempotent re-run the
    # written counts are 0, which is how Property 2 is observed.
    rooms_writer = writer_factory(rooms_table)
    guests_writer = writer_factory(guests_table)
    revenues_writer = writer_factory(revenues_table)
    reservations_writer = writer_factory(reservations_table)
    work_orders_writer = writer_factory(work_orders_table)

    # Rooms first: reservations and work orders reference the room inventory.
    rooms_lookup = generate_rooms(
        rooms_writer, reference_date=reference_date, idempotent=True
    )
    # Guests: reservations assign valid guestIds from this lookup.
    guests_lookup = generate_guests(
        guests_writer, reference_date=reference_date, idempotent=True
    )
    # Revenue: reservation volumes match these occupancy/arrival targets.
    revenue_lookup = generate_revenue(
        revenues_writer, reference_date=reference_date, idempotent=True
    )
    # Reservations: consume rooms/guests/revenue lookups, anchored to the date.
    reservations = generate_reservations(
        reservations_writer,
        rooms_lookup,
        guests_lookup,
        revenue_lookup,
        reference_date=reference_date,
        idempotent=True,
    )
    # Work orders: lifecycle status derived from the same reference date.
    work_orders = generate_work_orders(
        work_orders_writer,
        rooms_lookup,
        reference_date=reference_date,
        idempotent=True,
    )

    # Revenue lookup is keyed by (propertyId, date_str); flatten to values so
    # the scoped counter can read each item's propertyId uniformly.
    revenue_items = list(revenue_lookup.values())
    guest_items = [
        guest for guests in guests_lookup.values() for guest in guests
    ]
    room_items = [room for rooms in rooms_lookup.values() for room in rooms]

    # Items actually written this run (new/changed). Goes to 0 on an idempotent
    # re-run, which is exactly the "no net data change" invariant (Property 2).
    per_table_counts = {
        "rooms": rooms_writer.success_count,
        "guests": guests_writer.success_count,
        "revenues": revenues_writer.success_count,
        "reservations": reservations_writer.success_count,
        "work_orders": work_orders_writer.success_count,
    }

    # Items generated for the target properties this run (present regardless of
    # whether they were written), so a coherent dataset can be confirmed even on
    # a no-op re-run (Requirement 2.2).
    generated_counts = {
        "rooms": _scoped_count(room_items, target_properties),
        "guests": _scoped_count(guest_items, target_properties),
        "revenues": _scoped_count(revenue_items, target_properties),
        "reservations": _scoped_count(reservations, target_properties),
        "work_orders": _scoped_count(work_orders, target_properties),
    }

    logger.info(
        "generation pipeline complete",
        extra={
            "referenceDate": reference_date.isoformat(),
            "perTableCounts": per_table_counts,
            "generatedCounts": generated_counts,
        },
    )

    return GenerationResult(
        reference_date=reference_date,
        per_table_counts=per_table_counts,
        generated_counts=generated_counts,
        rooms_lookup=rooms_lookup,
        reservations=reservations,
        work_orders=work_orders,
    )


def build_generation_window(reference_date_raw: str) -> GenerationResult:
    """Build the deterministic window in memory WITHOUT writing to DynamoDB.

    Runs the same deterministic generators as :func:`run_generation` but with a
    :class:`_NullBatchWriter`, so it produces the ``reservations`` /
    ``work_orders`` / ``rooms_lookup`` structures reconcile needs while issuing
    zero DynamoDB writes and zero ``BatchGetItem`` read-backs (review finding
    CR-5). Per-table WRITE counts are therefore all 0; ``generated_counts`` is
    not meaningful here (target scope is empty) and is ignored by reconcile.

    Args:
        reference_date_raw: The ISO ``YYYY-MM-DD`` reference date to anchor the
            window to.

    Returns:
        A :class:`GenerationResult` whose lookups/lists reconcile consumes.

    Raises:
        MissingTableConfigError: If any required table env var is unset.
    """
    return run_generation(
        reference_date_raw, target_properties=[], writer_factory=_NullBatchWriter
    )


def run_reconcile(reference_date_raw: str) -> Dict[str, int]:
    """Reconcile room status against the resolved reference date.

    Rebuilds the deterministic window (rooms/reservations/work orders) for the
    reference date and applies ``reconcile_room_status`` so rooms reflect the
    reference date's CHECKED_IN reservations and OPEN/IN_PROGRESS work orders
    (Requirement 2.1). Reconciliation only issues per-item UpdateItem calls, so
    re-running with the same reference date converges to the same statuses
    (idempotent, Requirement 2.4).

    Args:
        reference_date_raw: The ISO ``YYYY-MM-DD`` reference date to reconcile
            against.

    Returns:
        Reconciliation counts:
        ``{"occupied", "ooo", "maintenance", "available", "errors"}``.

    Raises:
        MissingTableConfigError: If a required table env var is unset.
    """
    reference_date = resolve_reference_date(reference_date_raw)
    rooms_table = _resolve_table_name("rooms")

    # Rebuild the in-memory window deterministically WITHOUT re-writing every
    # table. run_generation (the Generate step) already persisted the window via
    # idempotent upsert; reconcile only needs the built structures, so it uses a
    # null writer to avoid a redundant full write + ~50k-item BatchGetItem
    # read-back on every daily execution (review finding CR-5).
    generation = build_generation_window(reference_date.isoformat())

    counts = reconcile_room_status(
        generation.reservations,
        generation.work_orders,
        generation.rooms_lookup,
        rooms_table,
        reference_date=reference_date,
    )

    logger.info(
        "room status reconciliation complete",
        extra={
            "referenceDate": reference_date.isoformat(),
            "reconciledCounts": counts,
        },
    )
    return counts
