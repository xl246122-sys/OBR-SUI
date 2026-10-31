"""A-01 Mathematical Market Regime Classifier (§3, §7.1, P3_PLAN, Task A.7).

Classifies the market environment into 5 mutually exclusive states (`RegimeType`):
- `UP_TREND`: Sustained directional upward momentum with supportive multi-horizon alignment.
- `CONSOLIDATION`: Bounded volatility and range-bound structure where upward volume breakouts originate.
- `DOWN_TREND`: Negative trend structure; new entries strictly forbidden (`allow_entry=False`).
- `HIGH_VOLATILITY`: Extreme ATR / realized return dispersion; new entries strictly forbidden (`allow_entry=False`).
- `DATA_UNHEALTHY`: Stale, gapped, incomplete, or missing market data; new entries strictly forbidden (`allow_entry=False`).

Task A.7 Explicit `BTCUSDT` Flash-Crash Time Windows & Ablation Support:
- `5m` window (`1` closed 5m bar `[t-5m, t]`): `btc_ret_5m <= -0.80%` (`Decimal("-0.0080")`)
- `15m` window (`3` closed 5m bars `[t-15m, t]`): `btc_ret_15m <= -1.50%` (`Decimal("-0.0150")`)
- `30m` window (`6` closed 5m bars `[t-30m, t]`): `btc_ret_30m <= -2.20%` (`Decimal("-0.0220")`)
- Supports `enable_btc_flash_drop_filter=True/False` for rigorous out-of-sample ablation comparison.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Mapping

from sui_quant.contracts import (
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    DataQuality,
    MarketFeatures,
    RegimeDecision,
    RegimeType,
    require_decimal,
)
from sui_quant.models.features import compute_feature_snapshot_hash
from sui_quant.utils.clock import ensure_utc


QUANT_6 = Decimal("0.000001")


def _q6(val: Decimal) -> Decimal:
    return val.quantize(QUANT_6, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class A01RegimeConfig:
    """Hyperparameters and safety thresholds for the A-01 5-state regime classifier (Task A.7)."""

    model_version: str = "a01-regime-v1.0.0"
    decision_ttl_seconds: int = 300  # 5 minutes (valid until next 5m close)
    high_volatility_atr_pct_threshold: Decimal = Decimal("0.0220")     # >= 2.2% 5m ATR -> HIGH_VOLATILITY
    high_volatility_ret_std_threshold: Decimal = Decimal("0.0160")     # >= 1.6% 5m return std -> HIGH_VOLATILITY
    up_trend_score_threshold: Decimal = Decimal("0.6500")
    down_trend_score_threshold: Decimal = Decimal("-0.5500")
    down_trend_ret_12_threshold: Decimal = Decimal("-0.0150")          # <= -1.5% over 1h -> DOWN_TREND

    # Task A.7: Explicit BTC flash-drop time windows (5m = 1 bar, 15m = 3 bars, 30m = 6 bars)
    enable_btc_flash_drop_filter: bool = True
    btc_flash_drop_5m_window_minutes: int = 5
    btc_flash_drop_15m_window_minutes: int = 15
    btc_flash_drop_30m_window_minutes: int = 30
    btc_flash_drop_1bar_threshold: Decimal = Decimal("-0.0080")        # <= -0.80% over 5m (1 closed 5m bar)
    btc_flash_drop_3bar_threshold: Decimal = Decimal("-0.0150")        # <= -1.50% over 15m (3 closed 5m bars)
    btc_flash_drop_6bar_threshold: Decimal = Decimal("-0.0220")        # <= -2.20% over 30m (6 closed 5m bars)

    min_entry_confidence: Decimal = Decimal("0.4000")
    allow_consolidation_breakout_entry: bool = True
    softmax_temperature: Decimal = Decimal("1.1500")


@dataclass(frozen=True)
class A01RegimeDiagnostics:
    """Rich mathematical diagnostics accompanying a `RegimeDecision` (including Task A.7 window tags)."""

    trend_strength: Decimal
    volatility_ratio: Decimal
    btc_ret_1: Decimal
    btc_ret_3: Decimal
    btc_ret_6: Decimal
    btc_veto_triggered: bool
    btc_veto_windows: tuple[str, ...]
    regime_probabilities: Mapping[RegimeType, Decimal]
    feature_snapshot_hash: str


class A01RegimeClassifier:
    """Deterministic mathematical 5-state regime model for `SUIUSDT` with `BTCUSDT` risk filtering (§3)."""

    REQUIRED_FEATURE_KEYS: tuple[str, ...] = (
        "close",
        "ret_3",
        "ret_6",
        "ret_12",
        "ret_20",
        "atr_14_pct",
        "realized_vol_20",
        "er_20",
        "ema_spread_pct",
    )

    def __init__(self, config: A01RegimeConfig | None = None) -> None:
        self.config = config or A01RegimeConfig()

    def classify(
        self,
        sui_features: MarketFeatures | None,
        data_quality: DataQuality = DataQuality.OK,
        btc_features: MarketFeatures | None = None,
        now_utc: datetime | None = None,
    ) -> RegimeDecision:
        """Evaluate current market regime and return a standard `RegimeDecision` contract."""
        decision, _ = self.classify_with_diagnostics(
            sui_features=sui_features,
            data_quality=data_quality,
            btc_features=btc_features,
            now_utc=now_utc,
        )
        return decision

    def classify_with_diagnostics(
        self,
        sui_features: MarketFeatures | None,
        data_quality: DataQuality = DataQuality.OK,
        btc_features: MarketFeatures | None = None,
        now_utc: datetime | None = None,
    ) -> tuple[RegimeDecision, A01RegimeDiagnostics]:
        """Evaluate market regime and return both `RegimeDecision` and `A01RegimeDiagnostics`."""
        cfg = self.config
        ttl_delta = timedelta(seconds=cfg.decision_ttl_seconds)

        # 1. Hard Data Health Gate (DATA_UNHEALTHY)
        unhealthy_reasons: list[str] = []
        if data_quality != DataQuality.OK:
            unhealthy_reasons.append(f"DATA_QUALITY_{data_quality.value}")
        if sui_features is None:
            unhealthy_reasons.append("MISSING_SUI_FEATURES")
        else:
            if sui_features.symbol != TRADING_SYMBOL:
                raise ValueError(
                    f"A-01 primary features must be for '{TRADING_SYMBOL}', got '{sui_features.symbol}'"
                )
            if not sui_features.complete_bar_only:
                unhealthy_reasons.append("INCOMPLETE_BAR_REJECTED")
            missing_keys = [k for k in self.REQUIRED_FEATURE_KEYS if k not in sui_features.features]
            if missing_keys:
                unhealthy_reasons.append(f"MISSING_KEYS_{'_'.join(missing_keys[:3])}")
            if now_utc is not None:
                ensure_utc(now_utc, "now_utc")
                age_sec = (now_utc - sui_features.feature_time_utc).total_seconds()
                if age_sec > cfg.decision_ttl_seconds * 2:
                    unhealthy_reasons.append("FEATURE_SNAPSHOT_STALE")

        if btc_features is not None:
            if btc_features.symbol != REFERENCE_SYMBOL:
                raise ValueError(
                    f"A-01 reference features must be for '{REFERENCE_SYMBOL}', got '{btc_features.symbol}'"
                )
            if not btc_features.complete_bar_only:
                unhealthy_reasons.append("BTC_INCOMPLETE_BAR_REJECTED")

        if unhealthy_reasons:
            ref_time = (
                sui_features.feature_time_utc
                if sui_features is not None
                else (ensure_utc(now_utc, "now_utc") if now_utc is not None else datetime.now().astimezone())
            )
            valid_until = ref_time + ttl_delta
            decision_id = self._make_decision_id(ref_time, RegimeType.DATA_UNHEALTHY, unhealthy_reasons)
            probs = {
                RegimeType.UP_TREND: Decimal("0.000000"),
                RegimeType.CONSOLIDATION: Decimal("0.000000"),
                RegimeType.DOWN_TREND: Decimal("0.000000"),
                RegimeType.HIGH_VOLATILITY: Decimal("0.000000"),
                RegimeType.DATA_UNHEALTHY: Decimal("1.000000"),
            }
            decision = RegimeDecision(
                decision_id=decision_id,
                regime=RegimeType.DATA_UNHEALTHY,
                confidence=Decimal("1.000000"),
                allow_entry=False,
                reason_codes=tuple(["DATA_UNHEALTHY", *unhealthy_reasons]),
                valid_until=valid_until,
                model_version=cfg.model_version,
            )
            diag = A01RegimeDiagnostics(
                trend_strength=Decimal("0"),
                volatility_ratio=Decimal("0"),
                btc_ret_1=Decimal("0"),
                btc_ret_3=Decimal("0"),
                btc_ret_6=Decimal("0"),
                btc_veto_triggered=False,
                btc_veto_windows=(),
                regime_probabilities=probs,
                feature_snapshot_hash="UNHEALTHY",
            )
            return decision, diag

        assert sui_features is not None
        f = sui_features.features
        feat_hash = compute_feature_snapshot_hash(sui_features)
        ref_time = sui_features.feature_time_utc
        valid_until = ref_time + ttl_delta

        ret_3 = require_decimal(f["ret_3"], "ret_3")
        ret_6 = require_decimal(f["ret_6"], "ret_6")
        ret_12 = require_decimal(f["ret_12"], "ret_12")
        ret_20 = require_decimal(f["ret_20"], "ret_20")
        atr_14_pct = require_decimal(f["atr_14_pct"], "atr_14_pct")
        realized_vol_20 = require_decimal(f["realized_vol_20"], "realized_vol_20")
        er_20 = require_decimal(f["er_20"], "er_20")
        ema_spread_pct = require_decimal(f["ema_spread_pct"], "ema_spread_pct")
        ret_15m_1 = require_decimal(f.get("ret_15m_1", ret_3), "ret_15m_1")
        ret_1h_1 = require_decimal(f.get("ret_1h_1", ret_12), "ret_1h_1")

        # Extract BTC returns across explicit 5m (1 bar), 15m (3 bars), and 30m (6 bars) windows
        if btc_features is not None:
            bf = btc_features.features
            btc_ret_1 = require_decimal(bf.get("ret_1", bf.get("btc_ret_1", Decimal("0"))), "btc_ret_1")
            btc_ret_3 = require_decimal(bf.get("ret_3", bf.get("btc_ret_3", Decimal("0"))), "btc_ret_3")
            btc_ret_6 = require_decimal(bf.get("ret_6", bf.get("btc_ret_6", Decimal("0"))), "btc_ret_6")
        else:
            btc_ret_1 = require_decimal(f.get("btc_ret_5m", f.get("btc_ret_1", Decimal("0"))), "btc_ret_1")
            btc_ret_3 = require_decimal(f.get("btc_ret_15m", f.get("btc_ret_3", Decimal("0"))), "btc_ret_3")
            btc_ret_6 = require_decimal(f.get("btc_ret_30m", f.get("btc_ret_6", Decimal("0"))), "btc_ret_6")

        veto_windows: list[str] = []
        if btc_ret_1 <= cfg.btc_flash_drop_1bar_threshold:
            veto_windows.append("BTC_DROP_5M_LE_MINUS_80BPS")
        if btc_ret_3 <= cfg.btc_flash_drop_3bar_threshold:
            veto_windows.append("BTC_DROP_15M_LE_MINUS_150BPS")
        if btc_ret_6 <= cfg.btc_flash_drop_6bar_threshold:
            veto_windows.append("BTC_DROP_30M_LE_MINUS_220BPS")

        raw_btc_drop = len(veto_windows) > 0
        btc_veto = bool(cfg.enable_btc_flash_drop_filter and raw_btc_drop)

        # 2. Mathematical Trend Strength & Volatility Normalization
        vol_scale = max(atr_14_pct, Decimal("0.0025"))
        norm_ret_6 = ret_6 / (vol_scale * Decimal("2.0"))
        norm_ret_12 = ret_12 / (vol_scale * Decimal("3.0"))
        norm_ret_20 = ret_20 / (vol_scale * Decimal("4.0"))
        norm_ema = ema_spread_pct / vol_scale
        norm_mtf = (ret_15m_1 + ret_1h_1) / (vol_scale * Decimal("3.5"))
        dir_sign = Decimal("1") if ret_20 >= Decimal("0") else Decimal("-1")

        trend_strength = _q6(
            Decimal("0.25") * norm_ret_6
            + Decimal("0.25") * norm_ret_12
            + Decimal("0.20") * norm_ret_20
            + Decimal("0.15") * norm_ema
            + Decimal("0.10") * (dir_sign * er_20 * Decimal("2.0"))
            + Decimal("0.05") * norm_mtf
        )

        vol_ratio_atr = atr_14_pct / cfg.high_volatility_atr_pct_threshold
        vol_ratio_std = realized_vol_20 / cfg.high_volatility_ret_std_threshold
        volatility_ratio = _q6(max(vol_ratio_atr, vol_ratio_std))

        # 3. Calibrated Softmax Probability Distribution over the 4 healthy-data regimes
        temp = float(cfg.softmax_temperature)
        ts_f = float(trend_strength)
        vr_f = float(volatility_ratio)
        er_f = float(er_20)

        logit_up = (1.8 * max(0.0, ts_f) + 0.8 * er_f - 0.6 * max(0.0, vr_f - 0.8)) / temp
        logit_down = (1.9 * max(0.0, -ts_f) + (0.9 if btc_veto else 0.0) - 0.4 * max(0.0, vr_f - 0.9)) / temp
        logit_consol = (1.3 * (1.0 - min(1.0, abs(ts_f))) + 0.9 * (1.0 - er_f) - 0.8 * max(0.0, vr_f - 0.85)) / temp
        logit_high_vol = (3.2 * (vr_f - 0.92)) / temp

        max_logit = max(logit_up, logit_down, logit_consol, logit_high_vol)
        exp_up = math.exp(logit_up - max_logit)
        exp_down = math.exp(logit_down - max_logit)
        exp_consol = math.exp(logit_consol - max_logit)
        exp_hv = math.exp(logit_high_vol - max_logit)
        denom = exp_up + exp_down + exp_consol + exp_hv

        p_up = _q6(Decimal(str(exp_up / denom)))
        p_down = _q6(Decimal(str(exp_down / denom)))
        p_hv = _q6(Decimal(str(exp_hv / denom)))
        p_consol = Decimal("1.000000") - (p_up + p_down + p_hv)
        if p_consol < Decimal("0"):
            p_consol = Decimal("0.000000")

        probs: dict[RegimeType, Decimal] = {
            RegimeType.UP_TREND: p_up,
            RegimeType.CONSOLIDATION: p_consol,
            RegimeType.DOWN_TREND: p_down,
            RegimeType.HIGH_VOLATILITY: p_hv,
            RegimeType.DATA_UNHEALTHY: Decimal("0.000000"),
        }

        # 4. Regime Selection & Hard Entry Gate Rules
        reason_codes: list[str] = []
        is_extreme_vol = (
            atr_14_pct >= cfg.high_volatility_atr_pct_threshold
            or realized_vol_20 >= cfg.high_volatility_ret_std_threshold
        )

        if is_extreme_vol:
            regime = RegimeType.HIGH_VOLATILITY
            confidence = max(p_hv, min(Decimal("0.990000"), _q6(volatility_ratio / Decimal("1.5"))))
            allow_entry = False
            reason_codes.append("HIGH_VOLATILITY_BLOCK")
            if atr_14_pct >= cfg.high_volatility_atr_pct_threshold:
                reason_codes.append("ATR_PCT_EXCEEDED")
            if realized_vol_20 >= cfg.high_volatility_ret_std_threshold:
                reason_codes.append("REALIZED_VOL_EXCEEDED")
        elif (
            trend_strength <= cfg.down_trend_score_threshold
            or ret_12 <= cfg.down_trend_ret_12_threshold
            or (btc_veto and ret_3 < Decimal("0"))
        ):
            regime = RegimeType.DOWN_TREND
            confidence = max(p_down, Decimal("0.550000"))
            allow_entry = False
            reason_codes.append("DOWN_TREND_BLOCK")
            if ret_12 <= cfg.down_trend_ret_12_threshold:
                reason_codes.append("NEGATIVE_1H_MOMENTUM")
        elif trend_strength >= cfg.up_trend_score_threshold and ema_spread_pct >= Decimal("0"):
            regime = RegimeType.UP_TREND
            confidence = max(p_up, Decimal("0.550000"))
            allow_entry = True
            reason_codes.append("UP_TREND_CONFIRMED")
        else:
            regime = RegimeType.CONSOLIDATION
            confidence = max(p_consol, Decimal("0.500000"))
            allow_entry = cfg.allow_consolidation_breakout_entry
            reason_codes.append("CONSOLIDATION_RANGE")

        # 5. Enforce BTCUSDT Short-Window Flash Crash Veto (Task A.7)
        if btc_veto:
            allow_entry = False
            if "BTC_FLASH_DROP_VETO" not in reason_codes:
                reason_codes.append("BTC_FLASH_DROP_VETO")
            for w_tag in veto_windows:
                reason_codes.append(w_tag)

        # 6. Enforce Minimum Confidence Gate
        if allow_entry and confidence < cfg.min_entry_confidence:
            allow_entry = False
            reason_codes.append("LOW_REGIME_CONFIDENCE")

        if allow_entry:
            reason_codes.append("REGIME_ENTRY_PERMITTED")

        decision_id = self._make_decision_id(ref_time, regime, reason_codes)
        decision = RegimeDecision(
            decision_id=decision_id,
            regime=regime,
            confidence=_q6(min(Decimal("1"), max(Decimal("0"), confidence))),
            allow_entry=allow_entry,
            reason_codes=tuple(reason_codes),
            valid_until=valid_until,
            model_version=cfg.model_version,
        )
        diag = A01RegimeDiagnostics(
            trend_strength=trend_strength,
            volatility_ratio=volatility_ratio,
            btc_ret_1=btc_ret_1,
            btc_ret_3=btc_ret_3,
            btc_ret_6=btc_ret_6,
            btc_veto_triggered=btc_veto,
            btc_veto_windows=tuple(veto_windows),
            regime_probabilities=probs,
            feature_snapshot_hash=feat_hash,
        )
        return decision, diag

    def _make_decision_id(
        self,
        ref_time: datetime,
        regime: RegimeType,
        reasons: list[str],
    ) -> str:
        raw = f"{self.config.model_version}|{ref_time.isoformat()}|{regime.value}|{','.join(reasons)}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"REG-{digest}"
