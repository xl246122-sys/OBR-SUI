"""D-01 Autonomous Decision & State Machine Engine (§7, §14.2, QA-04, QA-05, QA-08).

Single Entry Point Invariant:
- Only `D01DecisionEngine` holds authority to transition the trading state machine and submit orders.
- Every state change is validated by `assert_valid_transition` and persisted to SQLite WAL (`state_transitions`).
- Enforces Write-Ahead Intent (`p4_order_intents`) before order submission, deterministic `ORDER_UNKNOWN`
  idempotent reconciliation (`INV-R6`), partial-fill residual cancellation + immediate `-1%` stop protection
  (`INV-R7`), micro-fill unprotectable freeze (`QA-04c`), and crash-restart naked position recovery (`QA-05`).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sui_quant.config import ExecutionMode, SystemConfig
from sui_quant.contracts import (
    TRADING_SYMBOL,
    AlertSeverity,
    BreakoutProposal,
    OrderIntent,
    OrderSide,
    PositionLedger,
    RegimeDecision,
    RiskVerdict,
    TimeInForce,
    require_decimal,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.data.quality_guard import PartialOrderbookSnapshot, StreamQualityGuard
from sui_quant.exchange_rules import (
    PostFillSellableAllocation,
    SymbolFilters,
    compute_post_fill_sellable_position,
)
from sui_quant.execution.b01_limit_executor import (
    B01LimitOrderOptimizer,
    B01OptimizationResult,
    compute_deterministic_p4_client_order_id,
)
from sui_quant.execution.paper_exchange import (
    ExchangeTimeoutError,
    OrderSnapshot,
    OrderStatus,
    PaperOrderbookSimulator,
)
from sui_quant.execution.r01_risk_evaluator import (
    R01EvaluationResult,
    R01PreTradeRiskGate,
    RealizedPnLEntry,
    compute_rolling_24h_drawdown_pct,
)
from sui_quant.state_machine import (
    StateTransitionRecord,
    TradingState,
    assert_valid_transition,
    prioritize_startup_reconciliation,
)
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


@dataclass(frozen=True)
class D01CycleOutcome:
    """Structured audit outcome of a single D-01 decision or reconciliation step."""

    final_state: TradingState
    r01_result: R01EvaluationResult | None
    b01_result: B01OptimizationResult | None
    order_snapshot: OrderSnapshot | None
    position_ledger: PositionLedger | None
    dust_qty: Decimal
    notes: str
    end_to_end_latency_ns: int


class D01DecisionEngine:
    """Sole autonomous trading decision and state transition controller (§7, §3.2)."""

    def __init__(
        self,
        *,
        config: SystemConfig,
        store: StateStore,
        alert_manager: AlertManager,
        exchange: PaperOrderbookSimulator,
        r01_gate: R01PreTradeRiskGate,
        b01_optimizer: B01LimitOrderOptimizer,
        filters: SymbolFilters,
        quality_guard: StreamQualityGuard | None = None,
        initial_24h_equity_usdt: Decimal = Decimal("10000.0000"),
    ) -> None:
        # Constructor & Type Hard Locks (§3.7, INV-R1)
        if not isinstance(config, SystemConfig):
            raise TypeError("config must be a SystemConfig instance")
        if config.mode != ExecutionMode.PAPER:
            raise PermissionError(
                f"P4 strictly locks execution_mode to PAPER, got {config.mode}"
            )
        if not isinstance(exchange, PaperOrderbookSimulator):
            raise TypeError(
                f"P4 requires PaperOrderbookSimulator instance, got {type(exchange).__name__}"
            )
        if exchange.execution_mode != ExecutionMode.PAPER:
            raise PermissionError("PaperOrderbookSimulator must be in ExecutionMode.PAPER")

        self.config = config
        self.store = store
        self.alert_manager = alert_manager
        self.exchange = exchange
        self.r01_gate = r01_gate
        self.b01_optimizer = b01_optimizer
        self.filters = filters
        self.quality_guard = quality_guard
        self.initial_24h_equity_usdt = require_decimal(
            initial_24h_equity_usdt, "initial_24h_equity_usdt"
        )

        self._init_p4_wal_tables()
        self._active_intent: OrderIntent | None = self._load_latest_unresolved_intent()
        self._active_verdict: RiskVerdict | None = None
        self._active_ledger, self._active_dust_qty = self._load_active_position_ledger()
        self._intent_seq_counter = 1
        self._reconcile_failure_count = 0
        self._unknown_state_new_buy_count = 0

    def _init_p4_wal_tables(self) -> None:
        conn = self.store._conn
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS p4_order_intents (
                intent_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                qty TEXT NOT NULL,
                tif TEXT NOT NULL,
                price_cap TEXT NOT NULL,
                expiration_utc TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                risk_check_id TEXT NOT NULL,
                intent_status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS p4_order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT NOT NULL,
                exchange_status TEXT NOT NULL,
                filled_qty TEXT NOT NULL,
                cumulative_quote_qty TEXT NOT NULL,
                fee_asset TEXT NOT NULL,
                fee_amount TEXT NOT NULL,
                source_timestamp_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS p4_position_ledger (
                position_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                actual_qty TEXT NOT NULL,
                avg_fill_price TEXT NOT NULL,
                fees TEXT NOT NULL,
                reserved_qty TEXT NOT NULL,
                dust_qty TEXT NOT NULL,
                protection_orders_json TEXT NOT NULL,
                exit_strategy_version TEXT NOT NULL,
                risk_config_version TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS p4_realized_pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                closed_at_utc TEXT NOT NULL,
                net_pnl_usdt TEXT NOT NULL
            );
            """
        )

    @property
    def current_state(self) -> TradingState:
        return self.store.get_trading_state()

    @property
    def active_position_ledger(self) -> PositionLedger | None:
        return self._active_ledger

    @property
    def active_dust_qty(self) -> Decimal:
        return self._active_dust_qty

    @property
    def active_intent(self) -> OrderIntent | None:
        return self._active_intent

    @property
    def unknown_state_new_buy_count(self) -> int:
        """Strictly 0 (INV-R6)."""
        return self._unknown_state_new_buy_count

    def _transition_to(
        self,
        next_state: TradingState,
        reason: str,
        now_utc: datetime | None = None,
    ) -> TradingState:
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        curr = self.current_state
        if curr == next_state and next_state in {TradingState.ENTRY_FROZEN, TradingState.SECURITY_LOCKDOWN}:
            return curr
        assert_valid_transition(curr, next_state)
        rec = StateTransitionRecord(
            transition_id=f"TR-{uuid.uuid4().hex[:12]}",
            from_state=curr,
            to_state=next_state,
            reason=reason,
            occurred_at_utc=ref_now,
        )
        self.store.record_transition(rec)
        return next_state

    def _write_ahead_order_intent(
        self,
        intent: OrderIntent,
        status: str,
        now_utc: datetime,
    ) -> None:
        """Write-Ahead Intent: persist `OrderIntent` to SQLite WAL BEFORE calling exchange (§3.2)."""
        self.store._conn.execute(
            """
            INSERT INTO p4_order_intents (
                intent_id, signal_id, client_order_id, symbol, side,
                price, qty, tif, price_cap, expiration_utc,
                strategy_version, risk_check_id, intent_status, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                intent_status=excluded.intent_status
            """,
            (
                intent.intent_id,
                intent.signal_id,
                intent.client_order_id,
                intent.symbol,
                intent.side.value,
                str(intent.price),
                str(intent.qty),
                intent.tif.value,
                str(intent.price_cap),
                intent.expiration.isoformat(),
                intent.strategy_version,
                intent.risk_check_id,
                status,
                now_utc.isoformat(),
            ),
        )

    def _update_intent_status(self, client_order_id: str, status: str) -> None:
        self.store._conn.execute(
            "UPDATE p4_order_intents SET intent_status = ? WHERE client_order_id = ?",
            (status, client_order_id),
        )

    def _load_latest_unresolved_intent(self) -> OrderIntent | None:
        row = self.store._conn.execute(
            """
            SELECT * FROM p4_order_intents
            WHERE intent_status IN ('WRITE_AHEAD_PENDING', 'SUBMITTED', 'UNKNOWN', 'PARTIALLY_FILLED')
            ORDER BY created_at_utc DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return OrderIntent(
            intent_id=str(row["intent_id"]),
            signal_id=str(row["signal_id"]),
            client_order_id=str(row["client_order_id"]),
            symbol=str(row["symbol"]),
            side=OrderSide(str(row["side"])),
            price=Decimal(str(row["price"])),
            qty=Decimal(str(row["qty"])),
            tif=TimeInForce(str(row["tif"])),
            price_cap=Decimal(str(row["price_cap"])),
            expiration=datetime.fromisoformat(str(row["expiration_utc"])),
            strategy_version=str(row["strategy_version"]),
            risk_check_id=str(row["risk_check_id"]),
        )

    def _record_order_snapshot_event(self, snap: OrderSnapshot) -> None:
        evt = snap.to_order_event()
        self.store._conn.execute(
            """
            INSERT INTO p4_order_events (
                client_order_id, exchange_status, filled_qty, cumulative_quote_qty,
                fee_asset, fee_amount, source_timestamp_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evt.client_order_id,
                evt.exchange_status,
                str(evt.filled_qty),
                str(evt.cumulative_quote_qty),
                evt.fee_asset,
                str(evt.fee_amount),
                evt.source_timestamp.isoformat(),
            ),
        )

    def _persist_position_ledger(
        self,
        ledger: PositionLedger,
        dust_qty: Decimal,
        is_active: bool,
        now_utc: datetime,
    ) -> None:
        self.store._conn.execute(
            """
            INSERT INTO p4_position_ledger (
                position_id, symbol, actual_qty, avg_fill_price, fees,
                reserved_qty, dust_qty, protection_orders_json,
                exit_strategy_version, risk_config_version, is_active, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                actual_qty=excluded.actual_qty,
                avg_fill_price=excluded.avg_fill_price,
                fees=excluded.fees,
                reserved_qty=excluded.reserved_qty,
                dust_qty=excluded.dust_qty,
                protection_orders_json=excluded.protection_orders_json,
                is_active=excluded.is_active,
                updated_at_utc=excluded.updated_at_utc
            """,
            (
                ledger.position_id,
                ledger.symbol,
                str(ledger.actual_qty),
                str(ledger.avg_fill_price),
                str(ledger.fees),
                str(ledger.reserved_qty),
                str(dust_qty),
                json.dumps(list(ledger.protection_orders)),
                ledger.exit_strategy_version,
                ledger.risk_config_version,
                1 if is_active else 0,
                now_utc.isoformat(),
            ),
        )
        if is_active:
            self._active_ledger = ledger
            self._active_dust_qty = dust_qty
        else:
            self._active_ledger = None
            self._active_dust_qty = dust_qty

    def _load_active_position_ledger(self) -> tuple[PositionLedger | None, Decimal]:
        row = self.store._conn.execute(
            "SELECT * FROM p4_position_ledger WHERE is_active = 1 ORDER BY updated_at_utc DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, Decimal("0.0")
        ledger = PositionLedger(
            position_id=str(row["position_id"]),
            symbol=str(row["symbol"]),
            actual_qty=Decimal(str(row["actual_qty"])),
            avg_fill_price=Decimal(str(row["avg_fill_price"])),
            fees=Decimal(str(row["fees"])),
            reserved_qty=Decimal(str(row["reserved_qty"])),
            protection_orders=tuple(json.loads(str(row["protection_orders_json"]))),
            exit_strategy_version=str(row["exit_strategy_version"]),
            risk_config_version=str(row["risk_config_version"]),
        )
        return ledger, Decimal(str(row["dust_qty"]))

    def record_realized_pnl(self, net_pnl_usdt: Decimal, closed_at_utc: datetime) -> None:
        ensure_utc(closed_at_utc, "closed_at_utc")
        pnl = require_decimal(net_pnl_usdt, "net_pnl_usdt")
        self.store._conn.execute(
            "INSERT INTO p4_realized_pnl (closed_at_utc, net_pnl_usdt) VALUES (?, ?)",
            (closed_at_utc.isoformat(), str(pnl)),
        )

    def get_realized_pnl_entries(self) -> list[RealizedPnLEntry]:
        rows = self.store._conn.execute(
            "SELECT closed_at_utc, net_pnl_usdt FROM p4_realized_pnl ORDER BY id ASC"
        ).fetchall()
        return [
            RealizedPnLEntry(
                closed_at_utc=datetime.fromisoformat(str(r["closed_at_utc"])),
                net_pnl_usdt=Decimal(str(r["net_pnl_usdt"])),
            )
            for r in rows
        ]

    def current_rolling_24h_drawdown_pct(
        self,
        *,
        mark_price_sui: Decimal | None = None,
        now_utc: datetime | None = None,
    ) -> Decimal:
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        unrealized = Decimal("0")
        if self._active_ledger is not None and mark_price_sui is not None:
            mp = require_decimal(mark_price_sui, "mark_price_sui")
            unrealized = (mp - self._active_ledger.avg_fill_price) * self._active_ledger.actual_qty
        return compute_rolling_24h_drawdown_pct(
            initial_24h_equity_usdt=self.initial_24h_equity_usdt,
            realized_pnl_entries=self.get_realized_pnl_entries(),
            unrealized_pnl_usdt=unrealized,
            now_utc=ref_now,
        )

    def boot_and_reconcile(
        self,
        *,
        s01_lockdown_active: bool = False,
        current_market_price: Decimal | None = None,
        now_utc: datetime | None = None,
    ) -> D01CycleOutcome:
        """Execute `BOOT -> SYNCING` startup reconciliation with naked-position top priority (QA-05, FI-P4-06)."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")

        if self.current_state != TradingState.SYNCING:
            if self.current_state not in {
                TradingState.BOOT,
                TradingState.READY_NO_POSITION,
                TradingState.FLAT,
                TradingState.ENTRY_FROZEN,
                TradingState.SECURITY_LOCKDOWN,
            }:
                self.store.set_trading_state(TradingState.BOOT)
            self._transition_to(TradingState.SYNCING, "STARTUP_RECONCILIATION", now_utc=ref_now)

        # Check if there is an unresolved intent in WAL that filled on the exchange right before crash
        if self._active_intent is not None and self._active_ledger is None:
            snap = self.exchange.query_order_by_client_order_id(
                symbol=TRADING_SYMBOL,
                client_order_id=self._active_intent.client_order_id,
                now_utc=ref_now,
            )
            if snap.status == OrderStatus.FILLED:
                self._build_and_persist_ledger_from_fill(snap, ref_now)
                self._update_intent_status(snap.client_order_id, "RECONCILED_FILLED")

        open_sell_orders = self.exchange.list_open_orders(side=OrderSide.SELL)
        has_active_protection = len(open_sell_orders) > 0
        has_open_pos = self._active_ledger is not None and self._active_ledger.actual_qty > Decimal("0")

        is_protectable = True
        if has_open_pos and self._active_ledger is not None:
            alloc = compute_post_fill_sellable_position(
                gross_filled_qty=self._active_ledger.actual_qty,
                avg_fill_price=self._active_ledger.avg_fill_price,
                fee_asset="USDT",
                fee_amount=Decimal("0"),
                stop_loss_pct=self.config.risk.stop_loss_trigger_pct,
                filters=self.filters,
            )
            is_protectable = alloc.is_protectable

        open_buy_orders = self.exchange.list_open_orders(side=OrderSide.BUY)
        has_unknown_buy = len(open_buy_orders) > 0 or (
            self._active_intent is not None and not has_open_pos
        )

        dd_24h = self.current_rolling_24h_drawdown_pct(
            mark_price_sui=current_market_price, now_utc=ref_now
        )
        loss_exceeded = dd_24h >= self.config.risk.max_rolling_24h_loss_pct

        target_state = prioritize_startup_reconciliation(
            has_open_position=has_open_pos,
            has_active_protection_order=has_active_protection,
            is_position_protectable=is_protectable,
            has_unknown_open_buy_order=has_unknown_buy,
            rolling_24h_loss_exceeded=loss_exceeded,
            s01_lockdown_active=s01_lockdown_active,
        )

        self._transition_to(
            target_state,
            f"SYNCING_RESOLVED_TO_{target_state.value}",
            now_utc=ref_now,
        )

        # QA-05 / INV-R7: If naked position discovered on restart, immediately place -1% stop protection!
        if target_state == TradingState.PROTECTION_PENDING:
            return self._execute_protection_placement(
                current_market_price=current_market_price,
                now_utc=ref_now,
                start_ns=t0_ns,
            )

        return D01CycleOutcome(
            final_state=self.current_state,
            r01_result=None,
            b01_result=None,
            order_snapshot=None,
            position_ledger=self._active_ledger,
            dust_qty=self._active_dust_qty,
            notes=f"SYNCING completed -> {self.current_state.value}",
            end_to_end_latency_ns=monotonic_ns() - t0_ns,
        )

    def process_entry_signal(
        self,
        *,
        regime: RegimeDecision,
        proposal: BreakoutProposal,
        orderbook: PartialOrderbookSnapshot,
        symbol: str = TRADING_SYMBOL,
        immediate_fill_qty: Decimal | None = None,
        immediate_fill_price: Decimal | None = None,
        simulate_crash_Right_after_fill: bool = False,
        current_monotonic_ns: int | None = None,
        now_utc: datetime | None = None,
    ) -> D01CycleOutcome:
        """Drive `READY_NO_POSITION -> SIGNAL_CANDIDATE -> RISK_CHECK -> ORDER_SUBMITTED` pipeline."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        ref_mono = current_monotonic_ns if current_monotonic_ns is not None else t0_ns

        # INV-R6: While in ORDER_UNKNOWN, strictly forbid sending any new buy orders
        if self.current_state == TradingState.ORDER_UNKNOWN:
            raise PermissionError(
                "INV-R6 violated: Cannot process new entry signal while in ORDER_UNKNOWN"
            )

        if self.current_state in {TradingState.ENTRY_FROZEN, TradingState.SECURITY_LOCKDOWN}:
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=None,
                b01_result=None,
                order_snapshot=None,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes=f"Blocked by emergency state {self.current_state.value}",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        if self.current_state != TradingState.READY_NO_POSITION:
            raise PermissionError(
                f"process_entry_signal requires state READY_NO_POSITION, got {self.current_state.value}"
            )

        if not regime.allow_entry:
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=None,
                b01_result=None,
                order_snapshot=None,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes=f"Regime {regime.regime.value} disallows entry",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        # 1. Transition READY_NO_POSITION -> SIGNAL_CANDIDATE -> RISK_CHECK
        self._transition_to(
            TradingState.SIGNAL_CANDIDATE,
            f"SIGNAL_{proposal.signal_id}",
            now_utc=ref_now,
        )
        self._transition_to(
            TradingState.RISK_CHECK,
            f"RISK_CHECK_{proposal.signal_id}",
            now_utc=ref_now,
        )

        # 2. Evaluate R-01 Final Veto
        mark_price = orderbook.best_ask or proposal.barrier_ref
        equity_usdt = self.exchange.total_equity_usdt(mark_price)
        dd_24h = self.current_rolling_24h_drawdown_pct(
            mark_price_sui=mark_price, now_utc=ref_now
        )
        r01_res = self.r01_gate.evaluate_candidate(
            proposal=proposal,
            symbol=symbol,
            planned_entry_price=proposal.barrier_ref,
            account_equity_usdt=equity_usdt,
            free_usdt=self.exchange.free_usdt,
            filters=self.filters,
            rolling_24h_drawdown_pct=dd_24h,
            current_trading_state=self.current_state,
            now_utc=ref_now,
        )

        if not r01_res.verdict.allowed:
            if r01_res.recommended_state_override in {
                TradingState.ENTRY_FROZEN,
                TradingState.SECURITY_LOCKDOWN,
            }:
                self._transition_to(
                    r01_res.recommended_state_override,
                    f"R01_VETO_{','.join(r01_res.verdict.reject_reasons)}",
                    now_utc=ref_now,
                )
                self.alert_manager.emit_alert(
                    severity=AlertSeverity.HIGH,
                    source="R-01",
                    dedup_key=f"R01-CIRCUIT-BREAKER-{r01_res.recommended_state_override.value}",
                    actions_taken=(r01_res.recommended_state_override.value,),
                    details={"reject_reasons": list(r01_res.verdict.reject_reasons)},
                    now_utc=ref_now,
                )
            else:
                self._transition_to(
                    TradingState.READY_NO_POSITION,
                    f"R01_REJECT_{','.join(r01_res.verdict.reject_reasons)}",
                    now_utc=ref_now,
                )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=r01_res,
                b01_result=None,
                order_snapshot=None,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes="R-01 vetoed candidate entry",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        # 3. Optimize Limit Quote via B-01
        b01_res = self.b01_optimizer.optimize_limit_buy(
            proposal=proposal,
            risk_verdict=r01_res.verdict,
            orderbook=orderbook,
            reference_price=proposal.barrier_ref,
            quality_guard=self.quality_guard,
            intent_seq=self._intent_seq_counter,
            current_monotonic_ns=ref_mono,
            now_utc=ref_now,
        )
        if b01_res.selected_intent is None:
            self._transition_to(
                TradingState.READY_NO_POSITION,
                f"B01_ABORT_{b01_res.rejected_reason}",
                now_utc=ref_now,
            )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=r01_res,
                b01_result=b01_res,
                order_snapshot=None,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes=f"B-01 aborted quote: {b01_res.rejected_reason}",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        intent = b01_res.selected_intent
        self._intent_seq_counter += 1

        # 4. Enforce Runtime Risk Invariants (INV-R1..R5, INV-R8)
        self.r01_gate.assert_order_intent_invariants(
            intent=intent,
            verdict=r01_res.verdict,
            account_equity_usdt=equity_usdt,
            rolling_24h_drawdown_pct=dd_24h,
            filters=self.filters,
        )

        # 5. Write-Ahead Intent persistence BEFORE order submission (§3.2)
        self._active_intent = intent
        self._active_verdict = r01_res.verdict
        self._write_ahead_order_intent(intent, "WRITE_AHEAD_PENDING", ref_now)

        # 6. Transition RISK_CHECK -> ORDER_SUBMITTED and submit to PaperOrderbookSimulator
        self._transition_to(
            TradingState.ORDER_SUBMITTED,
            f"SUBMIT_{intent.client_order_id}",
            now_utc=ref_now,
        )

        try:
            eff_fill_qty = (
                min(immediate_fill_qty, intent.qty)
                if immediate_fill_qty is not None
                else None
            )
            snap = self.exchange.submit_limit_buy(
                intent,
                immediate_fill_qty=eff_fill_qty,
                immediate_fill_price=immediate_fill_price,
                now_utc=ref_now,
            )
            self._update_intent_status(intent.client_order_id, f"EXCHANGE_{snap.status.value}")
            self._record_order_snapshot_event(snap)
        except ExchangeTimeoutError as exc:
            # QA-04d / FI-P4-05: Timeout enters ORDER_UNKNOWN without resending a new order!
            self._update_intent_status(intent.client_order_id, "UNKNOWN")
            self._transition_to(
                TradingState.ORDER_UNKNOWN,
                f"SUBMIT_TIMEOUT:{exc}",
                now_utc=ref_now,
            )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=r01_res,
                b01_result=b01_res,
                order_snapshot=None,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes=f"Entered ORDER_UNKNOWN due to timeout: {exc}",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        # If immediate fill occurred during submission, advance state machine
        if snap.status == OrderStatus.FILLED:
            self._transition_to(
                TradingState.FILLED,
                f"BUY_FILLED_{snap.client_order_id}",
                now_utc=ref_now,
            )
            self._build_and_persist_ledger_from_fill(snap, ref_now)
            if simulate_crash_Right_after_fill:
                # FI-P4-06 / QA-05 fault injection: simulate process crash right after FILLED before protection!
                return D01CycleOutcome(
                    final_state=self.current_state,
                    r01_result=r01_res,
                    b01_result=b01_res,
                    order_snapshot=snap,
                    position_ledger=self._active_ledger,
                    dust_qty=self._active_dust_qty,
                    notes="SIMULATED_CRASH_AFTER_FILL_BEFORE_PROTECTION",
                    end_to_end_latency_ns=monotonic_ns() - t0_ns,
                )
            self._transition_to(
                TradingState.PROTECTION_PENDING,
                f"PREPARE_STOP_{snap.client_order_id}",
                now_utc=ref_now,
            )
            return self._execute_protection_placement(
                current_market_price=orderbook.best_bid,
                now_utc=ref_now,
                start_ns=t0_ns,
                r01_res=r01_res,
                b01_res=b01_res,
                buy_snap=snap,
            )

        if snap.status == OrderStatus.PARTIALLY_FILLED:
            self._transition_to(
                TradingState.PARTIALLY_FILLED,
                f"BUY_PARTIAL_{snap.client_order_id}",
                now_utc=ref_now,
            )

        return D01CycleOutcome(
            final_state=self.current_state,
            r01_result=r01_res,
            b01_result=b01_res,
            order_snapshot=snap,
            position_ledger=self._active_ledger,
            dust_qty=self._active_dust_qty,
            notes=f"Order submitted ({snap.status.value})",
            end_to_end_latency_ns=monotonic_ns() - t0_ns,
        )

    def reconcile_order_unknown(
        self,
        *,
        current_market_price: Decimal | None = None,
        max_reconcile_failures: int = 3,
        simulate_query_error: bool = False,
        now_utc: datetime | None = None,
    ) -> D01CycleOutcome:
        """Execute deterministic `ORDER_UNKNOWN` idempotent reconciliation protocol (§3.4, QA-04d, FI-P4-05).

        Strictly queries by existing `active_intent.client_order_id`; NEVER generates a new buy order (INV-R6).
        """
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if self.current_state != TradingState.ORDER_UNKNOWN:
            raise PermissionError(
                f"reconcile_order_unknown requires ORDER_UNKNOWN, got {self.current_state.value}"
            )
        if self._active_intent is None:
            self._transition_to(
                TradingState.ENTRY_FROZEN,
                "ORDER_UNKNOWN_MISSING_ACTIVE_INTENT",
                now_utc=ref_now,
            )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=None,
                b01_result=None,
                order_snapshot=None,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes="Missing active intent in ORDER_UNKNOWN -> ENTRY_FROZEN",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        if simulate_query_error:
            self._reconcile_failure_count += 1
            if self._reconcile_failure_count >= max_reconcile_failures:
                self._transition_to(
                    TradingState.ENTRY_FROZEN,
                    "ORDER_UNKNOWN_MAX_RECONCILE_FAILURES_EXCEEDED",
                    now_utc=ref_now,
                )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=None,
                b01_result=None,
                order_snapshot=None,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes=f"Reconcile query error #{self._reconcile_failure_count}",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        self._reconcile_failure_count = 0
        cid = self._active_intent.client_order_id
        snap = self.exchange.query_order_by_client_order_id(
            symbol=TRADING_SYMBOL,
            client_order_id=cid,
            now_utc=ref_now,
        )

        # Case A: NOT_FOUND -> order never reached matching engine
        if snap.status == OrderStatus.NOT_FOUND:
            self._update_intent_status(cid, "ABORTED_UNSENT")
            self._active_intent = None
            self._transition_to(
                TradingState.READY_NO_POSITION,
                f"RECONCILED_NOT_FOUND_{cid}",
                now_utc=ref_now,
            )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=None,
                b01_result=None,
                order_snapshot=snap,
                position_ledger=None,
                dust_qty=Decimal("0"),
                notes="Case A: Order NOT_FOUND on exchange -> READY_NO_POSITION",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        # Case B & C: NEW on book
        if snap.status == OrderStatus.NEW:
            if ref_now < self._active_intent.expiration:
                # Case B: Still within TTL -> return to ORDER_SUBMITTED
                self._update_intent_status(cid, "SUBMITTED")
                self._transition_to(
                    TradingState.ORDER_SUBMITTED,
                    f"RECONCILED_NEW_ACTIVE_{cid}",
                    now_utc=ref_now,
                )
                return D01CycleOutcome(
                    final_state=self.current_state,
                    r01_result=None,
                    b01_result=None,
                    order_snapshot=snap,
                    position_ledger=None,
                    dust_qty=Decimal("0"),
                    notes="Case B: Order NEW and within TTL -> ORDER_SUBMITTED",
                    end_to_end_latency_ns=monotonic_ns() - t0_ns,
                )
            # Case C: Expired -> idempotent cancel
            canceled = self.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=cid,
                now_utc=ref_now,
            )
            self._record_order_snapshot_event(canceled)
            self._update_intent_status(cid, "CANCELED_ZERO_FILL")
            self._active_intent = None
            self._transition_to(
                TradingState.READY_NO_POSITION,
                f"RECONCILED_EXPIRED_CANCELED_{cid}",
                now_utc=ref_now,
            )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=None,
                b01_result=None,
                order_snapshot=canceled,
                position_ledger=None,
                dust_qty=Decimal("0"),
                notes="Case C: Order NEW past TTL canceled -> READY_NO_POSITION",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        # Case D: PARTIALLY_FILLED -> cancel remaining and protect filled shares
        if snap.status == OrderStatus.PARTIALLY_FILLED:
            self._transition_to(
                TradingState.PARTIALLY_FILLED,
                f"RECONCILED_PARTIAL_{cid}",
                now_utc=ref_now,
            )
            return self.handle_order_ttl_or_partial_completion(
                force_timeout_cancel=True,
                current_market_price=current_market_price,
                now_utc=ref_now,
            )

        # Case E: FILLED -> protect filled shares immediately
        if snap.status == OrderStatus.FILLED:
            self._record_order_snapshot_event(snap)
            self._update_intent_status(cid, "FILLED")
            self._transition_to(
                TradingState.FILLED,
                f"RECONCILED_FILLED_{cid}",
                now_utc=ref_now,
            )
            self._build_and_persist_ledger_from_fill(snap, ref_now)
            self._transition_to(
                TradingState.PROTECTION_PENDING,
                f"PROTECTION_AFTER_RECONCILE_{cid}",
                now_utc=ref_now,
            )
            return self._execute_protection_placement(
                current_market_price=current_market_price,
                now_utc=ref_now,
                start_ns=t0_ns,
                buy_snap=snap,
            )

        raise RuntimeError(f"Unexpected OrderStatus during reconciliation: {snap.status}")

    def handle_order_ttl_or_partial_completion(
        self,
        *,
        force_timeout_cancel: bool = False,
        current_market_price: Decimal | None = None,
        now_utc: datetime | None = None,
    ) -> D01CycleOutcome:
        """Manage an active buy order in `ORDER_SUBMITTED` or `PARTIALLY_FILLED` (§3.5, QA-04a/b/c).

        - If 0% filled and expired (`FI-P4-02`): cancels order and returns cleanly to `READY_NO_POSITION`.
        - If partially filled and expired (`FI-P4-03` / `FI-P4-04`): cancels remaining open qty, computes
          net sellable position after fees & `stepSize=0.1` floor, and either places `-1%` stop protection
          (`PROTECTED`) or freezes into `ENTRY_FROZEN` if micro-fill is below `minQty`/`minNotional`.
        - If 100% filled: transitions to `FILLED -> PROTECTION_PENDING -> PROTECTED`.
        """
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if self._active_intent is None:
            raise RuntimeError("No active buy OrderIntent to evaluate")

        cid = self._active_intent.client_order_id
        snap = self.exchange.query_order_by_client_order_id(
            symbol=TRADING_SYMBOL,
            client_order_id=cid,
            now_utc=ref_now,
        )

        if snap.status == OrderStatus.FILLED:
            if self.current_state != TradingState.FILLED:
                self._transition_to(
                    TradingState.FILLED,
                    f"BUY_COMPLETED_{cid}",
                    now_utc=ref_now,
                )
            self._record_order_snapshot_event(snap)
            self._update_intent_status(cid, "FILLED")
            self._build_and_persist_ledger_from_fill(snap, ref_now)
            self._transition_to(
                TradingState.PROTECTION_PENDING,
                f"PROTECT_FULL_FILL_{cid}",
                now_utc=ref_now,
            )
            return self._execute_protection_placement(
                current_market_price=current_market_price,
                now_utc=ref_now,
                start_ns=t0_ns,
                buy_snap=snap,
            )

        is_expired = force_timeout_cancel or (ref_now >= self._active_intent.expiration)

        if snap.status == OrderStatus.NEW:
            if not is_expired:
                return D01CycleOutcome(
                    final_state=self.current_state,
                    r01_result=None,
                    b01_result=None,
                    order_snapshot=snap,
                    position_ledger=None,
                    dust_qty=Decimal("0"),
                    notes="Order still resting NEW within TTL",
                    end_to_end_latency_ns=monotonic_ns() - t0_ns,
                )
            # FI-P4-02 (QA-04a): Never-filled order expires -> cancel and return to READY_NO_POSITION
            canceled = self.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=cid,
                now_utc=ref_now,
            )
            self._record_order_snapshot_event(canceled)
            self._update_intent_status(cid, "CANCELED_NEVER_FILLED")
            self._active_intent = None
            self._transition_to(
                TradingState.READY_NO_POSITION,
                f"TTL_EXPIRED_ZERO_FILL_{cid}",
                now_utc=ref_now,
            )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=None,
                b01_result=None,
                order_snapshot=canceled,
                position_ledger=None,
                dust_qty=Decimal("0"),
                notes="Never-filled buy order canceled at TTL -> READY_NO_POSITION",
                end_to_end_latency_ns=monotonic_ns() - t0_ns,
            )

        if snap.status == OrderStatus.PARTIALLY_FILLED:
            if self.current_state == TradingState.ORDER_SUBMITTED:
                self._transition_to(
                    TradingState.PARTIALLY_FILLED,
                    f"PARTIAL_FILL_DETECTED_{cid}",
                    now_utc=ref_now,
                )
            if not is_expired:
                return D01CycleOutcome(
                    final_state=self.current_state,
                    r01_result=None,
                    b01_result=None,
                    order_snapshot=snap,
                    position_ledger=None,
                    dust_qty=Decimal("0"),
                    notes="Order partially filled, still waiting within TTL",
                    end_to_end_latency_ns=monotonic_ns() - t0_ns,
                )

            # FI-P4-03 / FI-P4-04 (QA-04b/c): Partial fill timeout -> Step 1: cancel remaining open buy qty
            canceled_partial = self.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=cid,
                now_utc=ref_now,
            )
            self._record_order_snapshot_event(canceled_partial)
            self._update_intent_status(cid, "CANCELED_AFTER_PARTIAL_FILL")

            # Step 2: Compute net sellable position and protectability
            alloc = self._build_and_persist_ledger_from_fill(canceled_partial, ref_now)
            if not alloc.is_protectable:
                # FI-P4-04 (QA-04c): Residual micro-fill below minQty or minNotional -> ENTRY_FROZEN + HIGH alert!
                self._transition_to(
                    TradingState.ENTRY_FROZEN,
                    f"UNPROTECTABLE_MICRO_PARTIAL_FILL:{alloc.unprotectable_reason}",
                    now_utc=ref_now,
                )
                self.alert_manager.emit_alert(
                    severity=AlertSeverity.HIGH,
                    source="D-01",
                    dedup_key="D01-UNPROTECTABLE-MICRO-PARTIAL-FILL",
                    actions_taken=("CANCEL_REMAINING_BUY", "ENTRY_FROZEN"),
                    details={
                        "client_order_id": cid,
                        "gross_filled_qty": str(alloc.gross_filled_qty),
                        "sellable_qty": str(alloc.sellable_qty),
                        "dust_qty": str(alloc.dust_qty),
                        "stop_notional_usdt": str(alloc.stop_notional_usdt),
                        "reason": alloc.unprotectable_reason,
                    },
                    now_utc=ref_now,
                )
                return D01CycleOutcome(
                    final_state=self.current_state,
                    r01_result=None,
                    b01_result=None,
                    order_snapshot=canceled_partial,
                    position_ledger=self._active_ledger,
                    dust_qty=self._active_dust_qty,
                    notes=f"Micro partial fill unprotectable ({alloc.unprotectable_reason}) -> ENTRY_FROZEN",
                    end_to_end_latency_ns=monotonic_ns() - t0_ns,
                )

            # FI-P4-03 (QA-04b): Protectable partial fill -> PROTECTION_PENDING -> PROTECTED
            self._transition_to(
                TradingState.PROTECTION_PENDING,
                f"PROTECT_PARTIAL_FILL_{cid}",
                now_utc=ref_now,
            )
            return self._execute_protection_placement(
                current_market_price=current_market_price,
                now_utc=ref_now,
                start_ns=t0_ns,
                buy_snap=canceled_partial,
            )

        raise RuntimeError(f"Unhandled order status {snap.status}")

    def _build_and_persist_ledger_from_fill(
        self,
        snap: OrderSnapshot,
        now_utc: datetime,
    ) -> PostFillSellableAllocation:
        alloc = compute_post_fill_sellable_position(
            gross_filled_qty=snap.filled_qty,
            avg_fill_price=snap.avg_fill_price,
            fee_asset=snap.fee_asset,
            fee_amount=snap.fee_amount,
            stop_loss_pct=self.config.risk.stop_loss_trigger_pct,
            filters=self.filters,
        )
        ledger = PositionLedger(
            position_id=f"POS-{snap.client_order_id}",
            symbol=TRADING_SYMBOL,
            actual_qty=alloc.net_base_qty,
            avg_fill_price=snap.avg_fill_price,
            fees=snap.fee_amount,
            reserved_qty=Decimal("0.0"),
            protection_orders=(),
            exit_strategy_version="C1-v1.0",
            risk_config_version=self.config.risk.version,
        )
        self._persist_position_ledger(
            ledger=ledger,
            dust_qty=alloc.dust_qty,
            is_active=True,
            now_utc=now_utc,
        )
        return alloc

    def _execute_protection_placement(
        self,
        *,
        current_market_price: Decimal | None,
        now_utc: datetime,
        start_ns: int,
        r01_res: R01EvaluationResult | None = None,
        b01_res: B01OptimizationResult | None = None,
        buy_snap: OrderSnapshot | None = None,
    ) -> D01CycleOutcome:
        """Place -1% stop-loss protection order from `PROTECTION_PENDING` and transition to `PROTECTED` (INV-R7)."""
        if self._active_ledger is None:
            raise RuntimeError("PROTECTION_PENDING entered without active PositionLedger")

        alloc = compute_post_fill_sellable_position(
            gross_filled_qty=self._active_ledger.actual_qty,
            avg_fill_price=self._active_ledger.avg_fill_price,
            fee_asset="USDT",  # net_base_qty is already post-buy-fee in _active_ledger.actual_qty
            fee_amount=Decimal("0"),
            stop_loss_pct=self.config.risk.stop_loss_trigger_pct,
            filters=self.filters,
        )
        if not alloc.is_protectable:
            self._transition_to(
                TradingState.ENTRY_FROZEN,
                f"PROTECTION_IMPOSSIBLE:{alloc.unprotectable_reason}",
                now_utc=now_utc,
            )
            return D01CycleOutcome(
                final_state=self.current_state,
                r01_result=r01_res,
                b01_result=b01_res,
                order_snapshot=buy_snap,
                position_ledger=self._active_ledger,
                dust_qty=self._active_dust_qty,
                notes=f"Unprotectable position ({alloc.unprotectable_reason}) -> ENTRY_FROZEN",
                end_to_end_latency_ns=monotonic_ns() - start_ns,
            )

        # If market price already dropped below -1% stop trigger before protection order could be placed,
        # transition immediately PROTECTION_PENDING -> EXITING -> FLAT (§3.2)
        if current_market_price is not None and current_market_price <= alloc.stop_trigger_price:
            return self.execute_position_exit(
                exit_price=current_market_price,
                reason="IMMEDIATE_STOP_BREACH_DURING_PROTECTION_PENDING",
                now_utc=now_utc,
            )

        ts_ms = int(now_utc.timestamp() * 1000)
        stop_cid = compute_deterministic_p4_client_order_id(
            mode=ExecutionMode.PAPER,
            signal_bar_close_ms=ts_ms,
            intent_seq=1,
            leg="sl",
        )
        stop_snap = self.exchange.submit_stop_loss_protection_order(
            symbol=TRADING_SYMBOL,
            client_order_id=stop_cid,
            qty=alloc.sellable_qty,
            stop_trigger_price=alloc.stop_trigger_price,
            limit_price=alloc.stop_trigger_price,
            now_utc=now_utc,
        )
        self._record_order_snapshot_event(stop_snap)

        protected_ledger = PositionLedger(
            position_id=self._active_ledger.position_id,
            symbol=self._active_ledger.symbol,
            actual_qty=self._active_ledger.actual_qty,
            avg_fill_price=self._active_ledger.avg_fill_price,
            fees=self._active_ledger.fees,
            reserved_qty=alloc.sellable_qty,
            protection_orders=(stop_cid,),
            exit_strategy_version=self._active_ledger.exit_strategy_version,
            risk_config_version=self._active_ledger.risk_config_version,
        )
        self._persist_position_ledger(
            ledger=protected_ledger,
            dust_qty=self._active_dust_qty,
            is_active=True,
            now_utc=now_utc,
        )
        self._active_intent = None
        self._transition_to(
            TradingState.PROTECTED,
            f"STOP_LOSS_ACTIVE_{stop_cid}",
            now_utc=now_utc,
        )
        return D01CycleOutcome(
            final_state=self.current_state,
            r01_result=r01_res,
            b01_result=b01_res,
            order_snapshot=stop_snap,
            position_ledger=self._active_ledger,
            dust_qty=self._active_dust_qty,
            notes=f"Position protected by -1% stop order {stop_cid} for {alloc.sellable_qty} SUI",
            end_to_end_latency_ns=monotonic_ns() - start_ns,
        )

    def execute_position_exit(
        self,
        *,
        exit_price: Decimal,
        reason: str = "C1_TAKE_PROFIT_OR_STOP_EXIT",
        now_utc: datetime | None = None,
    ) -> D01CycleOutcome:
        """Execute `PROTECTED / PROTECTION_PENDING -> EXITING -> FLAT -> READY_NO_POSITION` exit lifecycle (§3.2)."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if self._active_ledger is None:
            raise RuntimeError("Cannot execute position exit without an active PositionLedger")

        if self.current_state != TradingState.EXITING:
            self._transition_to(
                TradingState.EXITING,
                f"EXIT_{reason}",
                now_utc=ref_now,
            )

        sellable_qty = (
            self._active_ledger.reserved_qty
            if self._active_ledger.reserved_qty > Decimal("0")
            else self.filters.quantize_qty_down(self._active_ledger.actual_qty)
        )
        prot_cid = (
            self._active_ledger.protection_orders[0]
            if self._active_ledger.protection_orders
            else None
        )
        ts_ms = int(ref_now.timestamp() * 1000)
        exit_cid = compute_deterministic_p4_client_order_id(
            mode=ExecutionMode.PAPER,
            signal_bar_close_ms=ts_ms,
            intent_seq=1,
            leg="ex",
        )
        sell_snap = self.exchange.submit_exit_sell_order(
            symbol=TRADING_SYMBOL,
            client_order_id=exit_cid,
            qty=sellable_qty,
            exec_price=exit_price,
            cancel_protection_client_order_id=prot_cid,
            now_utc=ref_now,
        )
        self._record_order_snapshot_event(sell_snap)

        # Compute realized net PnL and persist to 24h rolling ledger
        buy_cost_usdt = sellable_qty * self._active_ledger.avg_fill_price
        sell_net_usdt = sell_snap.cumulative_quote_qty - sell_snap.fee_amount
        realized_pnl_usdt = sell_net_usdt - buy_cost_usdt
        self.record_realized_pnl(realized_pnl_usdt, ref_now)

        flat_ledger = PositionLedger(
            position_id=self._active_ledger.position_id,
            symbol=TRADING_SYMBOL,
            actual_qty=self._active_dust_qty,
            avg_fill_price=self._active_ledger.avg_fill_price,
            fees=self._active_ledger.fees + sell_snap.fee_amount,
            reserved_qty=Decimal("0.0"),
            protection_orders=(),
            exit_strategy_version=self._active_ledger.exit_strategy_version,
            risk_config_version=self._active_ledger.risk_config_version,
        )
        self._persist_position_ledger(
            ledger=flat_ledger,
            dust_qty=self._active_dust_qty,
            is_active=False,
            now_utc=ref_now,
        )

        self._transition_to(
            TradingState.FLAT,
            f"EXIT_FILLED_{exit_cid}",
            now_utc=ref_now,
        )
        self._transition_to(
            TradingState.READY_NO_POSITION,
            "LEDGER_SETTLED_FLAT",
            now_utc=ref_now,
        )
        return D01CycleOutcome(
            final_state=self.current_state,
            r01_result=None,
            b01_result=None,
            order_snapshot=sell_snap,
            position_ledger=None,
            dust_qty=self._active_dust_qty,
            notes=f"Exited {sellable_qty} SUI at {sell_snap.avg_fill_price} (PnL={realized_pnl_usdt:.4f} USDT)",
            end_to_end_latency_ns=monotonic_ns() - t0_ns,
        )
