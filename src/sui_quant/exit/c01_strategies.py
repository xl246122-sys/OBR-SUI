"""C-01 Exit Candidate Strategies (C1, C2, C3) and OCO / Protection Quantity Coordinator (§6, §8.2, QA-06, QA-07).

Core Guarantees:
1. Unified -1.0% Stop-Loss Trigger Baseline (§6.1, QA-07):
   - Initial stop trigger is strictly anchored at `floor(avg_fill_price * 0.99 / tick_size) * tick_size`
     across C1, C2, and C3.
   - `-1.0%` is a trigger threshold, NOT a guaranteed maximum loss cap. Under flash gap-downs or
     orderbook depth penetration, actual realized loss can exceed `1.0%` and is recorded truthfully.
2. Three Candidate Exit Models (§6.2):
   - `C1FixedNetTargetExit`: Single limit sell at net `+1.5%` after round-trip fees, `-1.0%` stop-loss,
     and 3-hour (`36` 5m bars) vertical timeout barrier.
   - `C2GridTrailingExit`: Upward-only 3-tranche profit taking (`40%` at `+1.0%` net -> ratchet stop to
     fee-covered breakeven; `30%` at `+1.8%` net -> ratchet stop to `+1.0%` net; `30%` high-water-mark
     trailing stop with `0.6%` pullback), equipped with `plan_tranches()` min-notional degradation guard
     so no tranche or residual ever falls below `minQty (0.1 SUI)` or `minNotional * 1.05 (5.25 USDT)`.
   - `C3AdaptiveHybridExit`: Regime- and orderbook-imbalance-aware hybrid strategy (`UP_TREND` + `OBI_5 >= 0`
     -> `C2` trailing tranches; `CONSOLIDATION` -> `C1` fixed `+1.5%` net target; `DOWN_TREND` /
     `HIGH_VOLATILITY` / `DATA_UNHEALTHY` / `BTCUSDT` flash drop -> immediate defensive risk-off exit).
3. Zero Over-Reservation & Anti-Deadlock Protection Coordinator (`OcoProtectionCoordinator`, §6.3, QA-06):
   - Enforces `sum(locked_sell_qty) == PositionLedger.reserved_qty <= sellable_qty <= actual_qty` at all times.
   - Executes atomic Cancel-First Re-Protection whenever tranche fills or stop price ratchets up, preventing
     `Insufficient balance` rejections and naked positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN
from enum import Enum
from typing import Sequence

from sui_quant.config import ExecutionMode
from sui_quant.contracts import (
    TRADING_SYMBOL,
    AlertSeverity,
    OrderSide,
    PositionLedger,
    RegimeDecision,
    RegimeType,
    require_decimal,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.exchange_rules import (
    ExchangeFilterError,
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
    compute_c1_net_target_sell_price,
    compute_stop_loss_trigger_price,
)
from sui_quant.execution.b01_limit_executor import compute_deterministic_p4_client_order_id
from sui_quant.execution.paper_exchange import (
    OrderSnapshot,
    OrderStatus,
    PaperOrderbookSimulator,
)
from sui_quant.utils.clock import ensure_utc, utc_now


QUANT_8 = Decimal("0.00000001")
QUANT_4 = Decimal("0.0001")


def _q8(val: Decimal) -> Decimal:
    return val.quantize(QUANT_8, rounding=ROUND_HALF_EVEN)


class ExitStrategyId(str, Enum):
    C1 = "C1"  # Fixed +1.5% net profit target full exit
    C2 = "C2"  # Upward 3-tranche grid (40%/30%/30%) + breakeven ratchet + trailing stop
    C3 = "C3"  # A-01 regime & OBI_5 adaptive hybrid exit


class ExitSignalAction(str, Enum):
    HOLD_PROTECTED = "HOLD_PROTECTED"
    PARTIAL_TAKE_PROFIT_AND_RATCHET_STOP = "PARTIAL_TAKE_PROFIT_AND_RATCHET_STOP"
    RATCHET_STOP_ONLY = "RATCHET_STOP_ONLY"
    FULL_EXIT_TAKE_PROFIT = "FULL_EXIT_TAKE_PROFIT"
    FULL_EXIT_TRAILING_STOP = "FULL_EXIT_TRAILING_STOP"
    FULL_EXIT_STOP_LOSS = "FULL_EXIT_STOP_LOSS"
    FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP = "FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP"
    FULL_EXIT_DEFENSIVE_RISK_OFF = "FULL_EXIT_DEFENSIVE_RISK_OFF"
    FULL_EXIT_TIMEOUT = "FULL_EXIT_TIMEOUT"


@dataclass(frozen=True)
class TrancheSlice:
    """Single profit-taking tranche planned by C2/C3 with pre-verified filter compliance (§6.2, QA-06)."""

    tranche_index: int
    qty: Decimal
    target_net_return: Decimal
    target_sell_price: Decimal
    stop_price_after_fill: Decimal
    remaining_qty_after_fill: Decimal
    tranche_stop_notional_usdt: Decimal
    remaining_stop_notional_usdt: Decimal
    is_trailing_leg: bool

    def __post_init__(self) -> None:
        require_decimal(self.qty, "qty")
        require_decimal(self.target_net_return, "target_net_return")
        require_decimal(self.target_sell_price, "target_sell_price")
        require_decimal(self.stop_price_after_fill, "stop_price_after_fill")
        require_decimal(self.remaining_qty_after_fill, "remaining_qty_after_fill")
        require_decimal(self.tranche_stop_notional_usdt, "tranche_stop_notional_usdt")
        require_decimal(self.remaining_stop_notional_usdt, "remaining_stop_notional_usdt")


@dataclass(frozen=True)
class TranchePlanResult:
    """Verified multi-tranche exit plan with automatic minNotional degradation metadata (QA-06)."""

    sellable_qty: Decimal
    avg_buy_price: Decimal
    initial_stop_price: Decimal
    breakeven_price: Decimal
    min_safe_notional_usdt: Decimal
    plan_mode: str
    degraded: bool
    degradation_reason: str
    tranches: tuple[TrancheSlice, ...]

    def verify_invariants(self, filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS) -> None:
        """Prove that tranche quantities sum to `sellable_qty` and every leg/remainder is protectable."""
        total_planned = sum((t.qty for t in self.tranches), Decimal("0.0"))
        if total_planned != self.sellable_qty:
            raise ValueError(
                f"Tranche quantity sum ({total_planned}) != sellable_qty ({self.sellable_qty})"
            )
        for t in self.tranches:
            if t.qty < filters.min_qty or (t.qty % filters.step_size) != Decimal("0"):
                raise ValueError(f"Tranche #{t.tranche_index} invalid qty {t.qty}")
            if t.tranche_stop_notional_usdt < filters.min_notional:
                raise ValueError(
                    f"Tranche #{t.tranche_index} notional {t.tranche_stop_notional_usdt} < {filters.min_notional}"
                )
            if t.remaining_qty_after_fill > Decimal("0"):
                if t.remaining_qty_after_fill < filters.min_qty:
                    raise ValueError(
                        f"Tranche #{t.tranche_index} leaves unprotectable remainder qty {t.remaining_qty_after_fill}"
                    )
                if t.remaining_stop_notional_usdt < self.min_safe_notional_usdt:
                    raise ValueError(
                        f"Tranche #{t.tranche_index} leaves remainder notional "
                        f"{t.remaining_stop_notional_usdt} < min_safe_notional {self.min_safe_notional_usdt}"
                    )


@dataclass(frozen=True)
class ExitFillSlice:
    """Record of an executed full or partial exit fill."""

    slice_index: int
    client_order_id: str
    qty: Decimal
    exec_price: Decimal
    gross_quote_usdt: Decimal
    fee_usdt: Decimal
    net_quote_usdt: Decimal
    action: ExitSignalAction
    reason: str
    executed_at_utc: datetime


@dataclass(frozen=True)
class CoordinatorStepReport:
    """Audit snapshot produced after `OcoProtectionCoordinator` processes an exit or protection update."""

    action: ExitSignalAction
    position_ledger: PositionLedger
    active_protection_order_id: str | None
    active_protection_stop_price: Decimal | None
    active_protection_limit_price: Decimal | None
    locked_sui_on_exchange: Decimal
    free_sui_on_exchange: Decimal
    fill_slice: ExitFillSlice | None
    over_reservation_detected: bool
    naked_position_detected: bool
    notes: str


def compute_fee_covered_breakeven_price(
    avg_buy_price: Decimal,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
    filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
) -> Decimal:
    """Compute exact limit sell price covering round-trip buy & sell fees (0.00% net return)."""
    return compute_c1_net_target_sell_price(
        avg_buy_price=avg_buy_price,
        buy_fee_rate=buy_fee_rate,
        sell_fee_rate=sell_fee_rate,
        target_net_return=Decimal("0.0000"),
        filters=filters,
    )


class OcoProtectionCoordinator:
    """Manages stop-loss protection and multi-tranche exit execution with zero over-reservation (§6.3, QA-06, QA-07).

    Invariants Enforced:
    - `INV-P5-LOCK-1`: `exchange.locked_sui == ledger.reserved_qty <= sellable_qty <= ledger.actual_qty`.
    - `INV-P5-LOCK-2`: Before placing any partial take-profit or ratcheted stop order, any prior active
      protection order is atomically canceled first so `locked_sui` never double-locks the same SUI.
    - `INV-P5-LOCK-3`: While `remaining_sellable_qty > 0`, the position is NEVER left without an active
      protection order at the end of a coordinator step (`naked_position_count == 0`).
    - `INV-P5-GAP-4 (QA-07)`: If a flash gap-down breaches `-1%` stop-loss AND bypasses the resting
      `STOP_LOSS_LIMIT` limit price (`best_bid < limit_price`), or if a stop order is rejected, the
      coordinator immediately cancels the bypassed protection order and executes an emergency market
      sweep exit, recording the true `> 1%` realized loss.
    """

    def __init__(
        self,
        *,
        exchange: PaperOrderbookSimulator,
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
        alert_manager: AlertManager | None = None,
    ) -> None:
        if exchange.execution_mode != ExecutionMode.PAPER:
            raise PermissionError("OcoProtectionCoordinator strictly requires PAPER mode")
        self.exchange = exchange
        self.filters = filters
        self.alert_manager = alert_manager
        self._seq_counter = 1
        self._over_reservation_violations = 0
        self._naked_position_violations = 0
        self._simulated_next_protection_reject = False

    @property
    def over_reservation_violations(self) -> int:
        return self._over_reservation_violations

    @property
    def naked_position_violations(self) -> int:
        return self._naked_position_violations

    def inject_next_protection_order_rejection(self) -> None:
        """Fault-injection hook for QA-07: simulate exchange rejecting a stop/protection order placement."""
        self._simulated_next_protection_reject = True

    def _next_cid(self, leg: str, now_utc: datetime) -> str:
        ts_ms = int(now_utc.timestamp() * 1000)
        cid = compute_deterministic_p4_client_order_id(
            mode=ExecutionMode.PAPER,
            signal_bar_close_ms=ts_ms,
            intent_seq=self._seq_counter,
            leg=leg[:4],
        )
        self._seq_counter += 1
        return cid

    def assert_locking_invariants(
        self,
        ledger: PositionLedger,
        expect_flat: bool = False,
    ) -> tuple[bool, bool]:
        """Verify QA-06 locking and non-naked position invariants against `PaperOrderbookSimulator`."""
        open_sells = self.exchange.list_open_orders(side=OrderSide.SELL)
        sum_open_sell_qty = sum((o.remaining_qty for o in open_sells), Decimal("0.0"))
        over_reserved = (
            sum_open_sell_qty > ledger.actual_qty
            or self.exchange.locked_sui > ledger.actual_qty
            or sum_open_sell_qty != ledger.reserved_qty
        )
        if over_reserved:
            self._over_reservation_violations += 1
            raise RuntimeError(
                f"QA-06 Over-Reservation Violation: sum_open_sell_qty={sum_open_sell_qty}, "
                f"exchange.locked_sui={self.exchange.locked_sui}, ledger.reserved_qty={ledger.reserved_qty}, "
                f"ledger.actual_qty={ledger.actual_qty}"
            )

        sellable_rem = self.filters.quantize_qty_down(ledger.actual_qty)
        is_naked = (not expect_flat) and (sellable_rem >= self.filters.min_qty) and (
            ledger.reserved_qty < sellable_rem or len(open_sells) == 0
        )
        if is_naked:
            self._naked_position_violations += 1
            raise RuntimeError(
                f"QA-06 Naked Position Violation: sellable_rem={sellable_rem} SUI has no active protection order"
            )
        return over_reserved, is_naked

    def establish_initial_protection(
        self,
        *,
        ledger: PositionLedger,
        stop_trigger_price: Decimal,
        stop_limit_price: Decimal | None = None,
        current_bid_price: Decimal | None = None,
        emergency_sweep_slippage_bps: Decimal = Decimal("15"),
        now_utc: datetime | None = None,
    ) -> CoordinatorStepReport:
        """Place initial `-1.0%` stop-loss protection order or trigger immediate emergency exit on rejection/gap."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        sellable_qty = self.filters.quantize_qty_down(ledger.actual_qty)
        sp = self.filters.quantize_price_down(require_decimal(stop_trigger_price, "stop_trigger_price"))
        lp = self.filters.quantize_price_down(
            require_decimal(stop_limit_price if stop_limit_price is not None else sp, "stop_limit_price")
        )

        # Cancel any pre-existing protection orders first (Cancel-First Protocol)
        for old_cid in ledger.protection_orders:
            self.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=old_cid,
                now_utc=ref_now,
            )

        if self._simulated_next_protection_reject:
            self._simulated_next_protection_reject = False
            fallback_price = current_bid_price if current_bid_price is not None else sp
            sweep_price = self.filters.quantize_price_down(
                fallback_price * (Decimal("1") - (emergency_sweep_slippage_bps / Decimal("10000")))
            )
            if self.alert_manager is not None:
                self.alert_manager.emit_alert(
                    severity=AlertSeverity.HIGH,
                    source="C-01",
                    dedup_key="C01-STOP-REJECTED-EMERGENCY-EXIT",
                    actions_taken=("EMERGENCY_MARKET_SWEEP_EXIT",),
                    details={"rejected_stop_price": str(sp), "sweep_price": str(sweep_price)},
                    now_utc=ref_now,
                )
            return self.execute_full_exit(
                ledger=PositionLedger(
                    position_id=ledger.position_id,
                    symbol=ledger.symbol,
                    actual_qty=ledger.actual_qty,
                    avg_fill_price=ledger.avg_fill_price,
                    fees=ledger.fees,
                    reserved_qty=Decimal("0.0"),
                    protection_orders=(),
                    exit_strategy_version=ledger.exit_strategy_version,
                    risk_config_version=ledger.risk_config_version,
                ),
                exec_price=sweep_price,
                action=ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP,
                reason="STOP_ORDER_REJECTED_EMERGENCY_SWEEP",
                slice_index=1,
                now_utc=ref_now,
            )

        stop_cid = self._next_cid("sl", ref_now)
        snap = self.exchange.submit_stop_loss_protection_order(
            symbol=TRADING_SYMBOL,
            client_order_id=stop_cid,
            qty=sellable_qty,
            stop_trigger_price=sp,
            limit_price=lp,
            now_utc=ref_now,
        )
        updated_ledger = PositionLedger(
            position_id=ledger.position_id,
            symbol=ledger.symbol,
            actual_qty=ledger.actual_qty,
            avg_fill_price=ledger.avg_fill_price,
            fees=ledger.fees,
            reserved_qty=sellable_qty,
            protection_orders=(stop_cid,),
            exit_strategy_version=ledger.exit_strategy_version,
            risk_config_version=ledger.risk_config_version,
        )
        over_res, naked = self.assert_locking_invariants(updated_ledger, expect_flat=False)
        return CoordinatorStepReport(
            action=ExitSignalAction.HOLD_PROTECTED,
            position_ledger=updated_ledger,
            active_protection_order_id=stop_cid,
            active_protection_stop_price=sp,
            active_protection_limit_price= snap.price,
            locked_sui_on_exchange=self.exchange.locked_sui,
            free_sui_on_exchange=self.exchange.free_sui,
            fill_slice=None,
            over_reservation_detected=over_res,
            naked_position_detected=naked,
            notes=f"Initial protection established: {stop_cid} locking {sellable_qty} SUI at stop={sp}",
        )

    def execute_partial_tranche_and_reprotect(
        self,
        *,
        ledger: PositionLedger,
        tranche_qty: Decimal,
        tranche_exec_price: Decimal,
        new_stop_trigger_price: Decimal,
        new_stop_limit_price: Decimal | None = None,
        slice_index: int = 1,
        reason: str = "C2_TRANCHE_PROFIT_LOCK",
        now_utc: datetime | None = None,
    ) -> CoordinatorStepReport:
        """Atomically execute a partial profit-taking tranche and re-lock remaining SUI with a ratcheted stop (§6.3, QA-06).

        Atomic 3-Step Protocol:
        1. Cancel existing protection order (`ledger.protection_orders`), releasing 100% of `locked_sui` to `free_sui`.
        2. Execute limit/exit sell for `tranche_qty` SUI at `tranche_exec_price`.
        3. Immediately place new stop-loss protection order for `remaining_sellable_qty` at `new_stop_trigger_price`,
           updating `PositionLedger` and verifying `reserved_qty == remaining_sellable_qty <= actual_qty`.
        """
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        tq = self.filters.quantize_qty_down(require_decimal(tranche_qty, "tranche_qty"))
        ep = self.filters.quantize_price_down(require_decimal(tranche_exec_price, "tranche_exec_price"))
        new_sp = self.filters.quantize_price_down(
            require_decimal(new_stop_trigger_price, "new_stop_trigger_price")
        )
        new_lp = self.filters.quantize_price_down(
            require_decimal(new_stop_limit_price if new_stop_limit_price is not None else new_sp, "new_stop_limit_price")
        )

        # Step 1: Release old protection order lock
        old_prot_cid = ledger.protection_orders[0] if ledger.protection_orders else None
        for cid in ledger.protection_orders:
            self.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=cid,
                now_utc=ref_now,
            )

        # Step 2: Execute partial tranche sell
        tp_cid = self._next_cid(f"tp{slice_index}", ref_now)
        tp_snap = self.exchange.submit_exit_sell_order(
            symbol=TRADING_SYMBOL,
            client_order_id=tp_cid,
            qty=tq,
            exec_price=ep,
            cancel_protection_client_order_id=old_prot_cid,
            now_utc=ref_now,
        )

        new_actual_qty = max(Decimal("0.0"), ledger.actual_qty - tp_snap.filled_qty)
        rem_sellable_qty = self.filters.quantize_qty_down(new_actual_qty)
        fill_slice = ExitFillSlice(
            slice_index=slice_index,
            client_order_id=tp_cid,
            qty=tp_snap.filled_qty,
            exec_price=tp_snap.avg_fill_price,
            gross_quote_usdt=tp_snap.cumulative_quote_qty,
            fee_usdt=tp_snap.fee_amount,
            net_quote_usdt=tp_snap.cumulative_quote_qty - tp_snap.fee_amount,
            action=ExitSignalAction.PARTIAL_TAKE_PROFIT_AND_RATCHET_STOP,
            reason=reason,
            executed_at_utc=ref_now,
        )

        # Step 3: Re-protect remaining sellable SUI
        if rem_sellable_qty >= self.filters.min_qty:
            new_stop_cid = self._next_cid(f"sl{slice_index}", ref_now)
            stop_snap = self.exchange.submit_stop_loss_protection_order(
                symbol=TRADING_SYMBOL,
                client_order_id=new_stop_cid,
                qty=rem_sellable_qty,
                stop_trigger_price=new_sp,
                limit_price=new_lp,
                now_utc=ref_now,
            )
            updated_ledger = PositionLedger(
                position_id=ledger.position_id,
                symbol=ledger.symbol,
                actual_qty=new_actual_qty,
                avg_fill_price=ledger.avg_fill_price,
                fees=ledger.fees + tp_snap.fee_amount,
                reserved_qty=rem_sellable_qty,
                protection_orders=(new_stop_cid,),
                exit_strategy_version=ledger.exit_strategy_version,
                risk_config_version=ledger.risk_config_version,
            )
            over_res, naked = self.assert_locking_invariants(updated_ledger, expect_flat=False)
            return CoordinatorStepReport(
                action=ExitSignalAction.PARTIAL_TAKE_PROFIT_AND_RATCHET_STOP,
                position_ledger=updated_ledger,
                active_protection_order_id=new_stop_cid,
                active_protection_stop_price=new_sp,
                active_protection_limit_price=stop_snap.price,
                locked_sui_on_exchange=self.exchange.locked_sui,
                free_sui_on_exchange=self.exchange.free_sui,
                fill_slice=fill_slice,
                over_reservation_detected=over_res,
                naked_position_detected=naked,
                notes=(
                    f"Filled tranche #{slice_index} ({tq} SUI @ {ep}) and ratcheted stop to "
                    f"{new_sp} locking remaining {rem_sellable_qty} SUI"
                ),
            )

        # No remaining sellable quantity -> flat
        flat_ledger = PositionLedger(
            position_id=ledger.position_id,
            symbol=ledger.symbol,
            actual_qty=new_actual_qty,
            avg_fill_price=ledger.avg_fill_price,
            fees=ledger.fees + tp_snap.fee_amount,
            reserved_qty=Decimal("0.0"),
            protection_orders=(),
            exit_strategy_version=ledger.exit_strategy_version,
            risk_config_version=ledger.risk_config_version,
        )
        over_res, naked = self.assert_locking_invariants(flat_ledger, expect_flat=True)
        return CoordinatorStepReport(
            action=ExitSignalAction.FULL_EXIT_TAKE_PROFIT,
            position_ledger=flat_ledger,
            active_protection_order_id=None,
            active_protection_stop_price=None,
            active_protection_limit_price=None,
            locked_sui_on_exchange=self.exchange.locked_sui,
            free_sui_on_exchange=self.exchange.free_sui,
            fill_slice=fill_slice,
            over_reservation_detected=over_res,
            naked_position_detected=naked,
            notes=f"Filled final tranche #{slice_index} ({tq} SUI @ {ep}) -> position FLAT",
        )

    def ratchet_stop_protection_only(
        self,
        *,
        ledger: PositionLedger,
        new_stop_trigger_price: Decimal,
        new_stop_limit_price: Decimal | None = None,
        reason: str = "TRAILING_STOP_RATCHET_UP",
        now_utc: datetime | None = None,
    ) -> CoordinatorStepReport:
        """Atomically cancel current stop order and place a higher stop-loss order for the same quantity."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        sellable_qty = self.filters.quantize_qty_down(ledger.actual_qty)
        new_sp = self.filters.quantize_price_down(
            require_decimal(new_stop_trigger_price, "new_stop_trigger_price")
        )
        new_lp = self.filters.quantize_price_down(
            require_decimal(new_stop_limit_price if new_stop_limit_price is not None else new_sp, "new_stop_limit_price")
        )

        for cid in ledger.protection_orders:
            self.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=cid,
                now_utc=ref_now,
            )

        new_stop_cid = self._next_cid("slr", ref_now)
        stop_snap = self.exchange.submit_stop_loss_protection_order(
            symbol=TRADING_SYMBOL,
            client_order_id=new_stop_cid,
            qty=sellable_qty,
            stop_trigger_price=new_sp,
            limit_price=new_lp,
            now_utc=ref_now,
        )
        updated_ledger = PositionLedger(
            position_id=ledger.position_id,
            symbol=ledger.symbol,
            actual_qty=ledger.actual_qty,
            avg_fill_price=ledger.avg_fill_price,
            fees=ledger.fees,
            reserved_qty=sellable_qty,
            protection_orders=(new_stop_cid,),
            exit_strategy_version=ledger.exit_strategy_version,
            risk_config_version=ledger.risk_config_version,
        )
        over_res, naked = self.assert_locking_invariants(updated_ledger, expect_flat=False)
        return CoordinatorStepReport(
            action=ExitSignalAction.RATCHET_STOP_ONLY,
            position_ledger=updated_ledger,
            active_protection_order_id=new_stop_cid,
            active_protection_stop_price=new_sp,
            active_protection_limit_price=stop_snap.price,
            locked_sui_on_exchange=self.exchange.locked_sui,
            free_sui_on_exchange=self.exchange.free_sui,
            fill_slice=None,
            over_reservation_detected=over_res,
            naked_position_detected=naked,
            notes=f"{reason}: ratcheted stop to {new_sp} locking {sellable_qty} SUI",
        )

    def execute_full_exit(
        self,
        *,
        ledger: PositionLedger,
        exec_price: Decimal,
        action: ExitSignalAction,
        reason: str,
        slice_index: int = 1,
        now_utc: datetime | None = None,
    ) -> CoordinatorStepReport:
        """Atomically release any active protection order and sell 100% of remaining sellable SUI."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        sellable_qty = self.filters.quantize_qty_down(ledger.actual_qty)
        ep = self.filters.quantize_price_down(require_decimal(exec_price, "exec_price"))

        old_prot_cid = ledger.protection_orders[0] if ledger.protection_orders else None
        for cid in ledger.protection_orders:
            self.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=cid,
                now_utc=ref_now,
            )

        exit_cid = self._next_cid("ex", ref_now)
        sell_snap = self.exchange.submit_exit_sell_order(
            symbol=TRADING_SYMBOL,
            client_order_id=exit_cid,
            qty=sellable_qty,
            exec_price=ep,
            cancel_protection_client_order_id=old_prot_cid,
            now_utc=ref_now,
        )
        dust_rem = max(Decimal("0.0"), ledger.actual_qty - sell_snap.filled_qty)
        flat_ledger = PositionLedger(
            position_id=ledger.position_id,
            symbol=ledger.symbol,
            actual_qty=dust_rem,
            avg_fill_price=ledger.avg_fill_price,
            fees=ledger.fees + sell_snap.fee_amount,
            reserved_qty=Decimal("0.0"),
            protection_orders=(),
            exit_strategy_version=ledger.exit_strategy_version,
            risk_config_version=ledger.risk_config_version,
        )
        fill_slice = ExitFillSlice(
            slice_index=slice_index,
            client_order_id=exit_cid,
            qty=sell_snap.filled_qty,
            exec_price=sell_snap.avg_fill_price,
            gross_quote_usdt=sell_snap.cumulative_quote_qty,
            fee_usdt=sell_snap.fee_amount,
            net_quote_usdt=sell_snap.cumulative_quote_qty - sell_snap.fee_amount,
            action=action,
            reason=reason,
            executed_at_utc=ref_now,
        )
        over_res, naked = self.assert_locking_invariants(flat_ledger, expect_flat=True)
        return CoordinatorStepReport(
            action=action,
            position_ledger=flat_ledger,
            active_protection_order_id=None,
            active_protection_stop_price=None,
            active_protection_limit_price=None,
            locked_sui_on_exchange=self.exchange.locked_sui,
            free_sui_on_exchange=self.exchange.free_sui,
            fill_slice=fill_slice,
            over_reservation_detected=over_res,
            naked_position_detected=naked,
            notes=f"Full exit ({reason}): sold {sell_snap.filled_qty} SUI @ {sell_snap.avg_fill_price}",
        )

    def handle_market_tick_or_gap_down(
        self,
        *,
        ledger: PositionLedger,
        best_bid_price: Decimal,
        best_ask_price: Decimal,
        emergency_sweep_slippage_bps: Decimal = Decimal("15"),
        now_utc: datetime | None = None,
    ) -> CoordinatorStepReport:
        """Process an orderbook tick against resting protection orders, including QA-07 flash gap-down fallback.

        If `best_bid_price` drops below `stop_price` AND gaps below the resting `STOP_LOSS_LIMIT` limit
        price (`best_bid_price < snap.price`), a realistic exchange limit order might rest unfilled while
        price crashes further! This method detects the gap-through, cancels the bypassed limit order, and
        executes an emergency market sweep at `best_bid_price * (1 - emergency_sweep_slippage_bps / 10000)`.
        """
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        bid = require_decimal(best_bid_price, "best_bid_price")
        if not ledger.protection_orders:
            raise RuntimeError("handle_market_tick_or_gap_down called without active protection order")

        prot_cid = ledger.protection_orders[0]
        snap = self.exchange.query_order_by_client_order_id(
            symbol=TRADING_SYMBOL,
            client_order_id=prot_cid,
            now_utc=ref_now,
        )
        if snap.stop_price is None:
            raise RuntimeError(f"Protection order {prot_cid} missing stop_price")

        if bid > snap.stop_price:
            over_res, naked = self.assert_locking_invariants(ledger, expect_flat=False)
            return CoordinatorStepReport(
                action=ExitSignalAction.HOLD_PROTECTED,
                position_ledger=ledger,
                active_protection_order_id=prot_cid,
                active_protection_stop_price=snap.stop_price,
                active_protection_limit_price=snap.price,
                locked_sui_on_exchange=self.exchange.locked_sui,
                free_sui_on_exchange=self.exchange.free_sui,
                fill_slice=None,
                over_reservation_detected=over_res,
                naked_position_detected=naked,
                notes=f"Bid {bid} > stop_price {snap.stop_price}; holding protected",
            )

        # Stop-loss triggered (bid <= snap.stop_price)!
        # Check if flash gap-down penetrated below the limit price of the STOP_LOSS_LIMIT order (QA-07)
        if bid < snap.price:
            sweep_price = self.filters.quantize_price_down(
                bid * (Decimal("1") - (emergency_sweep_slippage_bps / Decimal("10000")))
            )
            if self.alert_manager is not None:
                self.alert_manager.emit_alert(
                    severity=AlertSeverity.HIGH,
                    source="C-01",
                    dedup_key="C01-GAP-DOWN-BEYOND-STOP-LIMIT",
                    actions_taken=("CANCEL_BYPASSED_STOP_LIMIT", "EMERGENCY_SWEEP_EXIT"),
                    details={
                        "stop_trigger_price": str(snap.stop_price),
                        "stop_limit_price": str(snap.price),
                        "gap_bid_price": str(bid),
                        "sweep_exec_price": str(sweep_price),
                    },
                    now_utc=ref_now,
                )
            return self.execute_full_exit(
                ledger=ledger,
                exec_price=sweep_price,
                action=ExitSignalAction.FULL_EXIT_GAP_DOWN_EMERGENCY_SWEEP,
                reason="STOP_LOSS_GAP_DOWN_EMERGENCY_SWEEP",
                slice_index=1,
                now_utc=ref_now,
            )

        # Normal stop trigger fill at min(snap.price, bid)
        return self.execute_full_exit(
            ledger=ledger,
            exec_price=min(snap.price, bid),
            action=ExitSignalAction.FULL_EXIT_STOP_LOSS,
            reason="STOP_LOSS_1PCT_TRIGGER",
            slice_index=1,
            now_utc=ref_now,
        )


class C1FixedNetTargetExit:
    """Strategy C1: Fixed +1.50% net return target after buy & sell fees, -1.0% stop, 3h timeout (§6.2)."""

    def __init__(
        self,
        *,
        target_net_return: Decimal = Decimal("0.0150"),
        stop_loss_pct: Decimal = Decimal("0.0100"),
        max_holding_bars_5m: int = 36,
        buy_fee_rate: Decimal = Decimal("0.0010"),
        sell_fee_rate: Decimal = Decimal("0.0010"),
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
    ) -> None:
        self.strategy_id = ExitStrategyId.C1
        self.version = "C1-v1.0"
        self.target_net_return = require_decimal(target_net_return, "target_net_return")
        self.stop_loss_pct = require_decimal(stop_loss_pct, "stop_loss_pct")
        self.max_holding_bars_5m = int(max_holding_bars_5m)
        self.buy_fee_rate = require_decimal(buy_fee_rate, "buy_fee_rate")
        self.sell_fee_rate = require_decimal(sell_fee_rate, "sell_fee_rate")
        self.filters = filters

    def compute_target_and_stop(self, avg_buy_price: Decimal) -> tuple[Decimal, Decimal]:
        """Return `(target_sell_price, initial_stop_trigger_price)` quantized to `tick_size`."""
        target_p = compute_c1_net_target_sell_price(
            avg_buy_price=avg_buy_price,
            buy_fee_rate=self.buy_fee_rate,
            sell_fee_rate=self.sell_fee_rate,
            target_net_return=self.target_net_return,
            filters=self.filters,
        )
        stop_p = compute_stop_loss_trigger_price(
            avg_buy_price=avg_buy_price,
            stop_loss_pct=self.stop_loss_pct,
            filters=self.filters,
        )
        return target_p, stop_p


class C2GridTrailingExit:
    """Strategy C2: Upward 3-tranche profit taking (40% / 30% / 30%) + breakeven ratchet + trailing stop (§6.2).

    Tranche Schedule (strictly upward `> avg_buy_price`, zero downward grid / averaging down):
    - Tranche 1 (`40%`): Net profit `+1.00%` -> ratchets remaining `60%` stop-loss to fee-covered breakeven.
    - Tranche 2 (`30%`): Net profit `+1.80%` -> ratchets remaining `30%` stop-loss to Tranche 1 (`+1.00%` net).
    - Tranche 3 (`30%`): High-water-mark trailing stop (`0.60%` pullback from peak after T2 fill, floored at T1).

    Min-Notional Degradation Guard (`plan_tranches`, QA-06):
    - Automatically merges tranches into 2-tranche `(50%, 50%)` or 1-tranche `(100%)` if position size is
      too small for every tranche and remainder to satisfy `notional >= min_notional * 1.05 (5.25 USDT)`
      at the `-1%` stop trigger price.
    """

    def __init__(
        self,
        *,
        t1_net_return: Decimal = Decimal("0.0100"),
        t2_net_return: Decimal = Decimal("0.0180"),
        c1_fallback_net_return: Decimal = Decimal("0.0150"),
        trailing_pullback_pct: Decimal = Decimal("0.0060"),
        stop_loss_pct: Decimal = Decimal("0.0100"),
        notional_safety_buffer_pct: Decimal = Decimal("0.0500"),
        max_holding_bars_5m: int = 36,
        buy_fee_rate: Decimal = Decimal("0.0010"),
        sell_fee_rate: Decimal = Decimal("0.0010"),
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
    ) -> None:
        self.strategy_id = ExitStrategyId.C2
        self.version = "C2-v1.0"
        self.t1_net_return = require_decimal(t1_net_return, "t1_net_return")
        self.t2_net_return = require_decimal(t2_net_return, "t2_net_return")
        self.c1_fallback_net_return = require_decimal(
            c1_fallback_net_return, "c1_fallback_net_return"
        )
        self.trailing_pullback_pct = require_decimal(
            trailing_pullback_pct, "trailing_pullback_pct"
        )
        self.stop_loss_pct = require_decimal(stop_loss_pct, "stop_loss_pct")
        self.notional_safety_buffer_pct = require_decimal(
            notional_safety_buffer_pct, "notional_safety_buffer_pct"
        )
        self.max_holding_bars_5m = int(max_holding_bars_5m)
        self.buy_fee_rate = require_decimal(buy_fee_rate, "buy_fee_rate")
        self.sell_fee_rate = require_decimal(sell_fee_rate, "sell_fee_rate")
        self.filters = filters

    def plan_tranches(
        self,
        *,
        sellable_qty: Decimal,
        avg_buy_price: Decimal,
    ) -> TranchePlanResult:
        """Plan C2 upward tranches with strict `stepSize` quantization and `minNotional` degradation guard (QA-06)."""
        sq = self.filters.quantize_qty_down(require_decimal(sellable_qty, "sellable_qty"))
        bp = require_decimal(avg_buy_price, "avg_buy_price")
        if sq < self.filters.min_qty:
            raise ExchangeFilterError(f"sellable_qty ({sq}) is below min_qty ({self.filters.min_qty})")

        init_stop = compute_stop_loss_trigger_price(
            avg_buy_price=bp,
            stop_loss_pct=self.stop_loss_pct,
            filters=self.filters,
        )
        be_price = compute_fee_covered_breakeven_price(
            avg_buy_price=bp,
            buy_fee_rate=self.buy_fee_rate,
            sell_fee_rate=self.sell_fee_rate,
            filters=self.filters,
        )
        t1_price = compute_c1_net_target_sell_price(
            avg_buy_price=bp,
            buy_fee_rate=self.buy_fee_rate,
            sell_fee_rate=self.sell_fee_rate,
            target_net_return=self.t1_net_return,
            filters=self.filters,
        )
        t2_price = compute_c1_net_target_sell_price(
            avg_buy_price=bp,
            buy_fee_rate=self.buy_fee_rate,
            sell_fee_rate=self.sell_fee_rate,
            target_net_return=self.t2_net_return,
            filters=self.filters,
        )
        c1_price = compute_c1_net_target_sell_price(
            avg_buy_price=bp,
            buy_fee_rate=self.buy_fee_rate,
            sell_fee_rate=self.sell_fee_rate,
            target_net_return=self.c1_fallback_net_return,
            filters=self.filters,
        )
        min_safe_notional = self.filters.min_notional * (Decimal("1") + self.notional_safety_buffer_pct)

        # Attempt 1: Full 3-tranche split (40% / 30% / 30%)
        q1_3 = self.filters.quantize_qty_down(sq * Decimal("0.40"))
        q2_3 = self.filters.quantize_qty_down(sq * Decimal("0.30"))
        q3_3 = sq - q1_3 - q2_3
        rem_after_1 = sq - q1_3
        rem_after_2 = q3_3

        can_split_3 = (
            q1_3 >= self.filters.min_qty
            and q2_3 >= self.filters.min_qty
            and q3_3 >= self.filters.min_qty
            and (q1_3 * init_stop) >= min_safe_notional
            and (q2_3 * init_stop) >= min_safe_notional
            and (q3_3 * init_stop) >= min_safe_notional
            and (rem_after_1 * init_stop) >= min_safe_notional
            and (rem_after_2 * init_stop) >= min_safe_notional
        )
        if can_split_3:
            tranches_3 = (
                TrancheSlice(
                    tranche_index=1,
                    qty=q1_3,
                    target_net_return=self.t1_net_return,
                    target_sell_price=t1_price,
                    stop_price_after_fill=be_price,
                    remaining_qty_after_fill=rem_after_1,
                    tranche_stop_notional_usdt=q1_3 * init_stop,
                    remaining_stop_notional_usdt=rem_after_1 * be_price,
                    is_trailing_leg=False,
                ),
                TrancheSlice(
                    tranche_index=2,
                    qty=q2_3,
                    target_net_return=self.t2_net_return,
                    target_sell_price=t2_price,
                    stop_price_after_fill=t1_price,
                    remaining_qty_after_fill=rem_after_2,
                    tranche_stop_notional_usdt=q2_3 * be_price,
                    remaining_stop_notional_usdt=rem_after_2 * t1_price,
                    is_trailing_leg=False,
                ),
                TrancheSlice(
                    tranche_index=3,
                    qty=q3_3,
                    target_net_return=self.t2_net_return,
                    target_sell_price=t2_price,
                    stop_price_after_fill=t1_price,
                    remaining_qty_after_fill=Decimal("0.0"),
                    tranche_stop_notional_usdt=q3_3 * t1_price,
                    remaining_stop_notional_usdt=Decimal("0.0"),
                    is_trailing_leg=True,
                ),
            )
            plan = TranchePlanResult(
                sellable_qty=sq,
                avg_buy_price=bp,
                initial_stop_price=init_stop,
                breakeven_price=be_price,
                min_safe_notional_usdt=min_safe_notional,
                plan_mode="FULL_3_TRANCHE_40_30_30",
                degraded=False,
                degradation_reason="NONE",
                tranches=tranches_3,
            )
            plan.verify_invariants(self.filters)
            return plan

        # Attempt 2: Degraded 2-tranche split (50% at T1 +1.0% net / 50% trailing leg)
        q1_2 = self.filters.quantize_qty_down(sq * Decimal("0.50"))
        q2_2 = sq - q1_2
        can_split_2 = (
            q1_2 >= self.filters.min_qty
            and q2_2 >= self.filters.min_qty
            and (q1_2 * init_stop) >= min_safe_notional
            and (q2_2 * init_stop) >= min_safe_notional
        )
        if can_split_2:
            tranches_2 = (
                TrancheSlice(
                    tranche_index=1,
                    qty=q1_2,
                    target_net_return=self.t1_net_return,
                    target_sell_price=t1_price,
                    stop_price_after_fill=be_price,
                    remaining_qty_after_fill=q2_2,
                    tranche_stop_notional_usdt=q1_2 * init_stop,
                    remaining_stop_notional_usdt=q2_2 * be_price,
                    is_trailing_leg=False,
                ),
                TrancheSlice(
                    tranche_index=2,
                    qty=q2_2,
                    target_net_return=self.t2_net_return,
                    target_sell_price=t2_price,
                    stop_price_after_fill=be_price,
                    remaining_qty_after_fill=Decimal("0.0"),
                    tranche_stop_notional_usdt=q2_2 * be_price,
                    remaining_stop_notional_usdt=Decimal("0.0"),
                    is_trailing_leg=True,
                ),
            )
            plan = TranchePlanResult(
                sellable_qty=sq,
                avg_buy_price=bp,
                initial_stop_price=init_stop,
                breakeven_price=be_price,
                min_safe_notional_usdt=min_safe_notional,
                plan_mode="DEGRADED_2_TRANCHE_50_50",
                degraded=True,
                degradation_reason="SUB_TRANCHE_30PCT_BELOW_MIN_SAFE_NOTIONAL",
                tranches=tranches_2,
            )
            plan.verify_invariants(self.filters)
            return plan

        # Attempt 3: Degraded 1-tranche 100% C1 fallback (prevents any unprotectable remainder < 5.25 USDT)
        tranches_1 = (
            TrancheSlice(
                tranche_index=1,
                qty=sq,
                target_net_return=self.c1_fallback_net_return,
                target_sell_price=c1_price,
                stop_price_after_fill=init_stop,
                remaining_qty_after_fill=Decimal("0.0"),
                tranche_stop_notional_usdt=sq * init_stop,
                remaining_stop_notional_usdt=Decimal("0.0"),
                is_trailing_leg=False,
            ),
        )
        plan = TranchePlanResult(
            sellable_qty=sq,
            avg_buy_price=bp,
            initial_stop_price=init_stop,
            breakeven_price=be_price,
            min_safe_notional_usdt=min_safe_notional,
            plan_mode="DEGRADED_1_TRANCHE_100_C1_FALLBACK",
            degraded=True,
            degradation_reason="TOTAL_NOTIONAL_TOO_SMALL_FOR_MULTI_TRANCHE_SPLIT",
            tranches=tranches_1,
        )
        plan.verify_invariants(self.filters)
        return plan


class C3AdaptiveHybridExit:
    """Strategy C3: A-01 Market Regime & Orderbook Imbalance (OBI_5) Adaptive Hybrid Exit (§6.2).

    Decision Rules:
    1. Emergency Risk-Off Exit (`DOWN_TREND`, `HIGH_VOLATILITY`, `DATA_UNHEALTHY`, or `btc_flash_drop_flag`):
       Immediately exits 100% of remaining sellable position without waiting for the -1.0% stop trigger.
    2. Consolidation Mode (`CONSOLIDATION` or `obi_5 < 0` before Tranche 1 fills):
       Locks in profit at C1 `+1.50%` net target price in a single slice.
    3. Strong Trend Continuation Mode (`UP_TREND` and `obi_5 >= 0`):
       Uses C2 multi-tranche profit taking + breakeven ratchet + high-water-mark trailing stop.
    """

    def __init__(
        self,
        *,
        c1_delegate: C1FixedNetTargetExit | None = None,
        c2_delegate: C2GridTrailingExit | None = None,
        obi_trend_threshold: Decimal = Decimal("0.00"),
        defensive_exit_min_hold_bars: int = 1,
    ) -> None:
        self.strategy_id = ExitStrategyId.C3
        self.version = "C3-v1.0"
        self.c1 = c1_delegate or C1FixedNetTargetExit()
        self.c2 = c2_delegate or C2GridTrailingExit()
        self.obi_trend_threshold = require_decimal(obi_trend_threshold, "obi_trend_threshold")
        self.defensive_exit_min_hold_bars = int(defensive_exit_min_hold_bars)

    def should_trigger_emergency_risk_off(
        self,
        *,
        regime: RegimeType,
        btc_flash_drop_active: bool = False,
        bars_held: int = 1,
    ) -> tuple[bool, str]:
        """Determine whether an in-flight position must exit proactively due to regime breakdown."""
        if btc_flash_drop_active:
            return True, "C3_BTC_FLASH_DROP_EMERGENCY_EXIT"
        if bars_held >= self.defensive_exit_min_hold_bars and regime in {
            RegimeType.DOWN_TREND,
            RegimeType.HIGH_VOLATILITY,
            RegimeType.DATA_UNHEALTHY,
        }:
            return True, f"C3_REGIME_{regime.value}_DEFENSIVE_EXIT"
        return False, "NONE"

    def select_active_mode(
        self,
        *,
        regime: RegimeType,
        obi_5: Decimal,
    ) -> ExitStrategyId:
        """Select C2 multi-tranche trailing in strong `UP_TREND` with positive OBI_5, else C1 fixed target."""
        if regime == RegimeType.UP_TREND and obi_5 >= self.obi_trend_threshold:
            return ExitStrategyId.C2
        return ExitStrategyId.C1
