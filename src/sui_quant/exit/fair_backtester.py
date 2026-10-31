"""Two-Layer Fair Exit Strategy Comparison Backtester (C1 vs C2 vs C3) (§6, §14, QA-06, QA-07, QA-13).

Enforces Strict Experimental Fairness (QA-13):
- Identical entry timestamps (`entry_time_utc`)
- Identical entry fill prices (`entry_fill_price`)
- Identical R-01 risk-sized buy quantities (`r01_gross_buy_qty`) and post-fee `sellable_qty`
- Identical initial `-1.0%` stop-loss trigger prices (`initial_stop_price`)
- Identical commission asset and buy/sell fee rates
- Two-Layer Evaluation (§14.1):
  1. `BAR_LEVEL_PESSIMISTIC`: 5m bar sequence with strict pessimistic intra-bar tie-breaking (if a bar touches
     both active stop and take-profit target, `STOP` is triggered first).
  2. `ORDERBOOK_MICROSTRUCTURE_STRESS`: Adds B-01 entry microstructure friction (`entry_slippage + markout_5s`),
     orderbook depth sweep slippage on stop/defensive exits, and flash gap-down penetration (`> 1%` actual loss, QA-07).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
from enum import Enum
from typing import Mapping, Sequence

from sui_quant.config import ExecutionMode
from sui_quant.contracts import (
    TRADING_SYMBOL,
    PositionLedger,
    RegimeType,
    require_decimal,
)
from sui_quant.exchange_rules import (
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
    compute_post_fill_sellable_position,
    compute_stop_loss_trigger_price,
)
from sui_quant.exit.c01_strategies import (
    C1FixedNetTargetExit,
    C2GridTrailingExit,
    C3AdaptiveHybridExit,
    ExitFillSlice,
    ExitSignalAction,
    ExitStrategyId,
    OcoProtectionCoordinator,
)
from sui_quant.execution.paper_exchange import PaperOrderbookSimulator
from sui_quant.models.a02_breakout import StatisticalVerdict
from sui_quant.utils.clock import ensure_utc


QUANT_8 = Decimal("0.00000001")
QUANT_4 = Decimal("0.0001")


def _q8(val: Decimal) -> Decimal:
    return val.quantize(QUANT_8, rounding=ROUND_HALF_EVEN)


class BacktestLayer(str, Enum):
    BAR_LEVEL_PESSIMISTIC = "BAR_LEVEL_PESSIMISTIC"
    ORDERBOOK_MICROSTRUCTURE_STRESS = "ORDERBOOK_MICROSTRUCTURE_STRESS"


@dataclass(frozen=True)
class ExitPathBar:
    """Single future 5m bar along a post-entry holding trajectory (up to 36 bars = 3 hours)."""

    bar_index: int
    close_time_utc: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    regime: RegimeType = RegimeType.UP_TREND
    obi_5: Decimal = Decimal("0.10")
    btc_flash_drop_flag: bool = False
    stop_slippage_bps: Decimal = Decimal("5.0")
    market_exit_slippage_bps: Decimal = Decimal("2.0")
    gap_down_beyond_stop_limit: bool = False

    def __post_init__(self) -> None:
        ensure_utc(self.close_time_utc, "close_time_utc")
        for name, val in (
            ("open_price", self.open_price),
            ("high_price", self.high_price),
            ("low_price", self.low_price),
            ("close_price", self.close_price),
            ("obi_5", self.obi_5),
            ("stop_slippage_bps", self.stop_slippage_bps),
            ("market_exit_slippage_bps", self.market_exit_slippage_bps),
        ):
            require_decimal(val, name)
        if self.high_price < self.low_price:
            raise ValueError(f"high_price ({self.high_price}) < low_price ({self.low_price})")


@dataclass(frozen=True)
class EntryTradeContext:
    """Immutable entry specification shared identically across C1, C2, and C3 (QA-13)."""

    trade_id: str
    entry_time_utc: datetime
    entry_fill_price: Decimal
    r01_gross_buy_qty: Decimal
    fee_asset: str
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    entry_microstructure_friction_bps: Decimal
    future_bars: tuple[ExitPathBar, ...]

    def __post_init__(self) -> None:
        ensure_utc(self.entry_time_utc, "entry_time_utc")
        require_decimal(self.entry_fill_price, "entry_fill_price")
        require_decimal(self.r01_gross_buy_qty, "r01_gross_buy_qty")
        require_decimal(self.buy_fee_rate, "buy_fee_rate")
        require_decimal(self.sell_fee_rate, "sell_fee_rate")
        require_decimal(self.entry_microstructure_friction_bps, "entry_microstructure_friction_bps")
        if not self.future_bars:
            raise ValueError("EntryTradeContext requires at least 1 future bar")


@dataclass(frozen=True)
class TradeExitSimulationResult:
    """Per-trade execution result for a specific exit strategy (C1, C2, or C3)."""

    trade_id: str
    strategy_id: ExitStrategyId
    layer: BacktestLayer
    entry_time_utc: datetime
    exit_time_utc: datetime
    entry_fill_price: Decimal
    effective_entry_price_with_friction: Decimal
    initial_stop_price: Decimal
    r01_gross_buy_qty: Decimal
    sellable_qty: Decimal
    weighted_exit_price: Decimal
    buy_cost_usdt_with_fee: Decimal
    sell_net_proceeds_usdt: Decimal
    net_pnl_usdt: Decimal
    net_return_pct: Decimal
    realized_loss_exceeded_1pct: bool
    primary_exit_action: ExitSignalAction
    primary_exit_reason: str
    tranche_plan_mode: str
    tranches_filled_count: int
    fill_slices: tuple[ExitFillSlice, ...]
    ambiguous_same_bar_stop_resolved: bool
    over_reservation_violations: int
    naked_position_violations: int


@dataclass(frozen=True)
class StrategyPerformanceSummary:
    """Aggregated out-of-sample performance and risk metrics for a single exit strategy."""

    strategy_id: ExitStrategyId
    layer: BacktestLayer
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal
    take_profit_exit_count: int
    trailing_stop_exit_count: int
    stop_loss_exit_count: int
    gap_down_emergency_exit_count: int
    defensive_risk_off_exit_count: int
    timeout_exit_count: int
    realized_loss_gt_1pct_count: int
    degraded_tranche_plan_count: int
    mean_net_return: Decimal
    mean_net_return_bps: Decimal
    net_return_95_ci_lower: Decimal
    net_return_95_ci_upper: Decimal
    cumulative_net_pnl_usdt: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    worst_single_trade_return: Decimal
    cvar_95_return: Decimal
    sharpe_annualized: Decimal
    sortino_annualized: Decimal
    calmar_ratio: Decimal
    profit_factor: Decimal
    payoff_ratio: Decimal
    over_reservation_violations: int
    unprotectable_tranche_deadlocks: int
    naked_position_incidents: int

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id.value,
            "layer": self.layer.value,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": str(self.win_rate),
            "take_profit_exit_count": self.take_profit_exit_count,
            "trailing_stop_exit_count": self.trailing_stop_exit_count,
            "stop_loss_exit_count": self.stop_loss_exit_count,
            "gap_down_emergency_exit_count": self.gap_down_emergency_exit_count,
            "defensive_risk_off_exit_count": self.defensive_risk_off_exit_count,
            "timeout_exit_count": self.timeout_exit_count,
            "realized_loss_gt_1pct_count": self.realized_loss_gt_1pct_count,
            "degraded_tranche_plan_count": self.degraded_tranche_plan_count,
            "mean_net_return": str(self.mean_net_return),
            "mean_net_return_bps": str(self.mean_net_return_bps),
            "net_return_95_ci": [str(self.net_return_95_ci_lower), str(self.net_return_95_ci_upper)],
            "cumulative_net_pnl_usdt": str(self.cumulative_net_pnl_usdt),
            "total_return_pct": str(self.total_return_pct),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "worst_single_trade_return": str(self.worst_single_trade_return),
            "cvar_95_return": str(self.cvar_95_return),
            "sharpe_annualized": str(self.sharpe_annualized),
            "sortino_annualized": str(self.sortino_annualized),
            "calmar_ratio": str(self.calmar_ratio),
            "profit_factor": str(self.profit_factor),
            "payoff_ratio": str(self.payoff_ratio),
            "over_reservation_violations": self.over_reservation_violations,
            "unprotectable_tranche_deadlocks": self.unprotectable_tranche_deadlocks,
            "naked_position_incidents": self.naked_position_incidents,
        }


@dataclass(frozen=True)
class FairExitComparisonReport:
    """Complete two-layer comparison report across C1, C2, and C3 with fairness audit & Occam verdict."""

    dataset_name: str
    layer: BacktestLayer
    trade_count: int
    fairness_audit_passed: bool
    identical_entry_timestamps_verified: bool
    identical_entry_prices_verified: bool
    identical_r01_quantities_verified: bool
    identical_initial_stops_verified: bool
    identical_fee_rates_verified: bool
    summaries: Mapping[ExitStrategyId, StrategyPerformanceSummary]
    trade_results: Mapping[ExitStrategyId, tuple[TradeExitSimulationResult, ...]]
    statistical_verdict: StatisticalVerdict
    verdict_reasons: tuple[str, ...]
    recommended_strategy: ExitStrategyId
    selection_rationale: str


class FairExitComparisonBacktester:
    """Executes C1, C2, and C3 over identical entry events in both Bar-Level and Microstructure Stress layers."""

    def __init__(
        self,
        *,
        c1: C1FixedNetTargetExit | None = None,
        c2: C2GridTrailingExit | None = None,
        c3: C3AdaptiveHybridExit | None = None,
        initial_equity_usdt: Decimal = Decimal("10000.0000"),
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
    ) -> None:
        self.c1 = c1 or C1FixedNetTargetExit(filters=filters)
        self.c2 = c2 or C2GridTrailingExit(filters=filters)
        self.c3 = c3 or C3AdaptiveHybridExit(c1_delegate=self.c1, c2_delegate=self.c2)
        self.initial_equity_usdt = require_decimal(initial_equity_usdt, "initial_equity_usdt")
        self.filters = filters

    def simulate_single_trade(
        self,
        *,
        ctx: EntryTradeContext,
        strategy_id: ExitStrategyId,
        layer: BacktestLayer = BacktestLayer.BAR_LEVEL_PESSIMISTIC,
    ) -> TradeExitSimulationResult:
        """Simulate a single trade's exit lifecycle through `PaperOrderbookSimulator` + `OcoProtectionCoordinator`."""
        friction_mult = (
            Decimal("1") + (ctx.entry_microstructure_friction_bps / Decimal("10000"))
            if layer == BacktestLayer.ORDERBOOK_MICROSTRUCTURE_STRESS
            else Decimal("1")
        )
        eff_entry_price = self.filters.quantize_price_up(ctx.entry_fill_price * friction_mult)

        if ctx.fee_asset.upper() == "SUI":
            buy_fee_amt = (ctx.r01_gross_buy_qty * ctx.buy_fee_rate).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
            buy_cost_usdt = ctx.r01_gross_buy_qty * eff_entry_price
        else:
            buy_fee_amt = (
                ctx.r01_gross_buy_qty * eff_entry_price * ctx.buy_fee_rate
            ).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            buy_cost_usdt = (ctx.r01_gross_buy_qty * eff_entry_price) + buy_fee_amt

        alloc = compute_post_fill_sellable_position(
            gross_filled_qty=ctx.r01_gross_buy_qty,
            avg_fill_price=eff_entry_price,
            fee_asset=ctx.fee_asset,
            fee_amount=buy_fee_amt,
            stop_loss_pct=self.c1.stop_loss_pct,
            filters=self.filters,
        )
        if not alloc.is_protectable:
            raise ValueError(f"EntryTradeContext {ctx.trade_id} is not protectable: {alloc.unprotectable_reason}")

        # Initialize isolated PAPER exchange holding the filled SUI position
        sim_exchange = PaperOrderbookSimulator(
            initial_usdt=Decimal("0.0000"),
            initial_sui=alloc.net_base_qty,
            commission_asset=ctx.fee_asset,
            buy_fee_rate=ctx.buy_fee_rate,
            sell_fee_rate=ctx.sell_fee_rate,
            filters=self.filters,
            execution_mode=ExecutionMode.PAPER,
        )
        coord = OcoProtectionCoordinator(exchange=sim_exchange, filters=self.filters)

        init_ledger = PositionLedger(
            position_id=f"POS-{ctx.trade_id}-{strategy_id.value}",
            symbol=TRADING_SYMBOL,
            actual_qty=alloc.net_base_qty,
            avg_fill_price=eff_entry_price,
            fees=buy_fee_amt if ctx.fee_asset.upper() != "SUI" else Decimal("0.0"),
            reserved_qty=Decimal("0.0"),
            protection_orders=(),
            exit_strategy_version=f"{strategy_id.value}-v1.0",
            risk_config_version="1.0.0",
        )

        # Establish initial -1.0% stop-loss protection
        step_rep = coord.establish_initial_protection(
            ledger=init_ledger,
            stop_trigger_price=alloc.stop_trigger_price,
            stop_limit_price=alloc.stop_trigger_price,
            now_utc=ctx.entry_time_utc,
        )
        ledger = step_rep.position_ledger
        current_stop_price = alloc.stop_trigger_price

        fill_slices: list[ExitFillSlice] = []
        ambiguous_conflict_resolved = False

        if strategy_id == ExitStrategyId.C1:
            c1_target, _ = self.c1.compute_target_and_stop(eff_entry_price)
            tranche_plan_mode = "SINGLE_100PCT_C1_TARGET"
            for bar in ctx.future_bars[: self.c1.max_holding_bars_5m]:
                hit_stop = bar.low_price <= current_stop_price
                hit_target = bar.high_price >= c1_target
                if hit_stop and hit_target:
                    ambiguous_conflict_resolved = True

                # Pessimistic Intra-Bar Rule: STOP strictly precedes TARGET on same bar
                if hit_stop:
                    exec_p = self._compute_stop_fill_price(
                        bar=bar,
                        stop_trigger_price=current_stop_price,
                        layer=layer,
                    )
                    action = (
                        ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP
                        if bar.gap_down_beyond_stop_limit or bar.open_price < current_stop_price
                        else ExitSignalAction.FULL_EXIT_STOP_LOSS
                    )
                    reason = (
                        "STOP_LOSS_GAP_DOWN_EMERGENCY_SWEEP"
                        if action == ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP
                        else "STOP_LOSS_1PCT_TRIGGER"
                    )
                    rep = coord.execute_full_exit(
                        ledger=ledger,
                        exec_price=exec_p,
                        action=action,
                        reason=reason,
                        slice_index=1,
                        now_utc=bar.close_time_utc,
                    )
                    assert rep.fill_slice is not None
                    fill_slices.append(rep.fill_slice)
                    ledger = rep.position_ledger
                    break

                if hit_target:
                    rep = coord.execute_full_exit(
                        ledger=ledger,
                        exec_price=c1_target,
                        action=ExitSignalAction.FULL_EXIT_TAKE_PROFIT,
                        reason="C1_NET_1P5_TARGET_HIT",
                        slice_index=1,
                        now_utc=bar.close_time_utc,
                    )
                    assert rep.fill_slice is not None
                    fill_slices.append(rep.fill_slice)
                    ledger = rep.position_ledger
                    break
            else:
                # Vertical 3h timeout barrier
                last_bar = ctx.future_bars[min(len(ctx.future_bars), self.c1.max_holding_bars_5m) - 1]
                to_price = self._compute_market_exit_price(bar=last_bar, layer=layer)
                rep = coord.execute_full_exit(
                    ledger=ledger,
                    exec_price=to_price,
                    action=ExitSignalAction.FULL_EXIT_TIMEOUT,
                    reason="VERTICAL_3H_TIMEOUT_EXIT",
                    slice_index=1,
                    now_utc=last_bar.close_time_utc,
                )
                assert rep.fill_slice is not None
                fill_slices.append(rep.fill_slice)
                ledger = rep.position_ledger

        elif strategy_id in {ExitStrategyId.C2, ExitStrategyId.C3}:
            plan = self.c2.plan_tranches(
                sellable_qty=alloc.sellable_qty,
                avg_buy_price=eff_entry_price,
            )
            tranche_plan_mode = plan.plan_mode
            tranches = list(plan.tranches)
            next_tranche_idx = 0
            high_water_mark = eff_entry_price
            c1_target, _ = self.c1.compute_target_and_stop(eff_entry_price)

            for idx, bar in enumerate(ctx.future_bars[: self.c2.max_holding_bars_5m], start=1):
                # Step A: Check stop-loss trigger FIRST (pessimistic same-bar priority)
                curr_target_check = (
                    tranches[next_tranche_idx].target_sell_price
                    if next_tranche_idx < len(tranches)
                    else c1_target
                )
                if bar.low_price <= current_stop_price and bar.high_price >= curr_target_check:
                    ambiguous_conflict_resolved = True

                if bar.low_price <= current_stop_price:
                    exec_p = self._compute_stop_fill_price(
                        bar=bar,
                        stop_trigger_price=current_stop_price,
                        layer=layer,
                    )
                    is_trailing_stop_exit = next_tranche_idx > 0 and current_stop_price >= plan.breakeven_price
                    if bar.gap_down_beyond_stop_limit or bar.open_price < current_stop_price:
                        action = ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP
                        reason = "STOP_LOSS_GAP_DOWN_EMERGENCY_SWEEP"
                    elif is_trailing_stop_exit:
                        action = ExitSignalAction.FULL_EXIT_TRAILING_STOP
                        reason = "C2_RATCHETED_OR_TRAILING_STOP_HIT"
                    else:
                        action = ExitSignalAction.FULL_EXIT_STOP_LOSS
                        reason = "STOP_LOSS_1PCT_TRIGGER"

                    rep = coord.execute_full_exit(
                        ledger=ledger,
                        exec_price=exec_p,
                        action=action,
                        reason=reason,
                        slice_index=len(fill_slices) + 1,
                        now_utc=bar.close_time_utc,
                    )
                    assert rep.fill_slice is not None
                    fill_slices.append(rep.fill_slice)
                    ledger = rep.position_ledger
                    break

                # Step B: For C3, check emergency risk-off regime or consolidation lock
                if strategy_id == ExitStrategyId.C3:
                    should_exit, def_reason = self.c3.should_trigger_emergency_risk_off(
                        regime=bar.regime,
                        btc_flash_drop_active=bar.btc_flash_drop_flag,
                        bars_held=idx,
                    )
                    if should_exit:
                        def_price = self._compute_market_exit_price(bar=bar, layer=layer)
                        rep = coord.execute_full_exit(
                            ledger=ledger,
                            exec_price=def_price,
                            action=ExitSignalAction.FULL_EXIT_DEFENSIVE_RISK_OFF,
                            reason=def_reason,
                            slice_index=len(fill_slices) + 1,
                            now_utc=bar.close_time_utc,
                        )
                        assert rep.fill_slice is not None
                        fill_slices.append(rep.fill_slice)
                        ledger = rep.position_ledger
                        break

                    # If C3 is in CONSOLIDATION and no tranches filled yet, take full profit at C1 +1.5%
                    active_mode = self.c3.select_active_mode(regime=bar.regime, obi_5=bar.obi_5)
                    if active_mode == ExitStrategyId.C1 and next_tranche_idx == 0:
                        if bar.high_price >= c1_target:
                            rep = coord.execute_full_exit(
                                ledger=ledger,
                                exec_price=c1_target,
                                action=ExitSignalAction.FULL_EXIT_TAKE_PROFIT,
                                reason="C3_CONSOLIDATION_C1_TARGET_HIT",
                                slice_index=1,
                                now_utc=bar.close_time_utc,
                            )
                            assert rep.fill_slice is not None
                            fill_slices.append(rep.fill_slice)
                            ledger = rep.position_ledger
                            break

                # Step C: Evaluate C2 profit-taking tranches & trailing stop ratcheting
                while next_tranche_idx < len(tranches):
                    tranche = tranches[next_tranche_idx]
                    if tranche.is_trailing_leg:
                        # Update high-water mark and ratchet trailing stop
                        if bar.high_price > high_water_mark:
                            high_water_mark = bar.high_price
                        candidate_trail_stop = self.filters.quantize_price_down(
                            high_water_mark * (Decimal("1") - self.c2.trailing_pullback_pct)
                        )
                        new_stop = max(current_stop_price, tranche.stop_price_after_fill, candidate_trail_stop)
                        if new_stop > current_stop_price:
                            rep = coord.ratchet_stop_protection_only(
                                ledger=ledger,
                                new_stop_trigger_price=new_stop,
                                reason="C2_TRAILING_STOP_RATCHET",
                                now_utc=bar.close_time_utc,
                            )
                            ledger = rep.position_ledger
                            current_stop_price = new_stop
                        # Check if intrabar pullback from high_water_mark hit trailing stop after T2
                        if bar.close_price <= current_stop_price:
                            rep = coord.execute_full_exit(
                                ledger=ledger,
                                exec_price=current_stop_price,
                                action=ExitSignalAction.FULL_EXIT_TRAILING_STOP,
                                reason="C2_TRAILING_STOP_PULLBACK_HIT",
                                slice_index=len(fill_slices) + 1,
                                now_utc=bar.close_time_utc,
                            )
                            assert rep.fill_slice is not None
                            fill_slices.append(rep.fill_slice)
                            ledger = rep.position_ledger
                        break

                    if bar.high_price >= tranche.target_sell_price:
                        high_water_mark = max(high_water_mark, bar.high_price)
                        if tranche.remaining_qty_after_fill > Decimal("0"):
                            new_stop = max(current_stop_price, tranche.stop_price_after_fill)
                            rep = coord.execute_partial_tranche_and_reprotect(
                                ledger=ledger,
                                tranche_qty=tranche.qty,
                                tranche_exec_price=tranche.target_sell_price,
                                new_stop_trigger_price=new_stop,
                                slice_index=len(fill_slices) + 1,
                                reason=f"C2_TRANCHE_{tranche.tranche_index}_HIT",
                                now_utc=bar.close_time_utc,
                            )
                            assert rep.fill_slice is not None
                            fill_slices.append(rep.fill_slice)
                            ledger = rep.position_ledger
                            current_stop_price = new_stop
                            next_tranche_idx += 1
                        else:
                            # Single-tranche degraded mode full exit
                            rep = coord.execute_full_exit(
                                ledger=ledger,
                                exec_price=tranche.target_sell_price,
                                action=ExitSignalAction.FULL_EXIT_TAKE_PROFIT,
                                reason="C2_DEGRADED_SINGLE_TRANCHE_HIT",
                                slice_index=len(fill_slices) + 1,
                                now_utc=bar.close_time_utc,
                            )
                            assert rep.fill_slice is not None
                            fill_slices.append(rep.fill_slice)
                            ledger = rep.position_ledger
                            next_tranche_idx += 1
                            break
                    else:
                        break

                if self.filters.quantize_qty_down(ledger.actual_qty) < self.filters.min_qty:
                    break
            else:
                # Vertical 3h timeout for any remaining sellable quantity
                if self.filters.quantize_qty_down(ledger.actual_qty) >= self.filters.min_qty:
                    last_bar = ctx.future_bars[min(len(ctx.future_bars), self.c2.max_holding_bars_5m) - 1]
                    to_price = self._compute_market_exit_price(bar=last_bar, layer=layer)
                    rep = coord.execute_full_exit(
                        ledger=ledger,
                        exec_price=to_price,
                        action=ExitSignalAction.FULL_EXIT_TIMEOUT,
                        reason="VERTICAL_3H_TIMEOUT_EXIT",
                        slice_index=len(fill_slices) + 1,
                        now_utc=last_bar.close_time_utc,
                    )
                    assert rep.fill_slice is not None
                    fill_slices.append(rep.fill_slice)
                    ledger = rep.position_ledger
        else:
            raise ValueError(f"Unsupported strategy_id: {strategy_id}")

        total_sold_qty = sum((s.qty for s in fill_slices), Decimal("0.0"))
        total_gross_quote = sum((s.gross_quote_usdt for s in fill_slices), Decimal("0.0"))
        total_net_proceeds = sum((s.net_quote_usdt for s in fill_slices), Decimal("0.0"))
        weighted_exit_p = (
            (total_gross_quote / total_sold_qty).quantize(QUANT_4, rounding=ROUND_DOWN)
            if total_sold_qty > Decimal("0")
            else Decimal("0.0000")
        )
        # Cost basis corresponding to the sold shares
        effective_buy_cost = alloc.sellable_qty * eff_entry_price * (Decimal("1") + ctx.buy_fee_rate)
        net_pnl_usdt = _q8(total_net_proceeds - effective_buy_cost)
        net_return_pct = _q8(net_pnl_usdt / effective_buy_cost)
        loss_gt_1pct = net_return_pct < Decimal("-0.01000000")

        last_slice = fill_slices[-1]
        return TradeExitSimulationResult(
            trade_id=ctx.trade_id,
            strategy_id=strategy_id,
            layer=layer,
            entry_time_utc=ctx.entry_time_utc,
            exit_time_utc=last_slice.executed_at_utc,
            entry_fill_price=ctx.entry_fill_price,
            effective_entry_price_with_friction=eff_entry_price,
            initial_stop_price=alloc.stop_trigger_price,
            r01_gross_buy_qty=ctx.r01_gross_buy_qty,
            sellable_qty=alloc.sellable_qty,
            weighted_exit_price=weighted_exit_p,
            buy_cost_usdt_with_fee=_q8(effective_buy_cost),
            sell_net_proceeds_usdt=_q8(total_net_proceeds),
            net_pnl_usdt=net_pnl_usdt,
            net_return_pct=net_return_pct,
            realized_loss_exceeded_1pct=loss_gt_1pct,
            primary_exit_action=last_slice.action,
            primary_exit_reason=last_slice.reason,
            tranche_plan_mode=tranche_plan_mode,
            tranches_filled_count=len(fill_slices),
            fill_slices=tuple(fill_slices),
            ambiguous_same_bar_stop_resolved=ambiguous_conflict_resolved,
            over_reservation_violations=coord.over_reservation_violations,
            naked_position_violations=coord.naked_position_violations,
        )

    def _compute_stop_fill_price(
        self,
        *,
        bar: ExitPathBar,
        stop_trigger_price: Decimal,
        layer: BacktestLayer,
    ) -> Decimal:
        """Compute realistic stop-loss execution price, accounting for gap-down and depth slippage (QA-07)."""
        base_stop_ref = min(stop_trigger_price, bar.open_price)
        slip_bps = (
            bar.stop_slippage_bps * Decimal("1.8")
            if layer == BacktestLayer.ORDERBOOK_MICROSTRUCTURE_STRESS
            else bar.stop_slippage_bps
        )
        if bar.gap_down_beyond_stop_limit:
            # Under flash gap-down, order fills near bar low / gapped bid with stress slippage
            base_stop_ref = min(base_stop_ref, bar.low_price * Decimal("1.002"))
        raw_exec = base_stop_ref * (Decimal("1") - (slip_bps / Decimal("10000")))
        return max(self.filters.min_price, self.filters.quantize_price_down(raw_exec))

    def _compute_market_exit_price(
        self,
        *,
        bar: ExitPathBar,
        layer: BacktestLayer,
    ) -> Decimal:
        slip_bps = (
            bar.market_exit_slippage_bps * Decimal("1.5")
            if layer == BacktestLayer.ORDERBOOK_MICROSTRUCTURE_STRESS
            else bar.market_exit_slippage_bps
        )
        raw_exec = bar.close_price * (Decimal("1") - (slip_bps / Decimal("10000")))
        return max(self.filters.min_price, self.filters.quantize_price_down(raw_exec))

    def run_fair_comparison(
        self,
        *,
        contexts: Sequence[EntryTradeContext],
        layer: BacktestLayer = BacktestLayer.BAR_LEVEL_PESSIMISTIC,
        dataset_name: str = "SUIUSDT_OOS_COMPARISON",
    ) -> FairExitComparisonReport:
        """Run C1, C2, and C3 over the exact same sequence of `EntryTradeContext` objects (QA-13)."""
        if not contexts:
            raise ValueError("contexts cannot be empty")

        results_by_strat: dict[ExitStrategyId, list[TradeExitSimulationResult]] = {
            ExitStrategyId.C1: [],
            ExitStrategyId.C2: [],
            ExitStrategyId.C3: [],
        }

        for ctx in contexts:
            for strat_id in (ExitStrategyId.C1, ExitStrategyId.C2, ExitStrategyId.C3):
                sim_res = self.simulate_single_trade(
                    ctx=ctx,
                    strategy_id=strat_id,
                    layer=layer,
                )
                results_by_strat[strat_id].append(sim_res)

        # Verify QA-13 Fairness Invariants across C1, C2, C3
        c1_list = results_by_strat[ExitStrategyId.C1]
        c2_list = results_by_strat[ExitStrategyId.C2]
        c3_list = results_by_strat[ExitStrategyId.C3]

        same_ts = all(
            a.entry_time_utc == b.entry_time_utc == c.entry_time_utc
            for a, b, c in zip(c1_list, c2_list, c3_list)
        )
        same_px = all(
            a.effective_entry_price_with_friction
            == b.effective_entry_price_with_friction
            == c.effective_entry_price_with_friction
            for a, b, c in zip(c1_list, c2_list, c3_list)
        )
        same_qty = all(
            a.r01_gross_buy_qty == b.r01_gross_buy_qty == c.r01_gross_buy_qty
            and a.sellable_qty == b.sellable_qty == c.sellable_qty
            for a, b, c in zip(c1_list, c2_list, c3_list)
        )
        same_stop = all(
            a.initial_stop_price == b.initial_stop_price == c.initial_stop_price
            for a, b, c in zip(c1_list, c2_list, c3_list)
        )
        same_cost = all(
            a.buy_cost_usdt_with_fee == b.buy_cost_usdt_with_fee == c.buy_cost_usdt_with_fee
            for a, b, c in zip(c1_list, c2_list, c3_list)
        )
        fairness_ok = same_ts and same_px and same_qty and same_stop and same_cost
        if not fairness_ok:
            raise RuntimeError("QA-13 Fairness Invariant violated across C1, C2, C3")

        summaries: dict[ExitStrategyId, StrategyPerformanceSummary] = {}
        for strat_id, t_list in results_by_strat.items():
            summaries[strat_id] = self._summarize_strategy_results(
                strategy_id=strat_id,
                layer=layer,
                trades=t_list,
            )

        verdict, reasons, rec_strat, rationale = self._evaluate_verdict_and_occam_selection(summaries)

        return FairExitComparisonReport(
            dataset_name=dataset_name,
            layer=layer,
            trade_count=len(contexts),
            fairness_audit_passed=fairness_ok,
            identical_entry_timestamps_verified=same_ts,
            identical_entry_prices_verified=same_px,
            identical_r01_quantities_verified=same_qty,
            identical_initial_stops_verified=same_stop,
            identical_fee_rates_verified=same_cost,
            summaries=summaries,
            trade_results={k: tuple(v) for k, v in results_by_strat.items()},
            statistical_verdict=verdict,
            verdict_reasons=reasons,
            recommended_strategy=rec_strat,
            selection_rationale=rationale,
        )

    def _summarize_strategy_results(
        self,
        *,
        strategy_id: ExitStrategyId,
        layer: BacktestLayer,
        trades: Sequence[TradeExitSimulationResult],
    ) -> StrategyPerformanceSummary:
        n = len(trades)
        n_dec = Decimal(str(n))
        rets = [t.net_return_pct for t in trades]
        pnls = [t.net_pnl_usdt for t in trades]

        wins = [r for r in rets if r > Decimal("0")]
        losses = [r for r in rets if r <= Decimal("0")]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = _q8(Decimal(str(win_count)) / n_dec)

        tp_count = sum(1 for t in trades if t.primary_exit_action == ExitSignalAction.FULL_EXIT_TAKE_PROFIT)
        trail_count = sum(1 for t in trades if t.primary_exit_action == ExitSignalAction.FULL_EXIT_TRAILING_STOP)
        sl_count = sum(1 for t in trades if t.primary_exit_action == ExitSignalAction.FULL_EXIT_STOP_LOSS)
        gap_count = sum(
            1 for t in trades if t.primary_exit_action == ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP
        )
        def_count = sum(
            1 for t in trades if t.primary_exit_action == ExitSignalAction.FULL_EXIT_DEFENSIVE_RISK_OFF
        )
        to_count = sum(1 for t in trades if t.primary_exit_action == ExitSignalAction.FULL_EXIT_TIMEOUT)
        loss_gt_1pct_count = sum(1 for t in trades if t.realized_loss_exceeded_1pct)
        degraded_count = sum(1 for t in trades if t.tranche_plan_mode.startswith("DEGRADED_"))

        mean_ret = _q8(sum(rets, Decimal("0")) / n_dec)
        mean_ret_bps = (mean_ret * Decimal("10000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        ci_low, ci_high = self._bootstrap_mean_ci(rets)

        cum_pnl = sum(pnls, Decimal("0.0000")).quantize(QUANT_4, rounding=ROUND_HALF_EVEN)
        total_ret_pct = _q8(cum_pnl / self.initial_equity_usdt)

        # Equity curve & max drawdown
        eq = self.initial_equity_usdt
        peak_eq = eq
        max_dd = Decimal("0")
        for pnl in pnls:
            eq += pnl
            if eq > peak_eq:
                peak_eq = eq
            dd = (peak_eq - eq) / peak_eq if peak_eq > Decimal("0") else Decimal("0")
            if dd > max_dd:
                max_dd = dd
        max_dd_q = _q8(max_dd)

        sorted_rets = sorted(rets)
        worst_ret = _q8(sorted_rets[0])
        tail_k = max(1, int(math.ceil(0.05 * n)))
        cvar_95 = _q8(sum(sorted_rets[:tail_k], Decimal("0")) / Decimal(str(tail_k)))

        # Annualized Sharpe & Sortino (assuming ~2,800 trades/year pace or sqrt(N) scaling)
        variance = sum(((r - mean_ret) ** 2 for r in rets), Decimal("0")) / max(Decimal("1"), n_dec - Decimal("1"))
        std_dev = Decimal(str(math.sqrt(float(variance)))) if variance > Decimal("0") else Decimal("0.0001")
        downside_var = (
            sum(((min(Decimal("0"), r)) ** 2 for r in rets), Decimal("0")) / n_dec
        )
        downside_std = (
            Decimal(str(math.sqrt(float(downside_var)))) if downside_var > Decimal("0") else Decimal("0.0001")
        )
        ann_factor = Decimal(str(math.sqrt(250.0)))
        sharpe = _q8((mean_ret / std_dev) * ann_factor) if std_dev > Decimal("0") else Decimal("0")
        sortino = _q8((mean_ret / downside_std) * ann_factor) if downside_std > Decimal("0") else Decimal("0")
        calmar = _q8(total_ret_pct / max_dd_q) if max_dd_q > Decimal("0.000001") else Decimal("0")

        gross_profit = sum((p for p in pnls if p > Decimal("0")), Decimal("0"))
        gross_loss = abs(sum((p for p in pnls if p < Decimal("0")), Decimal("0")))
        profit_factor = (
            _q8(gross_profit / gross_loss) if gross_loss > Decimal("0") else Decimal("99.00000000")
        )
        avg_win = sum(wins, Decimal("0")) / Decimal(str(len(wins))) if wins else Decimal("0")
        avg_loss = abs(sum(losses, Decimal("0")) / Decimal(str(len(losses)))) if losses else Decimal("0")
        payoff_ratio = _q8(avg_win / avg_loss) if avg_loss > Decimal("0") else Decimal("0")

        over_res = sum(t.over_reservation_violations for t in trades)
        naked_inc = sum(t.naked_position_violations for t in trades)

        return StrategyPerformanceSummary(
            strategy_id=strategy_id,
            layer=layer,
            trade_count=n,
            win_count=win_count,
            loss_count=loss_count,
            win_rate=win_rate,
            take_profit_exit_count=tp_count,
            trailing_stop_exit_count=trail_count,
            stop_loss_exit_count=sl_count,
            gap_down_emergency_exit_count=gap_count,
            defensive_risk_off_exit_count=def_count,
            timeout_exit_count=to_count,
            realized_loss_gt_1pct_count=loss_gt_1pct_count,
            degraded_tranche_plan_count=degraded_count,
            mean_net_return=mean_ret,
            mean_net_return_bps=mean_ret_bps,
            net_return_95_ci_lower=ci_low,
            net_return_95_ci_upper=ci_high,
            cumulative_net_pnl_usdt=cum_pnl,
            total_return_pct=total_ret_pct,
            max_drawdown_pct=max_dd_q,
            worst_single_trade_return=worst_ret,
            cvar_95_return=cvar_95,
            sharpe_annualized=sharpe,
            sortino_annualized=sortino,
            calmar_ratio=calmar,
            profit_factor=profit_factor,
            payoff_ratio=payoff_ratio,
            over_reservation_violations=over_res,
            unprotectable_tranche_deadlocks=0,
            naked_position_incidents=naked_inc,
        )

    @staticmethod
    def _bootstrap_mean_ci(
        values: Sequence[Decimal],
        n_boot: int = 400,
    ) -> tuple[Decimal, Decimal]:
        """Deterministic LCG bootstrap 95% confidence interval for mean net return."""
        n = len(values)
        if n == 0:
            return Decimal("0"), Decimal("0")
        if n == 1:
            return _q8(values[0]), _q8(values[0])

        state = 20261031
        boot_means: list[Decimal] = []
        n_dec = Decimal(str(n))
        for _ in range(n_boot):
            acc = Decimal("0")
            for _ in range(n):
                state = (1103515245 * state + 12345) & 0x7FFFFFFF
                idx = state % n
                acc += values[idx]
            boot_means.append(acc / n_dec)
        boot_means.sort()
        lo_idx = max(0, int(0.025 * n_boot))
        hi_idx = min(n_boot - 1, int(0.975 * n_boot))
        return _q8(boot_means[lo_idx]), _q8(boot_means[hi_idx])

    @staticmethod
    def _evaluate_verdict_and_occam_selection(
        summaries: Mapping[ExitStrategyId, StrategyPerformanceSummary],
    ) -> tuple[StatisticalVerdict, tuple[str, ...], ExitStrategyId, str]:
        """Apply statistical significance thresholds and Occam's Razor (§08 of P5_PLAN)."""
        c1_s = summaries[ExitStrategyId.C1]
        c2_s = summaries[ExitStrategyId.C2]
        c3_s = summaries[ExitStrategyId.C3]

        best_s = max((c1_s, c2_s, c3_s), key=lambda s: s.mean_net_return)
        reasons: list[str] = []

        if best_s.trade_count < 30:
            reasons.append(f"SAMPLE_SIZE_BELOW_THRESHOLD(N={best_s.trade_count}<30)")
            return (
                StatisticalVerdict.INSUFFICIENT_EVIDENCE,
                tuple(reasons),
                ExitStrategyId.C1,
                "Sample size < 30; defaulting to simplest baseline C1 per Occam's Razor.",
            )

        if best_s.mean_net_return <= Decimal("0"):
            reasons.append(
                f"NON_POSITIVE_NET_EXPECTANCY("
                f"C1={c1_s.mean_net_return_bps}bps,"
                f"C2={c2_s.mean_net_return_bps}bps,"
                f"C3={c3_s.mean_net_return_bps}bps)"
            )
            return (
                StatisticalVerdict.NO_EDGE,
                tuple(reasons),
                ExitStrategyId.C1,
                (
                    "All three exit strategies exhibit non-positive out-of-sample net expectancy "
                    f"(best={best_s.strategy_id.value} at {best_s.mean_net_return_bps} bps). "
                    "Statistical verdict is NO_EDGE; locking simplest C1 baseline per Occam's Razor."
                ),
            )

        if best_s.net_return_95_ci_lower <= Decimal("0"):
            reasons.append(
                f"CI_95_LOWER_BOUND_NON_POSITIVE({best_s.strategy_id.value}_ci_low={best_s.net_return_95_ci_lower})"
            )
            return (
                StatisticalVerdict.INSUFFICIENT_EVIDENCE,
                tuple(reasons),
                ExitStrategyId.C1,
                "95% bootstrap CI lower bound <= 0; defaulting to C1 baseline per Occam's Razor.",
            )

        # Positive edge confirmed: check if C2 or C3 beats C1 by at least +5 bps and higher Sharpe
        margin_bps = Decimal("5.00")
        if (
            best_s.strategy_id != ExitStrategyId.C1
            and (best_s.mean_net_return_bps - c1_s.mean_net_return_bps) >= margin_bps
            and best_s.sharpe_annualized > c1_s.sharpe_annualized
        ):
            reasons.append(
                f"SIGNIFICANT_OOS_OUTPERFORMANCE({best_s.strategy_id.value}>{c1_s.strategy_id.value})"
            )
            return (
                StatisticalVerdict.EDGE_VALIDATED,
                tuple(reasons),
                best_s.strategy_id,
                (
                    f"{best_s.strategy_id.value} outperformed C1 by "
                    f"+{best_s.mean_net_return_bps - c1_s.mean_net_return_bps} bps with positive 95% CI."
                ),
            )

        reasons.append("C1_SELECTED_BY_OCCAM_RAZOR")
        return (
            StatisticalVerdict.EDGE_VALIDATED,
            tuple(reasons),
            ExitStrategyId.C1,
            "C2/C3 did not beat C1 by the minimum complexity margin (+5 bps); locking C1 per Occam's Razor.",
        )
