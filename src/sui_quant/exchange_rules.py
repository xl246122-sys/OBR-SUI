"""Binance Spot SUIUSDT and BTCUSDT exchange filter validation and Decimal quantization (§5, §6.2)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

from sui_quant.contracts import require_decimal


class ExchangeFilterError(ValueError):
    """Raised when an order price, quantity, or notional violates Binance Spot symbol filters."""


@dataclass(frozen=True)
class SymbolFilters:
    """Verified Binance Spot exchangeInfo filters for a trading symbol."""

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
        steps = (q / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.step_size).quantize(self.step_size)

    def validate_order(self, price: Decimal, qty: Decimal) -> None:
        """Validate price, quantity, and notional against PRICE_FILTER, LOT_SIZE, and NOTIONAL."""
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


# Live verified filters from GET https://api.binance.com/api/v3/exchangeInfo (2026-10-31 UTC)
SUIUSDT_FILTERS = SymbolFilters(
    symbol="SUIUSDT",
    min_price=Decimal("0.00010000"),
    max_price=Decimal("1000.00000000"),
    tick_size=Decimal("0.0001"),
    min_qty=Decimal("0.1"),
    max_qty=Decimal("92141578.0"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5.0"),
    max_notional=Decimal("9000000.0"),
    max_num_orders=200,
    max_num_algo_orders=5,
    max_num_order_lists=20,
)

BTCUSDT_FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    min_price=Decimal("0.01000000"),
    max_price=Decimal("1000000.00000000"),
    tick_size=Decimal("0.01"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("9000.0"),
    step_size=Decimal("0.00001"),
    min_notional=Decimal("5.0"),
    max_notional=Decimal("9000000.0"),
    max_num_orders=200,
    max_num_algo_orders=5,
    max_num_order_lists=20,
)


def compute_c1_net_target_sell_price(
    avg_buy_price: Decimal,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
    target_net_return: Decimal = Decimal("0.015"),
    filters: SymbolFilters = SUIUSDT_FILTERS,
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
    filters: SymbolFilters = SUIUSDT_FILTERS,
) -> Decimal:
    """Compute -1% stop-loss trigger price relative to actual fill weighted entry price (§6.1)."""
    p_buy = require_decimal(avg_buy_price, "avg_buy_price")
    sl = require_decimal(stop_loss_pct, "stop_loss_pct")
    if p_buy <= Decimal("0") or sl <= Decimal("0") or sl >= Decimal("1"):
        raise ValueError("Invalid avg_buy_price or stop_loss_pct")
    raw_trigger = p_buy * (Decimal("1") - sl)
    return filters.quantize_price_down(raw_trigger)
