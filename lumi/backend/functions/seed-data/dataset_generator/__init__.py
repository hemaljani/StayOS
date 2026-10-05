"""Hotel operations dataset generator package.

This package generates 30 days of deterministic hotel operations data across
5 pilot properties (Chicago, Miami, Tokyo, Madrid, Mumbai) for the LUMI
intelligence brief system. The generated data populates 5 DynamoDB tables:
stayos-rooms, stayos-guests, stayos-revenues, stayos-reservations, and stayos-work-orders.

All generation is deterministic (no randomness) to ensure identical output
across deploys, supporting idempotent CloudFormation custom resources.

Modules:
    config: Property profiles, room type distributions, VIP name pool, constants.
    rooms_generator: Full room inventory generation per property.
    guests_generator: VIP guest profile generation (50 per property).
    revenue_generator: Daily KPI/revenue snapshot generation (30 days).
    reservations_generator: Reservation generation matching occupancy targets.
    work_orders_generator: Work order lifecycle generation with realistic timing.
    writer: BatchWriteItem utility with exponential backoff on UnprocessedItems.
"""

from dataset_generator.reference_date import resolve_reference_date
from dataset_generator.rooms_generator import generate_rooms, reconcile_room_status
from dataset_generator.guests_generator import generate_guests
from dataset_generator.revenue_generator import generate_revenue
from dataset_generator.reservations_generator import generate_reservations
from dataset_generator.work_orders_generator import generate_work_orders
from dataset_generator.writer import BatchWriter

__all__ = [
    "generate_rooms",
    "generate_guests",
    "generate_revenue",
    "generate_reservations",
    "generate_work_orders",
    "reconcile_room_status",
    "resolve_reference_date",
    "BatchWriter",
]
