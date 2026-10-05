"""Shared operational-table schema conventions for the closed loop.

The Demo Scenario Simulator (:mod:`pulse.demo_simulator.simulator`) writes to
the LUMI operational tables to raise a triggering condition, and the Action
Executor (:mod:`pulse.action_executor.executor`) writes back to the *same*
items to clear it. For the loop to close, both components must agree on the
item keys they read and write, and the rule evaluators
(:mod:`pulse.rule_engine.evaluators`) must read the attributes those items
carry.

This module is the single source of truth for that agreement:

    * **Table names** come from environment variables (never hardcoded --
      PYQUALITY-06 / NAMING-03): ``RESERVATIONS_TABLE_NAME``,
      ``ROOMS_TABLE_NAME``, ``GUESTS_TABLE_NAME``.
    * **Key conventions.** Each operational table uses a single
      ``(propertyId, <sort>)`` key schema; the sort key encodes the entity kind
      with a prefix so several scenario entities can coexist on one table
      (e.g. a Walk Risk aggregate and a premium reservation both live in
      ``stayos-reservations``). The evaluators ignore the sort key and read the
      item's business attributes; the prefix only disambiguates storage.

Attribute names are camelCase (NAMING-05). These are prototype schemas modeling
the LUMI operational tables; the exact production LUMI key schema is out of
scope (design Open Question 8).
"""

from __future__ import annotations

from typing import Any

from pulse.common.config import get_optional_env

# ---------------------------------------------------------------------------
# Operational-table environment-variable names (single source of truth).
# ---------------------------------------------------------------------------

ENV_RESERVATIONS_TABLE = "RESERVATIONS_TABLE_NAME"
ENV_ROOMS_TABLE = "ROOMS_TABLE_NAME"
ENV_GUESTS_TABLE = "GUESTS_TABLE_NAME"

# ---------------------------------------------------------------------------
# Key attribute names (one key schema per operational table).
# ---------------------------------------------------------------------------

# stayos-reservations: propertyId (PK) + dateReservationId (SK). Holds Walk Risk
# arrival aggregates, VIP arrival records, and individual reservations. The SK
# attribute name matches the deployed LUMI stayos-reservations schema; the
# prefix in the SK VALUE disambiguates entity kinds (see BUG-014).
RESERVATIONS_PK = "propertyId"
RESERVATIONS_SK = "dateReservationId"

# stayos-rooms: propertyId (PK) + roomNumber (SK). Holds the per-property
# OOO/blocks snapshot the OOO Cluster evaluator reads. SK name matches the
# deployed LUMI stayos-rooms schema.
ROOMS_PK = "propertyId"
ROOMS_SK = "roomNumber"

# stayos-guests: propertyId (PK) + guestId (SK). Holds complaint records and VIP
# check-in records. SK name matches the deployed LUMI stayos-guests schema.
GUESTS_PK = "propertyId"
GUESTS_SK = "guestId"

# Sort-key prefixes disambiguating entity kinds stored on one table.
WALK_SK_PREFIX = "WALK#"
ARRIVAL_SK_PREFIX = "ARRIVAL#"
RESERVATION_SK_PREFIX = "RES#"
COMPLAINT_SK_PREFIX = "COMPLAINT#"
CHECKIN_SK_PREFIX = "CHECKIN#"

# Fixed sort key of the per-property OOO/group-block snapshot item.
OOO_SNAPSHOT_SK = "OOO_SNAPSHOT"


# ---------------------------------------------------------------------------
# Table-name resolution (from the environment)
# ---------------------------------------------------------------------------


def reservations_table_name() -> str:
    """Return the ``stayos-reservations`` table name from the environment.

    Returns:
        The configured reservations table name, or an empty string when unset.
    """
    return get_optional_env(ENV_RESERVATIONS_TABLE, "") or ""


def rooms_table_name() -> str:
    """Return the ``stayos-rooms`` table name from the environment.

    Returns:
        The configured rooms table name, or an empty string when unset.
    """
    return get_optional_env(ENV_ROOMS_TABLE, "") or ""


def guests_table_name() -> str:
    """Return the ``stayos-guests`` table name from the environment.

    Returns:
        The configured guests table name, or an empty string when unset.
    """
    return get_optional_env(ENV_GUESTS_TABLE, "") or ""


# ---------------------------------------------------------------------------
# Key builders (pure)
# ---------------------------------------------------------------------------


def walk_reservation_key(property_id: str, arrival_date: str) -> dict[str, Any]:
    """Build the reservations key for a Walk Risk arrival aggregate.

    Args:
        property_id: The property the aggregate belongs to.
        arrival_date: The arrival date the aggregate covers (``YYYY-MM-DD``).

    Returns:
        The ``stayos-reservations`` primary key for the aggregate item.
    """
    return {
        RESERVATIONS_PK: property_id,
        RESERVATIONS_SK: f"{WALK_SK_PREFIX}{arrival_date}",
    }


def vip_arrival_key(property_id: str, guest_id: str) -> dict[str, Any]:
    """Build the reservations key for a VIP arrival record.

    Args:
        property_id: The property the arrival belongs to.
        guest_id: The arriving VIP guest identifier.

    Returns:
        The ``stayos-reservations`` primary key for the arrival item.
    """
    return {
        RESERVATIONS_PK: property_id,
        RESERVATIONS_SK: f"{ARRIVAL_SK_PREFIX}{guest_id}",
    }


def premium_reservation_key(property_id: str, reservation_id: str) -> dict[str, Any]:
    """Build the reservations key for an individual reservation.

    Args:
        property_id: The property the reservation belongs to.
        reservation_id: The reservation identifier.

    Returns:
        The ``stayos-reservations`` primary key for the reservation item.
    """
    return {
        RESERVATIONS_PK: property_id,
        RESERVATIONS_SK: f"{RESERVATION_SK_PREFIX}{reservation_id}",
    }


def ooo_snapshot_key(property_id: str) -> dict[str, Any]:
    """Build the rooms key for a property's OOO/group-block snapshot.

    Args:
        property_id: The property the snapshot belongs to.

    Returns:
        The ``stayos-rooms`` primary key for the snapshot item.
    """
    return {ROOMS_PK: property_id, ROOMS_SK: OOO_SNAPSHOT_SK}


def complaint_key(property_id: str, complaint_id: str) -> dict[str, Any]:
    """Build the guests key for a complaint record.

    The stayos-guests sort key attribute is ``guestId``. This scenario has no
    business ``guestId`` attribute (the complaint evaluator reads ``complaintId``),
    so the complaint id is used as the guests sort-key value, prefixed to keep
    synthetic demo complaint rows from colliding with real seeded guest rows.

    Args:
        property_id: The property the complaint belongs to.
        complaint_id: The complaint identifier.

    Returns:
        The ``stayos-guests`` primary key for the complaint item.
    """
    return {GUESTS_PK: property_id, GUESTS_SK: f"{COMPLAINT_SK_PREFIX}{complaint_id}"}


def vip_checkin_key(property_id: str, guest_id: str, stay_id: str) -> dict[str, Any]:
    """Build the guests key for a VIP check-in record.

    The stayos-guests sort key attribute is ``guestId`` -- the SAME attribute the
    VIP check-in evaluator reads as a business value. So the sort-key VALUE must be
    the business guest id (no prefix), otherwise the evaluator would read a
    synthetic key value and build a malformed dedupe key (see BUG-014). ``stay_id``
    is carried as a separate business attribute, not folded into the key.

    Args:
        property_id: The property the check-in belongs to.
        guest_id: The checking-in guest identifier.
        stay_id: The stay identifier (unused in the key; kept for signature
            compatibility with callers and the Action Executor).

    Returns:
        The ``stayos-guests`` primary key for the check-in item.
    """
    return {GUESTS_PK: property_id, GUESTS_SK: guest_id}


__all__ = [
    "ENV_RESERVATIONS_TABLE",
    "ENV_ROOMS_TABLE",
    "ENV_GUESTS_TABLE",
    "RESERVATIONS_PK",
    "RESERVATIONS_SK",
    "ROOMS_PK",
    "ROOMS_SK",
    "GUESTS_PK",
    "GUESTS_SK",
    "WALK_SK_PREFIX",
    "ARRIVAL_SK_PREFIX",
    "RESERVATION_SK_PREFIX",
    "COMPLAINT_SK_PREFIX",
    "CHECKIN_SK_PREFIX",
    "OOO_SNAPSHOT_SK",
    "reservations_table_name",
    "rooms_table_name",
    "guests_table_name",
    "walk_reservation_key",
    "vip_arrival_key",
    "premium_reservation_key",
    "ooo_snapshot_key",
    "complaint_key",
    "vip_checkin_key",
]
