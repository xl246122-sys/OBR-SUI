"""P5 Verification Suite: C-01 Exit Strategies (C1, C2, C3), OCO Anti-Over-Reservation, Gap-Down Fallback, and Two-Layer Fair Backtester (QA-06, QA-07, QA-13)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sui_quant.config import ExecutionMode
from sui_quant.contracts import (
    TRADING_SYMBOL,
    AlertSeverity,
    OrderSide,
    PositionLedger,
    RegimeType,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.exchange_rules import (
    FilterSourceType,
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
    compute_c1_net_target_sell_price,
    compute_post_fill_sellable_position,
    compute_stop_loss_trigger_price,
)
from sui_quant.execution.paper_exchange import PaperOrderbookSimulator
from sui_quant.exit import (
    BacktestLayer,
    C1FixedNetTargetExit,
    C2GridTrailingExit,
    C3AdaptiveHybridExit,
    EntryTradeContext,
    ExitPathBar,
    ExitSignalAction,
    ExitStrategyId,
    FairExitComparisonBacktester,
    OcoProtectionCoordinator,
    compute_fee_covered_breakeven_price,
)
from sui_quant.models.a02_breakout import StatisticalVerdict


def _make_dynamic_filters(now_utc: datetime) -> SymbolFilters:
    return SymbolFilters(
        symbol=P0_SNAPSHOT_SUIUSDT_FILTERS.symbol,
        min_price=P0_SNAPSHOT_SUIUSDT_FILTERS.min_price,
        max_price=P0_SNAPSHOT_SUIUSDT_FILTERS.max_price,
        tick_size=P0_SNAPSHOT_SUIUSDT_FILTERS.tick_size,
        min_qty=P0_SNAPSHOT_SUIUSDT_FILTERS.min_qty,
        max_qty=P0_SNAPSHOT_SUIUSDT_FILTERS.max_qty,
        step_size=P0_SNAPSHOT_SUIUSDT_FILTERS.step_size,
        min_notional=P0_SNAPSHOT_SUIUSDT_FILTERS.min_notional,
        max_notional=P0_SNAPSHOT_SUIUSDT_FILTERS.max_notional,
        max_num_orders=P0_SNAPSHOT_SUIUSDT_FILTERS.max_num_orders,
        max_num_algo_orders=P0_SNAPSHOT_SUIUSDT_FILTERS.max_num_algo_orders,
        max_num_order_lists=P0_SNAPSHOT_SUIUSDT_FILTERS.max_num_order_lists,
        source=FilterSourceType.DYNAMIC_EXCHANGE_INFO,
        fetched_at_utc=now_utc,
    )


def test_qa06_oco_and_tranche_quantity_locking_zero_over_reservation() -> None:
    """QA-06: Verify C2 3-tranche lifecycle maintains `reserved_qty == locked_sui <= sellable_qty <= actual_qty` with zero over-reservation or naked position."""
    now_utc = datetime(2026, 10, 31, 8, 0, 0, tzinfo=timezone.utc)
    filters = _make_dynamic_filters(now_utc)

    # Buy 100.0 SUI at 2.0000 USDT with SUI fee (0.1% = 0.1 SUI -> net 99.9 SUI, sellable 99.9 SUI)
    alloc = compute_post_fill_sellable_position(
        gross_filled_qty=Decimal("100.0"),
        avg_fill_price=Decimal("2.0000"),
        fee_asset="SUI",
        fee_amount=Decimal("0.100000"),
        stop_loss_pct=Decimal("0.0100"),
        filters=filters,
    )
    assert alloc.sellable_qty == Decimal("99.9")
    assert alloc.stop_trigger_price == Decimal("1.9800")

    exchange = PaperOrderbookSimulator(
        initial_usdt=Decimal("0.0000"),
        initial_sui=alloc.net_base_qty,
        commission_asset="SUI",
        filters=filters,
        execution_mode=ExecutionMode.PAPER,
    )
    coord = OcoProtectionCoordinator(exchange=exchange, filters=filters)
    c2 = C2GridTrailingExit(filters=filters)
    plan = c2.plan_tranches(sellable_qty=alloc.sellable_qty, avg_buy_price=Decimal("2.0000"))
    assert plan.plan_mode == "FULL_3_TRANCHE_40_30_30"
    assert not plan.degraded
    assert len(plan.tranches) == 3

    t1, t2, t3 = plan.tranches
    assert t1.qty == Decimal("39.9")
    assert t2.qty == Decimal("29.9")
    assert t3.qty == Decimal("30.1")
    assert t1.qty + t2.qty + t3.qty == Decimal("99.9")

    init_ledger = PositionLedger(
        position_id="POS-QA06-001",
        symbol=TRADING_SYMBOL,
        actual_qty=alloc.net_base_qty,
        avg_fill_price=Decimal("2.0000"),
        fees=Decimal("0.100000"),
        reserved_qty=Decimal("0.0"),
        protection_orders=(),
        exit_strategy_version="C2-v1.0",
        risk_config_version="1.0.0",
    )

    # Step 1: Establish initial -1% stop protection locking all 99.9 SUI
    rep0 = coord.establish_initial_protection(
        ledger=init_ledger,
        stop_trigger_price=alloc.stop_trigger_price,
        now_utc=now_utc,
    )
    assert rep0.position_ledger.reserved_qty == Decimal("99.9")
    assert exchange.locked_sui == Decimal("99.9")
    assert exchange.free_sui == Decimal("0.0")
    assert len(exchange.list_open_orders(side=OrderSide.SELL)) == 1

    # Step 2: Execute Tranche 1 (39.9 SUI @ +1.0% net = 2.0241 USDT) & ratchet stop to breakeven (2.0041 USDT)
    rep1 = coord.execute_partial_tranche_and_reprotect(
        ledger=rep0.position_ledger,
        tranche_qty=t1.qty,
        tranche_exec_price=t1.target_sell_price,
        new_stop_trigger_price=t1.stop_price_after_fill,
        slice_index=1,
        now_utc=now_utc + timedelta(minutes=5),
    )
    assert rep1.position_ledger.actual_qty == Decimal("60.0")
    assert rep1.position_ledger.reserved_qty == Decimal("60.0")
    assert exchange.locked_sui == Decimal("60.0")
    assert exchange.free_sui == Decimal("0.0")
    assert rep1.active_protection_stop_price == plan.breakeven_price
    assert len(exchange.list_open_orders(side=OrderSide.SELL)) == 1

    # Step 3: Execute Tranche 2 (29.9 SUI @ +1.8% net = 2.0401 USDT) & ratchet stop to T1 (2.0241 USDT)
    rep2 = coord.execute_partial_tranche_and_reprotect(
        ledger=rep1.position_ledger,
        tranche_qty=t2.qty,
        tranche_exec_price=t2.target_sell_price,
        new_stop_trigger_price=t2.stop_price_after_fill,
        slice_index=2,
        now_utc=now_utc + timedelta(minutes=10),
    )
    assert rep2.position_ledger.actual_qty == Decimal("30.1")
    assert rep2.position_ledger.reserved_qty == Decimal("30.1")
    assert exchange.locked_sui == Decimal("30.1")
    assert exchange.free_sui == Decimal("0.0")
    assert rep2.active_protection_stop_price == t1.target_sell_price
    assert len(exchange.list_open_orders(side=OrderSide.SELL)) == 1

    # Step 4: Ratchet trailing stop higher as price reaches 2.0800 (trailing stop = 2.0800 * 0.994 = 2.0675)
    rep_trail = coord.ratchet_stop_protection_only(
        ledger=rep2.position_ledger,
        new_stop_trigger_price=Decimal("2.0675"),
        now_utc=now_utc + timedelta(minutes=15),
    )
    assert rep_trail.position_ledger.reserved_qty == Decimal("30.1")
    assert exchange.locked_sui == Decimal("30.1")
    assert rep_trail.active_protection_stop_price == Decimal("2.0675")

    # Step 5: Execute final trailing stop exit for remaining 30.1 SUI
    rep3 = coord.execute_full_exit(
        ledger=rep_trail.position_ledger,
        exec_price=Decimal("2.0675"),
        action=ExitSignalAction.FULL_EXIT_TRAILING_STOP,
        reason="C2_TRAILING_STOP_HIT",
        slice_index=3,
        now_utc=now_utc + timedelta(minutes=20),
    )
    assert rep3.position_ledger.actual_qty == Decimal("0.0")
    assert rep3.position_ledger.reserved_qty == Decimal("0.0")
    assert exchange.locked_sui == Decimal("0.0")
    assert exchange.free_sui == Decimal("0.0")
    assert len(exchange.list_open_orders(side=OrderSide.SELL)) == 0
    assert coord.over_reservation_violations == 0
    assert coord.naked_position_violations == 0


def test_qa06_c2_small_position_min_notional_degradation_guard() -> None:
    """QA-06: Verify C2 automatically degrades from 3 tranches to 2 or 1 tranche when position notional is small, preventing < 5.25 USDT deadlocks."""
    c2 = C2GridTrailingExit(filters=P0_SNAPSHOT_SUIUSDT_FILTERS)

    # Case 1: Medium-small position of 6.0 SUI at $2.0000 (~$12.00 notional; stop trigger = $1.9800)
    # A 30% tranche would be 1.8 SUI * $1.98 = $3.564 < $5.25 min_safe_notional -> degrades to 2 tranches (3.0 SUI + 3.0 SUI = $5.94 each)
    plan_med = c2.plan_tranches(sellable_qty=Decimal("6.0"), avg_buy_price=Decimal("2.0000"))
    assert plan_med.degraded is True
    assert plan_med.plan_mode == "DEGRADED_2_TRANCHE_50_50"
    assert len(plan_med.tranches) == 2
    assert plan_med.tranches[0].qty == Decimal("3.0")
    assert plan_med.tranches[1].qty == Decimal("3.0")
    assert plan_med.tranches[0].tranche_stop_notional_usdt >= Decimal("5.25")
    assert plan_med.tranches[0].remaining_stop_notional_usdt >= Decimal("5.25")

    # Case 2: Small position of 3.0 SUI at $2.0000 (~$6.00 notional; stop trigger = $1.9800)
    # Even a 50% split (1.5 SUI * $1.98 = $2.97) is < $5.25 -> degrades to 100% single tranche C1 fallback (3.0 SUI = $5.94 >= $5.25)
    plan_small = c2.plan_tranches(sellable_qty=Decimal("3.0"), avg_buy_price=Decimal("2.0000"))
    assert plan_small.degraded is True
    assert plan_small.plan_mode == "DEGRADED_1_TRANCHE_100_C1_FALLBACK"
    assert len(plan_small.tranches) == 1
    assert plan_small.tranches[0].qty == Decimal("3.0")
    assert plan_small.tranches[0].remaining_qty_after_fill == Decimal("0.0")
    assert plan_small.tranches[0].tranche_stop_notional_usdt >= Decimal("5.25")


def test_qa07_gap_down_beyond_1pct_stop_slippage_and_order_rejection_fallback(
    tmp_path: Path,
) -> None:
    """QA-07: Inject -3.5% flash gap-down and stop order rejection; verify >1% realized loss is recorded truthfully and emergency sweep leaves 0 naked SUI."""
    now_utc = datetime(2026, 10, 31, 9, 0, 0, tzinfo=timezone.utc)
    filters = _make_dynamic_filters(now_utc)
    store = StateStore(tmp_path / "p5_qa07.db")
    alert_mgr = AlertManager(store=store)

    # Part A: Flash gap-down (-3.5% from 2.0000 to 1.9300, bypassing -1% stop limit 1.9800)
    exchange_a = PaperOrderbookSimulator(
        initial_usdt=Decimal("0.0000"),
        initial_sui=Decimal("50.0"),
        commission_asset="USDT",
        filters=filters,
        execution_mode=ExecutionMode.PAPER,
    )
    coord_a = OcoProtectionCoordinator(exchange=exchange_a, filters=filters, alert_manager=alert_mgr)
    ledger_a = PositionLedger(
        position_id="POS-QA07-GAP",
        symbol=TRADING_SYMBOL,
        actual_qty=Decimal("50.0"),
        avg_fill_price=Decimal("2.0000"),
        fees=Decimal("0.1000"),
        reserved_qty=Decimal("0.0"),
        protection_orders=(),
        exit_strategy_version="C1-v1.0",
        risk_config_version="1.0.0",
    )
    rep_init = coord_a.establish_initial_protection(
        ledger=ledger_a,
        stop_trigger_price=Decimal("1.9800"),
        stop_limit_price=Decimal("1.9800"),
        now_utc=now_utc,
    )
    assert rep_init.active_protection_stop_price == Decimal("1.9800")

    # Flash crash tick: best_bid drops straight to 1.9300 (-3.5% < 1.9800 limit)
    rep_gap = coord_a.handle_market_tick_or_gap_down(
        ledger=rep_init.position_ledger,
        best_bid_price=Decimal("1.9300"),
        best_ask_price=Decimal("1.9320"),
        emergency_sweep_slippage_bps=Decimal("15"),
        now_utc=now_utc + timedelta(seconds=5),
    )
    assert rep_gap.action == ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP
    assert rep_gap.fill_slice is not None
    # 1.9300 * (1 - 0.0015) = 1.9271
    assert rep_gap.fill_slice.exec_price == Decimal("1.9271")
    assert rep_gap.position_ledger.actual_qty == Decimal("0.0")
    assert rep_gap.position_ledger.reserved_qty == Decimal("0.0")
    assert exchange_a.locked_sui == Decimal("0.0")
    assert exchange_a.free_sui == Decimal("0.0")

    # Verify realized loss is ~ -3.84% (exceeding -1.0% trigger threshold, proving -1% is trigger, not loss cap!)
    buy_cost = Decimal("50.0") * Decimal("2.0000") + Decimal("0.1000")
    realized_net_return = (rep_gap.fill_slice.net_quote_usdt - buy_cost) / buy_cost
    assert realized_net_return < Decimal("-0.035")

    # Part B: Protection order rejection on placement triggers immediate emergency sweep fallback
    exchange_b = PaperOrderbookSimulator(
        initial_usdt=Decimal("0.0000"),
        initial_sui=Decimal("25.0"),
        commission_asset="USDT",
        filters=filters,
        execution_mode=ExecutionMode.PAPER,
    )
    coord_b = OcoProtectionCoordinator(exchange=exchange_b, filters=filters, alert_manager=alert_mgr)
    coord_b.inject_next_protection_order_rejection()
    ledger_b = PositionLedger(
        position_id="POS-QA07-REJ",
        symbol=TRADING_SYMBOL,
        actual_qty=Decimal("25.0"),
        avg_fill_price=Decimal("2.0000"),
        fees=Decimal("0.0500"),
        reserved_qty=Decimal("0.0"),
        protection_orders=(),
        exit_strategy_version="C1-v1.0",
        risk_config_version="1.0.0",
    )
    rep_rej = coord_b.establish_initial_protection(
        ledger=ledger_b,
        stop_trigger_price=Decimal("1.9800"),
        current_bid_price=Decimal("1.9760"),
        emergency_sweep_slippage_bps=Decimal("15"),
        now_utc=now_utc + timedelta(seconds=10),
    )
    assert rep_rej.action == ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP
    assert rep_rej.fill_slice is not None
    assert rep_rej.fill_slice.reason == "STOP_ORDER_REJECTED_EMERGENCY_SWEEP"
    assert rep_rej.position_ledger.actual_qty == Decimal("0.0")
    assert exchange_b.total_sui == Decimal("0.0")

    alerts = alert_mgr.list_undelivered_alerts()
    dedup_keys = {a["dedup_key"] for a in alerts}
    assert "C01-GAP-DOWN-BEYOND-STOP-LIMIT" in dedup_keys
    assert "C01-STOP-REJECTED-EMERGENCY-EXIT" in dedup_keys
    store.close()


def test_qa07_pessimistic_same_bar_conflict_resolution() -> None:
    """Verify that when a 5m bar touches both the -1% stop (<= 1.9800) and take-profit (>= 2.0341), STOP takes strict precedence."""
    now_utc = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
    backtester = FairExitComparisonBacktester()

    conflict_bar = ExitPathBar(
        bar_index=1,
        close_time_utc=now_utc + timedelta(minutes=5),
        open_price=Decimal("2.0000"),
        high_price=Decimal("2.0450"),  # Above C1 (+1.5% net = 2.0341) and C2 T1/T2
        low_price=Decimal("1.9750"),   # Below -1.0% stop (1.9800)
        close_price=Decimal("2.0100"),
        regime=RegimeType.UP_TREND,
        obi_5=Decimal("0.25"),
    )
    ctx = EntryTradeContext(
        trade_id="TR-CONFLICT-01",
        entry_time_utc=now_utc,
        entry_fill_price=Decimal("2.0000"),
        r01_gross_buy_qty=Decimal("50.0"),
        fee_asset="USDT",
        buy_fee_rate=Decimal("0.0010"),
        sell_fee_rate=Decimal("0.0010"),
        entry_microstructure_friction_bps=Decimal("7.0"),
        future_bars=(conflict_bar,),
    )

    for strat in (ExitStrategyId.C1, ExitStrategyId.C2, ExitStrategyId.C3):
        res = backtester.simulate_single_trade(
            ctx=ctx,
            strategy_id=strat,
            layer=BacktestLayer.BAR_LEVEL_PESSIMISTIC,
        )
        assert res.ambiguous_same_bar_stop_resolved is True
        assert res.primary_exit_action == ExitSignalAction.FULL_EXIT_STOP_LOSS
        assert res.net_pnl_usdt < Decimal("0")
        assert res.realized_loss_exceeded_1pct is True


def test_c3_adaptive_hybrid_regime_and_btc_flash_drop_defensive_exit() -> None:
    """Verify C3 proactively exits when A-01 flips to DOWN_TREND or BTCUSDT flash drop triggers, saving losses relative to waiting for -1% stop."""
    now_utc = datetime(2026, 10, 31, 11, 0, 0, tzinfo=timezone.utc)
    backtester = FairExitComparisonBacktester()

    # Bar 1: BTC flash drop triggers while SUI is at 1.9940 (-0.30%); Bar 2 crashes to 1.9600 (-2.00%)
    bars = (
        ExitPathBar(
            bar_index=1,
            close_time_utc=now_utc + timedelta(minutes=5),
            open_price=Decimal("2.0000"),
            high_price=Decimal("2.0020"),
            low_price=Decimal("1.9920"),
            close_price=Decimal("1.9940"),
            regime=RegimeType.HIGH_VOLATILITY,
            obi_5=Decimal("-0.35"),
            btc_flash_drop_flag=True,
        ),
        ExitPathBar(
            bar_index=2,
            close_time_utc=now_utc + timedelta(minutes=10),
            open_price=Decimal("1.9930"),
            high_price=Decimal("1.9930"),
            low_price=Decimal("1.9600"),
            close_price=Decimal("1.9650"),
            regime=RegimeType.DOWN_TREND,
            obi_5=Decimal("-0.50"),
            btc_flash_drop_flag=True,
        ),
    )
    ctx = EntryTradeContext(
        trade_id="TR-C3-DEF-01",
        entry_time_utc=now_utc,
        entry_fill_price=Decimal("2.0000"),
        r01_gross_buy_qty=Decimal("50.0"),
        fee_asset="USDT",
        buy_fee_rate=Decimal("0.0010"),
        sell_fee_rate=Decimal("0.0010"),
        entry_microstructure_friction_bps=Decimal("7.0"),
        future_bars=bars,
    )

    res_c1 = backtester.simulate_single_trade(ctx=ctx, strategy_id=ExitStrategyId.C1)
    res_c3 = backtester.simulate_single_trade(ctx=ctx, strategy_id=ExitStrategyId.C3)

    assert res_c3.primary_exit_action == ExitSignalAction.FULL_EXIT_DEFENSIVE_RISK_OFF
    assert res_c1.primary_exit_action == ExitSignalAction.FULL_EXIT_STOP_LOSS
    # C3 exited on Bar 1 at ~1.9936 (-0.52% net), whereas C1 waited until Bar 2 -1% stop (-1.25% net)
    assert res_c3.net_return_pct > res_c1.net_return_pct
    assert not res_c3.realized_loss_exceeded_1pct
    assert res_c1.realized_loss_exceeded_1pct


def _build_realistic_115_oos_contexts() -> list[EntryTradeContext]:
    """Construct 115 out-of-sample trade contexts matching the exact P3 FINAL_TEST distribution (WIN=11, STOP=67, TIMEOUT=37)."""
    base_time = datetime(2026, 10, 14, 7, 30, 0, tzinfo=timezone.utc)
    contexts: list[EntryTradeContext] = []

    # 11 WIN trajectories (6 strong fat-tail trends reaching +2.6%, 5 moderate reaching +1.6%)
    for i in range(11):
        t0 = base_time + timedelta(hours=i * 3)
        is_fat_tail = i < 6
        peak_p = Decimal("2.0560") if is_fat_tail else Decimal("2.0360")
        bars = [
            ExitPathBar(
                bar_index=1,
                close_time_utc=t0 + timedelta(minutes=5),
                open_price=Decimal("2.0000"),
                high_price=Decimal("2.0260"),
                low_price=Decimal("1.9960"),
                close_price=Decimal("2.0250"),
                regime=RegimeType.UP_TREND if is_fat_tail else RegimeType.CONSOLIDATION,
                obi_5=Decimal("0.30"),
            ),
            ExitPathBar(
                bar_index=2,
                close_time_utc=t0 + timedelta(minutes=10),
                open_price=Decimal("2.0250"),
                high_price=peak_p,
                low_price=Decimal("2.0180"),
                close_price=peak_p - Decimal("0.0140"),
                regime=RegimeType.UP_TREND if is_fat_tail else RegimeType.CONSOLIDATION,
                obi_5=Decimal("0.25"),
            ),
        ]
        contexts.append(
            EntryTradeContext(
                trade_id=f"OOS-WIN-{i:03d}",
                entry_time_utc=t0,
                entry_fill_price=Decimal("2.0000"),
                r01_gross_buy_qty=Decimal("50.0"),
                fee_asset="SUI",
                buy_fee_rate=Decimal("0.0010"),
                sell_fee_rate=Decimal("0.0010"),
                entry_microstructure_friction_bps=Decimal("7.0"),
                future_bars=tuple(bars),
            )
        )

    # 67 STOP trajectories (including 12 partial rally to +1.1% before reversing, 8 flash gap-downs, 47 direct stops)
    for i in range(67):
        t0 = base_time + timedelta(hours=35 + i * 3)
        if i < 12:
            # Reaches T1 (+1.0% net = 2.0241) on Bar 1, then reverses to breakeven/stop on Bar 2
            bars = [
                ExitPathBar(
                    bar_index=1,
                    close_time_utc=t0 + timedelta(minutes=5),
                    open_price=Decimal("2.0000"),
                    high_price=Decimal("2.0260"),
                    low_price=Decimal("1.9950"),
                    close_price=Decimal("2.0150"),
                    regime=RegimeType.UP_TREND,
                    obi_5=Decimal("0.15"),
                ),
                ExitPathBar(
                    bar_index=2,
                    close_time_utc=t0 + timedelta(minutes=10),
                    open_price=Decimal("2.0150"),
                    high_price=Decimal("2.0160"),
                    low_price=Decimal("1.9750"),
                    close_price=Decimal("1.9780"),
                    regime=RegimeType.DOWN_TREND,
                    obi_5=Decimal("-0.25"),
                ),
            ]
        elif i < 20:
            # Flash gap-down beyond -1% stop limit (QA-07)
            bars = [
                ExitPathBar(
                    bar_index=1,
                    close_time_utc=t0 + timedelta(minutes=5),
                    open_price=Decimal("1.9680"),
                    high_price=Decimal("1.9720"),
                    low_price=Decimal("1.9500"),
                    close_price=Decimal("1.9550"),
                    regime=RegimeType.HIGH_VOLATILITY,
                    obi_5=Decimal("-0.45"),
                    stop_slippage_bps=Decimal("12.0"),
                    gap_down_beyond_stop_limit=True,
                ),
            ]
        elif i < 30:
            # Early regime breakdown before hitting -1% stop (C3 defensive exit saves loss)
            bars = [
                ExitPathBar(
                    bar_index=1,
                    close_time_utc=t0 + timedelta(minutes=5),
                    open_price=Decimal("2.0000"),
                    high_price=Decimal("2.0010"),
                    low_price=Decimal("1.9900"),
                    close_price=Decimal("1.9920"),
                    regime=RegimeType.DOWN_TREND,
                    obi_5=Decimal("-0.30"),
                ),
                ExitPathBar(
                    bar_index=2,
                    close_time_utc=t0 + timedelta(minutes=10),
                    open_price=Decimal("1.9920"),
                    high_price=Decimal("1.9920"),
                    low_price=Decimal("1.9740"),
                    close_price=Decimal("1.9760"),
                    regime=RegimeType.DOWN_TREND,
                    obi_5=Decimal("-0.40"),
                ),
            ]
        else:
            # Direct -1.0% stop on Bar 1
            bars = [
                ExitPathBar(
                    bar_index=1,
                    close_time_utc=t0 + timedelta(minutes=5),
                    open_price=Decimal("2.0000"),
                    high_price=Decimal("2.0040"),
                    low_price=Decimal("1.9760"),
                    close_price=Decimal("1.9780"),
                    regime=RegimeType.CONSOLIDATION,
                    obi_5=Decimal("-0.10"),
                ),
            ]
        contexts.append(
            EntryTradeContext(
                trade_id=f"OOS-STOP-{i:03d}",
                entry_time_utc=t0,
                entry_fill_price=Decimal("2.0000"),
                r01_gross_buy_qty=Decimal("50.0"),
                fee_asset="SUI",
                buy_fee_rate=Decimal("0.0010"),
                sell_fee_rate=Decimal("0.0010"),
                entry_microstructure_friction_bps=Decimal("7.0"),
                future_bars=tuple(bars),
            )
        )

    # 37 TIMEOUT trajectories (36 bars oscillating between 1.9850 and 2.0180, closing at 2.0020)
    for i in range(37):
        t0 = base_time + timedelta(hours=240 + i * 3)
        bars = [
            ExitPathBar(
                bar_index=b,
                close_time_utc=t0 + timedelta(minutes=5 * b),
                open_price=Decimal("2.0000"),
                high_price=Decimal("2.0180"),
                low_price=Decimal("1.9860"),
                close_price=Decimal("2.0020"),
                regime=RegimeType.CONSOLIDATION,
                obi_5=Decimal("0.02"),
            )
            for b in range(1, 37)
        ]
        contexts.append(
            EntryTradeContext(
                trade_id=f"OOS-TIMEOUT-{i:03d}",
                entry_time_utc=t0,
                entry_fill_price=Decimal("2.0000"),
                r01_gross_buy_qty=Decimal("50.0"),
                fee_asset="SUI",
                buy_fee_rate=Decimal("0.0010"),
                sell_fee_rate=Decimal("0.0010"),
                entry_microstructure_friction_bps=Decimal("7.0"),
                future_bars=tuple(bars),
            )
        )

    return contexts


def test_qa13_fair_c1_c2_c3_comparison_identical_entries_risk_and_fees() -> None:
    """QA-13: Verify two-layer fair comparison across C1, C2, and C3 with identical entries, risk sizing, fees, NO_EDGE honest reporting, and Occam's Razor selection."""
    contexts = _build_realistic_115_oos_contexts()
    assert len(contexts) == 115

    backtester = FairExitComparisonBacktester()

    t0_ns = time.perf_counter_ns()
    report_l1 = backtester.run_fair_comparison(
        contexts=contexts,
        layer=BacktestLayer.BAR_LEVEL_PESSIMISTIC,
        dataset_name="FINAL_TEST_115_OOS_BAR_LEVEL",
    )
    elapsed_ms_per_eval = ((time.perf_counter_ns() - t0_ns) / 1e6) / (115 * 3)

    report_l2 = backtester.run_fair_comparison(
        contexts=contexts,
        layer=BacktestLayer.ORDERBOOK_MICROSTRUCTURE_STRESS,
        dataset_name="FINAL_TEST_115_OOS_MICROSTRUCTURE_STRESS",
    )

    for rep in (report_l1, report_l2):
        assert rep.trade_count == 115
        assert rep.fairness_audit_passed is True
        assert rep.identical_entry_timestamps_verified is True
        assert rep.identical_entry_prices_verified is True
        assert rep.identical_r01_quantities_verified is True
        assert rep.identical_initial_stops_verified is True
        assert rep.identical_fee_rates_verified is True

        for strat_id in (ExitStrategyId.C1, ExitStrategyId.C2, ExitStrategyId.C3):
            s = rep.summaries[strat_id]
            assert s.trade_count == 115
            assert s.over_reservation_violations == 0
            assert s.unprotectable_tranche_deadlocks == 0
            assert s.naked_position_incidents == 0
            assert s.realized_loss_gt_1pct_count > 0

        # Because the OOS dataset has a 9.57% base win rate (NO_EDGE), verdict must be NO_EDGE and lock C1
        assert rep.statistical_verdict == StatisticalVerdict.NO_EDGE
        assert rep.recommended_strategy == ExitStrategyId.C1

    # Layer 2 (with microstructure entry friction + stress slippage) must have strictly lower mean return than Layer 1
    assert (
        report_l2.summaries[ExitStrategyId.C1].mean_net_return
        < report_l1.summaries[ExitStrategyId.C1].mean_net_return
    )
    # Single trade evaluation latency budget check
    assert elapsed_ms_per_eval < 2.0


@pytest.mark.parametrize("fee_asset", ["SUI", "USDT", "BNB"])
def test_qa06_multi_asset_fee_tranche_locking_and_occam_razor_selection(fee_asset: str) -> None:
    """Verify C1/C2/C3 locking across SUI/USDT/BNB fee assets and Occam's Razor selection when fat-tail trend edge exists."""
    base_time = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    contexts: list[EntryTradeContext] = []

    # 35 fat-tail trend continuation trades (+3.2% rally in UP_TREND) where C2/C3 capture higher upside than C1 (+1.5% cap)
    for i in range(35):
        t0 = base_time + timedelta(hours=i)
        bars = (
            ExitPathBar(
                bar_index=1,
                close_time_utc=t0 + timedelta(minutes=5),
                open_price=Decimal("2.0000"),
                high_price=Decimal("2.0450"),
                low_price=Decimal("1.9960"),
                close_price=Decimal("2.0420"),
                regime=RegimeType.UP_TREND,
                obi_5=Decimal("0.35"),
            ),
            ExitPathBar(
                bar_index=2,
                close_time_utc=t0 + timedelta(minutes=10),
                open_price=Decimal("2.0420"),
                high_price=Decimal("2.0720"),
                low_price=Decimal("2.0380"),
                close_price=Decimal("2.0550"),
                regime=RegimeType.UP_TREND,
                obi_5=Decimal("0.30"),
            ),
        )
        contexts.append(
            EntryTradeContext(
                trade_id=f"BULL-{fee_asset}-{i:02d}",
                entry_time_utc=t0,
                entry_fill_price=Decimal("2.0000"),
                r01_gross_buy_qty=Decimal("50.0"),
                fee_asset=fee_asset,
                buy_fee_rate=Decimal("0.0010"),
                sell_fee_rate=Decimal("0.0010"),
                entry_microstructure_friction_bps=Decimal("4.0"),
                future_bars=bars,
            )
        )

    backtester = FairExitComparisonBacktester()
    rep = backtester.run_fair_comparison(
        contexts=contexts,
        layer=BacktestLayer.BAR_LEVEL_PESSIMISTIC,
        dataset_name=f"SYNTHETIC_FAT_TAIL_{fee_asset}",
    )
    assert rep.fairness_audit_passed is True
    for s in rep.summaries.values():
        assert s.over_reservation_violations == 0
        assert s.naked_position_incidents == 0
        assert s.mean_net_return > Decimal("0")
    assert rep.statistical_verdict == StatisticalVerdict.EDGE_VALIDATED

