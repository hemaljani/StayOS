"""moto DynamoDB integration test for the Generate and Reconcile steps.

Exercises the real Task 3 wiring end to end against an in-memory DynamoDB:

* Create the 5 LUMI operational tables (keys per ``docs/data-model.md``).
* Run the Generate step for ONE property with a fixed ``referenceDate`` and
  assert a coherent, re-anchored dataset is written (Property 1: the window is
  contiguous and reservation status relationships are valid relative to the
  reference date).
* Run Generate a SECOND time with the same ``referenceDate`` and assert
  idempotence - no new items and nothing rewritten (Property 2).
* Run the Reconcile step and assert room statuses converge coherently.

The Task 1 generators bind module-level boto3 clients at import time. To make
those clients moto-backed, the generator and handler modules are (re)imported
*inside* the ``mock_aws`` context by the ``dataset_stack`` fixture.

# Feature: data-Orchestrator, Property 1, Property 2
Validates: Requirements 1.4, 2.2, 2.3, 2.4.
"""

from __future__ import annotations

import importlib
from datetime import date
from typing import Any, Dict, Iterator, List, Tuple

import boto3
import pytest
from moto import mock_aws

from conftest import DATASET_TABLE_ENV, DATASET_TABLE_KEYS, make_lambda_context

# One fixed reference date so the deterministic window is stable across runs.
REFERENCE_DATE = "2026-08-17"
# Roll-forward is scoped to a single property (Requirement 1.4).
TARGET_PROPERTY = "ALOHA-CHI-001"
REGION = "us-east-1"


def _create_tables(client: Any) -> None:
    """Create the 5 LUMI operational tables with their documented key schema.

    Args:
        client: A moto-backed DynamoDB client.
    """
    for table_name, (partition_key, sort_key) in DATASET_TABLE_KEYS.items():
        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": partition_key, "KeyType": "HASH"},
                {"AttributeName": sort_key, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": partition_key, "AttributeType": "S"},
                {"AttributeName": sort_key, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=table_name)


def _reload_wired_modules() -> Tuple[Any, Any]:
    """Reimport generator + handler modules so their boto3 clients are mocked.

    The generators (and reconcile) create module-level DynamoDB clients at
    import time; reloading them while ``mock_aws`` is active rebinds those
    clients to moto. Returns the freshly loaded Generate and Reconcile handler
    modules.

    Returns:
        A tuple ``(generate_handler, reconcile_handler)`` bound to moto clients.
    """
    import dataset_generator.writer as writer_module
    import dataset_generator.rooms_generator as rooms_module

    # Rebind the module-level boto3 client/resource under the active mock.
    importlib.reload(writer_module)
    importlib.reload(rooms_module)

    # Rebuild the shim/runner/handlers so they reference the reloaded modules.
    import dataset_generator_shim as shim_module
    import generation_runner as runner_module
    import generate_handler as generate_module
    import reconcile_handler as reconcile_module

    importlib.reload(shim_module)
    importlib.reload(runner_module)
    importlib.reload(generate_module)
    importlib.reload(reconcile_module)

    return generate_module, reconcile_module


@pytest.fixture()
def dataset_stack() -> Iterator[Dict[str, Any]]:
    """Stand up a moto DynamoDB stack with the 5 tables and wired modules.

    Yields:
        A dict with the moto ``client`` and the reloaded ``generate_handler``
        and ``reconcile_handler`` modules.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        _create_tables(client)
        generate_module, reconcile_module = _reload_wired_modules()
        yield {
            "client": client,
            "generate_handler": generate_module,
            "reconcile_handler": reconcile_module,
        }


def _scan_all(client: Any, table_name: str) -> List[Dict[str, Any]]:
    """Return every item in a table via paginated Scan.

    Args:
        client: A moto-backed DynamoDB client.
        table_name: The table to scan.

    Returns:
        All items as raw DynamoDB-typed dicts.
    """
    items: List[Dict[str, Any]] = []
    paginator = client.get_paginator("scan")
    for page in paginator.paginate(TableName=table_name):
        items.extend(page.get("Items", []))
    return items


def _roll_forward_event() -> Dict[str, str]:
    """Build a roll-forward step event scoped to the target property."""
    return {
        "mode": "roll-forward",
        "propertyId": TARGET_PROPERTY,
        "referenceDate": REFERENCE_DATE,
    }


class TestGenerateProducesCoherentDataset:
    """Property 1: one execution yields a coherent re-anchored dataset."""

    def test_generate_writes_coherent_window(self, dataset_stack: Dict[str, Any]) -> None:
        # Feature: data-Orchestrator, Property 1
        client = dataset_stack["client"]
        generate_handler = dataset_stack["generate_handler"]

        result = generate_handler.lambda_handler(_roll_forward_event(), make_lambda_context())

        # Envelope + scoping contract.
        assert result["step"] == "Generate"
        assert result["status"] == "ok"
        assert result["details"]["targetProperties"] == [TARGET_PROPERTY]
        assert result["details"]["upsertMode"] == "idempotent"

        counts = result["details"]["perTableCounts"]
        generated = result["details"]["generatedCounts"]
        # A coherent dataset means every table generated items for the property.
        assert set(generated) == {"rooms", "guests", "revenues", "reservations", "work_orders"}
        for purpose, count in generated.items():
            assert count > 0, f"expected {purpose} items for {TARGET_PROPERTY}"
        # First run writes new items to every table.
        assert set(counts) == {"rooms", "guests", "revenues", "reservations", "work_orders"}
        assert all(count > 0 for count in counts.values()), (
            f"expected first-run writes to every table, got {counts}"
        )

        # Rooms actually landed in DynamoDB for the target property.
        rooms = _scan_all(client, DATASET_TABLE_ENV["ROOMS_TABLE_NAME"])
        target_rooms = [r for r in rooms if r["propertyId"]["S"] == TARGET_PROPERTY]
        assert len(target_rooms) == generated["rooms"]

        # Property 1 - window coherence: reservations for the target property
        # are contiguous relative to the reference date, and every reservation's
        # arrival/departure/status relationship is valid.
        reservations = _scan_all(client, DATASET_TABLE_ENV["RESERVATIONS_TABLE_NAME"])
        target_res = [r for r in reservations if r["propertyId"]["S"] == TARGET_PROPERTY]
        assert target_res, "expected reservations for the target property"

        ref = date.fromisoformat(REFERENCE_DATE)
        arrival_dates: List[date] = []
        for reservation in target_res:
            arrival = date.fromisoformat(reservation["arrivalDate"]["S"])
            departure = date.fromisoformat(reservation["departureDate"]["S"])
            status = reservation["status"]["S"]
            arrival_dates.append(arrival)

            # Departure is always strictly after arrival (valid stay).
            assert departure > arrival, (
                f"reservation {reservation['dateReservationId']['S']} has "
                f"departure {departure} not after arrival {arrival}"
            )

            # Status must be consistent with the arrival date vs the reference
            # date (re-anchored window, Requirement 2.2).
            if status == "CHECKED_OUT":
                assert departure <= ref, (
                    f"CHECKED_OUT reservation departs {departure} after ref {ref}"
                )
            elif status == "CONFIRMED":
                assert arrival > ref, (
                    f"CONFIRMED reservation arrives {arrival} on/before ref {ref}"
                )
            elif status == "CHECKED_IN":
                assert arrival <= ref < departure, (
                    f"CHECKED_IN reservation window [{arrival},{departure}) "
                    f"does not straddle ref {ref}"
                )

        # The window is anchored to the reference date: the latest arrival is
        # within the generated forward horizon and the earliest is in the past.
        assert min(arrival_dates) <= ref, "window should include past arrivals"
        assert max(arrival_dates) >= ref, "window should reach the reference date"


class TestRollForwardIsIdempotent:
    """Property 2: a re-run with the same referenceDate is a no-op."""

    def test_second_run_writes_nothing_new(self, dataset_stack: Dict[str, Any]) -> None:
        # Feature: data-Orchestrator, Property 2
        client = dataset_stack["client"]
        generate_handler = dataset_stack["generate_handler"]

        # First run: seeds the window and reports real per-table write counts.
        first = generate_handler.lambda_handler(_roll_forward_event(), make_lambda_context())
        first_counts = first["details"]["perTableCounts"]
        assert any(count > 0 for count in first_counts.values())

        # Snapshot per-table item totals after the first run.
        def _table_sizes() -> Dict[str, int]:
            return {
                purpose: len(_scan_all(client, env_table))
                for purpose, env_table in (
                    ("rooms", DATASET_TABLE_ENV["ROOMS_TABLE_NAME"]),
                    ("guests", DATASET_TABLE_ENV["GUESTS_TABLE_NAME"]),
                    ("revenues", DATASET_TABLE_ENV["REVENUES_TABLE_NAME"]),
                    ("reservations", DATASET_TABLE_ENV["RESERVATIONS_TABLE_NAME"]),
                    ("work_orders", DATASET_TABLE_ENV["WORK_ORDERS_TABLE_NAME"]),
                )
            }

        sizes_after_first = _table_sizes()

        # Second run with the same reference date: Idempotent_Upsert must write
        # nothing new. Scoped per-table counts (new/changed items) are all zero.
        second = generate_handler.lambda_handler(_roll_forward_event(), make_lambda_context())
        second_counts = second["details"]["perTableCounts"]
        assert all(count == 0 for count in second_counts.values()), (
            f"expected no net change on re-run, got {second_counts}"
        )

        # And item counts in every table are unchanged (no growth, no deletes).
        sizes_after_second = _table_sizes()
        assert sizes_after_second == sizes_after_first, (
            f"table sizes changed on idempotent re-run: "
            f"{sizes_after_first} -> {sizes_after_second}"
        )


class TestReconcileConvergesRoomStatus:
    """Reconcile applies coherent room-status updates for the reference date."""

    def test_reconcile_returns_counts_and_updates_rooms(
        self, dataset_stack: Dict[str, Any]
    ) -> None:
        # Feature: data-Orchestrator, Property 1
        client = dataset_stack["client"]
        generate_handler = dataset_stack["generate_handler"]
        reconcile_handler = dataset_stack["reconcile_handler"]

        # Generate first so rooms/reservations/work orders exist to reconcile.
        generate_handler.lambda_handler(_roll_forward_event(), make_lambda_context())

        result = reconcile_handler.lambda_handler(_roll_forward_event(), make_lambda_context())
        assert result["step"] == "Reconcile"
        assert result["status"] == "ok"
        counts = result["details"]["reconciledCounts"]
        # CR-2: reconcile now also emits explicit AVAILABLE resets for rooms
        # with no active reservation/work order, so "available" is part of the
        # counts contract alongside the occupied/ooo/maintenance/errors keys.
        assert set(counts) == {"occupied", "ooo", "maintenance", "available", "errors"}
        assert counts["errors"] == 0

        # At least some rooms should have moved off AVAILABLE for the ref date.
        rooms = _scan_all(client, DATASET_TABLE_ENV["ROOMS_TABLE_NAME"])
        target_rooms = [r for r in rooms if r["propertyId"]["S"] == TARGET_PROPERTY]
        statuses = {r["status"]["S"] for r in target_rooms}
        # Reconciliation is idempotent: re-running yields the same non-error result.
        rerun = reconcile_handler.lambda_handler(_roll_forward_event(), make_lambda_context())
        assert rerun["details"]["reconciledCounts"]["errors"] == 0
        # Coherence: every room carries a statusRoomNumber composite matching its status.
        for room in target_rooms:
            status = room["status"]["S"]
            composite = room["statusRoomNumber"]["S"]
            assert composite.startswith(f"{status}#"), (
                f"room {room['roomNumber']['S']} status {status} inconsistent "
                f"with composite {composite}"
            )
        assert statuses  # non-empty
