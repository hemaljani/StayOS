"""Unit tests for the CR-5 fix (AI-13): reconcile must not re-write/read-back.

Review finding CR-5: ``run_reconcile`` previously called ``run_generation`` with
the real :class:`BatchWriter`, re-writing every table and issuing a ~50k-item
``BatchGetItem`` read-back on every daily execution. The fix routes reconcile
through :func:`build_generation_window`, which uses a null writer so the window
is built in memory with ZERO DynamoDB writes and ZERO read-backs; only
``reconcile_room_status`` then issues per-item ``UpdateItem`` calls.

These tests pin that behavior so a regression (reconcile reverting to a full
generation write) is caught.

# Feature: data-Orchestrator, Property 2
Validates: review finding CR-5 (AI-13).
"""

from __future__ import annotations

from typing import Any, Dict, List

import generation_runner


class _RecordingWriter:
    """Writer stand-in that records whether any write was attempted.

    Implements the surface the generators use (``write_items`` + ``*_count``
    attributes) and flags ``wrote`` True if ``write_items`` is ever called with
    a non-empty batch.
    """

    instances: List["_RecordingWriter"] = []

    def __init__(self, table_name: str) -> None:
        self.table_name = table_name
        self.success_count = 0
        self.failure_count = 0
        self.skipped_count = 0
        self.readback_fallback_count = 0
        self.wrote = False
        _RecordingWriter.instances.append(self)

    def write_items(
        self, items: List[Dict[str, Any]], idempotent: bool = False
    ) -> Dict[str, int]:
        """Record a write attempt; persist nothing.

        Args:
            items: The generated items (not persisted).
            idempotent: Accepted for signature parity; ignored.

        Returns:
            A zeroed counts dict.
        """
        if items:
            self.wrote = True
        return {"success": 0, "failed": 0, "skipped": 0, "readback_fallback": 0}


def test_reconcile_builds_window_with_null_writer(monkeypatch: Any) -> None:
    """run_reconcile must NOT write the dataset (uses the null-writer window).

    Spies on ``reconcile_room_status`` (so no real DynamoDB call happens) and
    asserts the writer type used to build the reconcile window is the
    ``_NullBatchWriter`` — i.e. reconcile builds the window in memory and issues
    no BatchWriteItem/BatchGetItem, per CR-5.
    """
    seen_writer_types: List[type] = []

    real_run_generation = generation_runner.run_generation

    def _spy_run_generation(reference_date_raw, target_properties, writer_factory=None):
        # Record which writer the caller passed. build_generation_window (used
        # by run_reconcile) must pass _NullBatchWriter, never the real BatchWriter.
        seen_writer_types.append(writer_factory)
        return real_run_generation(
            reference_date_raw, target_properties, writer_factory=writer_factory
        )

    monkeypatch.setattr(generation_runner, "run_generation", _spy_run_generation)

    captured: Dict[str, Any] = {}

    def _fake_reconcile(reservations, work_orders, rooms_lookup, rooms_table, reference_date):
        captured["called"] = True
        captured["rooms_table"] = rooms_table
        return {"occupied": 0, "ooo": 0, "maintenance": 0, "available": 0, "errors": 0}

    monkeypatch.setattr(generation_runner, "reconcile_room_status", _fake_reconcile)

    counts = generation_runner.run_reconcile("2026-08-17")

    # Reconcile ran and returned the counts contract.
    assert captured.get("called") is True
    assert set(counts) == {"occupied", "ooo", "maintenance", "available", "errors"}
    # The window was built with the NULL writer, not the real BatchWriter.
    assert seen_writer_types, "run_generation was not invoked by run_reconcile"
    assert seen_writer_types[-1] is generation_runner._NullBatchWriter, (
        "run_reconcile must build the window with _NullBatchWriter (CR-5) — "
        f"got {seen_writer_types[-1]}"
    )


def test_null_writer_records_no_writes() -> None:
    """The null writer never reports a persisted item (defensive on CR-5)."""
    writer = generation_runner._NullBatchWriter("stayos-rooms-test")
    result = writer.write_items([{"propertyId": "ALOHA-CHI-001"}], idempotent=True)
    assert result == {"success": 0, "failed": 0, "skipped": 0, "readback_fallback": 0}
    assert writer.success_count == 0
