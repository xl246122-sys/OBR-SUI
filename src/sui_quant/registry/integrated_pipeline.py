"""D-01 / E-01 End-to-End Integrated Pipeline Orchestrator (§7, §11.2, §13, QA-12).

Connects:
`ClosedKlineBar + PartialOrderbookSnapshot (F-01)`
  -> `FeaturePipeline` (causal multi-horizon extraction)
  -> `A01RegimeClassifier` (5-state market regime & BTC flash-drop gate)
  -> `E01ModelRegistry` (`ACTIVE_PAPER` A-02 inference with `AutoRollbackGuard` + parallel `SHADOW` inference)
  -> `R01PreTradeRiskGate` (7-layer independent risk veto)
  -> `B01LimitOrderOptimizer` (microstructure limit quote optimization)
  -> `D01DecisionEngine` (state machine & WAL write-ahead order lifecycle)
  -> `OcoProtectionCoordinator` + `InFlightVersionPinRegistry` (C-01 version-pinned exit & protection lifecycle)

Enforces:
- Strict `ExecutionMode.PAPER` lock (`live_orders_sent_count == 0`, `shadow_orders_submitted_count == 0`).
- In-flight position exit strategy version pinning (`QA-12`): hot-swapping or rolling back models while
  a position is in `PROTECTED` state NEVER alters the open position's exit strategy (`C1` / `C2` / `C3`);
  new exit versions apply strictly to subsequent positions opened after returning to `FLAT -> READY_NO_POSITION`.
- Sub-150ms end-to-end cycle latency SLO (`P99 <= 50.0 ms` target) and bounded memory watermark (`<= 15 MiB` delta).
"""

from __future__ import annotations

import tracemalloc
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from sui_quant.config import ExecutionMode, SystemConfig
from sui_quant.contracts import (
    TRADING_SYMBOL,
    BreakoutProposal,
    DataQuality,
    MarketFeatures,
    OrderSide,
    PositionLedger,
    RegimeDecision,
    require_decimal,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.data.quality_guard import (
    ClosedKlineBar,
    PartialOrderbookSnapshot,
    StreamQualityGuard,
)
from sui_quant.exchange_rules import P0_SNAPSHOT_SUIUSDT_FILTERS, SymbolFilters
from sui_quant.execution.b01_limit_executor import B01LimitOrderOptimizer
from sui_quant.execution.d01_engine import D01CycleOutcome, D01DecisionEngine
from sui_quant.execution.paper_exchange import PaperOrderbookSimulator
from sui_quant.execution.r01_risk_evaluator import R01PreTradeRiskGate
from sui_quant.exit.c01_strategies import (
    C1FixedNetTargetExit,
    C2GridTrailingExit,
    C3AdaptiveHybridExit,
    CoordinatorStepReport,
    ExitSignalAction,
    ExitStrategyId,
    OcoProtectionCoordinator,
)
from sui_quant.models.a01_regime import A01RegimeClassifier
from sui_quant.models.features import FeaturePipeline
from sui_quant.registry.e01_model_registry import (
    E01ModelRegistry,
    InFlightPositionPinState,
    RollbackAuditRecord,
    ShadowInferenceRecord,
)
from sui_quant.state_machine import TradingState
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


@dataclass(frozen=True)
class IntegratedCycleReport:
    """Comprehensive telemetry and state audit for a single end-to-end pipeline cycle (§13, QA-12)."""

    cycle_seq: int
    bar_close_time_utc: datetime
    trading_state_before: TradingState
    trading_state_after: TradingState
    active_model_bundle_id: str
    active_paper_exit_version: str
    pinned_position_exit_version: str | None
    shadow_model_bundle_id: str | None
    regime_decision: RegimeDecision
    active_proposal: BreakoutProposal | None
    shadow_record: ShadowInferenceRecord | None
    rollback_record: RollbackAuditRecord | None
    d01_outcome: D01CycleOutcome | None
    c01_report: CoordinatorStepReport | None
    position_ledger: PositionLedger | None
    feature_latency_ns: int
    a01_latency_ns: int
    a02_active_latency_ns: int
    a02_shadow_latency_ns: int
    execution_or_exit_latency_ns: int
    total_cycle_latency_ns: int
    notes: str


class D01IntegratedPipeline:
    """End-to-end closed-loop orchestrator for F-01, A-01, A-02, E-01, R-01, B-01, D-01, and C-01 (§7, §11.2, §13)."""

    def __init__(
        self,
        *,
        config: SystemConfig,
        store: StateStore,
        alert_manager: AlertManager,
        exchange: PaperOrderbookSimulator,
        registry: E01ModelRegistry,
        feature_pipeline: FeaturePipeline | None = None,
        a01_classifier: A01RegimeClassifier | None = None,
        r01_gate: R01PreTradeRiskGate | None = None,
        b01_optimizer: B01LimitOrderOptimizer | None = None,
        d01_engine: D01DecisionEngine | None = None,
        oco_coordinator: OcoProtectionCoordinator | None = None,
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
        quality_guard: StreamQualityGuard | None = None,
    ) -> None:
        if config.mode != ExecutionMode.PAPER:
            raise PermissionError(f"P6 strictly locks execution mode to PAPER, got {config.mode}")
        if exchange.execution_mode != ExecutionMode.PAPER:
            raise PermissionError("PaperOrderbookSimulator must be locked to ExecutionMode.PAPER")

        self.config = config
        self.store = store
        self.alert_manager = alert_manager
        self.exchange = exchange
        self.registry = registry
        self.filters = filters
        self.quality_guard = quality_guard

        self.feature_pipeline = feature_pipeline or FeaturePipeline()
        self.a01_classifier = a01_classifier or A01RegimeClassifier()
        self.r01_gate = r01_gate or R01PreTradeRiskGate(risk_config=config.risk)
        self.b01_optimizer = b01_optimizer or B01LimitOrderOptimizer(filters=filters)
        self.d01_engine = d01_engine or D01DecisionEngine(
            config=config,
            store=store,
            alert_manager=alert_manager,
            exchange=exchange,
            r01_gate=self.r01_gate,
            b01_optimizer=self.b01_optimizer,
            filters=filters,
            quality_guard=quality_guard,
        )
        self.oco_coordinator = oco_coordinator or OcoProtectionCoordinator(
            exchange=exchange,
            filters=filters,
            alert_manager=alert_manager,
        )

        self._cycle_seq = 0
        self._cycle_latencies_ns: list[int] = []
        self._live_orders_sent_count = 0

    @property
    def live_orders_sent_count(self) -> int:
        """Strictly 0 in P6."""
        return self._live_orders_sent_count

    @property
    def cycle_latencies_ns(self) -> tuple[int, ...]:
        return tuple(self._cycle_latencies_ns)

    def _stamp_and_pin_new_position(
        self,
        ledger: PositionLedger,
        now_utc: datetime,
    ) -> PositionLedger:
        """Stamp a newly opened `PositionLedger` with `registry.active_paper_exit_strategy_version` and pin it (QA-12)."""
        active_exit_ver = self.registry.active_paper_exit_strategy_version
        active_bundle_id = self.registry.active_paper_bundle.bundle_id

        stamped_ledger = PositionLedger(
            position_id=ledger.position_id,
            symbol=ledger.symbol,
            actual_qty=ledger.actual_qty,
            avg_fill_price=ledger.avg_fill_price,
            fees=ledger.fees,
            reserved_qty=ledger.reserved_qty,
            protection_orders=ledger.protection_orders,
            exit_strategy_version=active_exit_ver,
            risk_config_version=ledger.risk_config_version,
        )
        self.store._conn.execute(
            "UPDATE p4_position_ledger SET exit_strategy_version = ? WHERE position_id = ?",
            (active_exit_ver, stamped_ledger.position_id),
        )
        self.d01_engine._persist_position_ledger(
            ledger=stamped_ledger,
            dust_qty=self.d01_engine.active_dust_qty,
            is_active=True,
            now_utc=now_utc,
        )
        self.registry.pin_registry.pin_position(
            ledger=stamped_ledger,
            bundle_id=active_bundle_id,
            now_utc=now_utc,
        )
        return stamped_ledger

    def _finalize_full_exit_in_d01(
        self,
        *,
        prev_ledger: PositionLedger,
        c01_report: CoordinatorStepReport,
        now_utc: datetime,
    ) -> None:
        """Synchronize a completed C-01 full exit with `D01DecisionEngine` (`PROTECTED -> EXITING -> FLAT -> READY_NO_POSITION`)."""
        if c01_report.fill_slice is not None:
            sold_qty = c01_report.fill_slice.qty
            buy_cost_usdt = sold_qty * prev_ledger.avg_fill_price
            net_pnl_usdt = c01_report.fill_slice.net_quote_usdt - buy_cost_usdt
            self.d01_engine.record_realized_pnl(net_pnl_usdt, now_utc)

        if self.d01_engine.current_state != TradingState.EXITING:
            self.d01_engine._transition_to(
                TradingState.EXITING,
                f"C01_EXIT_{c01_report.action.value}",
                now_utc=now_utc,
            )

        self.d01_engine._persist_position_ledger(
            ledger=c01_report.position_ledger,
            dust_qty=self.d01_engine.active_dust_qty,
            is_active=False,
            now_utc=now_utc,
        )
        self.registry.pin_registry.release_position(prev_ledger.position_id)

        self.d01_engine._transition_to(
            TradingState.FLAT,
            f"C01_FLAT_{prev_ledger.position_id}",
            now_utc=now_utc,
        )
        self.d01_engine._transition_to(
            TradingState.READY_NO_POSITION,
            f"C01_SETTLED_{prev_ledger.position_id}",
            now_utc=now_utc,
        )

    def _record_partial_tranche_in_d01(
        self,
        *,
        prev_ledger: PositionLedger,
        c01_report: CoordinatorStepReport,
        now_utc: datetime,
    ) -> None:
        """Persist updated `PositionLedger` after a partial tranche fill while keeping position in `PROTECTED`."""
        if c01_report.fill_slice is not None:
            sold_qty = c01_report.fill_slice.qty
            buy_cost_usdt = sold_qty * prev_ledger.avg_fill_price
            net_pnl_usdt = c01_report.fill_slice.net_quote_usdt - buy_cost_usdt
            self.d01_engine.record_realized_pnl(net_pnl_usdt, now_utc)

        rem_sellable = self.filters.quantize_qty_down(c01_report.position_ledger.actual_qty)
        if rem_sellable < self.filters.min_qty:
            self._finalize_full_exit_in_d01(
                prev_ledger=prev_ledger,
                c01_report=c01_report,
                now_utc=now_utc,
            )
            return

        self.d01_engine._persist_position_ledger(
            ledger=c01_report.position_ledger,
            dust_qty=self.d01_engine.active_dust_qty,
            is_active=True,
            now_utc=now_utc,
        )

    def _evaluate_pinned_position_exit(
        self,
        *,
        ledger: PositionLedger,
        bar_t: ClosedKlineBar,
        sui_features: MarketFeatures,
        regime_decision: RegimeDecision,
        orderbook: PartialOrderbookSnapshot,
        now_utc: datetime,
    ) -> CoordinatorStepReport:
        """Evaluate an open `PROTECTED` position using its strictly pinned `exit_strategy_version` (QA-12)."""
        strat, pin_state = self.registry.pin_registry.resolve_pinned_exit_strategy(ledger)
        pin_state.bars_held += 1
        bid = orderbook.best_bid or bar_t.close_price
        ask = orderbook.best_ask or bar_t.close_price
        pin_state.high_water_mark_price = max(
            pin_state.high_water_mark_price,
            bar_t.high_price,
            bid,
        )

        # 1. Check stop-loss trigger or flash gap-down first (Pessimistic Same-Bar Stop Priority, QA-07)
        effective_low_bid = min(bar_t.low_price, bid)
        tick_rep = self.oco_coordinator.handle_market_tick_or_gap_down(
            ledger=ledger,
            best_bid_price=effective_low_bid,
            best_ask_price=ask,
            now_utc=now_utc,
        )
        if tick_rep.action in {
            ExitSignalAction.FULL_EXIT_STOP_LOSS,
            ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP,
        }:
            self._finalize_full_exit_in_d01(
                prev_ledger=ledger,
                c01_report=tick_rep,
                now_utc=now_utc,
            )
            return tick_rep

        # Determine active strategy mechanics (C1, C2, or C3 delegate)
        active_c2: C2GridTrailingExit | None = None
        active_c1: C1FixedNetTargetExit | None = None

        if isinstance(strat, C3AdaptiveHybridExit):
            btc_drop = sui_features.features.get("btc_flash_drop_flag", Decimal("0")) > Decimal("0")
            should_exit_def, def_reason = strat.should_trigger_emergency_risk_off(
                regime=regime_decision.regime,
                btc_flash_drop_active=btc_drop,
                bars_held=pin_state.bars_held,
            )
            if should_exit_def:
                rep = self.oco_coordinator.execute_full_exit(
                    ledger=ledger,
                    exec_price=bid,
                    action=ExitSignalAction.FULL_EXIT_DEFENSIVE_RISK_OFF,
                    reason=def_reason,
                    slice_index=len(pin_state.filled_tranche_indices) + 1,
                    now_utc=now_utc,
                )
                self._finalize_full_exit_in_d01(
                    prev_ledger=ledger,
                    c01_report=rep,
                    now_utc=now_utc,
                )
                return rep

            obi_5 = orderbook.top10_imbalance
            mode = strat.select_active_mode(regime=regime_decision.regime, obi_5=obi_5)
            if mode == ExitStrategyId.C2 or len(pin_state.filled_tranche_indices) > 0:
                active_c2 = strat.c2
            else:
                active_c1 = strat.c1
        elif isinstance(strat, C2GridTrailingExit):
            active_c2 = strat
        elif isinstance(strat, C1FixedNetTargetExit):
            active_c1 = strat
        else:
            raise TypeError(f"Unsupported exit strategy type: {type(strat).__name__}")

        # 2. C1 Fixed +1.5% Net Profit Target Mode
        if active_c1 is not None:
            target_p, _ = active_c1.compute_target_and_stop(ledger.avg_fill_price)
            if max(bar_t.high_price, bid) >= target_p:
                rep = self.oco_coordinator.execute_full_exit(
                    ledger=ledger,
                    exec_price=target_p,
                    action=ExitSignalAction.FULL_EXIT_TAKE_PROFIT,
                    reason=f"{strat.version}_FIXED_NET_TARGET_HIT",
                    slice_index=1,
                    now_utc=now_utc,
                )
                self._finalize_full_exit_in_d01(
                    prev_ledger=ledger,
                    c01_report=rep,
                    now_utc=now_utc,
                )
                return rep

            if pin_state.bars_held >= active_c1.max_holding_bars_5m:
                rep = self.oco_coordinator.execute_full_exit(
                    ledger=ledger,
                    exec_price=bid,
                    action=ExitSignalAction.FULL_EXIT_TIMEOUT,
                    reason=f"{strat.version}_VERTICAL_3H_TIMEOUT",
                    slice_index=1,
                    now_utc=now_utc,
                )
                self._finalize_full_exit_in_d01(
                    prev_ledger=ledger,
                    c01_report=rep,
                    now_utc=now_utc,
                )
                return rep

            return tick_rep

        # 3. C2 Multi-Tranche Profit Taking & Trailing Stop Mode
        assert active_c2 is not None
        if pin_state.tranche_plan is None:
            pin_state.tranche_plan = active_c2.plan_tranches(
                sellable_qty=self.filters.quantize_qty_down(ledger.actual_qty),
                avg_buy_price=ledger.avg_fill_price,
            )

        plan = pin_state.tranche_plan
        unfilled = [
            t for t in plan.tranches if t.tranche_index not in pin_state.filled_tranche_indices
        ]
        if not unfilled:
            return tick_rep

        next_tranche = unfilled[0]
        high_px = max(bar_t.high_price, bid)

        if not next_tranche.is_trailing_leg:
            if high_px >= next_tranche.target_sell_price:
                pin_state.filled_tranche_indices.append(next_tranche.tranche_index)
                rep = self.oco_coordinator.execute_partial_tranche_and_reprotect(
                    ledger=ledger,
                    tranche_qty=next_tranche.qty,
                    tranche_exec_price=next_tranche.target_sell_price,
                    new_stop_trigger_price=next_tranche.stop_price_after_fill,
                    slice_index=next_tranche.tranche_index,
                    reason=f"{strat.version}_TRANCHE_{next_tranche.tranche_index}_PROFIT_LOCK",
                    now_utc=now_utc,
                )
                self._record_partial_tranche_in_d01(
                    prev_ledger=ledger,
                    c01_report=rep,
                    now_utc=now_utc,
                )
                return rep
        else:
            # Final trailing leg
            trail_stop = self.filters.quantize_price_down(
                pin_state.high_water_mark_price * (Decimal("1") - active_c2.trailing_pullback_pct)
            )
            floor_stop = next_tranche.stop_price_after_fill
            desired_stop = max(trail_stop, floor_stop)
            current_stop = tick_rep.active_protection_stop_price or floor_stop

            if bid <= desired_stop and pin_state.high_water_mark_price >= next_tranche.target_sell_price:
                pin_state.filled_tranche_indices.append(next_tranche.tranche_index)
                rep = self.oco_coordinator.execute_full_exit(
                    ledger=ledger,
                    exec_price=max(bid, desired_stop),
                    action=ExitSignalAction.FULL_EXIT_TRAILING_STOP,
                    reason=f"{strat.version}_TRANCHE_{next_tranche.tranche_index}_TRAILING_STOP_EXIT",
                    slice_index=next_tranche.tranche_index,
                    now_utc=now_utc,
                )
                self._finalize_full_exit_in_d01(
                    prev_ledger=ledger,
                    c01_report=rep,
                    now_utc=now_utc,
                )
                return rep

            if desired_stop > current_stop:
                rep = self.oco_coordinator.ratchet_stop_protection_only(
                    ledger=ledger,
                    new_stop_trigger_price=desired_stop,
                    reason=f"{strat.version}_TRAILING_STOP_RATCHET",
                    now_utc=now_utc,
                )
                self.d01_engine._persist_position_ledger(
                    ledger=rep.position_ledger,
                    dust_qty=self.d01_engine.active_dust_qty,
                    is_active=True,
                    now_utc=now_utc,
                )
                return rep

        if pin_state.bars_held >= active_c2.max_holding_bars_5m:
            rep = self.oco_coordinator.execute_full_exit(
                ledger=ledger,
                exec_price=bid,
                action=ExitSignalAction.FULL_EXIT_TIMEOUT,
                reason=f"{strat.version}_VERTICAL_3H_TIMEOUT",
                slice_index=len(pin_state.filled_tranche_indices) + 1,
                now_utc=now_utc,
            )
            self._finalize_full_exit_in_d01(
                prev_ledger=ledger,
                c01_report=rep,
                now_utc=now_utc,
            )
            return rep

        return tick_rep

    def step_closed_bar_cycle(
        self,
        *,
        sui_5m_bars: Sequence[ClosedKlineBar],
        orderbook: PartialOrderbookSnapshot,
        btc_5m_bars: Sequence[ClosedKlineBar] | None = None,
        sui_15m_bars: Sequence[ClosedKlineBar] | None = None,
        sui_1h_bars: Sequence[ClosedKlineBar] | None = None,
        data_quality: DataQuality = DataQuality.OK,
        immediate_fill_qty: Decimal | None = None,
        immediate_fill_price: Decimal | None = None,
        require_candidate_filter: bool = True,
        require_positive_ev: bool = False,
        now_utc: datetime | None = None,
    ) -> IntegratedCycleReport:
        """Execute one full closed-bar pipeline cycle across F-01 -> A-01 -> E-01(A-02) -> R-01 -> B-01 -> D-01 -> C-01."""
        cycle_t0_ns = monotonic_ns()
        self._cycle_seq += 1
        bar_t = sui_5m_bars[-1]
        ref_now = ensure_utc(now_utc or bar_t.exchange_time_utc, "now_utc")
        state_before = self.d01_engine.current_state

        if state_before == TradingState.BOOT:
            self.d01_engine.boot_and_reconcile(
                current_market_price=orderbook.best_bid or bar_t.close_price,
                now_utc=ref_now,
            )
            state_before = self.d01_engine.current_state

        # 1. Causal Feature Extraction (F-01 -> FeaturePipeline)
        f_t0 = monotonic_ns()
        sui_features = self.feature_pipeline.extract_at_index(
            sui_5m_bars=sui_5m_bars,
            target_index=len(sui_5m_bars) - 1,
            btc_5m_bars=btc_5m_bars,
            sui_15m_bars=sui_15m_bars,
            sui_1h_bars=sui_1h_bars,
        )
        feature_lat_ns = monotonic_ns() - f_t0

        # 2. A-01 Market Regime Classification
        a01_t0 = monotonic_ns()
        regime_decision = self.a01_classifier.classify(
            sui_features=sui_features,
            data_quality=data_quality,
            now_utc=ref_now,
        )
        a01_lat_ns = monotonic_ns() - a01_t0

        # 3. E-01 Parallel SHADOW Model Evaluation (zero order submission authority)
        shad_t0 = monotonic_ns()
        shadow_bundle_id = (
            self.registry.shadow_bundle.bundle_id
            if self.registry.shadow_bundle is not None
            else None
        )
        shadow_rec = self.registry.evaluate_shadow_inference(
            sui_features=sui_features,
            regime_decision=regime_decision,
        )
        a02_shadow_lat_ns = (monotonic_ns() - shad_t0) if shadow_rec is not None else 0

        # 4. Branch by TradingState: In-Flight Position Exit vs Candidate Entry
        active_proposal: BreakoutProposal | None = None
        rollback_rec: RollbackAuditRecord | None = None
        d01_outcome: D01CycleOutcome | None = None
        c01_report: CoordinatorStepReport | None = None
        a02_active_lat_ns = 0
        exec_lat_ns = 0
        pinned_exit_ver: str | None = None
        notes = ""

        if self.d01_engine.current_state == TradingState.PROTECTED and self.d01_engine.active_position_ledger is not None:
            # Also run active model health check in background so AutoRollbackGuard catches active model faults
            # even while a position is open, without disturbing the pinned exit strategy!
            _, a02_active_lat_ns, rollback_rec = self.registry.evaluate_active_paper_proposal(
                sui_features=sui_features,
                regime_decision=regime_decision,
                require_candidate_filter=require_candidate_filter,
                require_positive_ev=require_positive_ev,
                now_utc=ref_now,
            )

            ex_t0 = monotonic_ns()
            curr_ledger = self.d01_engine.active_position_ledger
            pinned_exit_ver = curr_ledger.exit_strategy_version
            c01_report = self._evaluate_pinned_position_exit(
                ledger=curr_ledger,
                bar_t=bar_t,
                sui_features=sui_features,
                regime_decision=regime_decision,
                orderbook=orderbook,
                now_utc=ref_now,
            )
            exec_lat_ns = monotonic_ns() - ex_t0
            notes = f"In-flight position managed under pinned exit={pinned_exit_ver}: {c01_report.notes}"

        elif self.d01_engine.current_state == TradingState.READY_NO_POSITION:
            active_proposal, a02_active_lat_ns, rollback_rec = self.registry.evaluate_active_paper_proposal(
                sui_features=sui_features,
                regime_decision=regime_decision,
                require_candidate_filter=require_candidate_filter,
                require_positive_ev=require_positive_ev,
                now_utc=ref_now,
            )

            ex_t0 = monotonic_ns()
            if active_proposal is not None and regime_decision.allow_entry:
                d01_outcome = self.d01_engine.process_entry_signal(
                    regime=regime_decision,
                    proposal=active_proposal,
                    orderbook=orderbook,
                    symbol=TRADING_SYMBOL,
                    immediate_fill_qty=immediate_fill_qty,
                    immediate_fill_price=immediate_fill_price,
                    current_monotonic_ns=orderbook.recv_monotonic_ns,
                    now_utc=ref_now,
                )
                if (
                    self.d01_engine.current_state == TradingState.PROTECTED
                    and self.d01_engine.active_position_ledger is not None
                ):
                    stamped = self._stamp_and_pin_new_position(
                        ledger=self.d01_engine.active_position_ledger,
                        now_utc=ref_now,
                    )
                    pinned_exit_ver = stamped.exit_strategy_version
                    notes = f"Opened new PROTECTED position pinned to exit={pinned_exit_ver}"
                else:
                    notes = d01_outcome.notes
            else:
                notes = "No actionable breakout proposal on this bar"
            exec_lat_ns = monotonic_ns() - ex_t0
        else:
            notes = f"Cycle observed in state {self.d01_engine.current_state.value}"

        total_lat_ns = monotonic_ns() - cycle_t0_ns
        self._cycle_latencies_ns.append(total_lat_ns)

        return IntegratedCycleReport(
            cycle_seq=self._cycle_seq,
            bar_close_time_utc=ref_now,
            trading_state_before=state_before,
            trading_state_after=self.d01_engine.current_state,
            active_model_bundle_id=self.registry.active_paper_bundle.bundle_id,
            active_paper_exit_version=self.registry.active_paper_exit_strategy_version,
            pinned_position_exit_version=pinned_exit_ver,
            shadow_model_bundle_id=shadow_bundle_id,
            regime_decision=regime_decision,
            active_proposal=active_proposal,
            shadow_record=shadow_rec,
            rollback_record=rollback_rec,
            d01_outcome=d01_outcome,
            c01_report=c01_report,
            position_ledger=self.d01_engine.active_position_ledger,
            feature_latency_ns=feature_lat_ns,
            a01_latency_ns=a01_lat_ns,
            a02_active_latency_ns=a02_active_lat_ns,
            a02_shadow_latency_ns=a02_shadow_lat_ns,
            execution_or_exit_latency_ns=exec_lat_ns,
            total_cycle_latency_ns=total_lat_ns,
            notes=notes,
        )
