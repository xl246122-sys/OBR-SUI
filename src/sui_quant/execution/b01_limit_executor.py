"""B-01 Microstructure Limit Order Optimizer, Fill Probability & Markout Evaluator (§5, §14.2, QA-03, QA-04).

Key mechanisms:
1. Hard price cap (`price_cap`): Bounds limit buy price to `floor_tick(p_ref * (1 + eta_max))` so entry price
   slippage never erodes the A-02 net expected value.
2. Four microstructure quote tiers (`PASSIVE_BID`, `STEP_UP_BID`, `MID_FLOOR`, `AGGRESSIVE_CAP`).
3. Purely causal 10-level orderbook features: queue ahead depth ratio `q_ratio`, `OBI_1/5/10`, spread bps,
   and tick distance from best ask `d_ticks`.
4. Multi-horizon adverse selection `markout(1s, 5s, 30s, 60s)` estimation and net utility maximization:
   `U(delta) = P(fill | delta, TTL) * (EV_net(p(delta)) - AS_5s(delta))`.
5. Orderbook staleness gate (`<= 500 ms` monotonic lag) and `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION`
   window rejection via `StreamQualityGuard.is_window_valid_for_b01_calibration`.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Mapping, Sequence

from sui_quant.config import ExecutionMode
from sui_quant.contracts import (
    TRADING_SYMBOL,
    BreakoutProposal,
    OrderIntent,
    OrderSide,
    RiskVerdict,
    TimeInForce,
    require_decimal,
)
from sui_quant.data.quality_guard import PartialOrderbookSnapshot, StreamQualityGuard
from sui_quant.exchange_rules import P0_SNAPSHOT_SUIUSDT_FILTERS, SymbolFilters
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


class QuoteTier(str, Enum):
    PASSIVE_BID = "PASSIVE_BID"
    STEP_UP_BID = "STEP_UP_BID"
    MID_FLOOR = "MID_FLOOR"
    AGGRESSIVE_CAP = "AGGRESSIVE_CAP"


@dataclass(frozen=True)
class MarkoutProfile:
    """Post-fill adverse selection markout profile across 1s, 5s, 30s, and 60s horizons (§5.2)."""

    markout_1s: Decimal
    markout_5s: Decimal
    markout_30s: Decimal
    markout_60s: Decimal
    adverse_selection_5s_penalty: Decimal

    def __post_init__(self) -> None:
        for name, val in (
            ("markout_1s", self.markout_1s),
            ("markout_5s", self.markout_5s),
            ("markout_30s", self.markout_30s),
            ("markout_60s", self.markout_60s),
            ("adverse_selection_5s_penalty", self.adverse_selection_5s_penalty),
        ):
            require_decimal(val, name)


@dataclass(frozen=True)
class CandidateQuoteEvaluation:
    """Microstructure evaluation for a single candidate limit price tier delta."""

    tier: QuoteTier
    price: Decimal
    d_ticks: Decimal
    queue_ahead_qty: Decimal
    q_ratio: Decimal
    obi_1: Decimal
    obi_5: Decimal
    obi_10: Decimal
    spread_bps: Decimal
    fill_probability: Decimal
    adjusted_ev_net: Decimal
    markout_profile: MarkoutProfile
    expected_net_utility: Decimal


@dataclass(frozen=True)
class B01OptimizationResult:
    """Output of B-01 limit price optimization over the 10-level orderbook."""

    selected_intent: OrderIntent | None
    selected_candidate: CandidateQuoteEvaluation | None
    all_candidates: tuple[CandidateQuoteEvaluation, ...]
    price_cap: Decimal
    orderbook_age_ms: Decimal
    rejected_reason: str | None
    decision_latency_ns: int


@dataclass(frozen=True)
class CalibrationWindowSample:
    """Historical orderbook replay window for B-01 fill probability and markout calibration."""

    window_id: str
    symbol: str
    window_start_utc: datetime
    window_end_utc: datetime
    d_ticks: Decimal
    q_ratio: Decimal
    obi_5: Decimal
    ttl_seconds: Decimal
    did_fill: bool
    fill_price: Decimal | None = None
    post_fill_mids: Mapping[int, Decimal] | None = None

    def __post_init__(self) -> None:
        ensure_utc(self.window_start_utc, "window_start_utc")
        ensure_utc(self.window_end_utc, "window_end_utc")


@dataclass(frozen=True)
class B01CalibrationReport:
    """Summary of B-01 calibration showing strict rejection of unrecoverable gap windows."""

    total_windows_submitted: int
    accepted_windows: int
    rejected_gap_windows: int
    rejected_window_ids: tuple[str, ...]
    empirical_fill_rate: Decimal
    mean_adverse_selection_5s: Decimal


def compute_deterministic_p4_client_order_id(
    *,
    mode: ExecutionMode,
    signal_bar_close_ms: int,
    intent_seq: int = 1,
    leg: str = "buy",
) -> str:
    """Generate deterministic idempotent clientOrderId per P4_PLAN §3.4:

    Format: `sui_p4_{mode}_{signal_bar_close_ms}_{intent_seq:04d}` (or with leg suffix for stop/exit).
    Strictly <= 36 chars and alphanumeric/underscore.
    """
    if intent_seq < 1 or intent_seq > 9999:
        raise ValueError("intent_seq must be in [1, 9999]")
    mode_tag = mode.value.lower()[:5]
    if leg == "buy":
        cid = f"sui_p4_{mode_tag}_{signal_bar_close_ms}_{intent_seq:04d}"
    else:
        clean_leg = leg.lower()[:2]
        cid = f"sui_p4_{mode_tag}_{clean_leg}_{signal_bar_close_ms}_{intent_seq:02d}"
    if len(cid) > 36:
        cid = cid[:36]
    return cid


def compute_multi_horizon_markout(
    fill_price: Decimal,
    future_mid_prices_sec: Mapping[int, Decimal],
) -> MarkoutProfile:
    """Compute realized markout(1s, 5s, 30s, 60s) = (mid(t_fill + tau) - p_fill) / p_fill (§3.1.3)."""
    p_fill = require_decimal(fill_price, "fill_price")
    if p_fill <= Decimal("0"):
        raise ValueError("fill_price must be > 0")

    def _calc(horizon_sec: int) -> Decimal:
        mid_tau = future_mid_prices_sec.get(horizon_sec, p_fill)
        m = require_decimal(mid_tau, f"mid_{horizon_sec}s")
        return ((m - p_fill) / p_fill).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    m1 = _calc(1)
    m5 = _calc(5)
    m30 = _calc(30)
    m60 = _calc(60)
    as_5s = max(Decimal("0"), -m5)
    return MarkoutProfile(
        markout_1s=m1,
        markout_5s=m5,
        markout_30s=m30,
        markout_60s=m60,
        adverse_selection_5s_penalty=as_5s,
    )


def _compute_obi_k(
    bids: Sequence[tuple[Decimal, Decimal]],
    asks: Sequence[tuple[Decimal, Decimal]],
    k: int,
) -> Decimal:
    b_sum = sum((q for _, q in bids[:k]), Decimal("0"))
    a_sum = sum((q for _, q in asks[:k]), Decimal("0"))
    tot = b_sum + a_sum
    if tot <= Decimal("0"):
        return Decimal("0")
    return ((b_sum - a_sum) / tot).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


class B01LimitOrderOptimizer:
    """B-01 Smart Limit Order & Microstructure Execution Optimizer (§5, §3.1)."""

    def __init__(
        self,
        *,
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
        execution_mode: ExecutionMode = ExecutionMode.PAPER,
        entry_slippage_budget: Decimal = Decimal("0.0005"),
        cap_by_half_ev: bool = True,
        default_ttl_ms: int = 15_000,
        max_orderbook_age_ms: int = 500,
        avg_15s_trade_volume_sui: Decimal = Decimal("250.0"),
        beta_0: Decimal = Decimal("0.85"),
        beta_d: Decimal = Decimal("0.55"),
        beta_q: Decimal = Decimal("1.10"),
        beta_obi: Decimal = Decimal("0.40"),
        beta_t: Decimal = Decimal("0.35"),
        base_adverse_selection_5s: Decimal = Decimal("0.0004"),
    ) -> None:
        if default_ttl_ms < 5_000 or default_ttl_ms > 30_000:
            raise ValueError("default_ttl_ms must be within [5000, 30000] ms (§3.1.2)")
        if max_orderbook_age_ms <= 0:
            raise ValueError("max_orderbook_age_ms must be > 0")
        self.filters = filters
        self.execution_mode = execution_mode
        self.entry_slippage_budget = require_decimal(entry_slippage_budget, "entry_slippage_budget")
        self.cap_by_half_ev = cap_by_half_ev
        self.default_ttl_ms = default_ttl_ms
        self.max_orderbook_age_ms = max_orderbook_age_ms
        self.avg_15s_trade_volume_sui = require_decimal(
            avg_15s_trade_volume_sui, "avg_15s_trade_volume_sui"
        )
        self.beta_0 = require_decimal(beta_0, "beta_0")
        self.beta_d = require_decimal(beta_d, "beta_d")
        self.beta_q = require_decimal(beta_q, "beta_q")
        self.beta_obi = require_decimal(beta_obi, "beta_obi")
        self.beta_t = require_decimal(beta_t, "beta_t")
        self.base_adverse_selection_5s = require_decimal(
            base_adverse_selection_5s, "base_adverse_selection_5s"
        )

    def compute_price_cap(
        self,
        reference_price: Decimal,
        ev_net: Decimal,
    ) -> Decimal:
        """Compute hard limit price ceiling `price_cap`:

        eta_max = min(entry_slippage_budget, max(0, ev_net / 2)) if cap_by_half_ev else entry_slippage_budget
        price_cap = floor(reference_price * (1 + eta_max) / tickSize) * tickSize
        """
        p_ref = require_decimal(reference_price, "reference_price")
        ev = require_decimal(ev_net, "ev_net")
        if p_ref <= Decimal("0"):
            raise ValueError("reference_price must be > 0")
        if self.cap_by_half_ev:
            half_ev = max(Decimal("0"), ev / Decimal("2"))
            eta_max = min(self.entry_slippage_budget, half_ev)
        else:
            eta_max = self.entry_slippage_budget
        raw_cap = p_ref * (Decimal("1") + eta_max)
        return self.filters.quantize_price_down(raw_cap)

    def estimate_fill_probability(
        self,
        *,
        d_ticks: Decimal,
        q_ratio: Decimal,
        obi_5: Decimal,
        ttl_seconds: Decimal,
        crosses_ask_with_sufficient_depth: bool = False,
    ) -> Decimal:
        """Evaluate monotonic logistic fill probability P(fill | delta, Delta_t) (§3.1.2)."""
        if crosses_ask_with_sufficient_depth and d_ticks <= Decimal("0"):
            return Decimal("0.980000")
        logit = (
            float(self.beta_0)
            - float(self.beta_d) * float(d_ticks)
            - float(self.beta_q) * float(q_ratio)
            - float(self.beta_obi) * float(obi_5)
            + float(self.beta_t) * math.log(1.0 + max(0.0, float(ttl_seconds)))
        )
        logit = max(-20.0, min(20.0, logit))
        prob = 1.0 / (1.0 + math.exp(-logit))
        return Decimal(f"{prob:.6f}")

    def estimate_markout_profile(
        self,
        *,
        tier: QuoteTier,
        obi_5: Decimal,
        spread_bps: Decimal,
    ) -> MarkoutProfile:
        """Estimate expected post-fill markout(1s, 5s, 30s, 60s) and 5s adverse selection penalty (§3.1.3).

        Passive fills on the bid experience stronger adverse selection when sell pressure dominates (`obi_5 < 0`).
        """
        tier_multiplier = {
            QuoteTier.PASSIVE_BID: Decimal("1.25"),
            QuoteTier.STEP_UP_BID: Decimal("1.00"),
            QuoteTier.MID_FLOOR: Decimal("0.80"),
            QuoteTier.AGGRESSIVE_CAP: Decimal("0.55"),
        }[tier]
        imbalance_stress = max(Decimal("0.30"), Decimal("1.0") - Decimal("0.60") * obi_5)
        spread_factor = Decimal("1.0") + min(Decimal("0.50"), spread_bps / Decimal("100.0"))
        as_5s = (
            self.base_adverse_selection_5s * tier_multiplier * imbalance_stress * spread_factor
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

        m1 = (-as_5s * Decimal("0.65")).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        m5 = (-as_5s).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        m30 = (-as_5s * Decimal("0.85") + Decimal("0.000150") * obi_5).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        m60 = (-as_5s * Decimal("0.70") + Decimal("0.000300") * obi_5).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        return MarkoutProfile(
            markout_1s=m1,
            markout_5s=m5,
            markout_30s=m30,
            markout_60s=m60,
            adverse_selection_5s_penalty=as_5s,
        )

    def optimize_limit_buy(
        self,
        *,
        proposal: BreakoutProposal,
        risk_verdict: RiskVerdict,
        orderbook: PartialOrderbookSnapshot,
        reference_price: Decimal | None = None,
        quality_guard: StreamQualityGuard | None = None,
        intent_seq: int = 1,
        ttl_ms: int | None = None,
        current_monotonic_ns: int | None = None,
        now_utc: datetime | None = None,
    ) -> B01OptimizationResult:
        """Select the utility-maximizing limit buy quote `delta*` subject to `p(delta) <= price_cap`."""
        t0_ns = monotonic_ns()
        ref_mono = current_monotonic_ns if current_monotonic_ns is not None else t0_ns
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        p_ref = require_decimal(
            reference_price if reference_price is not None else proposal.barrier_ref,
            "reference_price",
        )
        price_cap = self.compute_price_cap(reference_price=p_ref, ev_net=proposal.ev_net)

        # 1. Verify RiskVerdict is approved (INV-R3)
        if not risk_verdict.allowed or risk_verdict.max_qty < self.filters.min_qty:
            return B01OptimizationResult(
                selected_intent=None,
                selected_candidate=None,
                all_candidates=(),
                price_cap=price_cap,
                orderbook_age_ms=Decimal("0"),
                rejected_reason="RISK_VERDICT_NOT_ALLOWED",
                decision_latency_ns=monotonic_ns() - t0_ns,
            )

        # 2. Check orderbook symbol and non-empty levels
        if orderbook.symbol != TRADING_SYMBOL or not orderbook.bids or not orderbook.asks:
            return B01OptimizationResult(
                selected_intent=None,
                selected_candidate=None,
                all_candidates=(),
                price_cap=price_cap,
                orderbook_age_ms=Decimal("0"),
                rejected_reason="EMPTY_OR_INVALID_ORDERBOOK",
                decision_latency_ns=monotonic_ns() - t0_ns,
            )

        # 3. Orderbook monotonic staleness gate (<= 500 ms)
        ob_lag_ns = max(0, ref_mono - orderbook.recv_monotonic_ns)
        ob_age_ms = (Decimal(ob_lag_ns) / Decimal("1000000")).quantize(Decimal("0.001"))
        if ob_age_ms > Decimal(self.max_orderbook_age_ms):
            return B01OptimizationResult(
                selected_intent=None,
                selected_candidate=None,
                all_candidates=(),
                price_cap=price_cap,
                orderbook_age_ms=ob_age_ms,
                rejected_reason="STALE_ORDERBOOK",
                decision_latency_ns=monotonic_ns() - t0_ns,
            )

        # 4. Check StreamQualityGuard for active unrecoverable gap window
        if quality_guard is not None:
            window_ok = quality_guard.is_window_valid_for_b01_calibration(
                symbol=TRADING_SYMBOL,
                window_start_utc=orderbook.exchange_time_utc - timedelta(seconds=5),
                window_end_utc=ref_now,
            )
            if not window_ok:
                return B01OptimizationResult(
                    selected_intent=None,
                    selected_candidate=None,
                    all_candidates=(),
                    price_cap=price_cap,
                    orderbook_age_ms=ob_age_ms,
                    rejected_reason="ORDERBOOK_GAP_UNRECOVERABLE_WINDOW",
                    decision_latency_ns=monotonic_ns() - t0_ns,
                )

        best_bid = orderbook.bids[0][0]
        best_ask = orderbook.asks[0][0]
        if best_ask <= best_bid:
            return B01OptimizationResult(
                selected_intent=None,
                selected_candidate=None,
                all_candidates=(),
                price_cap=price_cap,
                orderbook_age_ms=ob_age_ms,
                rejected_reason="CROSSED_ORDERBOOK",
                decision_latency_ns=monotonic_ns() - t0_ns,
            )

        # 5. Construct candidate quote grid P_cand (§3.1.1)
        tick = self.filters.tick_size
        raw_tiers: list[tuple[QuoteTier, Decimal]] = []

        # delta_0: PASSIVE_BID
        p0 = min(best_bid, price_cap)
        raw_tiers.append((QuoteTier.PASSIVE_BID, self.filters.quantize_price_down(p0)))

        # delta_1: STEP_UP_BID (only when spread >= 2 * tickSize)
        if (best_ask - best_bid) >= (Decimal("2") * tick):
            p1 = min(best_bid + tick, price_cap)
            raw_tiers.append((QuoteTier.STEP_UP_BID, self.filters.quantize_price_down(p1)))

        # delta_2: MID_FLOOR
        mid_floor = self.filters.quantize_price_down((best_bid + best_ask) / Decimal("2"))
        p2 = min(mid_floor, price_cap)
        raw_tiers.append((QuoteTier.MID_FLOOR, self.filters.quantize_price_down(p2)))

        # delta_3: AGGRESSIVE_CAP (only when best_ask <= price_cap)
        if best_ask <= price_cap:
            p3 = min(best_ask, price_cap)
            raw_tiers.append((QuoteTier.AGGRESSIVE_CAP, self.filters.quantize_price_down(p3)))

        # Compute microstructure features shared across tiers
        obi_1 = _compute_obi_k(orderbook.bids, orderbook.asks, 1)
        obi_5 = _compute_obi_k(orderbook.bids, orderbook.asks, 5)
        obi_10 = _compute_obi_k(orderbook.bids, orderbook.asks, 10)
        spread_bps = orderbook.spread_bps or Decimal("0")
        order_qty = risk_verdict.max_qty
        effective_ttl_ms = ttl_ms if ttl_ms is not None else self.default_ttl_ms
        ttl_sec = Decimal(effective_ttl_ms) / Decimal("1000")

        evaluations: list[CandidateQuoteEvaluation] = []
        for tier, cand_price in raw_tiers:
            if cand_price <= Decimal("0") or cand_price > price_cap:
                continue
            d_ticks = max(Decimal("0"), (best_ask - cand_price) / tick)
            if cand_price >= best_ask:
                q_ahead = Decimal("0")
            else:
                q_ahead = sum((q for p, q in orderbook.bids if p >= cand_price), Decimal("0"))
            denom = order_qty + self.avg_15s_trade_volume_sui
            q_ratio = (q_ahead / denom).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

            crosses_ask = cand_price >= best_ask and orderbook.asks[0][1] >= order_qty
            p_fill = self.estimate_fill_probability(
                d_ticks=d_ticks,
                q_ratio=q_ratio,
                obi_5=obi_5,
                ttl_seconds=ttl_sec,
                crosses_ask_with_sufficient_depth=crosses_ask,
            )
            price_concession = (cand_price - p_ref) / p_ref
            adj_ev = (proposal.ev_net - price_concession).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            markout_prof = self.estimate_markout_profile(
                tier=tier,
                obi_5=obi_5,
                spread_bps=spread_bps,
            )
            net_utility = (
                p_fill * (adj_ev - markout_prof.adverse_selection_5s_penalty)
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

            evaluations.append(
                CandidateQuoteEvaluation(
                    tier=tier,
                    price=cand_price,
                    d_ticks=d_ticks,
                    queue_ahead_qty=q_ahead,
                    q_ratio=q_ratio,
                    obi_1=obi_1,
                    obi_5=obi_5,
                    obi_10=obi_10,
                    spread_bps=spread_bps,
                    fill_probability=p_fill,
                    adjusted_ev_net=adj_ev,
                    markout_profile=markout_prof,
                    expected_net_utility=net_utility,
                )
            )

        if not evaluations:
            return B01OptimizationResult(
                selected_intent=None,
                selected_candidate=None,
                all_candidates=(),
                price_cap=price_cap,
                orderbook_age_ms=ob_age_ms,
                rejected_reason="NO_CANDIDATE_WITHIN_PRICE_CAP",
                decision_latency_ns=monotonic_ns() - t0_ns,
            )

        best_eval = max(evaluations, key=lambda c: (c.expected_net_utility, -c.price))
        if best_eval.expected_net_utility <= Decimal("0"):
            return B01OptimizationResult(
                selected_intent=None,
                selected_candidate=best_eval,
                all_candidates=tuple(evaluations),
                price_cap=price_cap,
                orderbook_age_ms=ob_age_ms,
                rejected_reason="NON_POSITIVE_EXPECTED_UTILITY",
                decision_latency_ns=monotonic_ns() - t0_ns,
            )

        intent_qty = min(
            risk_verdict.max_qty,
            self.filters.quantize_qty_down(risk_verdict.max_notional / best_eval.price),
        )
        if intent_qty < self.filters.min_qty:
            return B01OptimizationResult(
                selected_intent=None,
                selected_candidate=best_eval,
                all_candidates=tuple(evaluations),
                price_cap=price_cap,
                orderbook_age_ms=ob_age_ms,
                rejected_reason="QTY_BELOW_MIN_AFTER_NOTIONAL_CAP",
                decision_latency_ns=monotonic_ns() - t0_ns,
            )

        signal_bar_close_ms = int(proposal.candle_close_utc.timestamp() * 1000)
        client_order_id = compute_deterministic_p4_client_order_id(
            mode=self.execution_mode,
            signal_bar_close_ms=signal_bar_close_ms,
            intent_seq=intent_seq,
            leg="buy",
        )
        intent = OrderIntent(
            intent_id=f"INT-{uuid.uuid4().hex[:12]}",
            signal_id=proposal.signal_id,
            client_order_id=client_order_id,
            symbol=TRADING_SYMBOL,
            side=OrderSide.BUY,
            price=best_eval.price,
            qty=intent_qty,
            tif=TimeInForce.GTC,
            price_cap=price_cap,
            expiration=ref_now + timedelta(milliseconds=effective_ttl_ms),
            strategy_version=proposal.model_version,
            risk_check_id=risk_verdict.risk_check_id,
        )
        return B01OptimizationResult(
            selected_intent=intent,
            selected_candidate=best_eval,
            all_candidates=tuple(evaluations),
            price_cap=price_cap,
            orderbook_age_ms=ob_age_ms,
            rejected_reason=None,
            decision_latency_ns=monotonic_ns() - t0_ns,
        )

    def calibrate_from_orderbook_windows(
        self,
        *,
        windows: Sequence[CalibrationWindowSample],
        quality_guard: StreamQualityGuard,
    ) -> B01CalibrationReport:
        """Calibrate B-01 empirical fill rates and markout penalties while strictly rejecting any window
        that overlaps an `OrderbookGapMarker` (`GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION`, FI-P4-08).
        """
        accepted: list[CalibrationWindowSample] = []
        rejected_ids: list[str] = []

        for w in windows:
            is_valid = quality_guard.is_window_valid_for_b01_calibration(
                symbol=w.symbol,
                window_start_utc=w.window_start_utc,
                window_end_utc=w.window_end_utc,
            )
            if not is_valid:
                rejected_ids.append(w.window_id)
                continue
            accepted.append(w)

        if not accepted:
            return B01CalibrationReport(
                total_windows_submitted=len(windows),
                accepted_windows=0,
                rejected_gap_windows=len(rejected_ids),
                rejected_window_ids=tuple(rejected_ids),
                empirical_fill_rate=Decimal("0"),
                mean_adverse_selection_5s=self.base_adverse_selection_5s,
            )

        fills = [w for w in accepted if w.did_fill]
        emp_fill_rate = (Decimal(len(fills)) / Decimal(len(accepted))).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )

        as_penalties: list[Decimal] = []
        for w in fills:
            if w.fill_price is not None and w.post_fill_mids is not None:
                prof = compute_multi_horizon_markout(w.fill_price, w.post_fill_mids)
                as_penalties.append(prof.adverse_selection_5s_penalty)

        if as_penalties:
            mean_as = (sum(as_penalties, Decimal("0")) / Decimal(len(as_penalties))).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            self.base_adverse_selection_5s = max(Decimal("0.000100"), mean_as)
        else:
            mean_as = self.base_adverse_selection_5s

        return B01CalibrationReport(
            total_windows_submitted=len(windows),
            accepted_windows=len(accepted),
            rejected_gap_windows=len(rejected_ids),
            rejected_window_ids=tuple(rejected_ids),
            empirical_fill_rate=emp_fill_rate,
            mean_adverse_selection_5s=mean_as,
        )
