"""R-01 Pre-Trade Independent Risk Evaluator and Final Veto Gate (§8, §14.2, QA-03, QA-08).

Implements the 7-layer deterministic pre-trade risk verification pipeline completely decoupled
from signal/ML models:
1. Mode Lock (INV-R1): Strictly enforces `ExecutionMode.PAPER` in P4; triggers `SECURITY_LOCKDOWN` otherwise.
2. Symbol Lock (INV-R2): Strictly locks trading symbol to `SUIUSDT`; rejects `BTCUSDT` or any other symbol (`QA-08`).
3. Rolling 24h Drawdown Circuit Breaker (INV-R5): Freezes new entries (`ENTRY_FROZEN`) if rolling 24h loss >= 1.0%.
4. Dynamic Exchange Filter Freshness Gate: Requires fresh `DYNAMIC_EXCHANGE_INFO` filters (`<= 3600s`).
5. Dual-Cap Risk Position Sizing (INV-R4): Enforces `min(0.25% equity risk, 10.0% equity allocation)` floored to `stepSize=0.1`.
6. Available Free Balance Check (`QA-03`): Caps order size by `free_usdt / (p_entry * (1 + buy_fee_rate))`.
7. Pre-Buy Protectability Gate (`QA-03`, `QA-06`): Verifies post-fill net sellable quantity after worst-case SUI fee
   deduction and `stepSize` floor satisfies both `minQty (0.1 SUI)` and `minNotional * 1.05 (5.25 USDT)` at `-1%` stop.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from sui_quant.config import ExecutionMode, RiskConfig
from sui_quant.contracts import (
    TRADING_SYMBOL,
    BreakoutProposal,
    OrderIntent,
    OrderSide,
    RiskVerdict,
    require_decimal,
)
from sui_quant.exchange_rules import (
    ExchangeFilterError,
    PostFillSellableAllocation,
    StaleExchangeFilterError,
    SymbolFilters,
    validate_pre_buy_protectability,
)
from sui_quant.state_machine import TradingState
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


@dataclass(frozen=True)
class RealizedPnLEntry:
    """Record of a realized trade net PnL in USDT for rolling 24h drawdown calculation."""

    closed_at_utc: datetime
    net_pnl_usdt: Decimal

    def __post_init__(self) -> None:
        ensure_utc(self.closed_at_utc, "closed_at_utc")
        require_decimal(self.net_pnl_usdt, "net_pnl_usdt")


@dataclass(frozen=True)
class R01EvaluationResult:
    """Detailed output of R-01 7-layer pre-trade risk evaluation."""

    verdict: RiskVerdict
    recommended_state_override: TradingState | None
    rolling_24h_drawdown_pct: Decimal
    q_risk_raw: Decimal
    q_cap_raw: Decimal
    q_free_raw: Decimal
    final_qty: Decimal
    protectability: PostFillSellableAllocation | None
    decision_latency_ns: int


def compute_rolling_24h_drawdown_pct(
    *,
    initial_24h_equity_usdt: Decimal,
    realized_pnl_entries: Sequence[RealizedPnLEntry] = (),
    unrealized_pnl_usdt: Decimal = Decimal("0"),
    now_utc: datetime,
) -> Decimal:
    """Compute rolling 24h drawdown ratio:

    DD_24h = -min(0, sum_{tau in [t-24h, t]} PnL_realized(tau) + PnL_unrealized(t)) / E_24h
    """
    ensure_utc(now_utc, "now_utc")
    e24 = require_decimal(initial_24h_equity_usdt, "initial_24h_equity_usdt")
    if e24 <= Decimal("0"):
        raise ValueError("initial_24h_equity_usdt must be > 0")
    unrealized = require_decimal(unrealized_pnl_usdt, "unrealized_pnl_usdt")
    cutoff = now_utc - timedelta(hours=24)
    realized_24h = sum(
        (entry.net_pnl_usdt for entry in realized_pnl_entries if cutoff <= entry.closed_at_utc <= now_utc),
        Decimal("0"),
    )
    total_24h_pnl = realized_24h + unrealized
    if total_24h_pnl >= Decimal("0"):
        return Decimal("0")
    return (-total_24h_pnl) / e24


class R01PreTradeRiskGate:
    """Independent deterministic R-01 risk gate with final veto authority over all candidate entries (§8)."""

    def __init__(
        self,
        risk_config: RiskConfig | None = None,
        execution_mode: ExecutionMode = ExecutionMode.PAPER,
        buy_fee_rate: Decimal = Decimal("0.0010"),
        sell_fee_rate: Decimal = Decimal("0.0010"),
        stop_slippage_buffer_rate: Decimal = Decimal("0.0010"),
        notional_safety_buffer_pct: Decimal = Decimal("0.05"),
        max_filter_age_seconds: int = 3600,
    ) -> None:
        self.risk_config = risk_config or RiskConfig()
        self.execution_mode = execution_mode
        self.buy_fee_rate = require_decimal(buy_fee_rate, "buy_fee_rate")
        self.sell_fee_rate = require_decimal(sell_fee_rate, "sell_fee_rate")
        self.stop_slippage_buffer_rate = require_decimal(
            stop_slippage_buffer_rate, "stop_slippage_buffer_rate"
        )
        self.notional_safety_buffer_pct = require_decimal(
            notional_safety_buffer_pct, "notional_safety_buffer_pct"
        )
        self.max_filter_age_seconds = max_filter_age_seconds

    @property
    def bilateral_cost_and_slippage_buffer(self) -> Decimal:
        """Total friction buffer `c_buf = f_buy + f_sell + s_stop` (default 30 bps = 0.0030)."""
        return self.buy_fee_rate + self.sell_fee_rate + self.stop_slippage_buffer_rate

    def evaluate_candidate(
        self,
        *,
        proposal: BreakoutProposal,
        symbol: str,
        planned_entry_price: Decimal,
        account_equity_usdt: Decimal,
        free_usdt: Decimal,
        filters: SymbolFilters,
        rolling_24h_drawdown_pct: Decimal | None = None,
        initial_24h_equity_usdt: Decimal | None = None,
        realized_pnl_24h: Sequence[RealizedPnLEntry] = (),
        unrealized_pnl_usdt: Decimal = Decimal("0"),
        current_trading_state: TradingState = TradingState.RISK_CHECK,
        now_utc: datetime | None = None,
    ) -> R01EvaluationResult:
        """Execute the 7-layer pre-trade risk check pipeline in strict priority order (§3.3)."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        p_entry = require_decimal(planned_entry_price, "planned_entry_price")
        equity = require_decimal(account_equity_usdt, "account_equity_usdt")
        avail_usdt = require_decimal(free_usdt, "free_usdt")

        reject_reasons: list[str] = []
        state_override: TradingState | None = None

        # Compute or validate 24h rolling drawdown
        if rolling_24h_drawdown_pct is not None:
            dd_24h = require_decimal(rolling_24h_drawdown_pct, "rolling_24h_drawdown_pct")
        else:
            ref_e24 = initial_24h_equity_usdt if initial_24h_equity_usdt is not None else equity
            dd_24h = compute_rolling_24h_drawdown_pct(
                initial_24h_equity_usdt=ref_e24,
                realized_pnl_entries=realized_pnl_24h,
                unrealized_pnl_usdt=unrealized_pnl_usdt,
                now_utc=ref_now,
            )

        # Layer 0: Active emergency state check
        if current_trading_state in {TradingState.ENTRY_FROZEN, TradingState.SECURITY_LOCKDOWN}:
            reject_reasons.append(f"STATE_LOCKED_{current_trading_state.value}")
            state_override = current_trading_state

        # Layer 1: Execution Mode Hard Lock (INV-R1)
        if self.execution_mode != ExecutionMode.PAPER:
            reject_reasons.append("EXECUTION_MODE_NOT_PAPER")
            state_override = TradingState.SECURITY_LOCKDOWN

        # Layer 2: Symbol Hard Lock (INV-R2, QA-08)
        if symbol != TRADING_SYMBOL or symbol != self.risk_config.trading_symbol:
            reject_reasons.append("SYMBOL_FORBIDDEN")

        # Layer 3: Rolling 24h Loss Circuit Breaker (INV-R5, QA-08)
        if dd_24h >= self.risk_config.max_rolling_24h_loss_pct:
            reject_reasons.append("ROLLING_24H_LOSS_LIMIT_EXCEEDED")
            if state_override != TradingState.SECURITY_LOCKDOWN:
                state_override = TradingState.ENTRY_FROZEN

        # Layer 4: Dynamic Exchange Filter Freshness Gate
        try:
            filters.assert_fresh_for_execution(
                now_utc=ref_now,
                max_age_seconds=self.max_filter_age_seconds,
            )
        except (StaleExchangeFilterError, ExchangeFilterError) as exc:
            reject_reasons.append(f"STALE_SYMBOL_FILTERS:{exc}")

        # Layer 5: Dual-Cap Risk Position Sizing (0.25% Single-Trade Risk + 10% Capital Allocation Cap)
        if p_entry <= Decimal("0") or equity <= Decimal("0"):
            reject_reasons.append("INVALID_PRICE_OR_EQUITY")
            q_risk_raw = Decimal("0")
            q_cap_raw = Decimal("0")
            q_free_raw = Decimal("0")
            q_max = Decimal("0")
        else:
            unit_risk_pct = self.risk_config.stop_loss_trigger_pct + self.bilateral_cost_and_slippage_buffer
            unit_risk_usdt = p_entry * unit_risk_pct
            q_risk_raw = (self.risk_config.max_single_trade_risk_pct * equity) / unit_risk_usdt
            q_cap_raw = (self.risk_config.max_capital_allocation_pct * equity) / p_entry
            q_dual = min(q_risk_raw, q_cap_raw)
            q_max = filters.quantize_qty_down(q_dual)

            # Layer 6: Available Free Balance Check (QA-03)
            if avail_usdt <= Decimal("0"):
                q_free_raw = Decimal("0")
                q_max = Decimal("0")
                reject_reasons.append("INSUFFICIENT_FREE_BALANCE")
            else:
                q_free_raw = avail_usdt / (p_entry * (Decimal("1") + self.buy_fee_rate))
                q_free_floored = filters.quantize_qty_down(q_free_raw)
                if q_free_floored < q_max:
                    q_max = q_free_floored
                if q_max < filters.min_qty or (q_max * p_entry) < filters.min_notional:
                    if q_free_floored < filters.quantize_qty_down(q_dual):
                        reject_reasons.append("INSUFFICIENT_FREE_BALANCE")
                    else:
                        reject_reasons.append("UNPROTECTABLE_ORDER_SIZE:BELOW_MIN_QTY_OR_NOTIONAL")

        # Layer 7: Pre-Buy Protectability Hard Gate (QA-03, QA-06)
        protectability_alloc: PostFillSellableAllocation | None = None
        if not reject_reasons and q_max >= filters.min_qty:
            try:
                protectability_alloc = validate_pre_buy_protectability(
                    planned_buy_price=p_entry,
                    planned_buy_qty=q_max,
                    worst_case_base_fee_rate=self.buy_fee_rate,
                    stop_loss_pct=self.risk_config.stop_loss_trigger_pct,
                    notional_safety_buffer_pct=self.notional_safety_buffer_pct,
                    filters=filters,
                )
            except ExchangeFilterError as exc:
                reject_reasons.append(f"UNPROTECTABLE_ORDER_SIZE:{exc}")

        elapsed_ns = monotonic_ns() - t0_ns

        if reject_reasons:
            verdict = RiskVerdict(
                risk_check_id=f"RV-{uuid.uuid4().hex[:12]}",
                signal_id=proposal.signal_id,
                allowed=False,
                max_qty=Decimal("0"),
                max_notional=Decimal("0"),
                risk_config_version=self.risk_config.version,
                reject_reasons=tuple(reject_reasons),
                evaluated_at=ref_now,
            )
            return R01EvaluationResult(
                verdict=verdict,
                recommended_state_override=state_override,
                rolling_24h_drawdown_pct=dd_24h,
                q_risk_raw=q_risk_raw,
                q_cap_raw=q_cap_raw,
                q_free_raw=q_free_raw,
                final_qty=Decimal("0"),
                protectability=None,
                decision_latency_ns=elapsed_ns,
            )

        max_notional = (q_max * p_entry).quantize(Decimal("0.0001"))
        verdict = RiskVerdict(
            risk_check_id=f"RV-{uuid.uuid4().hex[:12]}",
            signal_id=proposal.signal_id,
            allowed=True,
            max_qty=q_max,
            max_notional=max_notional,
            risk_config_version=self.risk_config.version,
            reject_reasons=(),
            evaluated_at=ref_now,
        )
        return R01EvaluationResult(
            verdict=verdict,
            recommended_state_override=None,
            rolling_24h_drawdown_pct=dd_24h,
            q_risk_raw=q_risk_raw,
            q_cap_raw=q_cap_raw,
            q_free_raw=q_free_raw,
            final_qty=q_max,
            protectability=protectability_alloc,
            decision_latency_ns=elapsed_ns,
        )

    def assert_order_intent_invariants(
        self,
        *,
        intent: OrderIntent,
        verdict: RiskVerdict,
        account_equity_usdt: Decimal,
        rolling_24h_drawdown_pct: Decimal,
        filters: SymbolFilters,
    ) -> None:
        """Runtime invariant checker enforcing INV-R1 through INV-R5 and INV-R8 before order submission."""
        # INV-R1: Zero live order execution in P4
        if self.execution_mode != ExecutionMode.PAPER:
            raise PermissionError("INV-R1 violated: execution_mode must be strictly ExecutionMode.PAPER")
        if not intent.client_order_id.startswith("sui_p4_paper_"):
            raise ValueError(
                f"INV-R1 violated: clientOrderId '{intent.client_order_id}' lacks 'sui_p4_paper_' prefix"
            )

        # INV-R2: Symbol uniqueness
        if intent.symbol != TRADING_SYMBOL:
            raise ValueError(f"INV-R2 violated: OrderIntent symbol must be '{TRADING_SYMBOL}'")

        # INV-R3: RiskVerdict mandatory binding
        if not verdict.allowed or intent.risk_check_id != verdict.risk_check_id:
            raise PermissionError(
                "INV-R3 violated: OrderIntent must bind to an approved RiskVerdict(allowed=True)"
            )
        if intent.qty > verdict.max_qty:
            raise ValueError(
                f"INV-R3/R4 violated: OrderIntent qty ({intent.qty}) exceeds RiskVerdict max_qty ({verdict.max_qty})"
            )

        # INV-R4: Single-trade risk (<= 0.25% equity) and position cap (<= 10% equity)
        unit_risk_pct = self.risk_config.stop_loss_trigger_pct + self.bilateral_cost_and_slippage_buffer
        trade_risk_usdt = intent.qty * intent.price * unit_risk_pct
        max_risk_usdt = account_equity_usdt * self.risk_config.max_single_trade_risk_pct
        if trade_risk_usdt > max_risk_usdt + Decimal("0.0001"):
            raise ValueError(
                f"INV-R4 violated: trade risk {trade_risk_usdt} exceeds 0.25% equity cap {max_risk_usdt}"
            )
        notional = intent.qty * intent.price
        max_alloc_usdt = account_equity_usdt * self.risk_config.max_capital_allocation_pct
        if notional > max_alloc_usdt + Decimal("0.0001"):
            raise ValueError(
                f"INV-R4 violated: order notional {notional} exceeds 10% equity cap {max_alloc_usdt}"
            )

        # INV-R5: Rolling 24h loss circuit breaker
        if rolling_24h_drawdown_pct >= self.risk_config.max_rolling_24h_loss_pct:
            raise PermissionError(
                f"INV-R5 violated: rolling 24h drawdown {rolling_24h_drawdown_pct} >= {self.risk_config.max_rolling_24h_loss_pct}"
            )

        # INV-R8: Price and quantity exchange compliance and price_cap
        filters.validate_order(price=intent.price, qty=intent.qty)
        if intent.side == OrderSide.BUY and intent.price > intent.price_cap:
            raise ValueError(
                f"INV-R8 violated: BUY price ({intent.price}) exceeds price_cap ({intent.price_cap})"
            )


# Alias for backward/semantic convenience
R01RiskEvaluator = R01PreTradeRiskGate
