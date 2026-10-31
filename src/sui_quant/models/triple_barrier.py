"""Triple-Barrier Labeling, Breakout Candidate Screening, and Purged & Embargoed Walk-Forward Splitting (§4, §6.1, §6.2, §14, P3_PLAN).

Key Guarantees:
1. Upward Volume Breakout Candidate Filter (§4.1):
   - Only evaluates closed 5m K-lines (`is_closed=True`, `complete_bar_only=True`) on `SUIUSDT`.
   - Requires `close_t > prev_20_high`, volume amplification (`rvol_20 >= min_rvol` or `vol_zscore_20 >= min_vol_zscore`),
     bounded upper shadow (`upper_wick_ratio <= max_upper_wick_ratio`), and positive body (`close_t > open_t`).
2. Fee-Aware Triple-Barrier Method (§4.2):
   - Upper `WIN` barrier: `compute_c1_net_target_sell_price` achieving `+1.5%` NET profit after both buy and sell fees.
   - Lower `STOP` barrier: `compute_stop_loss_trigger_price` at `-1.0%` below weighted entry price.
   - Vertical `TIMEOUT` barrier: `3 hours` = `36` closed 5m bars.
3. Intra-Bar Ambiguity Pessimistic Tie-Breaking (§4.2, P3_PLAN §03.2):
   - When a future K-line touches BOTH `high >= win_barrier_price` and `low <= stop_barrier_price` within the same bar,
     unless sub-bar tick sequence explicitly proves `WIN` was reached first, the outcome is strictly resolved as
     `BarrierOutcome.STOP` (`ambiguous_same_bar_conflict=True`).
4. Purged & Embargoed Walk-Forward Time-Series Validation (§4.3, §14.1):
   - Purges any training event whose 3-hour label horizon overlaps the validation fold and enforces a minimum
     `36` 5m bar (`3 hours = 10,800,000 ms`) embargo buffer between training and validation events.
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
from sui_quant.models.features import UnclosedBarRejectedError, _validate_closed_bar_sequence
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
    """Parameters for Triple-Barrier labeling (§4.2, §6.1, §6.2)."""

    horizon_bars: int = THREE_HOURS_BARS                 # 36 bars * 5m = 3 hours
    target_net_return: Decimal = Decimal("0.0150")       # +1.5% net return after buy & sell fees
    stop_loss_pct: Decimal = Decimal("0.0100")           # -1.0% trigger relative to entry price
    buy_fee_rate: Decimal = Decimal("0.0010")            # 10 bps spot taker/maker baseline
    sell_fee_rate: Decimal = Decimal("0.0010")           # 10 bps spot taker/maker baseline
    stop_slippage_rate: Decimal = Decimal("0.0005")      # 5 bps execution slippage on stop trigger
    filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS


@dataclass(frozen=True)
class TripleBarrierLabel:
    """Immutable Triple-Barrier outcome record for a single entry event."""

    event_index: int
    symbol: str
    entry_time_utc: datetime
    entry_open_time_ms: int
    entry_close_time_ms: int
    label_end_time_utc: datetime
    label_end_close_time_ms: int
    horizon_end_index: int
    entry_price: Decimal
    win_barrier_price: Decimal
    stop_barrier_price: Decimal
    outcome: BarrierOutcome
    bars_to_hit: int
    exit_price: Decimal
    gross_return: Decimal
    net_return: Decimal
    ambiguous_same_bar_conflict: bool

    def __post_init__(self) -> None:
        if self.symbol != TRADING_SYMBOL:
            raise ValueError(f"TripleBarrierLabel symbol must be '{TRADING_SYMBOL}'")
        ensure_utc(self.entry_time_utc, "entry_time_utc")
        ensure_utc(self.label_end_time_utc, "label_end_time_utc")
        require_decimal(self.entry_price, "entry_price")
        require_decimal(self.win_barrier_price, "win_barrier_price")
        require_decimal(self.stop_barrier_price, "stop_barrier_price")
        require_decimal(self.exit_price, "exit_price")
        require_decimal(self.gross_return, "gross_return")
        require_decimal(self.net_return, "net_return")


class TripleBarrierLabeler:
    """Fee-aware Triple-Barrier labeler with pessimistic intra-bar conflict resolution (§4.2)."""

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

    def label_at_index(
        self,
        sui_5m_bars: Sequence[ClosedKlineBar],
        entry_index: int,
        entry_price_override: Decimal | None = None,
        sub_bar_trade_prices: Mapping[int, Sequence[Decimal]] | None = None,
    ) -> TripleBarrierLabel:
        """Compute Triple-Barrier label for an entry at the close of `sui_5m_bars[entry_index]`.

        Requires at least `horizon_bars` (default 36) closed 5m bars after `entry_index` so that
        the full 3-hour horizon is observed without truncation bias.
        """
        validated = _validate_closed_bar_sequence(sui_5m_bars, TRADING_SYMBOL)
        cfg = self.config
        horizon = cfg.horizon_bars
        if entry_index < 0 or entry_index + horizon >= len(validated):
            raise ValueError(
                f"Cannot label entry_index={entry_index}: need {horizon} future bars, "
                f"but sequence length is {len(validated)}."
            )

        entry_bar = validated[entry_index]
        entry_price = (
            require_decimal(entry_price_override, "entry_price_override")
            if entry_price_override is not None
            else entry_bar.close_price
        )
        win_barrier, stop_barrier = self.compute_barriers(entry_price)

        outcome = BarrierOutcome.TIMEOUT
        bars_to_hit = horizon
        ambiguous_conflict = False
        horizon_end_idx = entry_index + horizon
        resolving_bar = validated[horizon_end_idx]
        exit_price = resolving_bar.close_price

        for step in range(1, horizon + 1):
            fut_bar = validated[entry_index + step]
            hit_win = fut_bar.high_price >= win_barrier
            hit_stop = fut_bar.low_price <= stop_barrier

            if hit_win and hit_stop:
                ambiguous_conflict = True
                # Check if sub-bar chronological trade sequence is available and resolves first touch
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
                    exit_price = win_barrier
                else:
                    outcome = BarrierOutcome.STOP
                    exit_price = _q8(stop_barrier * (Decimal("1") - cfg.stop_slippage_rate))
                bars_to_hit = step
                resolving_bar = fut_bar
                break

            if hit_stop:
                outcome = BarrierOutcome.STOP
                exit_price = _q8(stop_barrier * (Decimal("1") - cfg.stop_slippage_rate))
                bars_to_hit = step
                resolving_bar = fut_bar
                break

            if hit_win:
                outcome = BarrierOutcome.WIN
                exit_price = win_barrier
                bars_to_hit = step
                resolving_bar = fut_bar
                break

        gross_ret = _q8((exit_price - entry_price) / entry_price)
        # Exact round-trip net return after buy & sell fees:
        # net_proceeds / gross_cost - 1 = [exit_price * (1 - f_sell)] / [entry_price * (1 + f_buy)] - 1
        cost_basis = entry_price * (Decimal("1") + cfg.buy_fee_rate)
        net_proceeds = exit_price * (Decimal("1") - cfg.sell_fee_rate)
        net_ret = _q8((net_proceeds - cost_basis) / cost_basis)

        # Full label span ends at the 3h horizon close time (for conservative Purge & Embargo accounting)
        full_horizon_bar = validated[horizon_end_idx]
        return TripleBarrierLabel(
            event_index=entry_index,
            symbol=TRADING_SYMBOL,
            entry_time_utc=entry_bar.exchange_time_utc,
            entry_open_time_ms=entry_bar.open_time_ms,
            entry_close_time_ms=entry_bar.close_time_ms,
            label_end_time_utc=full_horizon_bar.exchange_time_utc,
            label_end_close_time_ms=full_horizon_bar.close_time_ms,
            horizon_end_index=horizon_end_idx,
            entry_price=entry_price,
            win_barrier_price=win_barrier,
            stop_barrier_price=stop_barrier,
            outcome=outcome,
            bars_to_hit=bars_to_hit,
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


class PurgedWalkForwardSplitter:
    """Purged and Embargoed Walk-Forward cross-validation splitter (§4.3, §14.1, P3_PLAN).

    Guarantees that between the last training event in a fold and the first validation event:
    1. No training label's 3-hour forward window (`horizon_end_index` / `label_end_close_time_ms`)
       overlaps with the validation fold start.
    2. An explicit embargo buffer of `>= embargo_bars` (default `36` 5m bars = `3 hours`) separates
       all training event indices from validation event indices (`val_event_index - train_event_index >= 36`).
    """

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

        # Ensure labels are strictly ordered by event_index
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

            val_Positions = tuple(range(val_start_pos, val_end_pos))
            first_val_label = labels[val_start_pos]

            train_positions: list[int] = []
            purged_cnt = 0
            for pos in range(0, val_start_pos):
                cand = labels[pos]
                bar_gap = first_val_label.event_index - cand.event_index
                # Purge if candidate's 3h label horizon reaches into validation start OR violates embargo_bars
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
                val_indices=val_Positions,
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
