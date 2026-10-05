"""Time-based escalation state machine (pure transition logic).

This module holds the **pure** transitions for the escalation chain
(Requirement 6): given an alert's current escalation state it computes the next
state on a timeout checkpoint or on an acknowledgement, with no I/O. The
orchestration that loads/persists state, delivers to recipients, and schedules
the next checkpoint lives in :mod:`pulse.escalation.service` and delegates every
decision to the functions here so they are unit-testable in isolation
(Properties 11, 12).

Chain model:
    * Every CRITICAL alert is assigned an escalation chain ordered
      ``[GM, AGM, MOD]`` (Requirement 6.1). The chain is stored as a list of
      recipient aliases with a 0-based ``position`` pointing at the current
      recipient.
    * A timeout advances the position by exactly one and never skips
      (Requirement 6.3); reaching a timeout at the last position exhausts the
      chain (Requirement 6.6).
    * An acknowledgement at any position halts escalation (Requirement 6.5).

An alert is "still open" for escalation purposes while its status is
``UNACKNOWLEDGED`` or ``ESCALATED``. ``ACKNOWLEDGED``, ``RESOLVED``, and
``ESCALATION_EXHAUSTED`` are terminal for the chain: a checkpoint that fires
against a terminal alert is a no-op (this is how an acknowledgement that races
a checkpoint is made safe).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pulse.common.models import AlertStatus

# The canonical escalation-chain roles in order (Requirement 6.1). Position 0 is
# the GM, 1 the AGM, 2 the MOD. Used to build a default chain and to label
# recipients for logging.
ESCALATION_ROLES: tuple[str, ...] = ("GM", "AGM", "MOD")

# Statuses in which the chain is still active and a timeout may advance it.
_OPEN_STATUSES = frozenset({AlertStatus.UNACKNOWLEDGED, AlertStatus.ESCALATED})


def default_escalation_chain(
    gm_alias: str, agm_alias: str, mod_alias: str
) -> list[str]:
    """Build the default ``[GM, AGM, MOD]`` escalation chain.

    Args:
        gm_alias: The General Manager alias (position 1 / index 0).
        agm_alias: The Assistant General Manager alias (position 2 / index 1).
        mod_alias: The Manager On Duty alias (position 3 / index 2).

    Returns:
        The ordered list of recipient aliases.
    """
    return [gm_alias, agm_alias, mod_alias]


@dataclass(frozen=True)
class EscalationChainState:
    """A snapshot of an alert's escalation state fed to the transitions.

    Attributes:
        status: The current alert status.
        chain: The ordered recipient aliases ``[GM, AGM, MOD]``.
        position: 0-based index of the current recipient within ``chain``.
    """

    status: AlertStatus
    chain: list[str]
    position: int


@dataclass(frozen=True)
class EscalationTransition:
    """The computed result of an escalation transition (pure output).

    Attributes:
        changed: Whether the transition changes the alert's state. ``False``
            marks a no-op (e.g. a checkpoint against a terminal alert).
        new_status: The status to apply.
        new_position: The chain position to apply.
        current_recipient: The recipient to deliver to as a result of this
            transition, or ``None`` when no delivery is required (acknowledge or
            exhaustion).
        all_notified: ``True`` only on exhaustion, when every chain recipient
            has been notified without acknowledgement (Requirement 6.6).
        schedule_next: Whether the caller should schedule the next timeout
            checkpoint (only after advancing to a non-last recipient).
    """

    changed: bool
    new_status: AlertStatus
    new_position: int
    current_recipient: Optional[str]
    all_notified: bool = False
    schedule_next: bool = False


def _noop(state: EscalationChainState) -> EscalationTransition:
    """Return a no-op transition that leaves the state unchanged.

    Args:
        state: The current escalation state.

    Returns:
        An unchanged :class:`EscalationTransition`.
    """
    return EscalationTransition(
        changed=False,
        new_status=state.status,
        new_position=state.position,
        current_recipient=None,
    )


def advance_on_timeout(state: EscalationChainState) -> EscalationTransition:
    """Compute the transition when an escalation timeout checkpoint fires.

    While the alert is still open (``UNACKNOWLEDGED`` or ``ESCALATED``):
        * If the current recipient is not the last in the chain, advance the
          position by exactly one, set status ``ESCALATED``, and require
          delivery to the new recipient plus scheduling of the next checkpoint
          (Requirement 6.3).
        * If the current recipient is the last position (MOD), set status
          ``ESCALATION_EXHAUSTED`` and record that all recipients were notified
          without acknowledgement (Requirement 6.6).

    A checkpoint against a terminal/acknowledged alert is a no-op.

    Args:
        state: The current escalation state.

    Returns:
        The computed :class:`EscalationTransition`.
    """
    if state.status not in _OPEN_STATUSES:
        return _noop(state)

    last_index = len(state.chain) - 1
    if state.position < last_index:
        next_position = state.position + 1
        return EscalationTransition(
            changed=True,
            new_status=AlertStatus.ESCALATED,
            new_position=next_position,
            current_recipient=state.chain[next_position],
            all_notified=False,
            schedule_next=next_position < last_index,
        )

    # At the last position: the chain is exhausted.
    return EscalationTransition(
        changed=True,
        new_status=AlertStatus.ESCALATION_EXHAUSTED,
        new_position=state.position,
        current_recipient=None,
        all_notified=True,
        schedule_next=False,
    )


def acknowledge(state: EscalationChainState) -> EscalationTransition:
    """Compute the transition when a recipient acknowledges the alert.

    Acknowledgement at any open position halts all further escalation and sets
    the status to ``ACKNOWLEDGED`` (Requirements 6.5, Property 12). An
    acknowledgement against an already-terminal alert (``RESOLVED``,
    ``ESCALATION_EXHAUSTED``) or an already ``ACKNOWLEDGED`` alert is a no-op so
    the operation is idempotent and safe against races.

    Args:
        state: The current escalation state.

    Returns:
        The computed :class:`EscalationTransition`.
    """
    if state.status not in _OPEN_STATUSES:
        return _noop(state)
    return EscalationTransition(
        changed=True,
        new_status=AlertStatus.ACKNOWLEDGED,
        new_position=state.position,
        current_recipient=None,
        all_notified=False,
        schedule_next=False,
    )


__all__ = [
    "ESCALATION_ROLES",
    "default_escalation_chain",
    "EscalationChainState",
    "EscalationTransition",
    "advance_on_timeout",
    "acknowledge",
]
