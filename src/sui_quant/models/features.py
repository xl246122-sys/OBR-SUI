"""Causal, zero-lookahead-leak multi-horizon feature extraction pipeline (§2.1, §3, §4, §14.1, P3_PLAN).

Enforces:
1. Physical rejection of unclosed K-lines (`is_closed=False` / `UnclosedKlineTransientView` /
   `complete_bar_only=False`).
2. Strict causal windowing: at bar `t`, rolling reference levels (`prev_20_high`, `prev_20_low`,
   `prev_20_mean_vol`, `prev_20_std_vol`) use ONLY closed bars `[t-20 .. t-1]`, and cross-asset
   `BTCUSDT` or higher-horizon `15m`/`1h` bars are filtered strictly by `close_time_ms <= bar_t.close_time_ms`.
3. Deterministic `compute_feature_snapshot_hash` and built-in `verify_causality_no_lookahead` self-audit
   proving that mutating or truncating future bars `t+1 .. t+N` produces 0% change in `MarketFeatures` at `t`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Mapping, Sequence

from sui_quant.contracts import (
    ALLOWED_SYMBOLS,
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    MarketFeatures,
    require_decimal,
)
from sui_quant.data.quality_guard import ClosedKlineBar, UnclosedKlineTransientView
from sui_quant.utils.clock import ensure_utc


QUANT_8 = Decimal("0.00000001")
EPSILON = Decimal("0.00000001")
MIN_BREAKOUT_LOOKBACK_BARS = 20


class UnclosedBarRejectedError(ValueError):
    """Raised when an unclosed K-line or incomplete bar feature set is passed to a model pipeline."""


class CausalityViolationError(ValueError):
    """Raised when non-chronological bars or future lookahead data are detected."""


def _q8(val: Decimal) -> Decimal:
    return val.quantize(QUANT_8, rounding=ROUND_HALF_EVEN)


def compute_feature_snapshot_hash(features: MarketFeatures | Mapping[str, Decimal]) -> str:
    """Compute a deterministic 64-character SHA-256 hex digest for a feature snapshot."""
    if isinstance(features, MarketFeatures):
        if not features.complete_bar_only:
            raise UnclosedBarRejectedError(
                "Cannot compute model feature_snapshot_hash on incomplete bar features (complete_bar_only=False)."
            )
        items = {
            k: str(_q8(require_decimal(v, f"features[{k}]")))
            for k, v in sorted(features.features.items())
        }
        payload = {
            "symbol": features.symbol,
            "horizon": features.horizon,
            "feature_time_utc": features.feature_time_utc.isoformat(),
            "complete_bar_only": features.complete_bar_only,
            "data_version": features.data_version,
            "features": items,
        }
    elif isinstance(features, Mapping):
        items = {
            k: str(_q8(require_decimal(v, f"features[{k}]")))
            for k, v in sorted(features.items())
        }
        payload = {"features": items}
    else:
        raise TypeError(f"Unsupported type for compute_feature_snapshot_hash: {type(features).__name__}")

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_features_schema_hash(feature_names: Sequence[str]) -> str:
    """Compute deterministic 64-character SHA-256 hex digest of the ordered feature schema."""
    canonical = "|".join(sorted(str(name) for name in feature_names)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_closed_bar_sequence(bars: Sequence[ Any ], symbol: str) -> list[ClosedKlineBar]:
    """Ensure all bars are `ClosedKlineBar` with `is_closed=True` for `symbol` and strictly ordered in time."""
    if not bars:
        raise ValueError(f"Empty bar sequence for {symbol}")
    validated: list[ClosedKlineBar] = []
    prev_open_ms: int | None = None
    for idx, bar in enumerate(bars):
        if isinstance(bar, UnclosedKlineTransientView):
            raise UnclosedBarRejectedError(
                f"UnclosedKlineTransientView at index {idx} rejected: models strictly require ClosedKlineBar."
            )
        if not isinstance(bar, ClosedKlineBar) or not bar.is_closed:
            raise UnclosedBarRejectedError(
                f"Bar at index {idx} is not a verified ClosedKlineBar (is_closed=True required)."
            )
        if bar.symbol != symbol:
            raise ValueError(f"Expected bar symbol '{symbol}', got '{bar.symbol}' at index {idx}")
        if prev_open_ms is not None and bar.open_time_ms <= prev_open_ms:
            raise CausalityViolationError(
                f"Non-monotonic bar open_time_ms at index {idx}: {bar.open_time_ms} <= {prev_open_ms}"
            )
        prev_open_ms = bar.open_time_ms
        validated.append(bar)
    return validated


def _decimal_mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _decimal_std(values: Sequence[Decimal]) -> Decimal:
    n = len(values)
    if n <= 1:
        return Decimal("0")
    mean_v = _decimal_mean(values)
    var = sum((v - mean_v) * (v - mean_v) for v in values) / Decimal(n)
    if var <= Decimal("0"):
        return Decimal("0")
    return var.sqrt()


def _decimal_ema(values: Sequence[Decimal], span: int) -> Decimal:
    if not values:
        return Decimal("0")
    alpha = Decimal("2") / Decimal(span + 1)
    one_minus_alpha = Decimal("1") - alpha
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + one_minus_alpha * ema
    return ema


def _decimal_pearson_corr(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal:
    n = min(len(xs), len(ys))
    if n < 3:
        return Decimal("0")
    x_slice = xs[-n:]
    y_slice = ys[-n:]
    mx = _decimal_mean(x_slice)
    my = _decimal_mean(y_slice)
    cov = sum((x_slice[i] - mx) * (y_slice[i] - my) for i in range(n)) / Decimal(n)
    sx = _decimal_std(x_slice)
    sy = _decimal_std(y_slice)
    denom = sx * sy
    if denom <= EPSILON:
        return Decimal("0")
    corr = cov / denom
    if corr > Decimal("1"):
        return Decimal("1")
    if corr < Decimal("-1"):
        return Decimal("-1")
    return corr


STANDARD_MODEL_FEATURE_NAMES: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_volume",
    "trade_count",
    "prev_20_high",
    "prev_20_low",
    "breakout_above_prev_20_high",
    "breakout_pct",
    "prev_20_mean_vol",
    "prev_20_std_vol",
    "rvol_20",
    "vol_zscore_20",
    "candle_range",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "body_ratio",
    "taker_buy_ratio",
    "ret_1",
    "ret_3",
    "ret_6",
    "ret_12",
    "ret_20",
    "atr_14",
    "atr_14_pct",
    "realized_vol_20",
    "er_20",
    "ema_fast_9",
    "ema_slow_20",
    "ema_spread_pct",
    "ret_15m_1",
    "ret_1h_1",
    "btc_ret_1",
    "btc_ret_3",
    "btc_ret_6",
    "btc_ret_12",
    "btc_atr_14_pct",
    "sui_btc_rel_strength_6",
    "btc_sui_corr_20",
    "btc_flash_drop_flag",
)


@dataclass(frozen=True)
class FeaturePipelineConfig:
    """Configuration for causal multi-horizon feature extraction."""

    breakout_lookback: int = MIN_BREAKOUT_LOOKBACK_BARS
    atr_period: int = 14
    ema_fast_span: int = 9
    ema_slow_span: int = 20
    btc_flash_drop_1bar: Decimal = Decimal("-0.0080")   # -0.8% in a single 5m bar
    btc_flash_drop_3bar: Decimal = Decimal("-0.0150")   # -1.5% over 15m (3 bars)
    btc_flash_drop_6bar: Decimal = Decimal("-0.0220")   # -2.2% over 30m (6 bars)
    data_version: str = "v1.0-p3-causal-features"


class FeaturePipeline:
    """Causal multi-timeframe feature extractor for `SUIUSDT` and `BTCUSDT` (§3, §4, P3_PLAN)."""

    def __init__(self, config: FeaturePipelineConfig | None = None) -> None:
        self.config = config or FeaturePipelineConfig()
        if self.config.breakout_lookback < 5:
            raise ValueError("breakout_lookback must be >= 5")

    def extract_at_index(
        self,
        sui_5m_bars: Sequence[ClosedKlineBar],
        target_index: int | None = None,
        btc_5m_bars: Sequence[ClosedKlineBar] | None = None,
        sui_15m_bars: Sequence[ClosedKlineBar] | None = None,
        sui_1h_bars: Sequence[ClosedKlineBar] | None = None,
    ) -> MarketFeatures:
        """Extract causal `MarketFeatures(complete_bar_only=True)` at `target_index` (defaults to latest bar).

        Strictly truncates `sui_5m_bars` to `[:target_index + 1]` before any computation so future bars
        `[target_index + 1:]` can NEVER be read. Also filters `btc_5m_bars`, `sui_15m_bars`, and `sui_1h_bars`
        by `close_time_ms <= bar_t.close_time_ms`.
        """
        validated_sui = _validate_closed_bar_sequence(sui_5m_bars, TRADING_SYMBOL)
        idx = (len(validated_sui) - 1) if target_index is None else int(target_index)
        if idx < 0 or idx >= len(validated_sui):
            raise IndexError(f"target_index {idx} out of range for {len(validated_sui)} bars")

        lookback = self.config.breakout_lookback
        if idx < lookback:
            raise ValueError(
                f"Insufficient historical bars at index {idx}: need at least {lookback} prior closed bars "
                f"(total >= {lookback + 1} bars)."
            )

        # Hard causal truncation: only bars up to `idx` exist in our causal universe
        causal_sui = validated_sui[: idx + 1]
        bar_t = causal_sui[-1]
        prior_window = causal_sui[-1 - lookback : -1]  # strictly t-20 .. t-1 (excludes bar_t!)

        # 1. Prior 20 closed 5m bars high/low and breakout metrics
        prev_20_high = max(b.high_price for b in prior_window)
        prev_20_low = min(b.low_price for b in prior_window)
        is_breakout = Decimal("1") if bar_t.close_price > prev_20_high else Decimal("0")
        breakout_pct = (bar_t.close_price - prev_20_high) / max(prev_20_high, EPSILON)

        # 2. Volume amplification metrics strictly relative to prior 20 bars
        prior_vols = [b.base_volume for b in prior_window]
        prev_20_mean_vol = _decimal_mean(prior_vols)
        prev_20_std_vol = _decimal_std(prior_vols)
        rvol_20 = bar_t.base_volume / max(prev_20_mean_vol, EPSILON)
        vol_zscore_20 = (bar_t.base_volume - prev_20_mean_vol) / max(prev_20_std_vol, EPSILON)

        # 3. Candle microstructure & upper shadow filter
        raw_range = bar_t.high_price - bar_t.low_price
        candle_range = max(raw_range, Decimal("0.0001"))
        body_top = max(bar_t.open_price, bar_t.close_price)
        body_bot = min(bar_t.open_price, bar_t.close_price)
        upper_wick_ratio = max(Decimal("0"), (bar_t.high_price - body_top) / candle_range)
        lower_wick_ratio = max(Decimal("0"), (body_bot - bar_t.low_price) / candle_range)
        body_ratio = (bar_t.close_price - bar_t.open_price) / candle_range
        taker_buy_ratio = (
            bar_t.taker_buy_base_volume / bar_t.base_volume
            if bar_t.base_volume > Decimal("0")
            else Decimal("0.5")
        )

        # 4. Multi-step returns (strictly backwards looking)
        closes = [b.close_price for b in causal_sui]

        def _ret(lag: int) -> Decimal:
            ref_idx = max(0, len(closes) - 1 - lag)
            ref_p = max(closes[ref_idx], EPSILON)
            return (closes[-1] - ref_p) / ref_p

        ret_1 = _ret(1)
        ret_3 = _ret(3)
        ret_6 = _ret(6)
        ret_12 = _ret(12)
        ret_20 = _ret(20)

        # 5. True Range, ATR-14, Realized Volatility, Kaufman Efficiency Ratio, EMAs
        atr_len = min(self.config.atr_period, len(causal_sui) - 1)
        trs: list[Decimal] = []
        for k in range(len(causal_sui) - atr_len, len(causal_sui)):
            cur = causal_sui[k]
            prev_c = causal_sui[k - 1].close_price
            tr = max(
                cur.high_price - cur.low_price,
                abs(cur.high_price - prev_c),
                abs(cur.low_price - prev_c),
            )
            trs.append(tr)
        atr_14 = _decimal_mean(trs)
        atr_14_pct = atr_14 / max(bar_t.close_price, EPSILON)

        sui_1bar_rets: list[Decimal] = []
        for k in range(len(causal_sui) - lookback, len(causal_sui)):
            p0 = max(causal_sui[k - 1].close_price, EPSILON)
            sui_1bar_rets.append((causal_sui[k].close_price - p0) / p0)
        realized_vol_20 = _decimal_std(sui_1bar_rets)

        path_abs_diff = sum(
            (abs(causal_sui[k].close_price - causal_sui[k - 1].close_price) for k in range(len(causal_sui) - lookback, len(causal_sui))),
            Decimal("0"),
        )
        net_change_20 = abs(bar_t.close_price - causal_sui[-1 - lookback].close_price)
        er_20 = (net_change_20 / path_abs_diff) if path_abs_diff > EPSILON else Decimal("0")

        ema_window = closes[-max(lookback + 1, self.config.ema_slow_span * 2) :]
        ema_fast_9 = _decimal_ema(ema_window, self.config.ema_fast_span)
        ema_slow_20 = _decimal_ema(ema_window, self.config.ema_slow_span)
        ema_spread_pct = (ema_fast_9 - ema_slow_20) / max(ema_slow_20, EPSILON)

        # 6. Higher-timeframe causal alignment (15m / 1h closed bars with close_time_ms <= bar_t.close_time_ms)
        ret_15m_1 = ret_3
        if sui_15m_bars:
            valid_15m = [
                b for b in _validate_closed_bar_sequence(sui_15m_bars, TRADING_SYMBOL)
                if b.close_time_ms <= bar_t.close_time_ms
            ]
            if len(valid_15m) >= 2:
                ret_15m_1 = (valid_15m[-1].close_price - valid_15m[-2].close_price) / max(
                    valid_15m[-2].close_price, EPSILON
                )

        ret_1h_1 = ret_12
        if sui_1h_bars:
            valid_1h = [
                b for b in _validate_closed_bar_sequence(sui_1h_bars, TRADING_SYMBOL)
                if b.close_time_ms <= bar_t.close_time_ms
            ]
            if len(valid_1h) >= 2:
                ret_1h_1 = (valid_1h[-1].close_price - valid_1h[-2].close_price) / max(
                    valid_1h[-2].close_price, EPSILON
                )

        # 7. Cross-asset BTCUSDT reference features (strictly causal: close_time_ms <= bar_t.close_time_ms)
        btc_ret_1 = Decimal("0")
        btc_ret_3 = Decimal("0")
        btc_ret_6 = Decimal("0")
        btc_ret_12 = Decimal("0")
        btc_atr_14_pct = Decimal("0")
        btc_sui_corr_20 = Decimal("0")

        if btc_5m_bars:
            all_btc = _validate_closed_bar_sequence(btc_5m_bars, REFERENCE_SYMBOL)
            causal_btc = [b for b in all_btc if b.close_time_ms <= bar_t.close_time_ms]
            if len(causal_btc) >= 2:
                b_closes = [b.close_price for b in causal_btc]

                def _bret(lag: int) -> Decimal:
                    r_idx = max(0, len(b_closes) - 1 - lag)
                    rp = max(b_closes[r_idx], EPSILON)
                    return (b_closes[-1] - rp) / rp

                btc_ret_1 = _bret(1)
                btc_ret_3 = _bret(3)
                btc_ret_6 = _bret(6)
                btc_ret_12 = _bret(12)

                b_atr_len = min(self.config.atr_period, len(causal_btc) - 1)
                b_trs: list[Decimal] = []
                for k in range(len(causal_btc) - b_atr_len, len(causal_btc)):
                    cur_b = causal_btc[k]
                    prev_bc = causal_btc[k - 1].close_price
                    b_trs.append(
                        max(
                            cur_b.high_price - cur_b.low_price,
                            abs(cur_b.high_price - prev_bc),
                            abs(cur_b.low_price - prev_bc),
                        )
                    )
                btc_atr_14_pct = _decimal_mean(b_trs) / max(causal_btc[-1].close_price, EPSILON)

                corr_len = min(lookback, len(causal_btc) - 1)
                if corr_len >= 3:
                    btc_1bar_rets = [
                        (causal_btc[k].close_price - causal_btc[k - 1].close_price)
                        / max(causal_btc[k - 1].close_price, EPSILON)
                        for k in range(len(causal_btc) - corr_len, len(causal_btc))
                    ]
                    btc_sui_corr_20 = _decimal_pearson_corr(sui_1bar_rets[-corr_len:], btc_1bar_rets)

        sui_btc_rel_strength_6 = ret_6 - btc_ret_6
        btc_flash_drop = (
            btc_ret_1 <= self.config.btc_flash_drop_1bar
            or btc_ret_3 <= self.config.btc_flash_drop_3bar
            or btc_ret_6 <= self.config.btc_flash_drop_6bar
        )
        btc_flash_drop_flag = Decimal("1") if btc_flash_drop else Decimal("0")

        feat_dict: dict[str, Decimal] = {
            "open": _q8(bar_t.open_price),
            "high": _q8(bar_t.high_price),
            "low": _q8(bar_t.low_price),
            "close": _q8(bar_t.close_price),
            "volume": _q8(bar_t.base_volume),
            "quote_volume": _q8(bar_t.quote_volume),
            "taker_buy_volume": _q8(bar_t.taker_buy_base_volume),
            "trade_count": Decimal(bar_t.trade_count),
            "prev_20_high": _q8(prev_20_high),
            "prev_20_low": _q8(prev_20_low),
            "breakout_above_prev_20_high": is_breakout,
            "breakout_pct": _q8(breakout_pct),
            "prev_20_mean_vol": _q8(prev_20_mean_vol),
            "prev_20_std_vol": _q8(prev_20_std_vol),
            "rvol_20": _q8(rvol_20),
            "vol_zscore_20": _q8(vol_zscore_20),
            "candle_range": _q8(candle_range),
            "upper_wick_ratio": _q8(upper_wick_ratio),
            "lower_wick_ratio": _q8(lower_wick_ratio),
            "body_ratio": _q8(body_ratio),
            "taker_buy_ratio": _q8(taker_buy_ratio),
            "ret_1": _q8(ret_1),
            "ret_3": _q8(ret_3),
            "ret_6": _q8(ret_6),
            "ret_12": _q8(ret_12),
            "ret_20": _q8(ret_20),
            "atr_14": _q8(atr_14),
            "atr_14_pct": _q8(atr_14_pct),
            "realized_vol_20": _q8(realized_vol_20),
            "er_20": _q8(er_20),
            "ema_fast_9": _q8(ema_fast_9),
            "ema_slow_20": _q8(ema_slow_20),
            "ema_spread_pct": _q8(ema_spread_pct),
            "ret_15m_1": _q8(ret_15m_1),
            "ret_1h_1": _q8(ret_1h_1),
            "btc_ret_1": _q8(btc_ret_1),
            "btc_ret_3": _q8(btc_ret_3),
            "btc_ret_6": _q8(btc_ret_6),
            "btc_ret_12": _q8(btc_ret_12),
            "btc_atr_14_pct": _q8(btc_atr_14_pct),
            "sui_btc_rel_strength_6": _q8(sui_btc_rel_strength_6),
            "btc_sui_corr_20": _q8(btc_sui_corr_20),
            "btc_flash_drop_flag": btc_flash_drop_flag,
        }

        return MarketFeatures(
            feature_time_utc=bar_t.exchange_time_utc,
            horizon=bar_t.interval,
            symbol=TRADING_SYMBOL,
            complete_bar_only=True,
            features=feat_dict,
            data_version=self.config.data_version,
        )

    def verify_causality_no_lookahead(
        self,
        sui_5m_bars: Sequence[ClosedKlineBar],
        check_index: int,
        btc_5m_bars: Sequence[ClosedKlineBar] | None = None,
    ) -> tuple[bool, str]:
        """Mathematically verify zero look-ahead leakage at `check_index` (§14.1, P3_PLAN).

        Compares:
        1. Features extracted when the input array ends at `check_index` (`sui_5m_bars[:check_index + 1]`).
        2. Features extracted when future bars `check_index + 1 .. N` are present.
        3. Features extracted when future bars `check_index + 1 .. N` are adversarially mutated with
           100x price spikes, flash crashes, and 1000x volume anomalies.
        """
        if check_index >= len(sui_5m_bars) - 1:
            raise ValueError("check_index must have at least 1 future bar after it to test look-ahead immunity")

        # 1. Truncated causal slice (no future bars exist in memory)
        truncated_sui = list(sui_5m_bars[: check_index + 1])
        cutoff_ms = truncated_sui[-1].close_time_ms
        truncated_btc = (
            [b for b in btc_5m_bars if b.close_time_ms <= cutoff_ms]
            if btc_5m_bars is not None
            else None
        )
        feat_truncated = self.extract_at_index(
            sui_5m_bars=truncated_sui,
            target_index=check_index,
            btc_5m_bars=truncated_btc,
        )
        hash_truncated = compute_feature_snapshot_hash(feat_truncated)

        # 2. Full slice (with future bars present)
        feat_full = self.extract_at_index(
            sui_5m_bars=sui_5m_bars,
            target_index=check_index,
            btc_5m_bars=btc_5m_bars,
        )
        hash_full = compute_feature_snapshot_hash(feat_full)

        # 3. Mutated future slice (extreme future price/volume tampering)
        mutated_sui = list(sui_5m_bars[: check_index + 1])
        for fut_bar in sui_5m_bars[check_index + 1 :]:
            mutated_sui.append(
                ClosedKlineBar(
                    symbol=fut_bar.symbol,
                    interval=fut_bar.interval,
                    open_time_ms=fut_bar.open_time_ms,
                    close_time_ms=fut_bar.close_time_ms,
                    open_price=fut_bar.open_price * Decimal("50"),
                    high_price=fut_bar.high_price * Decimal("100"),
                    low_price=fut_bar.low_price * Decimal("0.01"),
                    close_price=fut_bar.close_price * Decimal("80"),
                    base_volume=fut_bar.base_volume * Decimal("1000"),
                    quote_volume=fut_bar.quote_volume * Decimal("10000"),
                    taker_buy_base_volume=fut_bar.taker_buy_base_volume * Decimal("500"),
                    taker_buy_quote_volume=fut_bar.taker_buy_quote_volume * Decimal("5000"),
                    trade_count=fut_bar.trade_count * 100,
                    exchange_time_utc=fut_bar.exchange_time_utc,
                    recv_time_utc=fut_bar.recv_time_utc,
                    is_closed=True,
                )
            )

        mutated_btc: list[ClosedKlineBar] | None = None
        if btc_5m_bars is not None:
            mutated_btc = []
            for b in btc_5m_bars:
                if b.close_time_ms <= cutoff_ms:
                    mutated_btc.append(b)
                else:
                    mutated_btc.append(
                        ClosedKlineBar(
                            symbol=b.symbol,
                            interval=b.interval,
                            open_time_ms=b.open_time_ms,
                            close_time_ms=b.close_time_ms,
                            open_price=b.open_price * Decimal("0.1"),
                            high_price=b.high_price * Decimal("10"),
                            low_price=b.low_price * Decimal("0.05"),
                            close_price=b.close_price * Decimal("0.1"),
                            base_volume=b.base_volume * Decimal("999"),
                            quote_volume=b.quote_volume * Decimal("999"),
                            taker_buy_base_volume=b.taker_buy_base_volume,
                            taker_buy_quote_volume=b.taker_buy_quote_volume,
                            trade_count=b.trade_count,
                            exchange_time_utc=b.exchange_time_utc,
                            recv_time_utc=b.recv_time_utc,
                            is_closed=True,
                        )
                    )

        feat_mutated = self.extract_at_index(
            sui_5m_bars=mutated_sui,
            target_index=check_index,
            btc_5m_bars=mutated_btc,
        )
        hash_mutated = compute_feature_snapshot_hash(feat_mutated)

        if hash_truncated != hash_full or hash_truncated != hash_mutated:
            raise CausalityViolationError(
                f"Look-ahead leakage detected at index {check_index}: "
                f"truncated={hash_truncated}, full={hash_full}, mutated={hash_mutated}"
            )
        if feat_truncated.features != feat_full.features or feat_truncated.features != feat_mutated.features:
            raise CausalityViolationError("Feature dictionary mismatch under future mutation")

        return True, hash_truncated


def load_closed_bars_from_warehouse(
    duckdb_path: str | Path,
    symbol: str = TRADING_SYMBOL,
    interval: str = "5m",
) -> list[ClosedKlineBar]:
    """Load verified `ClosedKlineBar` records from the local Windows `Parquet + DuckDB` warehouse."""
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError(f"Unsupported symbol '{symbol}'")
    db_file = Path(duckdb_path)
    if not db_file.exists():
        return []
    try:
        import duckdb

        conn = duckdb.connect(str(db_file), read_only=True)
    except ImportError:
        import sqlite3

        conn = sqlite3.connect(str(db_file))
    try:
        rows = conn.execute(
            """
            SELECT symbol, interval, open_time_ms, close_time_ms,
                   open_price, high_price, low_price, close_price,
                   base_volume, quote_volume, exchange_time_utc, recv_time_utc
            FROM market_klines
            WHERE symbol = ? AND interval = ?
            ORDER BY open_time_ms ASC
            """,
            (symbol, interval),
        ).fetchall()
    finally:
        conn.close()

    bars: list[ClosedKlineBar] = []
    for r in rows:
        base_vol = Decimal(str(r[8]))
        quote_vol = Decimal(str(r[9]))
        ex_utc = ensure_utc(datetime.fromisoformat(str(r[10])), "exchange_time_utc")
        rc_utc = ensure_utc(datetime.fromisoformat(str(r[11])), "recv_time_utc")
        bars.append(
            ClosedKlineBar(
                symbol=str(r[0]),
                interval=str(r[1]),
                open_time_ms=int(r[2]),
                close_time_ms=int(r[3]),
                open_price=Decimal(str(r[4])),
                high_price=Decimal(str(r[5])),
                low_price=Decimal(str(r[6])),
                close_price=Decimal(str(r[7])),
                base_volume=base_vol,
                quote_volume=quote_vol,
                taker_buy_base_volume=_q8(base_vol * Decimal("0.5")),
                taker_buy_quote_volume=_q8(quote_vol * Decimal("0.5")),
                trade_count=100,
                exchange_time_utc=ex_utc,
                recv_time_utc=rc_utc,
                is_closed=True,
            )
        )
    return bars
