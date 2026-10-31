"""Binance Spot SUIUSDT and BTCUSDT exchange filter validation, dynamic refresh guards, and dust math (§5, §6.2).

P0 Remediation:
1. Explicitly distinguishes `P0_STATIC_SNAPSHOT` filters (captured during P0 read-only probe) from
   `DYNAMIC_EXCHANGE_INFO` filters fetched at runtime during `BOOT -> SYNCING`. Runtime pre-trade
   execution (`assert_fresh_for_execution`) rejects static snapshots or stale filter caches.
2. Implements `compute_post_fill_sellable_position` and `validate_pre_buy_protectability` covering
   LOT_SIZE `step_size`, fee asset (`SUI` vs `BNB` vs `USDT`), residual dust, `min_notional`, and
   insufficient sellable quantity traps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from enum import Enum
from typing import Any, Mapping

from sui_quant.contracts import require_decimal
from sui_quant.utils.clock import ensure_utc


class ExchangeFilterError(ValueError):
    """Raised when an order price, quantity, or notional violates Binance Spot symbol filters."""


class StaleExchangeFilterError(ExchangeFilterError):
    """Raised when attempting runtime order execution using a static P0 snapshot or expired filter cache."""


class FilterSourceType(str, Enum):
    P0_STATIC_SNAPSHOT = "P0_STATIC_SNAPSHOT"
    DYNAMIC_EXCHANGE_INFO = "DYNAMIC_EXCHANGE_INFO"


@dataclass(frozen=True)
class PostFillSellableAllocation:
    """Result of computing net protectable/sellable quantity after buy fill and fee deduction."""

    gross_filled_qty: Decimal
    fee_asset: str
    fee_amount: Decimal
    net_base_qty: Decimal
    sellable_qty: Decimal
    dust_qty: Decimal
    stop_trigger_price: Decimal
    stop_notional_usdt: Decimal
    is_protectable: bool
    unprotectable_reason: str = "NONE"


@dataclass(frozen=True)
class SymbolFilters:
    """Binance Spot exchangeInfo filters with source and freshness metadata."""

    symbol: str
    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_notional: Decimal
    max_notional: Decimal
    max_num_orders: int
    max_num_algo_orders: int
    max_num_order_lists: int
    source: FilterSourceType = FilterSourceType.P0_STATIC_SNAPSHOT
    fetched_at_utc: datetime = datetime(2026, 10, 31, 4, 3, 47, tzinfo=timezone.utc)
    status: str = "TRADING"
    oco_allowed: bool = True
    oto_allowed: bool = True
    allow_trailing_stop: bool = True
    cancel_replace_allowed: bool = True

    def __post_init__(self) -> None:
        ensure_utc(self.fetched_at_utc, "fetched_at_utc")

    def assert_fresh_for_execution(
        self,
        now_utc: datetime,
        max_age_seconds: int = 3600,
    ) -> None:
        """Verify that filters come from a live runtime `exchangeInfo` sync and are not stale (§6.1, §16)."""
        ensure_utc(now_utc, "now_utc")
        if self.source != FilterSourceType.DYNAMIC_EXCHANGE_INFO:
            raise StaleExchangeFilterError(
                f"{self.symbol} filters are from {self.source.value}; runtime execution requires "
                "a fresh DYNAMIC_EXCHANGE_INFO sync during SYNCING before placing orders."
            )
        if self.status != "TRADING":
            raise ExchangeFilterError(f"{self.symbol} market status is '{self.status}', not TRADING")
        age_sec = (now_utc - self.fetched_at_utc).total_seconds()
        if age_sec < -5 or age_sec > max_age_seconds:
            raise StaleExchangeFilterError(
                f"{self.symbol} dynamic filters are stale (age={age_sec:.1f}s > max_age={max_age_seconds}s)"
            )

    @classmethod
    def from_binance_exchange_info_symbol(
        cls,
        symbol_payload: Mapping[str, Any],
        fetched_at_utc: datetime,
    ) -> SymbolFilters:
        """Parse a symbol entry from `GET /api/v3/exchangeInfo` into a dynamic `SymbolFilters` instance."""
        ensure_utc(fetched_at_utc, "fetched_at_utc")
        filters_by_type = {f["filterType"]: f for f in symbol_payload.get("filters", [])}
        pf = filters_by_type["PRICE_FILTER"]
        ls = filters_by_type["LOT_SIZE"]
        nf = filters_by_type.get("NOTIONAL") or filters_by_type.get("MIN_NOTIONAL", {})
        max_orders = int(filters_by_type.get("MAX_NUM_ORDERS", {}).get("maxNumOrders", 200))
        max_algo = int(filters_by_type.get("MAX_NUM_ALGO_ORDERS", {}).get("maxNumAlgoOrders", 5))
        max_lists = int(filters_by_type.get("MAX_NUM_ORDER_LISTS", {}).get("maxNumOrderLists", 20))
        return cls(
            symbol=str(symbol_payload["symbol"]),
            min_price=Decimal(str(pf["minPrice"])).normalize(),
            max_price=Decimal(str(pf["maxPrice"])).normalize(),
            tick_size=Decimal(str(pf["tickSize"])).normalize(),
            min_qty=Decimal(str(ls["minQty"])).normalize(),
            max_qty=Decimal(str(ls["maxQty"])).normalize(),
            step_size=Decimal(str(ls["stepSize"])).normalize(),
            min_notional=Decimal(str(nf.get("minNotional", "5.0"))).normalize(),
            max_notional=Decimal(str(nf.get("maxNotional", "9000000.0"))).normalize(),
            max_num_orders=max_orders,
            max_num_algo_orders=max_algo,
            max_num_order_lists=max_lists,
            source=FilterSourceType.DYNAMIC_EXCHANGE_INFO,
            fetched_at_utc=fetched_at_utc,
            status=str(symbol_payload.get("status", "TRADING")),
            oco_allowed=bool(symbol_payload.get("ocoAllowed", False)),
            oto_allowed=bool(symbol_payload.get("otoAllowed", False)),
            allow_trailing_stop=bool(symbol_payload.get("allowTrailingStop", False)),
            cancel_replace_allowed=bool(symbol_payload.get("cancelReplaceAllowed", False)),
        )

    def quantize_price_down(self, price: Decimal) -> Decimal:
        p = require_decimal(price, "price")
        steps = (p / self.tick_size).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.tick_size).quantize(self.tick_size)

    def quantize_price_up(self, price: Decimal) -> Decimal:
        p = require_decimal(price, "price")
        steps = (p / self.tick_size).to_integral_value(rounding=ROUND_CEILING)
        return (steps * self.tick_size).quantize(self.tick_size)

    def quantize_qty_down(self, qty: Decimal) -> Decimal:
        """Round quantity strictly DOWN to LOT_SIZE step_size to prevent insufficient balance / dust errors."""
        q = require_decimal(qty, "qty")
        if q <= Decimal("0"):
            return Decimal("0").quantize(self.step_size)
        steps = (q / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.step_size).quantize(self.step_size)

    def validate_order(
        self,
        price: Decimal,
        qty: Decimal,
        require_dynamic_freshness: bool = False,
        now_utc: datetime | None = None,
        max_age_seconds: int = 3600,
    ) -> None:
        """Validate price, quantity, and notional against PRICE_FILTER, LOT_SIZE, and NOTIONAL."""
        if require_dynamic_freshness:
            if now_utc is None:
                raise ValueError("now_utc is required when require_dynamic_freshness=True")
            self.assert_fresh_for_execution(now_utc=now_utc, max_age_seconds=max_age_seconds)

        p = require_decimal(price, "price")
        q = require_decimal(qty, "qty")
        if p < self.min_price or p > self.max_price:
            raise ExchangeFilterError(
                f"{self.symbol} price {p} outside [{self.min_price}, {self.max_price}]"
            )
        if (p % self.tick_size) != Decimal("0"):
            raise ExchangeFilterError(
                f"{self.symbol} price {p} is not a multiple of tick_size {self.tick_size}"
            )
        if q < self.min_qty or q > self.max_qty:
            raise ExchangeFilterError(
                f"{self.symbol} qty {q} outside [{self.min_qty}, {self.max_qty}]"
            )
        if (q % self.step_size) != Decimal("0"):
            raise ExchangeFilterError(
                f"{self.symbol} qty {q} is not a multiple of step_size {self.step_size}"
            )
        notional = p * q
        if notional < self.min_notional or notional > self.max_notional:
            raise ExchangeFilterError(
                f"{self.symbol} notional {notional} outside [{self.min_notional}, {self.max_notional}]"
            )


# P0 Static Snapshot filters captured from GET https://api.binance.com/api/v3/exchangeInfo (2026-10-31T04:03:47Z)
# Marked as FilterSourceType.P0_STATIC_SNAPSHOT; runtime execution must refresh via from_binance_exchange_info_symbol!
P0_SNAPSHOT_SUIUSDT_FILTERS = SymbolFilters(
    symbol="SUIUSDT",
    min_price=Decimal("0.0001"),
    max_price=Decimal("1000.0"),
    tick_size=Decimal("0.0001"),
    min_qty=Decimal("0.1"),
    max_qty=Decimal("92141578.0"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5.0"),
    max_notional=Decimal("9000000.0"),
    max_num_orders=200,
    max_num_algo_orders=5,
    max_num_order_lists=20,
    source=FilterSourceType.P0_STATIC_SNAPSHOT,
    fetched_at_utc=datetime(2026, 10, 31, 4, 3, 47, tzinfo=timezone.utc),
)

P0_SNAPSHOT_BTCUSDT_FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    min_price=Decimal("0.01"),
    max_price=Decimal("1000000.0"),
    tick_size=Decimal("0.01"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("9000.0"),
    step_size=Decimal("0.00001"),
    min_notional=Decimal("5.0"),
    max_notional=Decimal("9000000.0"),
    max_num_orders=200,
    max_num_algo_orders=5,
    max_num_order_lists=20,
    source=FilterSourceType.P0_STATIC_SNAPSHOT,
    fetched_at_utc=datetime(2026, 10, 31, 4, 3, 47, tzinfo=timezone.utc),
)

# Backwards-compatible aliases for P0 baseline references
SUIUSDT_FILTERS = P0_SNAPSHOT_SUIUSDT_FILTERS
BTCUSDT_FILTERS = P0_SNAPSHOT_BTCUSDT_FILTERS


def compute_c1_net_target_sell_price(
    avg_buy_price: Decimal,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
    target_net_return: Decimal = Decimal("0.015"),
    filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
) -> Decimal:
    """Compute C1 target limit sell price achieving `target_net_return` (+1.5%) after buy & sell fees (§6.2).

    Formula: P_sell = P_buy * (1 + f_buy) * (1 + target_net_return) / (1 - f_sell)
    Quantized UP to the nearest tick_size so net return after fees is at least target_net_return.
    """
    p_buy = require_decimal(avg_buy_price, "avg_buy_price")
    f_buy = require_decimal(buy_fee_rate, "buy_fee_rate")
    f_sell = require_decimal(sell_fee_rate, "sell_fee_rate")
    r_target = require_decimal(target_net_return, "target_net_return")
    if p_buy <= Decimal("0"):
        raise ValueError("avg_buy_price must be > 0")
    if f_buy < Decimal("0") or f_sell < Decimal("0") or f_sell >= Decimal("1"):
        raise ValueError("Invalid fee rates")
    raw_sell_price = (p_buy * (Decimal("1") + f_buy) * (Decimal("1") + r_target)) / (
        Decimal("1") - f_sell
    )
    return filters.quantize_price_up(raw_sell_price)


def compute_stop_loss_trigger_price(
    avg_buy_price: Decimal,
    stop_loss_pct: Decimal = Decimal("0.01"),
    filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
) -> Decimal:
    """Compute -1% stop-loss trigger price relative to actual fill weighted entry price (§6.1)."""
    p_buy = require_decimal(avg_buy_price, "avg_buy_price")
    sl = require_decimal(stop_loss_pct, "stop_loss_pct")
    if p_buy <= Decimal("0") or sl <= Decimal("0") or sl >= Decimal("1"):
        raise ValueError("Invalid avg_buy_price or stop_loss_pct")
    raw_trigger = p_buy * (Decimal("1") - sl)
    return filters.quantize_price_down(raw_trigger)


def compute_post_fill_sellable_position(
    gross_filled_qty: Decimal,
    avg_fill_price: Decimal,
    fee_asset: str,
    fee_amount: Decimal,
    stop_loss_pct: Decimal = Decimal("0.01"),
    filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
) -> PostFillSellableAllocation:
    """Calculate net base quantity, LOT_SIZE-rounded sellable quantity, residual dust, and protectability.

    Addresses:
    - Base-asset (`SUI`) fee deduction vs third-party (`BNB`) or quote (`USDT`) fee deduction
    - Residual dust (`dust_qty = net_base_qty - sellable_qty`)
    - Partial fill or small order falling below `min_qty` (`0.1 SUI`) or `min_notional` (`5.0 USDT`)
      at the -1% stop trigger price.
    """
    gross_q = require_decimal(gross_filled_qty, "gross_filled_qty")
    avg_p = require_decimal(avg_fill_price, "avg_fill_price")
    fee_amt = require_decimal(fee_amount, "fee_amount")
    if gross_q <= Decimal("0") or avg_p <= Decimal("0") or fee_amt < Decimal("0"):
        raise ValueError("Invalid gross_filled_qty, avg_fill_price, or fee_amount")

    asset_upper = fee_asset.strip().upper()
    base_asset = filters.symbol.replace("USDT", "")
    if asset_upper == base_asset:
        if fee_amt >= gross_q:
            raise ValueError("fee_amount cannot be >= gross_filled_qty")
        net_base = gross_q - fee_amt
    elif asset_upper in {"BNB", "USDT", "FDUSD", "USDC"}:
        net_base = gross_q
    else:
        raise ValueError(f"Unsupported fee_asset '{fee_asset}'")

    sellable_q = filters.quantize_qty_down(net_base)
    dust_q = net_base - sellable_q
    stop_trigger = compute_stop_loss_trigger_price(
        avg_buy_price=avg_p,
        stop_loss_pct=stop_loss_pct,
        filters=filters,
    )
    stop_notional = sellable_q * stop_trigger

    if sellable_q < filters.min_qty:
        return PostFillSellableAllocation(
            gross_filled_qty=gross_q,
            fee_asset=asset_upper,
            fee_amount=fee_amt,
            net_base_qty=net_base,
            sellable_qty=sellable_q,
            dust_qty=dust_q,
            stop_trigger_price=stop_trigger,
            stop_notional_usdt=stop_notional,
            is_protectable=False,
            unprotectable_reason="SELLABLE_QTY_BELOW_MIN_QTY",
        )

    if stop_notional < filters.min_notional:
        return PostFillSellableAllocation(
            gross_filled_qty=gross_q,
            fee_asset=asset_upper,
            fee_amount=fee_amt,
            net_base_qty=net_base,
            sellable_qty=sellable_q,
            dust_qty=dust_q,
            stop_trigger_price=stop_trigger,
            stop_notional_usdt=stop_notional,
            is_protectable=False,
            unprotectable_reason="STOP_NOTIONAL_BELOW_MIN_NOTIONAL",
        )

    return PostFillSellableAllocation(
        gross_filled_qty=gross_q,
        fee_asset=asset_upper,
        fee_amount=fee_amt,
        net_base_qty=net_base,
        sellable_qty=sellable_q,
        dust_qty=dust_q,
        stop_trigger_price=stop_trigger,
        stop_notional_usdt=stop_notional,
        is_protectable=True,
        unprotectable_reason="NONE",
    )


def validate_pre_buy_protectability(
    planned_buy_price: Decimal,
    planned_buy_qty: Decimal,
    worst_case_base_fee_rate: Decimal = Decimal("0.001"),
    stop_loss_pct: Decimal = Decimal("0.01"),
    notional_safety_buffer_pct: Decimal = Decimal("0.05"),
    filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
) -> PostFillSellableAllocation:
    """Pre-trade R-01/B-01 guard: verify that BEFORE a buy order is submitted, the worst-case net
    position (assuming fee is paid in SUI because BNB balance might be 0) will still be protectable
    by a valid stop-loss order at -1% with a safety notional margin above `min_notional`.
    """
    filters.validate_order(price=planned_buy_price, qty=planned_buy_qty)
    fee_rate = require_decimal(worst_case_base_fee_rate, "worst_case_base_fee_rate")
    buffer_pct = require_decimal(notional_safety_buffer_pct, "notional_safety_buffer_pct")
    if fee_rate < Decimal("0") or fee_rate >= Decimal("0.1"):
        raise ValueError("worst_case_base_fee_rate out of bounds")
    if buffer_pct < Decimal("0"):
        raise ValueError("notional_safety_buffer_pct must be >= 0")

    worst_case_fee_sui = planned_buy_qty * fee_rate
    alloc = compute_post_fill_sellable_position(
        gross_filled_qty=planned_buy_qty,
        avg_fill_price=planned_buy_price,
        fee_asset="SUI",
        fee_amount=worst_case_fee_sui,
        stop_loss_pct=stop_loss_pct,
        filters=filters,
    )
    if not alloc.is_protectable:
        raise ExchangeFilterError(
            f"Pre-buy protectability check failed ({alloc.unprotectable_reason}): "
            f"planned_qty={planned_buy_qty} at price={planned_buy_price} leaves sellable_qty={alloc.sellable_qty} "
            f"and stop_notional={alloc.stop_notional_usdt} USDT (< min_notional {filters.min_notional} USDT)."
        )
    min_safe_notional = filters.min_notional * (Decimal("1") + buffer_pct)
    if alloc.stop_notional_usdt < min_safe_notional:
        raise ExchangeFilterError(
            f"Pre-buy stop_notional ({alloc.stop_notional_usdt} USDT) is below safety buffer threshold "
            f"({min_safe_notional} USDT) required to survive stop-loss price slippage."
        )
    return alloc
