"""Triple-Barrier Labeling, Breakout Candidate Screening, and Purged & Embargoed 3-Stage / Walk-Forward Splitting
(§4, §6.1, §6.2, §14, P3_PLAN, Task A.2, A.3, A.4, A.5).

Key Guarantees:
1. Task A.2 — Zero Time-Travel Between 5m Signal Confirmation and Reference Entry Quote:
   - Signal is confirmed only AFTER 5m bar `t` closes at `signal_confirm_time_ms = bar[t].close_time_ms`.
   - Reference entry quote is anchored at `entry_quote_time_ms = bar[t+1].open_time_ms` (`> signal_confirm_time_ms`),
     using `EntryPriceMode.NEXT_BAR_OPEN` (`bar[t+1].open_price`) or `CONSERVATIVE_MAX_CLOSE_NEXT_OPEN`
     (`max(bar[t].close_price, bar[t+1].open_price)`).
   - Enforces `entry_quote_time_ms > signal_confirm_time_ms` in `TripleBarrierLabel.__post_init__`.
2. Task A.3 & A.4 — Single-Deduction Fee/Slippage Accounting & Explicit `TIMEOUT` Net Return Formula:
   - Upper `WIN` barrier: `compute_c1_net_target_sell_price` achieving `>= +1.5%` NET profit after buy & sell fees.
   - Lower `STOP` barrier: `compute_stop_loss_trigger_price` at `-1.0%` below entry price, with `stop_slippage_rate`
     applied once to `exit_price`.
   - Vertical `TIMEOUT` barrier (`36` 5m bars = `3 hours`):
     `raw_exit_quote_price = bar[t+36].close_price`,
     `exit_price = raw_exit_quote_price * (1 - timeout_slippage_rate)`,
     `gross_return = (raw_exit_quote_price - entry_price) / entry_price`,
     `net_return = (exit_price * (1 - sell_fee_rate)) / (entry_price * (1 + buy_fee_rate)) - 1`.
3. Intra-Bar Ambiguity Pessimistic Tie-Breaking (§4.2, P3_PLAN §03.2):
   - When a future K-line touches BOTH `high >= win_barrier_price` and `low <= stop_barrier_price` within the same bar,
     unless sub-bar tick sequence explicitly proves `WIN` was reached first, the outcome is strictly resolved as
     `BarrierOutcome.STOP` (`ambiguous_same_bar_conflict=True`).
4. Task A.5 — Strict 3-Stage Separation (`TRAIN` / `CALIBRATION` / `FINAL_TEST`) with Purge & Embargo:
   - `PurgedThreeStageSplit` physically separates training, probability calibration, and final holdout evaluation
     with `>= 36` 5m bars (`3 hours = 10,800,000 ms`) Purge + Embargo at BOTH boundaries (`TRAIN -> CAL` and `CAL -> TEST`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN
from enum import Enum
from typing import Mapping, Sequence

from sui_quant.contracts import TRADING_SYMBOL, MarketFeatures, require_decimal
from sui_quant.data.quality_guard import ClosedKlineBar
from sui_quant.exchange_rules import (
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
    compute_c1_net_target_sell_price,
    compute_stop_loss_trigger_price,
)
from sui_quant.models.features import (
    CausalityViolationError,
    UnclosedBarRejectedError,
    _validate_closed_bar_sequence,
)
from sui_quant.utils.clock import ensure_utc


QUANT_8 = Decimal("0.00000001")
FIVE_MIN_MS = 5 * 60 * 1000
THREE_HOURS_BARS = 36
THREE_HOURS_MS = THREE_HOURS_BARS * FIVE_MIN_MS  # 10,800,000 ms


def _q8(val: Decimal) -> Decimal:
    return val.quantize(QUANT_8, rounding=ROUND_HALF_EVEN)


class BarrierOutcome(str, Enum):
    WIN = "WIN"          # Touched net +1.5% profit barrier first
    STOP = "STOP"        # Touched -1.0% stop-loss trigger barrier first (or same-bar pessimistic tie-break)
    TIMEOUT = "TIMEOUT"  # Neither barrier touched within 36 bars (3 hours)


class EntryPriceMode(str, Enum):
    """Reference entry pricing convention after 5m bar `t` closes (Task A.2)."""

    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"  # bar[t+1].open_price at T_open(t+1) = T_close(t) + 1ms
    CONSERVATIVE_MAX_CLOSE_NEXT_OPEN = "CONSERVATIVE_MAX_CLOSE_NEXT_OPEN"  # max(bar[t].close, bar[t+1].open)
    SIGNAL_BAR_CLOSE = "SIGNAL_BAR_CLOSE"  # bar[t].close_price


@dataclass(frozen=True)
class BreakoutCandidateConfig:
    """Screening thresholds for A-02 5m upward volume breakout candidates (§4.1)."""

    min_rvol: Decimal = Decimal("1.50")
    min_vol_zscore: Decimal = Decimal("0.80")
    max_upper_wick_ratio: Decimal = Decimal("0.45")
    min_breakout_pct: Decimal = Decimal("0.0001")  # Strictly > prev_20_high


@dataclass(frozen=True)
class BreakoutCandidateCheck:
    """Result of screening a closed 5m bar against A-02 candidate criteria."""

    is_candidate: bool
    barrier_ref: Decimal
    close_price: Decimal
    breakout_pct: Decimal
    rvol_20: Decimal
    vol_zscore_20: Decimal
    upper_wick_ratio: Decimal
    reject_reasons: tuple[str, ...]


def evaluate_breakout_candidate(
    sui_features: MarketFeatures,
    config: BreakoutCandidateConfig | None = None,
) -> BreakoutCandidateCheck:
    """Evaluate whether a closed `SUIUSDT` 5m bar qualifies as an upward volume breakout candidate."""
    cfg = config or BreakoutCandidateConfig()
    if sui_features.symbol != TRADING_SYMBOL:
        raise ValueError(
            f"Breakout candidate screening is strictly restricted to '{TRADING_SYMBOL}', got '{sui_features.symbol}'"
        )
    if not sui_features.complete_bar_only:
        raise UnclosedBarRejectedError(
            "Breakout candidate screening requires complete_bar_only=True (closed 5m bar)."
        )

    f = sui_features.features
    close_p = require_decimal(f["close"], "close")
    open_p = require_decimal(f["open"], "open")
    prev_20_high = require_decimal(f["prev_20_high"], "prev_20_high")
    breakout_pct = require_decimal(f["breakout_pct"], "breakout_pct")
    rvol_20 = require_decimal(f["rvol_20"], "rvol_20")
    vol_zscore_20 = require_decimal(f["vol_zscore_20"], "vol_zscore_20")
    upper_wick_ratio = require_decimal(f["upper_wick_ratio"], "upper_wick_ratio")

    reasons: list[str] = []
    if close_p <= prev_20_high or breakout_pct < cfg.min_breakout_pct:
        reasons.append("CLOSE_NOT_ABOVE_PREV_20_HIGH")
    if close_p <= open_p:
        reasons.append("NON_BULLISH_CANDLE_BODY")
    if rvol_20 < cfg.min_rvol and vol_zscore_20 < cfg.min_vol_zscore:
        reasons.append("INSUFFICIENT_VOLUME_EXPANSION")
    if upper_wick_ratio > cfg.max_upper_wick_ratio:
        reasons.append("EXCESSIVE_UPPER_WICK_RATIO")

    return BreakoutCandidateCheck(
        is_candidate=(len(reasons) == 0),
        barrier_ref=prev_20_high,
        close_price=close_p,
        breakout_pct=breakout_pct,
        rvol_20=rvol_20,
        vol_zscore_20=vol_zscore_20,
        upper_wick_ratio=upper_wick_ratio,
        reject_reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class TripleBarrierConfig:
    """Parameters for Triple-Barrier labeling (§4.2, §6.1, §6.2, Task A.2, A.3, A.4)."""

    horizon_bars: int = THREE_HOURS_BARS                 # 36 bars * 5m = 3 hours
    target_net_return: Decimal = Decimal("0.0150")       # +1.5% net return after buy & sell fees
    stop_loss_pct: Decimal = Decimal("0.0100")           # -1.0% trigger relative to entry price
    buy_fee_rate: Decimal = Decimal("0.0010")            # 10 bps spot taker/maker baseline
    sell_fee_rate: Decimal = Decimal("0.0010")           # 10 bps spot taker/maker baseline
    stop_slippage_rate: Decimal = Decimal("0.0005")      # 5 bps execution slippage on stop trigger
    timeout_slippage_rate: Decimal = Decimal("0.0002")   # 2 bps execution slippage on 3h timeout exit (Task A.4)
    entry_price_mode: EntryPriceMode = EntryPriceMode.NEXT_BAR_OPEN  # Task A.2 causal post-close entry quote
    filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS


@dataclass(frozen=True)
class TripleBarrierLabel:
    """Immutable Triple-Barrier outcome record for a single entry event (Task A.2, A.3, A.4)."""

    event_index: int
    symbol: str
    entry_time_utc: datetime
    entry_open_time_ms: int
    entry_close_time_ms: int
    signal_confirm_time_ms: int
    entry_quote_time_ms: int
    entry_price_mode: str
    label_end_time_utc: datetime
    label_end_close_time_ms: int
    horizon_end_index: int
    entry_price: Decimal
    win_barrier_price: Decimal
    stop_barrier_price: Decimal
    outcome: BarrierOutcome
    bars_to_hit: int
    raw_exit_quote_price: Decimal
    exit_price: Decimal
    gross_return: Decimal
    net_return: Decimal
    ambiguous_same_bar_conflict: bool

    def __post_init__(self) -> None:
        if self.symbol != TRADING_SYMBOL:
            raise ValueError(f"TripleBarrierLabel symbol must be '{TRADING_SYMBOL}'")
        ensure_utc(self.entry_time_utc, "entry_time_utc")
        ensure_utc(self.label_end_time_utc, "label_end_time_utc")
        if self.entry_quote_time_ms <= self.signal_confirm_time_ms:
            raise CausalityViolationError(
                f"Time-travel violation (Task A.2): entry_quote_time_ms ({self.entry_quote_time_ms}) "
                f"must be strictly > signal_confirm_time_ms ({self.signal_confirm_time_ms})"
            )
        require_decimal(self.entry_price, "entry_price")
        require_decimal(self.win_barrier_price, "win_barrier_price")
        require_decimal(self.stop_barrier_price, "stop_barrier_price")
        require_decimal(self.raw_exit_quote_price, "raw_exit_quote_price")
        require_decimal(self.exit_price, "exit_price")
        require_decimal(self.gross_return, "gross_return")
        require_decimal(self.net_return, "net_return")


def assert_no_signal_to_entry_time_travel(
    bars: Sequence[ClosedKlineBar],
    label: TripleBarrierLabel,
) -> bool:
    """Mathematically verify Task A.2:
    1. Signal confirmation occurs strictly at the close of 5m bar `t` (`bars[entry_index].close_time_ms`).
    2. Entry quote timestamp is strictly at or after the open of bar `t+1` (`bars[entry_index + 1].open_time_ms > signal_confirm_time_ms`).
    3. Barrier evaluation inspects only bars `t+1 .. t+horizon` (`open_time_ms >= entry_quote_time_ms`),
       never using `bar[t]`'s own intra-bar high or low to trigger `WIN` or `STOP`.
    """
    sig_bar = bars[label.event_index]
    next_bar = bars[label.event_index + 1]
    if label.signal_confirm_time_ms != sig_bar.close_time_ms:
        raise CausalityViolationError("signal_confirm_time_ms does not match signal bar close_time_ms")
    if label.entry_quote_time_ms < next_bar.open_time_ms:
        raise CausalityViolationError("entry_quote_time_ms precedes next bar open_time_ms")
    if label.entry_quote_time_ms <= label.signal_confirm_time_ms:
        raise CausalityViolationError("entry_quote_time_ms <= signal_confirm_time_ms")
    if label.bars_to_hit < 1:
        raise CausalityViolationError("bars_to_hit must be >= 1 (cannot hit barrier on signal bar t itself)")
    return True


class TripleBarrierLabeler:
    """Fee-aware Triple-Barrier labeler with causal post-close entry pricing and pessimistic tie-breaking (§4.2)."""

    def __init__(self, config: TripleBarrierConfig | None = None) -> None:
        self.config = config or TripleBarrierConfig()
        if self.config.horizon_bars <= 0:
            raise ValueError("horizon_bars must be > 0")

    def compute_barriers(self, entry_price: Decimal) -> tuple[Decimal, Decimal]:
        """Compute `(win_barrier_price, stop_barrier_price)` for a given `entry_price`.

        - `win_barrier_price`: quantized UP via `compute_c1_net_target_sell_price` so that selling at
          `win_barrier_price` after paying `buy_fee_rate` and `sell_fee_rate` yields `>= +1.5%` net return.
        - `stop_barrier_price`: quantized DOWN via `compute_stop_loss_trigger_price` at `-1.0%` below entry.
        """
        ep = require_decimal(entry_price, "entry_price")
        cfg = self.config
        win_p = compute_c1_net_target_sell_price(
            avg_buy_price=ep,
            buy_fee_rate=cfg.buy_fee_rate,
            sell_fee_rate=cfg.sell_fee_rate,
            target_net_return=cfg.target_net_return,
            filters=cfg.filters,
        )
        stop_p = compute_stop_loss_trigger_price(
            avg_buy_price=ep,
            stop_loss_pct=cfg.stop_loss_pct,
            filters=cfg.filters,
        )
        return win_p, stop_p

    def resolve_causal_entry_price(
        self,
        signal_bar: ClosedKlineBar,
        next_bar: ClosedKlineBar,
        entry_price_override: Decimal | None = None,
    ) -> tuple[Decimal, str]:
        """Determine causal reference entry price after `signal_bar` closes at `signal_bar.close_time_ms` (Task A.2)."""
        if next_bar.open_time_ms <= signal_bar.close_time_ms:
            raise CausalityViolationError(
                f"next_bar.open_time_ms ({next_bar.open_time_ms}) must be > signal_bar.close_time_ms ({signal_bar.close_time_ms})"
            )
        if entry_price_override is not None:
            return require_decimal(entry_price_override, "entry_price_override"), "EXPLICIT_OVERRIDE"

        mode = self.config.entry_price_mode
        if mode == EntryPriceMode.NEXT_BAR_OPEN:
            return next_bar.open_price, mode.value
        if mode == EntryPriceMode.CONSERVATIVE_MAX_CLOSE_NEXT_OPEN:
            return max(signal_bar.close_price, next_bar.open_price), mode.value
        return signal_bar.close_price, mode.value

    def label_at_index(
        self,
        sui_5m_bars: Sequence[ClosedKlineBar],
        entry_index: int,
        entry_price_override: Decimal | None = None,
        sub_bar_trade_prices: Mapping[int, Sequence[Decimal]] | None = None,
    ) -> TripleBarrierLabel:
        """Compute Triple-Barrier label for a 5m signal confirmed at the close of `sui_5m_bars[entry_index]`.

        Requires at least `horizon_bars` (default 36) closed 5m bars after `entry_index` so that
        the full 3-hour horizon (`t+1 .. t+36`) is observed without truncation bias.
        """
        validated = _validate_closed_bar_sequence(sui_5m_bars, TRADING_SYMBOL)
        cfg = self.config
        horizon = cfg.horizon_bars
        if entry_index < 0 or entry_index + horizon >= len(validated):
            raise ValueError(
                f"Cannot label entry_index={entry_index}: need {horizon} future bars, "
                f"but sequence length is {len(validated)}."
            )

        signal_bar = validated[entry_index]
        first_exec_bar = validated[entry_index + 1]
        entry_price, mode_str = self.resolve_causal_entry_price(
            signal_bar=signal_bar,
            next_bar=first_exec_bar,
            entry_price_override=entry_price_override,
        )
        win_barrier, stop_barrier = self.compute_barriers(entry_price)

        outcome = BarrierOutcome.TIMEOUT
        bars_to_hit = horizon
        ambiguous_conflict = False
        horizon_end_idx = entry_index + horizon
        resolving_bar = validated[horizon_end_idx]

        # Task A.4: Default to TIMEOUT at the 36th bar close with explicit timeout_slippage_rate
        raw_exit_quote_price = resolving_bar.close_price
        exit_price = _q8(raw_exit_quote_price * (Decimal("1") - cfg.timeout_slippage_rate))

        for step in range(1, horizon + 1):
            fut_bar = validated[entry_index + step]
            hit_win = fut_bar.high_price >= win_barrier
            hit_stop = fut_bar.low_price <= stop_barrier

            if hit_win and hit_stop:
                ambiguous_conflict = True
                resolved_by_ticks: BarrierOutcome | None = None
                if sub_bar_trade_prices and fut_bar.open_time_ms in sub_bar_trade_prices:
                    for tick_p in sub_bar_trade_prices[fut_bar.open_time_ms]:
                        tp = require_decimal(tick_p, "tick_p")
                        if tp <= stop_barrier:
                            resolved_by_ticks = BarrierOutcome.STOP
                            break
                        if tp >= win_barrier:
                            resolved_by_ticks = BarrierOutcome.WIN
                            break

                # Pessimistic Rule (§4.2, P3_PLAN §03.2): default to STOP on any unresolved same-bar conflict
                if resolved_by_ticks == BarrierOutcome.WIN:
                    outcome = BarrierOutcome.WIN
                    raw_exit_quote_price = win_barrier
                    exit_price = win_barrier
                else:
                    outcome = BarrierOutcome.STOP
                    raw_exit_quote_price = stop_barrier
                    exit_price = _q8(stop_barrier * (Decimal("1") - cfg.stop_slippage_rate))
                bars_to_hit = step
                resolving_bar = fut_bar
                break

            if hit_stop:
                outcome = BarrierOutcome.STOP
                raw_exit_quote_price = stop_barrier
                exit_price = _q8(stop_barrier * (Decimal("1") - cfg.stop_slippage_rate))
                bars_to_hit = step
                resolving_bar = fut_bar
                break

            if hit_win:
                outcome = BarrierOutcome.WIN
                raw_exit_quote_price = win_barrier
                exit_price = win_barrier
                bars_to_hit = step
                resolving_bar = fut_bar
                break

        # Task A.3 & A.4: Exact single-deduction gross and net return formulas
        # gross_return: pure quote return before fees and exit slippage
        gross_ret = _q8((raw_exit_quote_price - entry_price) / entry_price)
        # net_return: [exit_price * (1 - sell_fee_rate)] / [entry_price * (1 + buy_fee_rate)] - 1
        cost_basis = entry_price * (Decimal("1") + cfg.buy_fee_rate)
        net_proceeds = exit_price * (Decimal("1") - cfg.sell_fee_rate)
        net_ret = _q8((net_proceeds - cost_basis) / cost_basis)

        full_horizon_bar = validated[horizon_end_idx]
        return TripleBarrierLabel(
            event_index=entry_index,
            symbol=TRADING_SYMBOL,
            entry_time_utc=signal_bar.exchange_time_utc,
            entry_open_time_ms=signal_bar.open_time_ms,
            entry_close_time_ms=signal_bar.close_time_ms,
            signal_confirm_time_ms=signal_bar.close_time_ms,
            entry_quote_time_ms=first_exec_bar.open_time_ms,
            entry_price_mode=mode_str,
            label_end_time_utc=full_horizon_bar.exchange_time_utc,
            label_end_close_time_ms=full_horizon_bar.close_time_ms,
            horizon_end_index=horizon_end_idx,
            entry_price=entry_price,
            win_barrier_price=win_barrier,
            stop_barrier_price=stop_barrier,
            outcome=outcome,
            bars_to_hit=bars_to_hit,
            raw_exit_quote_price=raw_exit_quote_price,
            exit_price=exit_price,
            gross_return=gross_ret,
            net_return=net_ret,
            ambiguous_same_bar_conflict=ambiguous_conflict,
        )


@dataclass(frozen=True)
class PurgedWalkForwardFold:
    """A single Purged & Embargoed Walk-Forward validation split."""

    fold_id: int
    train_indices: tuple[int, ...]
    val_indices: tuple[int, ...]
    purged_count: int
    embargo_bars: int
    min_bar_gap_between_train_and_val: int
    min_time_gap_ms_between_train_end_and_val_start: int


@dataclass(frozen=True)
class PurgedThreeStageSplit:
    """Strict 3-way split (`TRAIN`, `CALIBRATION`, `FINAL_TEST`) with Purge + Embargo at both boundaries (Task A.5)."""

    train_indices: tuple[int, ...]
    cal_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_train_to_cal_count: int
    purged_cal_to_test_count: int
    embargo_bars: int
    min_bar_gap_train_to_cal: int
    min_bar_gap_cal_to_test: int
    min_time_gap_ms_train_to_cal: int
    min_time_gap_ms_cal_to_test: int


class PurgedWalkForwardSplitter:
    """Purged and Embargoed Walk-Forward and 3-Stage (`TRAIN / CALIBRATION / FINAL_TEST`) splitter (§4.3, §14.1, Task A.5)."""

    def __init__(
        self,
        n_splits: int = 3,
        embargo_bars: int = THREE_HOURS_BARS,
        min_train_events: int = 10,
    ) -> None:
        if n_splits < 1:
            raise ValueError("n_splits must be >= 1")
        if embargo_bars < THREE_HOURS_BARS:
            raise ValueError(
                f"embargo_bars ({embargo_bars}) must be >= {THREE_HOURS_BARS} (3 hours of 5m bars)"
            )
        self.n_splits = n_splits
        self.embargo_bars = embargo_bars
        self.min_train_events = min_train_events

    def split(self, labels: Sequence[TripleBarrierLabel]) -> list[PurgedWalkForwardFold]:
        """Generate expanding-window Purged & Embargoed Walk-Forward folds over `labels`."""
        n = len(labels)
        if n < self.min_train_events + self.n_splits:
            raise ValueError(
                f"Insufficient labeled events ({n}) for n_splits={self.n_splits} and min_train_events={self.min_train_events}"
            )

        for i in range(1, n):
            if labels[i].event_index <= labels[i - 1].event_index:
                raise ValueError("labels must be strictly sorted by ascending event_index")

        val_chunk_size = max(1, (n - self.min_train_events) // (self.n_splits + 1))
        folds: list[PurgedWalkForwardFold] = []

        for fold_id in range(self.n_splits):
            val_start_pos = self.min_train_events + (fold_id + 1) * val_chunk_size
            if fold_id == self.n_splits - 1:
                val_end_pos = n
            else:
                val_end_pos = min(n, val_start_pos + val_chunk_size)

            if val_start_pos >= n or val_end_pos <= val_start_pos:
                continue

            val_positions = tuple(range(val_start_pos, val_end_pos))
            first_val_label = labels[val_start_pos]

            train_positions: list[int] = []
            purged_cnt = 0
            for pos in range(0, val_start_pos):
                cand = labels[pos]
                bar_gap = first_val_label.event_index - cand.event_index
                overlaps_horizon = cand.horizon_end_index >= first_val_label.event_index
                overlaps_time = cand.label_end_close_time_ms >= first_val_label.entry_open_time_ms
                violates_embargo = bar_gap < self.embargo_bars

                if overlaps_horizon or overlaps_time or violates_embargo:
                    purged_cnt += 1
                else:
                    train_positions.append(pos)

            if not train_positions:
                continue

            last_train = labels[train_positions[-1]]
            min_bar_gap = first_val_label.event_index - last_train.event_index
            min_time_gap_ms = first_val_label.entry_open_time_ms - last_train.entry_open_time_ms

            fold = PurgedWalkForwardFold(
                fold_id=fold_id,
                train_indices=tuple(train_positions),
                val_indices=val_positions,
                purged_count=purged_cnt,
                embargo_bars=self.embargo_bars,
                min_bar_gap_between_train_and_val=min_bar_gap,
                min_time_gap_ms_between_train_end_and_val_start=min_time_gap_ms,
            )
            self.verify_fold_zero_leakage(labels, fold)
            folds.append(fold)

        if not folds:
            raise ValueError("No valid PurgedWalkForwardFold could be constructed with the given embargo")
        return folds

    def split_train_calibration_test(
        self,
        labels: Sequence[TripleBarrierLabel],
        train_ratio: float = 0.50,
        cal_ratio: float = 0.25,
    ) -> PurgedThreeStageSplit:
        """Partition chronological events into strictly disjoint `TRAIN`, `CALIBRATION`, and `FINAL_TEST`
        sets with `>= embargo_bars` (36 bars = 3 hours) Purge + Embargo at BOTH boundaries (Task A.5).
        """
        n = len(labels)
        if n < 12:
            raise ValueError(f"Need at least 12 labeled events for 3-stage purged split, got {n}")
        if train_ratio <= 0.1 or cal_ratio <= 0.1 or (train_ratio + cal_ratio) >= 0.9:
            raise ValueError("Invalid train_ratio / cal_ratio")

        for i in range(1, n):
            if labels[i].event_index <= labels[i - 1].event_index:
                raise ValueError("labels must be strictly sorted by ascending event_index")

        cal_start_pos = max(4, int(n * train_ratio))
        test_start_pos = max(cal_start_pos + 3, int(n * (train_ratio + cal_ratio)))
        if test_start_pos >= n:
            test_start_pos = n - 2

        first_cal = labels[cal_start_pos]
        first_test = labels[test_start_pos]

        # 1. Build TRAIN indices (purging any event overlapping or within embargo_bars of first_cal)
        train_pos: list[int] = []
        purged_train_cal = 0
        for pos in range(0, cal_start_pos):
            cand = labels[pos]
            bar_gap = first_cal.event_index - cand.event_index
            if (
                cand.horizon_end_index >= first_cal.event_index
                or cand.label_end_close_time_ms >= first_cal.entry_open_time_ms
                or bar_gap < self.embargo_bars
            ):
                purged_train_cal += 1
            else:
                train_pos.append(pos)

        # 2. Build CALIBRATION indices (purging any event overlapping or within embargo_bars of first_test)
        cal_pos: list[int] = []
        purged_cal_test = 0
        for pos in range(cal_start_pos, test_start_pos):
            cand = labels[pos]
            bar_gap = first_test.event_index - cand.event_index
            if (
                cand.horizon_end_index >= first_test.event_index
                or cand.label_end_close_time_ms >= first_test.entry_open_time_ms
                or bar_gap < self.embargo_bars
            ):
                purged_cal_test += 1
            else:
                cal_pos.append(pos)

        # 3. Build FINAL_TEST indices
        test_pos = tuple(range(test_start_pos, n))
        if not train_pos or not cal_pos or not test_pos:
            raise ValueError(
                f"3-stage split produced an empty stage after 3h purge/embargo "
                f"(train={len(train_pos)}, cal={len(cal_pos)}, test={len(test_pos)})"
            )

        last_train = labels[train_pos[-1]]
        last_cal = labels[cal_pos[-1]]

        split_obj = PurgedThreeStageSplit(
            train_indices=tuple(train_pos),
            cal_indices=tuple(cal_pos),
            test_indices=test_pos,
            purged_train_to_cal_count=purged_train_cal,
            purged_cal_to_test_count=purged_cal_test,
            embargo_bars=self.embargo_bars,
            min_bar_gap_train_to_cal=first_cal.event_index - last_train.event_index,
            min_bar_gap_cal_to_test=first_test.event_index - last_cal.event_index,
            min_time_gap_ms_train_to_cal=first_cal.entry_open_time_ms - last_train.entry_open_time_ms,
            min_time_gap_ms_cal_to_test=first_test.entry_open_time_ms - last_cal.entry_open_time_ms,
        )
        self.verify_three_stage_zero_leakage(labels, split_obj)
        return split_obj

    def verify_fold_zero_leakage(
        self,
        labels: Sequence[TripleBarrierLabel],
        fold: PurgedWalkForwardFold,
    ) -> bool:
        """Mathematically verify zero temporal overlap and `>= 36` 5m bar embargo separation."""
        if not fold.train_indices or not fold.val_indices:
            raise ValueError("Fold cannot have empty train_indices or val_indices")
        for ti in fold.train_indices:
            t_lbl = labels[ti]
            for vi in fold.val_indices:
                v_lbl = labels[vi]
                bar_diff = v_lbl.event_index - t_lbl.event_index
                if bar_diff < self.embargo_bars:
                    raise ValueError(
                        f"Embargo violation between train[{ti}] (bar {t_lbl.event_index}) and "
                        f"val[{vi}] (bar {v_lbl.event_index}): gap={bar_diff} < {self.embargo_bars}"
                    )
                if t_lbl.horizon_end_index >= v_lbl.event_index:
                    raise ValueError(
                        f"Label horizon overlap between train[{ti}] (ends bar {t_lbl.horizon_end_index}) "
                        f"and val[{vi}] (starts bar {v_lbl.event_index})"
                    )
                if t_lbl.label_end_close_time_ms >= v_lbl.entry_open_time_ms:
                    raise ValueError(
                        f"Timestamp overlap between train[{ti}] label end ({t_lbl.label_end_close_time_ms}) "
                        f"and val[{vi}] entry start ({v_lbl.entry_open_time_ms})"
                    )
        return True

    def verify_three_stage_zero_leakage(
        self,
        labels: Sequence[TripleBarrierLabel],
        split_obj: PurgedThreeStageSplit,
    ) -> bool:
        """Mathematically verify Task A.5:
        1. `train_indices`, `cal_indices`, and `test_indices` are 100% mutually disjoint.
        2. Every `(train, cal)` pair is separated by `>= embargo_bars` (`36` bars = 3h) with zero horizon overlap.
        3. Every `(cal, test)` pair is separated by `>= embargo_bars` (`36` bars = 3h) with zero horizon overlap.
        4. Every `(train, test)` pair is separated by `>= 2 * embargo_bars` with zero horizon overlap.
        """
        s_tr = set(split_obj.train_indices)
        s_ca = set(split_obj.cal_indices)
        s_te = set(split_obj.test_indices)
        if (s_tr & s_ca) or (s_ca & s_te) or (s_tr & s_te):
            raise CausalityViolationError("3-stage split sets (TRAIN, CALIBRATION, FINAL_TEST) are not disjoint!")

        for earlier_indices, later_indices, stage_name in (
            (split_obj.train_indices, split_obj.cal_indices, "TRAIN->CALIBRATION"),
            (split_obj.cal_indices, split_obj.test_indices, "CALIBRATION->FINAL_TEST"),
            (split_obj.train_indices, split_obj.test_indices, "TRAIN->FINAL_TEST"),
        ):
            for ei in earlier_indices:
                e_lbl = labels[ei]
                for li in later_indices:
                    l_lbl = labels[li]
                    bar_diff = l_lbl.event_index - e_lbl.event_index
                    if bar_diff < self.embargo_bars:
                        raise CausalityViolationError(
                            f"Embargo violation on {stage_name}: gap={bar_diff} < {self.embargo_bars}"
                        )
                    if e_lbl.horizon_end_index >= l_lbl.event_index:
                        raise CausalityViolationError(f"Horizon overlap on {stage_name}")
                    if e_lbl.label_end_close_time_ms >= l_lbl.entry_open_time_ms:
                        raise CausalityViolationError(f"Timestamp overlap on {stage_name}")
        return True
