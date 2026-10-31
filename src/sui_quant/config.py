"""Risk boundary configuration and RESEARCH/SHADOW/PAPER/LIVE environment isolation gates (§0.1, §8.1, §16)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sui_quant.contracts import REFERENCE_SYMBOL, TRADING_SYMBOL, require_decimal
from sui_quant.utils.clock import ensure_utc, utc_now


class RiskBoundaryViolationError(ValueError):
    """Raised when a configuration attempts to relax hard risk or asset boundaries."""


class LiveAuthorizationError(PermissionError):
    """Raised when attempting to initialize or switch to LIVE mode without full authorization."""


class ExecutionMode(str, Enum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


# Hard ceilings that cannot be relaxed without explicit user code-level re-authorization (§8.1)
HARD_STOP_LOSS_TRIGGER_PCT = Decimal("0.01")          # -1% price trigger relative to weighted entry price
HARD_MAX_SINGLE_TRADE_RISK_PCT = Decimal("0.0025")    # <= 0.25% account equity risk per trade
HARD_MAX_CAPITAL_ALLOCATION_PCT = Decimal("0.10")     # <= 10% account equity per position
HARD_MAX_ROLLING_24H_LOSS_PCT = Decimal("0.01")       # 1.0% rolling 24h equity loss freeze threshold
HARD_C1_NET_PROFIT_TARGET_PCT = Decimal("0.015")      # +1.5% net profit target for C1 baseline


@dataclass(frozen=True)
class RiskConfig:
    """Immutable R-01 risk configuration enforcing non-relaxable governance boundaries."""

    version: str = "1.0.0-candidate"
    trading_symbol: str = TRADING_SYMBOL
    reference_symbol: str = REFERENCE_SYMBOL
    spot_only: bool = True
    allow_short: bool = False
    allow_leverage: bool = False
    allow_margin_or_loan: bool = False
    allow_averaging_down: bool = False
    stop_loss_trigger_pct: Decimal = HARD_STOP_LOSS_TRIGGER_PCT
    max_single_trade_risk_pct: Decimal = HARD_MAX_SINGLE_TRADE_RISK_PCT
    max_capital_allocation_pct: Decimal = HARD_MAX_CAPITAL_ALLOCATION_PCT
    max_rolling_24h_loss_pct: Decimal = HARD_MAX_ROLLING_24H_LOSS_PCT
    c1_net_profit_target_pct: Decimal = HARD_C1_NET_PROFIT_TARGET_PCT
    max_orders_per_10s: int = 10
    max_daily_entries: int = 12

    def __post_init__(self) -> None:
        if self.trading_symbol != TRADING_SYMBOL:
            raise RiskBoundaryViolationError(
                f"Only '{TRADING_SYMBOL}' is permitted as trading_symbol, got '{self.trading_symbol}'"
            )
        if self.reference_symbol != REFERENCE_SYMBOL:
            raise RiskBoundaryViolationError(
                f"Only '{REFERENCE_SYMBOL}' is permitted as reference_symbol, got '{self.reference_symbol}'"
            )
        if not self.spot_only or self.allow_short or self.allow_leverage or self.allow_margin_or_loan or self.allow_averaging_down:
            raise RiskBoundaryViolationError(
                "Spot-only upward breakout invariant violated: shorting, leverage, margin, loans, and averaging down are strictly forbidden."
            )

        sl = require_decimal(self.stop_loss_trigger_pct, "stop_loss_trigger_pct")
        sr = require_decimal(self.max_single_trade_risk_pct, "max_single_trade_risk_pct")
        ca = require_decimal(self.max_capital_allocation_pct, "max_capital_allocation_pct")
        dl = require_decimal(self.max_rolling_24h_loss_pct, "max_rolling_24h_loss_pct")
        c1 = require_decimal(self.c1_net_profit_target_pct, "c1_net_profit_target_pct")

        if sl <= Decimal("0") or sl > HARD_STOP_LOSS_TRIGGER_PCT:
            raise RiskBoundaryViolationError(
                f"stop_loss_trigger_pct ({sl}) cannot exceed hard ceiling {HARD_STOP_LOSS_TRIGGER_PCT} (1%)"
            )
        if sr <= Decimal("0") or sr > HARD_MAX_SINGLE_TRADE_RISK_PCT:
            raise RiskBoundaryViolationError(
                f"max_single_trade_risk_pct ({sr}) cannot exceed hard ceiling {HARD_MAX_SINGLE_TRADE_RISK_PCT} (0.25%)"
            )
        if ca <= Decimal("0") or ca > HARD_MAX_CAPITAL_ALLOCATION_PCT:
            raise RiskBoundaryViolationError(
                f"max_capital_allocation_pct ({ca}) cannot exceed hard ceiling {HARD_MAX_CAPITAL_ALLOCATION_PCT} (10%)"
            )
        if dl <= Decimal("0") or dl > HARD_MAX_ROLLING_24H_LOSS_PCT:
            raise RiskBoundaryViolationError(
                f"max_rolling_24h_loss_pct ({dl}) cannot exceed hard ceiling {HARD_MAX_ROLLING_24H_LOSS_PCT} (1%)"
            )
        if c1 <= Decimal("0"):
            raise RiskBoundaryViolationError("c1_net_profit_target_pct must be > 0")
        if self.max_orders_per_10s <= 0 or self.max_orders_per_10s > 50:
            raise RiskBoundaryViolationError("max_orders_per_10s must be in [1, 50]")


@dataclass(frozen=True)
class GoNoGoChecklist:
    """Section 16 non-bypassable 7-point GO/NO-GO gate before any real funds can be used."""

    account_spot_suiusdt_verified: bool = False
    protection_orders_fault_proven: bool = False
    user_risk_limits_explicitly_approved: bool = False
    anomaly_freeze_and_reconciliation_proven: bool = False
    out_of_sample_net_positive_proven: bool = False
    security_and_external_heartbeat_proven: bool = False
    independent_audit_record_signed: bool = False

    @property
    def all_passed(self) -> bool:
        return (
            self.account_spot_suiusdt_verified
            and self.protection_orders_fault_proven
            and self.user_risk_limits_explicitly_approved
            and self.anomaly_freeze_and_reconciliation_proven
            and self.out_of_sample_net_positive_proven
            and self.security_and_external_heartbeat_proven
            and self.independent_audit_record_signed
        )


@dataclass(frozen=True)
class LiveAuthorizationToken:
    """Explicit user authorization artifact required to unlock LIVE mode (§16)."""

    approver_id: str
    authorized_symbol: str
    max_capital_usdt: Decimal
    risk_config_version: str
    issued_at_utc: datetime
    expires_at_utc: datetime
    approval_statement_sha256: str

    def validate(self, expected_risk_version: str, now_utc: datetime | None = None) -> None:
        ensure_utc(self.issued_at_utc, "issued_at_utc")
        ensure_utc(self.expires_at_utc, "expires_at_utc")
        cap = require_decimal(self.max_capital_usdt, "max_capital_usdt")
        if not self.approver_id.strip():
            raise LiveAuthorizationError("approver_id cannot be empty")
        if self.authorized_symbol != TRADING_SYMBOL:
            raise LiveAuthorizationError(f"LiveAuthorizationToken only valid for {TRADING_SYMBOL}")
        if cap <= Decimal("0"):
            raise LiveAuthorizationError("max_capital_usdt must be > 0")
        if self.risk_config_version != expected_risk_version:
            raise LiveAuthorizationError(
                f"Token risk_config_version '{self.risk_config_version}' does not match '{expected_risk_version}'"
            )
        ref_now = now_utc or utc_now()
        ensure_utc(ref_now, "now_utc")
        if ref_now >= self.expires_at_utc or ref_now < self.issued_at_utc:
            raise LiveAuthorizationError("LiveAuthorizationToken is expired or not yet valid")
        expected_digest = self.compute_digest(
            approver_id=self.approver_id,
            authorized_symbol=self.authorized_symbol,
            max_capital_usdt=cap,
            risk_config_version=self.risk_config_version,
            issued_at_utc=self.issued_at_utc,
            expires_at_utc=self.expires_at_utc,
        )
        if self.approval_statement_sha256 != expected_digest:
            raise LiveAuthorizationError("LiveAuthorizationToken SHA-256 digest mismatch")

    @staticmethod
    def compute_digest(
        approver_id: str,
        authorized_symbol: str,
        max_capital_usdt: Decimal,
        risk_config_version: str,
        issued_at_utc: datetime,
        expires_at_utc: datetime,
    ) -> str:
        payload = (
            f"SUI-QUANT-LIVE-AUTH|{approver_id}|{authorized_symbol}|"
            f"{max_capital_usdt}|{risk_config_version}|"
            f"{issued_at_utc.isoformat()}|{expires_at_utc.isoformat()}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SystemConfig:
    """Top-level system configuration defaulting strictly to PAPER mode (QA-14)."""

    mode: ExecutionMode = ExecutionMode.PAPER
    risk: RiskConfig = field(default_factory=RiskConfig)
    allow_live_trading: bool = False
    go_no_go: GoNoGoChecklist = field(default_factory=GoNoGoChecklist)
    live_auth_token: LiveAuthorizationToken | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ExecutionMode):
            raise TypeError(f"mode must be ExecutionMode, got {self.mode}")
        if self.mode == ExecutionMode.LIVE:
            self.verify_live_gate()

    def verify_live_gate(self, now_utc: datetime | None = None) -> None:
        """Enforce non-bypassable multi-gate checks before permitting LIVE mode."""
        if not self.allow_live_trading:
            raise LiveAuthorizationError(
                "LIVE mode is strictly disabled by default (allow_live_trading=False)."
            )
        if not self.go_no_go.all_passed:
            raise LiveAuthorizationError(
                "LIVE mode blocked: Section 16 GO/NO-GO checklist is incomplete (NO-GO)."
            )
        if self.live_auth_token is None:
            raise LiveAuthorizationError(
                "LIVE mode blocked: Explicit user LiveAuthorizationToken is missing."
            )
        self.live_auth_token.validate(
            expected_risk_version=self.risk.version,
            now_utc=now_utc,
        )
