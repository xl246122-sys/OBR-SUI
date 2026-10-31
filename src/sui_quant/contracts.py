"""Standard cross-module data contracts for SUI-Quant V1.0 (§7.1).

All monetary, price, quantity, fee, and probability fields enforce strict Decimal types
(rejecting binary float). All timestamps enforce explicit UTC timezone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from sui_quant.utils.clock import ensure_utc


ALLOWED_SYMBOLS = frozenset({"SUIUSDT", "BTCUSDT"})
TRADING_SYMBOL = "SUIUSDT"
REFERENCE_SYMBOL = "BTCUSDT"


def require_decimal(value: Any, field_name: str) -> Decimal:
    """Validate that a numeric field is a Decimal (or str/int convertible), strictly rejecting float."""
    if isinstance(value, float):
        raise TypeError(
            f"Field '{field_name}' received binary float ({value}); strict Decimal is required."
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"Field '{field_name}' must be a finite Decimal, got {value}")
        return value
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        dec = Decimal(str(value))
        if not dec.is_finite():
            raise ValueError(f"Field '{field_name}' must be a finite Decimal, got {value}")
        return dec
    raise TypeError(f"Field '{field_name}' must be Decimal, got {type(value).__name__}")


class DataQuality(str, Enum):
    OK = "OK"
    STALE = "STALE"
    GAP_DETECTED = "GAP_DETECTED"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    DUPLICATE = "DUPLICATE"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"


class RegimeType(str, Enum):
    UP_TREND = "UP_TREND"
    CONSOLIDATION = "CONSOLIDATION"
    DOWN_TREND = "DOWN_TREND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    DATA_UNHEALTHY = "DATA_UNHEALTHY"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    LIMIT_MAKER = "LIMIT_MAKER"


class AlertSeverity(str, Enum):
    INFO = "INFO"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    symbol: str
    source: str
    exchange_time_utc: datetime
    recv_time_utc: datetime
    recv_monotonic_ns: int
    schema_version: str
    payload: Mapping[str, Any]
    data_quality: DataQuality = DataQuality.OK
    seq_or_update_id: int | None = None

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Unsupported symbol '{self.symbol}'. Allowed: {sorted(ALLOWED_SYMBOLS)}")
        ensure_utc(self.exchange_time_utc, "exchange_time_utc")
        ensure_utc(self.recv_time_utc, "recv_time_utc")
        if self.recv_monotonic_ns <= 0:
            raise ValueError("recv_monotonic_ns must be > 0")


@dataclass(frozen=True)
class MarketFeatures:
    feature_time_utc: datetime
    horizon: str
    symbol: str
    complete_bar_only: bool
    features: Mapping[str, Decimal]
    data_version: str

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Unsupported symbol '{self.symbol}'")
        ensure_utc(self.feature_time_utc, "feature_time_utc")
        for k, v in self.features.items():
            require_decimal(v, f"features[{k}]")


@dataclass(frozen=True)
class RegimeDecision:
    decision_id: str
    regime: RegimeType
    confidence: Decimal
    allow_entry: bool
    reason_codes: tuple[str, ...]
    valid_until: datetime
    model_version: str

    def __post_init__(self) -> None:
        conf = require_decimal(self.confidence, "confidence")
        if conf < Decimal("0") or conf > Decimal("1"):
            raise ValueError("confidence must be in [0, 1]")
        ensure_utc(self.valid_until, "valid_until")
        if self.regime in {RegimeType.DOWN_TREND, RegimeType.HIGH_VOLATILITY, RegimeType.DATA_UNHEALTHY}:
            if self.allow_entry:
                raise ValueError(f"Regime {self.regime.value} must never allow new entry")


@dataclass(frozen=True)
class BreakoutProposal:
    signal_id: str
    candle_close_utc: datetime
    barrier_ref: Decimal
    p_win: Decimal
    p_stop: Decimal
    p_timeout: Decimal
    ev_net: Decimal
    confidence: Decimal
    feature_snapshot_hash: str
    model_version: str
    expiry: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.candle_close_utc, "candle_close_utc")
        ensure_utc(self.expiry, "expiry")
        ref = require_decimal(self.barrier_ref, "barrier_ref")
        if ref <= Decimal("0"):
            raise ValueError("barrier_ref must be > 0")
        pw = require_decimal(self.p_win, "p_win")
        ps = require_decimal(self.p_stop, "p_stop")
        pt = require_decimal(self.p_timeout, "p_timeout")
        require_decimal(self.ev_net, "ev_net")
        conf = require_decimal(self.confidence, "confidence")
        for name, p in (("p_win", pw), ("p_stop", ps), ("p_timeout", pt), ("confidence", conf)):
            if p < Decimal("0") or p > Decimal("1"):
                raise ValueError(f"{name} must be in [0, 1], got {p}")
        prob_sum = pw + ps + pt
        if abs(prob_sum - Decimal("1")) > Decimal("0.000001"):
            raise ValueError(f"p_win + p_stop + p_timeout must equal 1, got {prob_sum}")


@dataclass(frozen=True)
class RiskVerdict:
    risk_check_id: str
    signal_id: str
    allowed: bool
    max_qty: Decimal
    max_notional: Decimal
    risk_config_version: str
    reject_reasons: tuple[str, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.evaluated_at, "evaluated_at")
        mq = require_decimal(self.max_qty, "max_qty")
        mn = require_decimal(self.max_notional, "max_notional")
        if mq < Decimal("0") or mn < Decimal("0"):
            raise ValueError("max_qty and max_notional must be >= 0")
        if not self.allowed and not self.reject_reasons:
            raise ValueError("Rejected RiskVerdict must provide at least one reject_reason")


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    signal_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    tif: TimeInForce
    price_cap: Decimal
    expiration: datetime
    strategy_version: str
    risk_check_id: str

    def __post_init__(self) -> None:
        if self.symbol != TRADING_SYMBOL:
            raise ValueError(
                f"OrderIntent symbol must be strictly '{TRADING_SYMBOL}', got '{self.symbol}'"
            )
        ensure_utc(self.expiration, "expiration")
        p = require_decimal(self.price, "price")
        q = require_decimal(self.qty, "qty")
        cap = require_decimal(self.price_cap, "price_cap")
        if p <= Decimal("0") or q <= Decimal("0") or cap <= Decimal("0"):
            raise ValueError("OrderIntent price, qty, and price_cap must be > 0")
        if self.side == OrderSide.BUY and p > cap:
            raise ValueError(f"BUY OrderIntent price ({p}) exceeds price_cap ({cap})")


@dataclass(frozen=True)
class OrderEvent:
    client_order_id: str
    exchange_status: str
    filled_qty: Decimal
    cumulative_quote_qty: Decimal
    fee_asset: str
    fee_amount: Decimal
    trade_ids: tuple[int, ...]
    source_timestamp: datetime
    exchange_order_id: int | None = None

    def __post_init__(self) -> None:
        ensure_utc(self.source_timestamp, "source_timestamp")
        fq = require_decimal(self.filled_qty, "filled_qty")
        cq = require_decimal(self.cumulative_quote_qty, "cumulative_quote_qty")
        fa = require_decimal(self.fee_amount, "fee_amount")
        if fq < Decimal("0") or cq < Decimal("0") or fa < Decimal("0"):
            raise ValueError("OrderEvent quantities and fees must be >= 0")


@dataclass(frozen=True)
class PositionLedger:
    position_id: str
    symbol: str
    actual_qty: Decimal
    avg_fill_price: Decimal
    fees: Decimal
    reserved_qty: Decimal
    protection_orders: tuple[str, ...]
    exit_strategy_version: str
    risk_config_version: str

    def __post_init__(self) -> None:
        if self.symbol != TRADING_SYMBOL:
            raise ValueError(f"PositionLedger symbol must be '{TRADING_SYMBOL}'")
        aq = require_decimal(self.actual_qty, "actual_qty")
        afp = require_decimal(self.avg_fill_price, "avg_fill_price")
        f = require_decimal(self.fees, "fees")
        rq = require_decimal(self.reserved_qty, "reserved_qty")
        if aq < Decimal("0") or afp < Decimal("0") or f < Decimal("0") or rq < Decimal("0"):
            raise ValueError("PositionLedger numeric fields must be >= 0")
        if rq > aq:
            raise ValueError(
                f"reserved_qty ({rq}) cannot exceed actual_qty ({aq}) — duplicate order locking is forbidden"
            )

    @property
    def stop_loss_trigger_price(self) -> Decimal:
        """Baseline -1% stop trigger price relative to actual fill weighted entry price (§6.1)."""
        return self.avg_fill_price * Decimal("0.99")


@dataclass(frozen=True)
class AlertEvent:
    alert_id: str
    severity: AlertSeverity
    source: str
    first_seen_utc: datetime
    dedup_key: str
    event_state: str
    actions_taken: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ensure_utc(self.first_seen_utc, "first_seen_utc")


@dataclass(frozen=True)
class ModelManifest:
    model_id: str
    version: str
    features_schema_hash: str
    train_end_utc: datetime
    test_periods: tuple[str, ...]
    calibration_metrics: Mapping[str, Decimal]
    cost_assumptions: Mapping[str, Decimal]
    sha256: str
    risk_compatibility: str
    rollback_ref: str

    def __post_init__(self) -> None:
        ensure_utc(self.train_end_utc, "train_end_utc")
        if len(self.sha256) != 64:
            raise ValueError("ModelManifest sha256 must be a 64-character hex digest")
        if not self.rollback_ref:
            raise ValueError("ModelManifest must specify a non-empty rollback_ref")
        for k, v in self.calibration_metrics.items():
            require_decimal(v, f"calibration_metrics[{k}]")
        for k, v in self.cost_assumptions.items():
            require_decimal(v, f"cost_assumptions[{k}]")
