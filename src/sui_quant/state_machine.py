"""D-01 trading and protection state machine definitions and transition guards (§7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sui_quant.utils.clock import ensure_utc


class InvalidStateTransitionError(RuntimeError):
    """Raised when an illegal state transition is attempted in D-01."""


class TradingState(str, Enum):
    BOOT = "BOOT"
    SYNCING = "SYNCING"
    READY_NO_POSITION = "READY_NO_POSITION"
    SIGNAL_CANDIDATE = "SIGNAL_CANDIDATE"
    RISK_CHECK = "RISK_CHECK"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_UNKNOWN = "ORDER_UNKNOWN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTED = "PROTECTED"
    EXITING = "EXITING"
    FLAT = "FLAT"
    ENTRY_FROZEN = "ENTRY_FROZEN"
    SECURITY_LOCKDOWN = "SECURITY_LOCKDOWN"


# Emergency states reachable from ANY state upon risk/security triggers (§7)
EMERGENCY_STATES = frozenset({TradingState.ENTRY_FROZEN, TradingState.SECURITY_LOCKDOWN})

# Normal state transition adjacency list (§7)
VALID_TRANSITIONS: dict[TradingState, frozenset[TradingState]] = {
    TradingState.BOOT: frozenset({TradingState.SYNCING}),
    TradingState.SYNCING: frozenset(
        {
            TradingState.READY_NO_POSITION,
            TradingState.PROTECTED,
            TradingState.PROTECTION_PENDING,
            TradingState.ORDER_UNKNOWN,
        }
    ),
    TradingState.READY_NO_POSITION: frozenset({TradingState.SIGNAL_CANDIDATE, TradingState.SYNCING}),
    TradingState.SIGNAL_CANDIDATE: frozenset({TradingState.RISK_CHECK, TradingState.READY_NO_POSITION}),
    TradingState.RISK_CHECK: frozenset({TradingState.ORDER_SUBMITTED, TradingState.READY_NO_POSITION}),
    TradingState.ORDER_SUBMITTED: frozenset(
        {
            TradingState.ORDER_UNKNOWN,
            TradingState.PARTIALLY_FILLED,
            TradingState.FILLED,
            TradingState.READY_NO_POSITION,
        }
    ),
    TradingState.ORDER_UNKNOWN: frozenset(
        {
            TradingState.PARTIALLY_FILLED,
            TradingState.FILLED,
            TradingState.READY_NO_POSITION,
        }
    ),
    TradingState.PARTIALLY_FILLED: frozenset({TradingState.PROTECTION_PENDING, TradingState.FILLED}),
    TradingState.FILLED: frozenset({TradingState.PROTECTION_PENDING}),
    TradingState.PROTECTION_PENDING: frozenset({TradingState.PROTECTED, TradingState.EXITING}),
    TradingState.PROTECTED: frozenset({TradingState.EXITING, TradingState.PROTECTION_PENDING}),
    TradingState.EXITING: frozenset({TradingState.FLAT, TradingState.PROTECTION_PENDING}),
    TradingState.FLAT: frozenset({TradingState.READY_NO_POSITION, TradingState.SYNCING}),
    # Leaving ENTRY_FROZEN or SECURITY_LOCKDOWN requires passing through SYNCING reconciliation gate
    TradingState.ENTRY_FROZEN: frozenset({TradingState.SYNCING, TradingState.EXITING}),
    TradingState.SECURITY_LOCKDOWN: frozenset({TradingState.SYNCING, TradingState.EXITING}),
}


@dataclass(frozen=True)
class StateTransitionRecord:
    transition_id: str
    from_state: TradingState
    to_state: TradingState
    reason: str
    occurred_at_utc: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.occurred_at_utc, "occurred_at_utc")


def can_transition(from_state: TradingState, to_state: TradingState) -> bool:
    """Return True if transitioning from `from_state` to `to_state` is valid under §7 rules."""
    if to_state in EMERGENCY_STATES:
        return True
    allowed = VALID_TRANSITIONS.get(from_state, frozenset())
    return to_state in allowed


def assert_valid_transition(from_state: TradingState, to_state: TradingState) -> None:
    """Raise InvalidStateTransitionError if the transition violates D-01 invariants."""
    if not can_transition(from_state, to_state):
        raise InvalidStateTransitionError(
            f"Illegal D-01 state transition: {from_state.value} -> {to_state.value}"
        )
