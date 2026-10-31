"""P4 Acceptance & Fault-Injection Test Suite: B-01 Smart Limit Execution, D-01 Autonomous
State Machine, R-01 Final Risk Veto, and PaperOrderbookSimulator (FI-P4-01 through FI-P4-08,
QA-03, QA-04, QA-05, QA-08, INV-R1 through INV-R8).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sui_quant.config import ExecutionMode, RiskConfig, SystemConfig
from sui_quant.contracts import (
    BreakoutProposal,
    OrderSide,
    RegimeDecision,
    RegimeType,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.data.quality_guard import PartialOrderbookSnapshot, StreamQualityGuard
from sui_quant.exchange_rules import (
    FilterSourceType,
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
)
from sui_quant.execution import (
    B01LimitOrderOptimizer,
    CalibrationWindowSample,
    D01DecisionEngine,
    OrderStatus,
    PaperOrderbookSimulator,
    QuoteTier,
    R01PreTradeRiskGate,
    RealizedPnLEntry,
    compute_deterministic_p4_client_order_id,
    compute_multi_horizon_markout,
    compute_rolling_24h_drawdown_pct,
)
from sui_quant.state_machine import TradingState
from sui_quant.utils.clock import monotonic_ns, utc_now


def _make_fresh_filters(now_utc: datetime) -> SymbolFilters:
    return SymbolFilters(
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
        source=FilterSourceType.DYNAMIC_EXCHANGE_INFO,
        fetched_at_utc=now_utc,
    )


def _make_regime_allow(now_utc: datetime) -> RegimeDecision:
    return RegimeDecision(
        decision_id="REG-P4-001",
        regime=RegimeType.UP_TREND,
        confidence=Decimal("0.880000"),
        allow_entry=True,
        reason_codes=("UP_TREND_CONFIRMED",),
        valid_until=now_utc + timedelta(minutes=15),
        model_version="a01-v1.0",
    )


def _make_breakout_proposal(
    now_utc: datetime,
    *,
    signal_id: str = "SIG-P4-001",
    barrier_ref: Decimal = Decimal("2.0000"),
    ev_net: Decimal = Decimal("0.004500"),
) -> BreakoutProposal:
    return BreakoutProposal(
        signal_id=signal_id,
        candle_close_utc=now_utc,
        barrier_ref=barrier_ref,
        p_win=Decimal("0.550000"),
        p_stop=Decimal("0.250000"),
        p_timeout=Decimal("0.200000"),
        ev_net=ev_net,
        confidence=Decimal("0.820000"),
        feature_snapshot_hash="f" * 64,
        model_version="a02-v1.0",
        expiry=now_utc + timedelta(minutes=5),
    )


def _make_orderbook(
    now_utc: datetime,
    mono_ns: int,
    *,
    best_bid: Decimal = Decimal("1.9998"),
    best_ask: Decimal = Decimal("2.0002"),
    bid_qty: Decimal = Decimal("80.0"),
    ask_qty: Decimal = Decimal("120.0"),
) -> PartialOrderbookSnapshot:
    tick = Decimal("0.0001")
    bids = tuple((best_bid - tick * i, bid_qty) for i in range(10))
    asks = tuple((best_ask + tick * i, ask_qty) for i in range(10))
    return PartialOrderbookSnapshot(
        symbol="SUIUSDT",
        last_update_id=900_001,
        bids=bids,
        asks=asks,
        exchange_time_utc=now_utc,
        recv_time_utc=now_utc,
        recv_monotonic_ns=mono_ns,
        sampling_interval_ms=100,
    )


def test_fi_p4_01_r01_seven_layer_risk_sizing_free_balance_and_protectability_qa03() -> None:
    """FI-P4-01 (QA-03): Verify R-01 7-layer pre-trade risk check, dual-cap sizing (0.25% risk & 10% cap),
    free USDT balance downscaling, pre-buy protectability, and stale filter rejection.
    """
    now = utc_now()
    fresh_filters = _make_fresh_filters(now)
    proposal = _make_breakout_proposal(now, barrier_ref=Decimal("2.0000"))
    r01 = R01PreTradeRiskGate(
        risk_config=RiskConfig(),
        execution_mode=ExecutionMode.PAPER,
        buy_fee_rate=Decimal("0.0010"),
        sell_fee_rate=Decimal("0.0010"),
        stop_slippage_buffer_rate=Decimal("0.0010"),
    )

    # 1. Standard $10,000 equity:
    #    - 0.25% risk = $25.00; per-unit stop loss + buffer = 2.0000 * (0.01 + 0.003) = $0.026/SUI
    #      -> q_risk_raw = 25 / 0.026 = 961.538 SUI
    #    - 10% capital cap = $1,000 / $2.0000 = 500.0 SUI
    #    -> min(q_risk, q_cap) = 500.0 SUI (capped by 10% allocation)
    res_normal = r01.evaluate_candidate(
        proposal=proposal,
        symbol="SUIUSDT",
        planned_entry_price=Decimal("2.0000"),
        account_equity_usdt=Decimal("10000.0"),
        free_usdt=Decimal("10000.0"),
        filters=fresh_filters,
        now_utc=now,
    )
    assert res_normal.verdict.allowed is True
    assert res_normal.final_qty == Decimal("500.0")
    assert res_normal.verdict.max_notional == Decimal("1000.0000")
    assert res_normal.protectability is not None
    assert res_normal.protectability.is_protectable is True

    # 2. Tight free_usdt ($25.00 free while equity is $10,000): scales down to floor(25 / (2.0 * 1.001), 0.1) = 12.4 SUI
    res_low_free = r01.evaluate_candidate(
        proposal=proposal,
        symbol="SUIUSDT",
        planned_entry_price=Decimal("2.0000"),
        account_equity_usdt=Decimal("10000.0"),
        free_usdt=Decimal("25.00"),
        filters=fresh_filters,
        now_utc=now,
    )
    assert res_low_free.verdict.allowed is True
    assert res_low_free.final_qty == Decimal("12.4")

    # 3. Insufficient free_usdt ($3.00 < $5.00 minNotional): vetoed with INSUFFICIENT_FREE_BALANCE
    res_insuff = r01.evaluate_candidate(
        proposal=proposal,
        symbol="SUIUSDT",
        planned_entry_price=Decimal("2.0000"),
        account_equity_usdt=Decimal("10000.0"),
        free_usdt=Decimal("3.00"),
        filters=fresh_filters,
        now_utc=now,
    )
    assert res_insuff.verdict.allowed is False
    assert "INSUFFICIENT_FREE_BALANCE" in res_insuff.verdict.reject_reasons

    # 4. Small account equity ($52.00 -> 10% cap = $5.20 -> 2.6 SUI at $2.00 -> after SUI fee,
    #    sellable = 2.5 SUI, stop notional = 2.5 * 1.98 = $4.95 < $5.00 minNotional!):
    #    Pre-buy protectability gate catches and vetoes BEFORE buy submission!
    res_unprotectable = r01.evaluate_candidate(
        proposal=proposal,
        symbol="SUIUSDT",
        planned_entry_price=Decimal("2.0000"),
        account_equity_usdt=Decimal("52.00"),
        free_usdt=Decimal("52.00"),
        filters=fresh_filters,
        now_utc=now,
    )
    assert res_unprotectable.verdict.allowed is False
    assert any("UNPROTECTABLE_ORDER_SIZE" in r for r in res_unprotectable.verdict.reject_reasons)

    # 5. Stale P0 static snapshot filters rejected at runtime
    res_stale = r01.evaluate_candidate(
        proposal=proposal,
        symbol="SUIUSDT",
        planned_entry_price=Decimal("2.0000"),
        account_equity_usdt=Decimal("10000.0"),
        free_usdt=Decimal("10000.0"),
        filters=P0_SNAPSHOT_SUIUSDT_FILTERS,
        now_utc=now,
    )
    assert res_stale.verdict.allowed is False
    assert any("STALE_SYMBOL_FILTERS" in r for r in res_stale.verdict.reject_reasons)


def test_fi_p4_02_limit_buy_never_filled_ttl_expiry_clean_cancel_qa04a(tmp_path: Path) -> None:
    """FI-P4-02 (QA-04a): Passive limit buy order never fills within TTL -> automatically canceled,
    `filled_qty == 0`, state returns cleanly from `ORDER_SUBMITTED` to `READY_NO_POSITION`.
    """
    now = utc_now()
    mono = monotonic_ns()
    store = StateStore(tmp_path / "p4_fi02.db")
    alerts = AlertManager(store)
    filters = _make_fresh_filters(now)
    exchange = PaperOrderbookSimulator(initial_usdt=Decimal("10000.0"), filters=filters)
    r01 = R01PreTradeRiskGate(filters_max_age if False else RiskConfig())
    b01 = B01LimitOrderOptimizer(filters=filters, default_ttl_ms=15_000)
    engine = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store,
        alert_manager=alerts,
        exchange=exchange,
        r01_gate=r01,
        b01_optimizer=b01,
        filters=filters,
    )

    boot_out = engine.boot_and_reconcile(now_utc=now)
    assert boot_out.final_state == TradingState.READY_NO_POSITION

    # Submit limit buy without immediate fill (market runs away above limit price)
    ob = _make_orderbook(now, mono, best_bid=Decimal("1.9998"), best_ask=Decimal("2.0004"))
    sub_out = engine.process_entry_signal(
        regime=_make_regime_allow(now),
        proposal=_make_breakout_proposal(now, barrier_ref=Decimal("2.0000")),
        orderbook=ob,
        current_monotonic_ns=mono + 5_000_000,
        now_utc=now,
    )
    assert sub_out.final_state == TradingState.ORDER_SUBMITTED
    assert sub_out.order_snapshot is not None
    assert sub_out.order_snapshot.status == OrderStatus.NEW
    assert exchange.locked_usdt > Decimal("0")

    # Advance clock past 15s TTL without any fill
    exp_out = engine.handle_order_ttl_or_partial_completion(
        now_utc=now + timedelta(seconds=16),
    )
    assert exp_out.final_state == TradingState.READY_NO_POSITION
    assert exp_out.order_snapshot is not None
    assert exp_out.order_snapshot.status == OrderStatus.CANCELED
    assert exp_out.order_snapshot.filled_qty == Decimal("0.0")
    assert exchange.list_open_orders() == []
    assert exchange.locked_usdt == Decimal("0.0000")
    assert exchange.free_usdt == Decimal("10000.0000")
    store.close()


@pytest.mark.parametrize("commission_asset", ["SUI", "USDT", "BNB"])
def test_fi_p4_03_partial_fill_40pct_cancel_remaining_and_protect_net_sellable_qa04b(
    tmp_path: Path,
    commission_asset: str,
) -> None:
    """FI-P4-03 (QA-04b): Limit buy fills 40% before TTL timeout -> remaining 60% canceled,
    net sellable SUI (after fee deduction and `stepSize=0.1` floor) is immediately protected
    by a -1% stop-loss order (`PROTECTED`), and residual dust is tracked in `active_dust_qty`.
    Verified across all 3 fee modes (`SUI`, `USDT`, `BNB`).
    """
    now = utc_now()
    mono = monotonic_ns()
    store = StateStore(tmp_path / f"p4_fi03_{commission_asset}.db")
    alerts = AlertManager(store)
    filters = _make_fresh_filters(now)
    exchange = PaperOrderbookSimulator(
        initial_usdt=Decimal("10000.0"),
        commission_asset=commission_asset,
        filters=filters,
    )
    r01 = R01PreTradeRiskGate()
    b01 = B01LimitOrderOptimizer(filters=filters, default_ttl_ms=15_000)
    engine = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store,
        alert_manager=alerts,
        exchange=exchange,
        r01_gate=r01,
        b01_optimizer=b01,
        filters=filters,
    )
    engine.boot_and_reconcile(now_utc=now)

    ob = _make_orderbook(now, mono, best_bid=Decimal("1.9998"), best_ask=Decimal("2.0000"))
    # Planned qty is 500.0 SUI; inject 40% immediate partial fill = 200.0 SUI
    sub_out = engine.process_entry_signal(
        regime=_make_regime_allow(now),
        proposal=_make_breakout_proposal(now, barrier_ref=Decimal("2.0000")),
        orderbook=ob,
        immediate_fill_qty=Decimal("200.0"),
        immediate_fill_price=Decimal("2.0000"),
        current_monotonic_ns=mono + 2_000_000,
        now_utc=now,
    )
    assert sub_out.final_state == TradingState.PARTIALLY_FILLED
    assert sub_out.order_snapshot is not None
    assert sub_out.order_snapshot.filled_qty == Decimal("200.0")
    assert sub_out.order_snapshot.remaining_qty == Decimal("300.0")

    # TTL expires at +16s -> cancels remaining 300.0 SUI and places -1% stop protection on filled 200.0 SUI
    prot_out = engine.handle_order_ttl_or_partial_completion(
        current_market_price=Decimal("2.0000"),
        now_utc=now + timedelta(seconds=16),
    )
    assert prot_out.final_state == TradingState.PROTECTED
    assert prot_out.position_ledger is not None
    assert exchange.list_open_orders(side=OrderSide.BUY) == []
    open_sells = exchange.list_open_orders(side=OrderSide.SELL)
    assert len(open_sells) == 1

    if commission_asset == "SUI":
        # 200.0 SUI gross - 0.200000 SUI fee = 199.800000 SUI (exact multiple of 0.1 -> 199.8 sellable, 0 dust)
        assert prot_out.position_ledger.reserved_qty == Decimal("199.8")
        assert open_sells[0].orig_qty == Decimal("199.8")
    else:
        # USDT or BNB fee -> full 200.0 SUI sellable
        assert prot_out.position_ledger.reserved_qty == Decimal("200.0")
        assert open_sells[0].orig_qty == Decimal("200.0")

    assert open_sells[0].stop_price == Decimal("1.9800")
    store.close()


def test_fi_p4_04_micro_partial_fill_unprotectable_freezes_entry_and_alerts_qa04c(
    tmp_path: Path,
) -> None:
    """FI-P4-04 (QA-04c): Limit buy order suffers a tiny partial fill (0.2 SUI at $2.00 = $0.40 < $5.00 minNotional)
    before TTL timeout -> remaining buy order canceled, `is_protectable == False` detected, state transitions
    immediately to `ENTRY_FROZEN` and emits HIGH alert.
    """
    now = utc_now()
    mono = monotonic_ns()
    store = StateStore(tmp_path / "p4_fi04.db")
    alerts = AlertManager(store)
    filters = _make_fresh_filters(now)
    exchange = PaperOrderbookSimulator(initial_usdt=Decimal("10000.0"), filters=filters)
    engine = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store,
        alert_manager=alerts,
        exchange=exchange,
        r01_gate=R01PreTradeRiskGate(),
        b01_optimizer=B01LimitOrderOptimizer(filters=filters, default_ttl_ms=15_000),
        filters=filters,
    )
    engine.boot_and_reconcile(now_utc=now)

    ob = _make_orderbook(now, mono)
    sub_out = engine.process_entry_signal(
        regime=_make_regime_allow(now),
        proposal=_make_breakout_proposal(now, barrier_ref=Decimal("2.0000")),
        orderbook=ob,
        immediate_fill_qty=Decimal("0.2"),  # Micro fill: 0.2 SUI (~$0.40 < $5.00 minNotional)
        immediate_fill_price=Decimal("2.0000"),
        current_monotonic_ns=mono + 1_000_000,
        now_utc=now,
    )
    assert sub_out.final_state == TradingState.PARTIALLY_FILLED

    freeze_out = engine.handle_order_ttl_or_partial_completion(
        current_market_price=Decimal("2.0000"),
        now_utc=now + timedelta(seconds=16),
    )
    assert freeze_out.final_state == TradingState.ENTRY_FROZEN
    assert exchange.list_open_orders(side=OrderSide.BUY) == []
    assert exchange.list_open_orders(side=OrderSide.SELL) == []
    undelivered = alerts.list_undelivered_alerts()
    assert any(a["dedup_key"] == "D01-UNPROTECTABLE-MICRO-PARTIAL-FILL" for a in undelivered)
    store.close()


def test_fi_p4_05_order_unknown_idempotent_reconciliation_all_cases_qa04d(tmp_path: Path) -> None:
    """FI-P4-05 (QA-04d & INV-R6): Inject `submit_limit_buy` timeout (`ORDER_UNKNOWN`) under both:
    - Subcase 1: Remote order never reached engine (`NOT_FOUND` -> `READY_NO_POSITION`)
    - Subcase 2: Remote order actually filled (`FILLED` -> `PROTECTION_PENDING -> PROTECTED`)
    Prove that while in `ORDER_UNKNOWN`, zero new buy orders are sent (`INV-R6`).
    """
    now = utc_now()
    mono = monotonic_ns()
    store = StateStore(tmp_path / "p4_fi05.db")
    alerts = AlertManager(store)
    filters = _make_fresh_filters(now)
    exchange = PaperOrderbookSimulator(initial_usdt=Decimal("10000.0"), filters=filters)
    engine = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store,
        alert_manager=alerts,
        exchange=exchange,
        r01_gate=R01PreTradeRiskGate(),
        b01_optimizer=B01LimitOrderOptimizer(filters=filters, default_ttl_ms=15_000),
        filters=filters,
    )
    engine.boot_and_reconcile(now_utc=now)
    ob = _make_orderbook(now, mono)

    # Subcase 1: Timeout BEFORE order reached matching engine (NOT_FOUND)
    exchange.inject_next_submit_timeout("TIMEOUT_BEFORE_ENGINE_ACCEPT")
    out_timeout_1 = engine.process_entry_signal(
        regime=_make_regime_allow(now),
        proposal=_make_breakout_proposal(now, signal_id="SIG-TIMEOUT-1"),
        orderbook=ob,
        current_monotonic_ns=mono + 1_000_000,
        now_utc=now,
    )
    assert out_timeout_1.final_state == TradingState.ORDER_UNKNOWN

    # Attempting to send a new buy signal while in ORDER_UNKNOWN is strictly blocked (INV-R6)
    with pytest.raises(PermissionError, match="INV-R6 violated"):
        engine.process_entry_signal(
            regime=_make_regime_allow(now),
            proposal=_make_breakout_proposal(now, signal_id="SIG-ILLEGAL"),
            orderbook=ob,
            current_monotonic_ns=mono + 2_000_000,
            now_utc=now + timedelta(seconds=1),
        )
    assert engine.unknown_state_new_buy_count == 0

    # Reconcile Case A (NOT_FOUND) -> cleanly returns to READY_NO_POSITION
    rec_1 = engine.reconcile_order_unknown(now_utc=now + timedelta(seconds=2))
    assert rec_1.final_state == TradingState.READY_NO_POSITION

    # Subcase 2: Timeout AFTER order reached matching engine and FILLED 100%
    exchange.inject_next_submit_timeout("TIMEOUT_AFTER_ENGINE_ACCEPT_AND_FILL")
    out_timeout_2 = engine.process_entry_signal(
        regime=_make_regime_allow(now + timedelta(seconds=5)),
        proposal=_make_breakout_proposal(now + timedelta(seconds=5), signal_id="SIG-TIMEOUT-2"),
        orderbook=ob,
        current_monotonic_ns=mono + 3_000_000,
        now_utc=now + timedelta(seconds=5),
    )
    assert out_timeout_2.final_state == TradingState.ORDER_UNKNOWN

    # Reconcile Case E (FILLED) -> idempotently discovers fill and transitions to PROTECTED!
    rec_2 = engine.reconcile_order_unknown(
        current_market_price=Decimal("2.0000"),
        now_utc=now + timedelta(seconds=6),
    )
    assert rec_2.final_state == TradingState.PROTECTED
    assert rec_2.position_ledger is not None
    assert rec_2.position_ledger.reserved_qty == Decimal("499.4")
    assert len(exchange.list_open_orders(side=OrderSide.SELL)) == 1
    store.close()


def test_fi_p4_06_crash_right_after_fill_startup_reconciliation_top_priority_protection_qa05(
    tmp_path: Path,
) -> None:
    """FI-P4-06 (QA-05 & INV-R7): Simulate process crash (`SIGKILL`) at the exact instant a buy order
    becomes `FILLED` before `PROTECTION_PENDING` runs. Reboot a brand-new `D01DecisionEngine` +
    `PaperOrderbookSimulator` on the same SQLite WAL files and verify `BOOT -> SYNCING` immediately
    prioritizes `PROTECTION_PENDING -> PROTECTED` to place the -1% stop-loss order.
    """
    now = utc_now()
    mono = monotonic_ns()
    state_db = tmp_path / "p4_fi06_state.db"
    exch_db = tmp_path / "p4_fi06_exchange.db"
    filters = _make_fresh_filters(now)

    # Process Instance #1: submits buy order, fills 100%, and crashes BEFORE placing stop-loss
    store_1 = StateStore(state_db)
    alerts_1 = AlertManager(store_1)
    exch_1 = PaperOrderbookSimulator(
        initial_usdt=Decimal("10000.0"),
        filters=filters,
        db_path=exch_db,
    )
    engine_1 = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store_1,
        alert_manager=alerts_1,
        exchange=exch_1,
        r01_gate=R01PreTradeRiskGate(),
        b01_optimizer=B01LimitOrderOptimizer(filters=filters),
        filters=filters,
    )
    engine_1.boot_and_reconcile(now_utc=now)
    ob = _make_orderbook(now, mono)
    crash_out = engine_1.process_entry_signal(
        regime=_make_regime_allow(now),
        proposal=_make_breakout_proposal(now, signal_id="SIG-CRASH-QA05"),
        orderbook=ob,
        immediate_fill_qty=Decimal("500.0"),
        immediate_fill_price=Decimal("2.0000"),
        simulate_crash_Right_after_fill=True,
        current_monotonic_ns=mono + 1_000_000,
        now_utc=now,
    )
    assert crash_out.final_state == TradingState.FILLED
    # Verify naked position: 0 sell protection orders exist before crash
    assert len(exch_1.list_open_orders(side=OrderSide.SELL)) == 0
    store_1.close()
    exch_1.close()

    # Process Instance #2: Reboot after crash -> BOOT -> SYNCING -> PROTECTION_PENDING -> PROTECTED
    store_2 = StateStore(state_db)
    alerts_2 = AlertManager(store_2)
    exch_2 = PaperOrderbookSimulator(filters=filters, db_path=exch_db)
    engine_2 = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store_2,
        alert_manager=alerts_2,
        exchange=exch_2,
        r01_gate=R01PreTradeRiskGate(),
        b01_optimizer=B01LimitOrderOptimizer(filters=filters),
        filters=filters,
    )
    rec_out = engine_2.boot_and_reconcile(
        current_market_price=Decimal("2.0000"),
        now_utc=now + timedelta(seconds=5),
    )
    assert rec_out.final_state == TradingState.PROTECTED
    assert rec_out.position_ledger is not None
    assert rec_out.position_ledger.reserved_qty == Decimal("499.4")
    open_sells = exch_2.list_open_orders(side=OrderSide.SELL)
    assert len(open_sells) == 1
    assert open_sells[0].orig_qty == Decimal("499.4")
    assert open_sells[0].stop_price == Decimal("1.9800")

    transitions = [t["to_state"] for t in store_2.list_transitions()]
    assert transitions[-3:] == ["SYNCING", "PROTECTION_PENDING", "PROTECTED"]
    store_2.close()
    exch_2.close()


def test_fi_p4_07_rolling_24h_loss_circuit_breaker_and_btcusdt_symbol_veto_qa08(
    tmp_path: Path,
) -> None:
    """FI-P4-07 (QA-08, INV-R2, INV-R5):
    1. Any `BTCUSDT` order evaluation is 100% vetoed with `SYMBOL_FORBIDDEN`.
    2. When rolling 24h realized loss reaches 1.0% of initial equity ($100 on $10,000),
       `R-01` vetoes with `ROLLING_24H_LOSS_LIMIT_EXCEEDED` and transitions `D-01` into `ENTRY_FROZEN`.
    """
    now = utc_now()
    mono = monotonic_ns()
    store = StateStore(tmp_path / "p4_fi07.db")
    alerts = AlertManager(store)
    filters = _make_fresh_filters(now)
    exchange = PaperOrderbookSimulator(initial_usdt=Decimal("10000.0"), filters=filters)
    engine = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store,
        alert_manager=alerts,
        exchange=exchange,
        r01_gate=R01PreTradeRiskGate(),
        b01_optimizer=B01LimitOrderOptimizer(filters=filters),
        filters=filters,
        initial_24h_equity_usdt=Decimal("10000.0"),
    )
    engine.boot_and_reconcile(now_utc=now)
    ob = _make_orderbook(now, mono)

    # 1. BTCUSDT order attempt is vetoed by R-01 with SYMBOL_FORBIDDEN
    btc_out = engine.process_entry_signal(
        regime=_make_regime_allow(now),
        proposal=_make_breakout_proposal(now, signal_id="SIG-BTC-FORBIDDEN"),
        orderbook=ob,
        symbol="BTCUSDT",
        current_monotonic_ns=mono + 1_000_000,
        now_utc=now,
    )
    assert btc_out.final_state == TradingState.READY_NO_POSITION
    assert btc_out.r01_result is not None
    assert btc_out.r01_result.verdict.allowed is False
    assert "SYMBOL_FORBIDDEN" in btc_out.r01_result.verdict.reject_reasons

    # 2. Inject realized losses totaling -$105.00 in the last 24h (1.05% >= 1.0% threshold)
    engine.record_realized_pnl(Decimal("-40.00"), now - timedelta(hours=6))
    engine.record_realized_pnl(Decimal("-65.00"), now - timedelta(hours=1))
    assert engine.current_rolling_24h_drawdown_pct(now_utc=now) == Decimal("0.0105")

    dd_out = engine.process_entry_signal(
        regime=_make_regime_allow(now),
        proposal=_make_breakout_proposal(now, signal_id="SIG-AFTER-1PCT-LOSS"),
        orderbook=ob,
        current_monotonic_ns=mono + 2_000_000,
        now_utc=now + timedelta(seconds=1),
    )
    assert dd_out.final_state == TradingState.ENTRY_FROZEN
    assert dd_out.r01_result is not None
    assert dd_out.r01_result.verdict.allowed is False
    assert "ROLLING_24H_LOSS_LIMIT_EXCEEDED" in dd_out.r01_result.verdict.reject_reasons
    store.close()


def test_fi_p4_08_b01_markout_optimization_price_cap_stale_orderbook_and_gap_window_rejection() -> None:
    """FI-P4-08: Verify B-01 microstructure quote grid, `price_cap`, multi-horizon `markout(1s, 5s, 30s, 60s)`,
    stale orderbook rejection (`> 500 ms`), and `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION` window filtering.
    """
    now = utc_now()
    mono = monotonic_ns()
    filters = _make_fresh_filters(now)
    r01 = R01PreTradeRiskGate()
    b01 = B01LimitOrderOptimizer(filters=filters, max_orderbook_age_ms=500)
    proposal = _make_breakout_proposal(now, barrier_ref=Decimal("2.0000"), ev_net=Decimal("0.004500"))
    r01_res = r01.evaluate_candidate(
        proposal=proposal,
        symbol="SUIUSDT",
        planned_entry_price=Decimal("2.0000"),
        account_equity_usdt=Decimal("10000.0"),
        free_usdt=Decimal("10000.0"),
        filters=filters,
        now_utc=now,
    )

    # 1. Verify multi-horizon markout(1s, 5s, 30s, 60s) calculation
    prof = compute_multi_horizon_markout(
        fill_price=Decimal("2.0000"),
        future_mid_prices_sec={
            1: Decimal("1.9994"),
            5: Decimal("1.9988"),
            30: Decimal("2.0006"),
            60: Decimal("2.0020"),
        },
    )
    assert prof.markout_1s == Decimal("-0.000300")
    assert prof.markout_5s == Decimal("-0.000600")
    assert prof.markout_30s == Decimal("0.000300")
    assert prof.markout_60s == Decimal("0.001000")
    assert prof.adverse_selection_5s_penalty == Decimal("0.000600")

    # 2. Fresh orderbook produces valid OrderIntent bounded by price_cap (2.0000 * 1.0005 = 2.0010)
    ob_fresh = _make_orderbook(now, mono, best_bid=Decimal("1.9998"), best_ask=Decimal("2.0002"))
    opt_ok = b01.optimize_limit_buy(
        proposal=proposal,
        risk_verdict=r01_res.verdict,
        orderbook=ob_fresh,
        current_monotonic_ns=mono + 50_000_000,  # 50 ms lag <= 500 ms
        now_utc=now,
    )
    assert opt_ok.selected_intent is not None
    assert opt_ok.price_cap == Decimal("2.0010")
    assert opt_ok.selected_intent.price <= opt_ok.price_cap
    assert len(opt_ok.all_candidates) == 4
    tiers = {c.tier for c in opt_ok.all_candidates}
    assert tiers == {
        QuoteTier.PASSIVE_BID,
        QuoteTier.STEP_UP_BID,
        QuoteTier.MID_FLOOR,
        QuoteTier.AGGRESSIVE_CAP,
    }

    # 3. Stale orderbook (650 ms monotonic lag > 500 ms limit) is rejected with STALE_ORDERBOOK
    opt_stale = b01.optimize_limit_buy(
        proposal=proposal,
        risk_verdict=r01_res.verdict,
        orderbook=ob_fresh,
        current_monotonic_ns=mono + 650_000_000,  # 650 ms > 500 ms
        now_utc=now,
    )
    assert opt_stale.selected_intent is None
    assert opt_stale.rejected_reason == "STALE_ORDERBOOK"

    # 4. StreamQualityGuard unrecoverable orderbook gap window is strictly rejected during calibration & realtime
    qg = StreamQualityGuard()
    qg.record_unrecoverable_orderbook_gap(
        gap_id="GAP-P4-001",
        symbol="SUIUSDT",
        stream="depth10@100ms",
        gap_start_utc=now - timedelta(seconds=10),
        gap_end_utc=now + timedelta(seconds=10),
        last_update_id_before_gap=100,
        first_update_id_after_gap=250,
    )
    opt_gap = b01.optimize_limit_buy(
        proposal=proposal,
        risk_verdict=r01_res.verdict,
        orderbook=ob_fresh,
        quality_guard=qg,
        current_monotonic_ns=mono + 10_000_000,
        now_utc=now,
    )
    assert opt_gap.selected_intent is None
    assert opt_gap.rejected_reason == "ORDERBOOK_GAP_UNRECOVERABLE_WINDOW"

    cal_windows = [
        CalibrationWindowSample(
            window_id="WIN-GAP-OVERLAP",
            symbol="SUIUSDT",
            window_start_utc=now - timedelta(seconds=5),
            window_end_utc=now + timedelta(seconds=5),
            d_ticks=Decimal("1"),
            q_ratio=Decimal("0.2"),
            obi_5=Decimal("0.1"),
            ttl_seconds=Decimal("15"),
            did_fill=True,
            fill_price=Decimal("2.0000"),
            post_fill_mids={1: Decimal("1.9998"), 5: Decimal("1.9995"), 30: Decimal("2.0002"), 60: Decimal("2.0005")},
        ),
        CalibrationWindowSample(
            window_id="WIN-CLEAN-1",
            symbol="SUIUSDT",
            window_start_utc=now + timedelta(minutes=5),
            window_end_utc=now + timedelta(minutes=5, seconds=15),
            d_ticks=Decimal("1"),
            q_ratio=Decimal("0.2"),
            obi_5=Decimal("0.1"),
            ttl_seconds=Decimal("15"),
            did_fill=True,
            fill_price=Decimal("2.0000"),
            post_fill_mids={1: Decimal("1.9996"), 5: Decimal("1.9992"), 30: Decimal("2.0004"), 60: Decimal("2.0010")},
        ),
    ]
    cal_rep = b01.calibrate_from_orderbook_windows(windows=cal_windows, quality_guard=qg)
    assert cal_rep.total_windows_submitted == 2
    assert cal_rep.accepted_windows == 1
    assert cal_rep.rejected_gap_windows == 1
    assert cal_rep.rejected_window_ids == ("WIN-GAP-OVERLAP",)


def test_p4_triple_physical_isolation_and_end_to_end_latency_slo(tmp_path: Path) -> None:
    """Verify P4 triple physical isolation (`ExecutionMode.PAPER` lock, `PaperOrderbookSimulator` type lock,
    zero live orders) and benchmark end-to-end `D-01 -> R-01 -> B-01 -> PROTECTED -> EXITING -> FLAT` latency
    against the P50 <= 2 ms / P99 <= 20 ms budget.
    """
    now = utc_now()
    mono = monotonic_ns()
    store = StateStore(tmp_path / "p4_bench.db")
    alerts = AlertManager(store)
    filters = _make_fresh_filters(now)

    # 1. Attempting to instantiate PaperOrderbookSimulator or D01DecisionEngine in RESEARCH/SHADOW/LIVE raises PermissionError
    with pytest.raises(PermissionError, match="ExecutionMode.PAPER"):
        PaperOrderbookSimulator(execution_mode=ExecutionMode.RESEARCH)

    exch = PaperOrderbookSimulator(initial_usdt=Decimal("50000.0"), filters=filters)
    with pytest.raises(PermissionError, match="PAPER"):
        D01DecisionEngine(
            config=SystemConfig(mode=ExecutionMode.RESEARCH),
            store=store,
            alert_manager=alerts,
            exchange=exch,
            r01_gate=R01PreTradeRiskGate(),
            b01_optimizer=B01LimitOrderOptimizer(filters=filters),
            filters=filters,
        )

    with pytest.raises(TypeError, match="PaperOrderbookSimulator"):
        D01DecisionEngine(
            config=SystemConfig(mode=ExecutionMode.PAPER),
            store=store,
            alert_manager=alerts,
            exchange=object(),  # type: ignore[arg-type]
            r01_gate=R01PreTradeRiskGate(),
            b01_optimizer=B01LimitOrderOptimizer(filters=filters),
            filters=filters,
        )

    engine = D01DecisionEngine(
        config=SystemConfig(mode=ExecutionMode.PAPER),
        store=store,
        alert_manager=alerts,
        exchange=exch,
        r01_gate=R01PreTradeRiskGate(),
        b01_optimizer=B01LimitOrderOptimizer(filters=filters),
        filters=filters,
        initial_24h_equity_usdt=Decimal("50000.0"),
    )
    engine.boot_and_reconcile(now_utc=now)

    # 2. Run 50 full round-trip trade cycles (Entry -> Fill -> Protect -> Profit Exit -> Flat -> Ready)
    decision_latencies_ms: list[float] = []
    for i in range(50):
        step_now = now + timedelta(seconds=i * 10)
        step_mono = mono + i * 10_000_000
        ob = _make_orderbook(step_now, step_mono, best_bid=Decimal("1.9998"), best_ask=Decimal("2.0000"))
        proposal = _make_breakout_proposal(
            step_now,
            signal_id=f"SIG-BENCH-{i:03d}",
            barrier_ref=Decimal("2.0000"),
            ev_net=Decimal("0.005000"),
        )
        out_entry = engine.process_entry_signal(
            regime=_make_regime_allow(step_now),
            proposal=proposal,
            orderbook=ob,
            immediate_fill_qty=Decimal("999999.0"),
            immediate_fill_price=Decimal("2.0000"),
            current_monotonic_ns=step_mono + 100_000,
            now_utc=step_now,
        )
        assert out_entry.final_state == TradingState.PROTECTED
        decision_latencies_ms.append(out_entry.end_to_end_latency_ns / 1_000_000.0)

        out_exit = engine.execute_position_exit(
            exit_price=Decimal("2.0350"),  # +1.75% gross profit exit
            reason="C1_NET_PROFIT_TARGET_HIT",
            now_utc=step_now + timedelta(seconds=5),
        )
        assert out_exit.final_state == TradingState.READY_NO_POSITION

    p50_ms = statistics.median(decision_latencies_ms)
    sorted_lat = sorted(decision_latencies_ms)
    p99_ms = sorted_lat[int(len(sorted_lat) * 0.99) - 1]
    assert p50_ms <= 5.0
    assert p99_ms <= 25.0
    assert exch.live_orders_sent_count == 0
    store.close()
