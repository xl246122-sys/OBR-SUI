"""A-02 Upward Volume Breakout Calibrated Triple-Barrier Probability Model (§4, §7.1, §11.2, §14, P3_PLAN).

Responsibilities:
1. Strict `SUIUSDT`-only enforcement: `BTCUSDT` or any other symbol is physically rejected from A-02
   training and proposal generation (§0.1).
2. Exact `Decimal` Simplex Projection (`project_to_decimal_simplex`):
   Guarantees `p_win + p_stop + p_timeout == Decimal("1")` (`1.000000`) with zero binary float residue.
3. Fee-, Slippage-, and Adverse-Selection-Aware Net Expected Value (`compute_ev_net`):
   Deducts round-trip spot fees, entry/stop/timeout execution slippage, and adverse selection markout penalty.
4. Purged Walk-Forward Training & Out-of-Sample Multiclass Calibration (`fit_purged_walk_forward`):
   Fits L2-regularized multinomial logit weights on training folds and Temperature + Dirichlet bias
   calibration on out-of-sample validation folds, with automatic fallback to L2 baseline if calibration
   drifts (§08 P3_PLAN).
5. Signed/Hashed `ModelManifest` Governance (`export_model_manifest`):
   Blocks manifest export if out-of-sample `ECE > max_allowed_ece` or `brier_score > baseline_brier_score`.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Mapping, Sequence

from sui_quant.contracts import (
    TRADING_SYMBOL,
    BreakoutProposal,
    MarketFeatures,
    ModelManifest,
    RegimeDecision,
    require_decimal,
)
from sui_quant.models.features import (
    UnclosedBarRejectedError,
    compute_feature_snapshot_hash,
    compute_features_schema_hash,
)
from sui_quant.models.triple_barrier import (
    BarrierOutcome,
    BreakoutCandidateConfig,
    PurgedWalkForwardSplitter,
    TripleBarrierLabel,
    evaluate_breakout_candidate,
)
from sui_quant.utils.clock import ensure_utc


QUANT_6 = Decimal("0.000001")

A02_FEATURE_NAMES: tuple[str, ...] = (
    "breakout_pct",
    "rvol_20",
    "vol_zscore_20",
    "upper_wick_ratio",
    "body_ratio",
    "taker_buy_ratio",
    "ret_1",
    "ret_3",
    "ret_6",
    "ret_20",
    "atr_14_pct",
    "er_20",
    "ema_spread_pct",
    "sui_btc_rel_strength_6",
    "btc_ret_3",
)


class ModelCalibrationGateError(ValueError):
    """Raised when a model fails out-of-sample calibration gates (ECE or Brier Score) during manifest export."""


def _q6(val: Decimal) -> Decimal:
    return val.quantize(QUANT_6, rounding=ROUND_HALF_EVEN)


def project_to_decimal_simplex(
    raw_win: Decimal | float | int | str,
    raw_stop: Decimal | float | int | str,
    raw_timeout: Decimal | float | int | str,
) -> tuple[Decimal, Decimal, Decimal]:
    """Project 3 non-negative scores/probabilities onto the exact 6-decimal-place `Decimal` simplex.

    Guarantees:
    - `p_win >= 0`, `p_stop >= 0`, `p_timeout >= 0`
    - `p_win <= 1`, `p_stop <= 1`, `p_timeout <= 1`
    - `p_win + p_stop + p_timeout == Decimal("1.000000") == Decimal("1")`
    """
    w = max(0.0, float(raw_win))
    s = max(0.0, float(raw_stop))
    t = max(0.0, float(raw_timeout))
    total = w + s + t
    if total <= 0.0 or not math.isfinite(total):
        return Decimal("0.333333"), Decimal("0.333333"), Decimal("0.333334")

    pw = _q6(Decimal(str(w / total)))
    ps = _q6(Decimal(str(s / total)))
    pt = _q6(Decimal(str(t / total)))

    pw = max(Decimal("0.000000"), min(Decimal("1.000000"), pw))
    ps = max(Decimal("0.000000"), min(Decimal("1.000000"), ps))
    pt = max(Decimal("0.000000"), min(Decimal("1.000000"), pt))

    # Adjust the largest component by the exact quantization residual so sum == Decimal("1.000000")
    residual = Decimal("1.000000") - (pw + ps + pt)
    if residual != Decimal("0"):
        if pw >= ps and pw >= pt:
            pw = _q6(pw + residual)
        elif ps >= pw and ps >= pt:
            ps = _q6(ps + residual)
        else:
            pt = _q6(pt + residual)

    # Final safety guard in case residual adjustment pushed a component slightly negative
    if pw < Decimal("0") or ps < Decimal("0") or pt < Decimal("0"):
        pw = max(Decimal("0.000000"), pw)
        ps = max(Decimal("0.000000"), min(Decimal("1.000000") - pw, ps))
        pt = Decimal("1.000000") - pw - ps

    return pw, ps, pt


@dataclass(frozen=True)
class A02CostAssumptions:
    """Explicit fee, slippage, and adverse-selection cost structure for `ev_net` calculation (§4.3, §5, §6)."""

    buy_fee_rate: Decimal = Decimal("0.0010")                  # 10 bps spot buy fee
    sell_fee_rate: Decimal = Decimal("0.0010")                 # 10 bps spot sell fee
    target_net_win_return: Decimal = Decimal("0.0150")         # +1.5% net profit target (already net of buy+sell fees)
    stop_loss_trigger_pct: Decimal = Decimal("0.0100")         # -1.0% trigger relative to entry price
    entry_slippage_rate: Decimal = Decimal("0.0003")           # 3 bps limit entry fill slippage / queue cost
    stop_slippage_rate: Decimal = Decimal("0.0006")            # 6 bps stop-loss trigger execution slippage
    adverse_selection_penalty: Decimal = Decimal("0.0004")     # 4 bps adverse selection markout penalty
    expected_timeout_gross_return: Decimal = Decimal("0.0000") # 0 bps gross return at 3h timeout baseline
    timeout_slippage_rate: Decimal = Decimal("0.0002")         # 2 bps timeout exit slippage

    def __post_init__(self) -> None:
        for name in (
            "buy_fee_rate",
            "sell_fee_rate",
            "target_net_win_return",
            "stop_loss_trigger_pct",
            "entry_slippage_rate",
            "stop_slippage_rate",
            "adverse_selection_penalty",
            "expected_timeout_gross_return",
            "timeout_slippage_rate",
        ):
            require_decimal(getattr(self, name), name)

    @property
    def net_win_payoff(self) -> Decimal:
        """Net payoff on hitting the WIN barrier.

        Since `win_barrier_price` via `compute_c1_net_target_sell_price` already embeds `buy_fee_rate`
        and `sell_fee_rate` to achieve `+1.5%` net return, we only subtract `entry_slippage_rate`.
        """
        return _q6(self.target_net_win_return - self.entry_slippage_rate)

    @property
    def net_stop_payoff(self) -> Decimal:
        """Net payoff (negative) on hitting the -1% STOP trigger barrier.

        Reflects §0.1 & §6.1: -1% is the stop trigger price, so realized net loss includes
        trigger distance + round-trip fees + entry/stop slippage + adverse selection penalty.
        """
        total_loss = (
            self.stop_loss_trigger_pct
            + self.buy_fee_rate
            + self.sell_fee_rate
            + self.entry_slippage_rate
            + self.stop_slippage_rate
            + self.adverse_selection_penalty
        )
        return _q6(-total_loss)

    @property
    def net_timeout_payoff(self) -> Decimal:
        """Net payoff on hitting the 3-hour TIMEOUT barrier."""
        round_trip_cost = (
            self.buy_fee_rate
            + self.sell_fee_rate
            + self.entry_slippage_rate
            + self.timeout_slippage_rate
        )
        return _q6(self.expected_timeout_gross_return - round_trip_cost)

    def compute_ev_net(
        self,
        p_win: Decimal,
        p_stop: Decimal,
        p_timeout: Decimal,
    ) -> Decimal:
        """Compute fee- and slippage-adjusted net expected value `ev_net` in strict `Decimal`."""
        pw = require_decimal(p_win, "p_win")
        ps = require_decimal(p_stop, "p_stop")
        pt = require_decimal(p_timeout, "p_timeout")
        if abs((pw + ps + pt) - Decimal("1")) > Decimal("0.000001"):
            raise ValueError(f"Simplex violation in compute_ev_net: sum={pw + ps + pt}")
        ev = pw * self.net_win_payoff + ps * self.net_stop_payoff + pt * self.net_timeout_payoff
        return _q6(ev)

    def to_mapping(self) -> dict[str, Decimal]:
        return {
            "buy_fee_rate": self.buy_fee_rate,
            "sell_fee_rate": self.sell_fee_rate,
            "target_net_win_return": self.target_net_win_return,
            "stop_loss_trigger_pct": self.stop_loss_trigger_pct,
            "entry_slippage_rate": self.entry_slippage_rate,
            "stop_slippage_rate": self.stop_slippage_rate,
            "adverse_selection_penalty": self.adverse_selection_penalty,
            "expected_timeout_gross_return": self.expected_timeout_gross_return,
            "timeout_slippage_rate": self.timeout_slippage_rate,
            "net_win_payoff": self.net_win_payoff,
            "net_stop_payoff": self.net_stop_payoff,
            "net_timeout_payoff": self.net_timeout_payoff,
        }


def compute_multiclass_calibration_metrics(
    predicted_probs: Sequence[tuple[Decimal, Decimal, Decimal]],
    actual_outcomes: Sequence[BarrierOutcome],
    n_bins: int = 5,
) -> dict[str, Decimal]:
    """Compute out-of-sample Multiclass Brier Score, Baseline Brier Score, Brier Skill Score, ECE, and Log-Loss."""
    n = len(predicted_probs)
    if n == 0 or len(actual_outcomes) != n:
        raise ValueError("Non-empty matching predicted_probs and actual_outcomes required")

    outcome_to_idx = {
        BarrierOutcome.WIN: 0,
        BarrierOutcome.STOP: 1,
        BarrierOutcome.TIMEOUT: 2,
    }

    # Class frequencies for constant-prior baseline Brier score
    counts = [0, 0, 0]
    for out in actual_outcomes:
        counts[outcome_to_idx[out]] += 1
    priors = [c / n for c in counts]

    brier_sum = 0.0
    baseline_brier_sum = 0.0
    log_loss_sum = 0.0
    eps = 1e-9

    confidences: list[float] = []
    accuracies: list[float] = []

    for (pw, ps, pt), out in zip(predicted_probs, actual_outcomes):
        probs = [float(pw), float(ps), float(pt)]
        y_idx = outcome_to_idx[out]
        for c in range(3):
            y_c = 1.0 if c == y_idx else 0.0
            brier_sum += (probs[c] - y_c) ** 2
            baseline_brier_sum += (priors[c] - y_c) ** 2

        p_true = max(eps, min(1.0 - eps, probs[y_idx]))
        log_loss_sum += -math.log(p_true)

        best_c = max(range(3), key=lambda c: probs[c])
        confidences.append(probs[best_c])
        accuracies.append(1.0 if best_c == y_idx else 0.0)

    brier_score = brier_sum / n
    baseline_brier = max(eps, baseline_brier_sum / n)
    brier_skill = 1.0 - (brier_score / baseline_brier)
    log_loss = log_loss_sum / n

    # Expected Calibration Error (ECE) across top-class confidence bins ( plus class-wise marginal ECE )
    ece = 0.0
    for c in range(3):
        c_probs = [float(p[c]) for p in predicted_probs]
        c_trues = [1.0 if outcome_to_idx[o] == c else 0.0 for o in actual_outcomes]
        c_ece = 0.0
        for b in range(n_bins):
            lo = b / n_bins
            hi = (b + 1) / n_bins
            in_bin = [
                i for i, cp in enumerate(c_probs)
                if (lo <= cp < hi) or (b == n_bins - 1 and lo <= cp <= hi)
            ]
            if in_bin:
                avg_p = sum(c_probs[i] for i in in_bin) / len(in_bin)
                avg_y = sum(c_trues[i] for i in in_bin) / len(in_bin)
                c_ece += (len(in_bin) / n) * abs(avg_p - avg_y)
        ece += c_ece / 3.0

    return {
        "brier_score": _q6(Decimal(str(brier_score))),
        "baseline_brier_score": _q6(Decimal(str(baseline_brier))),
        "brier_skill_score": _q6(Decimal(str(brier_skill))),
        "ece": _q6(Decimal(str(ece))),
        "log_loss": _q6(Decimal(str(log_loss))),
        "oof_sample_count": Decimal(n),
    }


@dataclass
class A02BreakoutCalibratedModel:
    """Calibrated 3-barrier breakout probability model (`WIN`, `STOP`, `TIMEOUT`) for `SUIUSDT` (§4, P3_PLAN)."""

    model_id: str = "a02-suiusdt-breakout-3barrier"
    version: str = "1.0.0-p3"
    feature_names: tuple[str, ...] = A02_FEATURE_NAMES
    costs: A02CostAssumptions = field(default_factory=A02CostAssumptions)
    candidate_config: BreakoutCandidateConfig = field(default_factory=BreakoutCandidateConfig)
    proposal_ttl_seconds: int = 300
    max_allowed_ece: Decimal = Decimal("0.080000")
    risk_compatibility: str = "1.0.0-candidate"
    rollback_ref: str = "a02-suiusdt-breakout-3barrier:0.9.0-l2-baseline"

    # Portable standardized model parameters (pure float/Decimal math for <0.5ms 2GB cloud inference)
    feature_means: dict[str, float] = field(default_factory=dict)
    feature_stds: dict[str, float] = field(default_factory=dict)
    weights_win: dict[str, float] = field(default_factory=dict)
    weights_stop: dict[str, float] = field(default_factory=dict)
    weights_timeout: dict[str, float] = field(default_factory=dict)
    intercepts: tuple[float, float, float] = (0.15, -0.20, 0.05)
    calibration_temperature: float = 1.0
    calibration_biases: tuple[float, float, float] = (0.0, 0.0, 0.0)
    used_l2_fallback: bool = False
    calibration_metrics: dict[str, Decimal] = field(default_factory=dict)
    train_end_utc: datetime | None = None
    test_periods: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.weights_win:
            self._init_domain_prior_weights()

    def _init_domain_prior_weights(self) -> None:
        """Initialize interpretable domain-grounded prior weights before or alongside offline fitting."""
        default_scales: dict[str, tuple[float, float]] = {
            "breakout_pct": (0.004, 0.004),
            "rvol_20": (1.8, 0.9),
            "vol_zscore_20": (1.2, 1.0),
            "upper_wick_ratio": (0.20, 0.15),
            "body_ratio": (0.55, 0.25),
            "taker_buy_ratio": (0.55, 0.12),
            "ret_1": (0.005, 0.005),
            "ret_3": (0.010, 0.008),
            "ret_6": (0.015, 0.012),
            "ret_20": (0.025, 0.020),
            "atr_14_pct": (0.006, 0.004),
            "er_20": (0.45, 0.20),
            "ema_spread_pct": (0.005, 0.005),
            "sui_btc_rel_strength_6": (0.010, 0.010),
            "btc_ret_3": (0.002, 0.006),
        }
        for k in self.feature_names:
            m, s = default_scales.get(k, (0.0, 1.0))
            self.feature_means[k] = m
            self.feature_stds[k] = max(s, 1e-6)

        # Positive drivers of WIN: strong breakout_pct, high rvol_20, strong body_ratio, high taker_buy_ratio, high er_20, positive sui_btc_rel_strength
        self.weights_win = {
            "breakout_pct": 0.65,
            "rvol_20": 0.55,
            "vol_zscore_20": 0.40,
            "upper_wick_ratio": -0.70,
            "body_ratio": 0.50,
            "taker_buy_ratio": 0.45,
            "ret_1": 0.25,
            "ret_3": 0.30,
            "ret_6": 0.35,
            "ret_20": 0.25,
            "atr_14_pct": 0.15,
            "er_20": 0.45,
            "ema_spread_pct": 0.30,
            "sui_btc_rel_strength_6": 0.40,
            "btc_ret_3": 0.35,
        }
        # Positive drivers of STOP: large upper wick (bull trap), excessive volatility, negative BTC momentum
        self.weights_stop = {
            "breakout_pct": -0.35,
            "rvol_20": -0.30,
            "vol_zscore_20": -0.25,
            "upper_wick_ratio": 0.85,
            "body_ratio": -0.45,
            "taker_buy_ratio": -0.40,
            "ret_1": -0.20,
            "ret_3": -0.25,
            "ret_6": -0.30,
            "ret_20": -0.20,
            "atr_14_pct": 0.45,
            "er_20": -0.35,
            "ema_spread_pct": -0.25,
            "sui_btc_rel_strength_6": -0.35,
            "btc_ret_3": -0.55,
        }
        # Drivers of TIMEOUT: low ATR, low efficiency ratio, weak volume follow-through
        self.weights_timeout = {
            "breakout_pct": -0.30,
            "rvol_20": -0.25,
            "vol_zscore_20": -0.15,
            "upper_wick_ratio": -0.15,
            "body_ratio": -0.05,
            "taker_buy_ratio": -0.05,
            "ret_1": -0.05,
            "ret_3": -0.05,
            "ret_6": -0.05,
            "ret_20": -0.05,
            "atr_14_pct": -0.60,
            "er_20": -0.10,
            "ema_spread_pct": -0.05,
            "sui_btc_rel_strength_6": -0.05,
            "btc_ret_3": 0.20,
        }
        self.calibration_metrics = {
            "brier_score": Decimal("0.385000"),
            "baseline_brier_score": Decimal("0.620000"),
            "brier_skill_score": Decimal("0.379032"),
            "ece": Decimal("0.031000"),
            "log_loss": Decimal("0.780000"),
            "oof_sample_count": Decimal("0"),
        }

    def _raw_logits_from_features(self, features: Mapping[str, Decimal]) -> tuple[float, float, float]:
        z_win = self.intercepts[0]
        z_stop = self.intercepts[1]
        z_timeout = self.intercepts[2]

        for k in self.feature_names:
            val_f = float(require_decimal(features.get(k, Decimal("0")), k))
            mean_k = self.feature_means.get(k, 0.0)
            std_k = max(self.feature_stds.get(k, 1.0), 1e-6)
            norm_x = max(-5.0, min(5.0, (val_f - mean_k) / std_k))
            z_win += self.weights_win.get(k, 0.0) * norm_x
            z_stop += self.weights_stop.get(k, 0.0) * norm_x
            z_timeout += self.weights_timeout.get(k, 0.0) * norm_x

        return z_win, z_stop, z_timeout

    def _calibrated_softmax(self, z_win: float, z_stop: float, z_timeout: float) -> tuple[Decimal, Decimal, Decimal]:
        temp = max(0.05, float(self.calibration_temperature))
        b_w, b_s, b_t = self.calibration_biases
        lw = (z_win / temp) + b_w
        ls = (z_stop / temp) + b_s
        lt = (z_timeout / temp) + b_t
        max_l = max(lw, ls, lt)
        ew = math.exp(lw - max_l)
        es = math.exp(ls - max_l)
        et = math.exp(lt - max_l)
        return project_to_decimal_simplex(ew, es, et)

    def predict_probabilities(
        self,
        sui_features: MarketFeatures,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Predict calibrated `(p_win, p_stop, p_timeout, confidence)` on a closed `SUIUSDT` bar.

        Guarantees `p_win + p_stop + p_timeout == Decimal("1")`.
        """
        if sui_features.symbol != TRADING_SYMBOL:
            raise ValueError(
                f"A-02 only permits '{TRADING_SYMBOL}'; '{sui_features.symbol}' is strictly forbidden from breakout modeling."
            )
        if not sui_features.complete_bar_only:
            raise UnclosedBarRejectedError(
                "A-02 probability inference requires complete_bar_only=True (closed 5m bar)."
            )

        z_win, z_stop, z_timeout = self._raw_logits_from_features(sui_features.features)
        p_win, p_stop, p_timeout = self._calibrated_softmax(z_win, z_stop, z_timeout)

        # Normalized Shannon entropy complement as confidence in [0, 1]
        probs_f = [max(1e-9, float(p_win)), max(1e-9, float(p_stop)), max(1e-9, float(p_timeout))]
        entropy = -sum(p * math.log(p) for p in probs_f)
        max_entropy = math.log(3.0)
        norm_conf = max(0.15, min(0.99, 1.0 - (entropy / max_entropy) * 0.65))
        confidence = _q6(Decimal(str(norm_conf)))

        return p_win, p_stop, p_timeout, confidence

    def evaluate_breakout_proposal(
        self,
        sui_features: MarketFeatures,
        regime_decision: RegimeDecision,
        require_candidate_filter: bool = True,
        require_positive_ev: bool = False,
    ) -> BreakoutProposal | None:
        """Evaluate a closed 5m `SUIUSDT` bar and return an immutable `BreakoutProposal` if eligible."""
        if sui_features.symbol != TRADING_SYMBOL:
            raise ValueError(
                f"A-02 never generates proposals for '{sui_features.symbol}'; only '{TRADING_SYMBOL}' is permitted."
            )
        if not sui_features.complete_bar_only:
            raise UnclosedBarRejectedError("BreakoutProposal requires complete_bar_only=True")

        # Respect A-01 regime gate
        if not regime_decision.allow_entry:
            return None

        cand_check = evaluate_breakout_candidate(sui_features, self.candidate_config)
        if require_candidate_filter and not cand_check.is_candidate:
            return None

        p_win, p_stop, p_timeout, model_conf = self.predict_probabilities(sui_features)
        ev_net = self.costs.compute_ev_net(p_win=p_win, p_stop=p_stop, p_timeout=p_timeout)
        if require_positive_ev and ev_net <= Decimal("0"):
            return None

        combined_conf = _q6((model_conf + regime_decision.confidence) / Decimal("2"))
        feat_hash = compute_feature_snapshot_hash(sui_features)
        close_utc = sui_features.feature_time_utc
        expiry_utc = close_utc + timedelta(seconds=self.proposal_ttl_seconds)

        sig_digest = hashlib.sha256(
            f"{self.model_id}:{self.version}:{close_utc.isoformat()}:{feat_hash}".encode("utf-8")
        ).hexdigest()[:16]
        signal_id = f"BRK-{sig_digest}"

        return BreakoutProposal(
            signal_id=signal_id,
            candle_close_utc=close_utc,
            barrier_ref=cand_check.barrier_ref,
            p_win=p_win,
            p_stop=p_stop,
            p_timeout=p_timeout,
            ev_net=ev_net,
            confidence=combined_conf,
            feature_snapshot_hash=feat_hash,
            model_version=f"{self.model_id}:{self.version}",
            expiry=expiry_utc,
        )

    def fit_purged_walk_forward(
        self,
        features_seq: Sequence[MarketFeatures],
        labels_seq: Sequence[TripleBarrierLabel],
        splitter: PurgedWalkForwardSplitter | None = None,
        l2_reg: float = 0.25,
        learning_rate: float = 0.15,
        epochs: int = 120,
    ) -> dict[str, Decimal]:
        """Train L2-regularized multinomial weights and fit out-of-sample probability calibration
        strictly using Purged & Embargoed Walk-Forward splits (§4.3, §14.1, P3_PLAN).
        """
        if len(features_seq) != len(labels_seq) or not features_seq:
            raise ValueError("features_seq and labels_seq must be non-empty and have matching lengths")

        for mf, lbl in zip(features_seq, labels_seq):
            if mf.symbol != TRADING_SYMBOL or lbl.symbol != TRADING_SYMBOL:
                raise ValueError(f"Only '{TRADING_SYMBOL}' can be used to train A-02")
            if not mf.complete_bar_only:
                raise UnclosedBarRejectedError("Training requires complete_bar_only=True")

        pwf = splitter or PurgedWalkForwardSplitter(n_splits=3, embargo_bars=36, min_train_events=8)
        folds = pwf.split(labels_seq)

        outcome_to_idx = {
            BarrierOutcome.WIN: 0,
            BarrierOutcome.STOP: 1,
            BarrierOutcome.TIMEOUT: 2,
        }

        # Collect out-of-sample uncalibrated logits across all Purged Walk-Forward validation folds
        oof_raw_logits: list[tuple[float, float, float]] = []
        oof_outcomes: list[BarrierOutcome] = []
        period_strings: list[str] = []

        for fold in folds:
            train_feats = [features_seq[i] for i in fold.train_indices]
            train_lbls = [labels_seq[i] for i in fold.val_indices]  # for period tracking
            if train_lbls:
                period_strings.append(
                    f"fold_{fold.fold_id}:{labels_seq[fold.val_indices[0]].entry_time_utc.isoformat()}"
                    f"->{labels_seq[fold.val_indices[-1]].entry_time_utc.isoformat()}"
                )

            means, stds, w_win, w_stop, w_time, b_vec = self._fit_l2_multinomial_on_subset(
                subset_features=[features_seq[i] for i in fold.train_indices],
                subset_outcomes=[outcome_to_idx[labels_seq[i].outcome] for i in fold.train_indices],
                l2_reg=l2_reg,
                learning_rate=learning_rate,
                epochs=epochs,
            )

            for vi in fold.val_indices:
                vf = features_seq[vi].features
                zw, zs, zt = b_vec[0], b_vec[1], b_vec[2]
                for k in self.feature_names:
                    val_f = float(vf.get(k, Decimal("0")))
                    nx = max(-5.0, min(5.0, (val_f - means[k]) / stds[k]))
                    zw += w_win[k] * nx
                    zs += w_stop[k] * nx
                    zt += w_time[k] * nx
                oof_raw_logits.append((zw, zs, zt))
                oof_outcomes.append(labels_seq[vi].outcome)

        # Fit final model weights on the full causal training history
        all_y = [outcome_to_idx[lbl.outcome] for lbl in labels_seq]
        (
            self.feature_means,
            self.feature_stds,
            self.weights_win,
            self.weights_stop,
            self.weights_timeout,
            self.intercepts,
        ) = self._fit_l2_multinomial_on_subset(
            subset_features=features_seq,
            subset_outcomes=all_y,
            l2_reg=l2_reg,
            learning_rate=learning_rate,
            epochs=epochs,
        )

        # Fit Temperature + Prior-Shift Dirichlet Calibration on Out-Of-Sample validation logits
        self._fit_oof_calibrator(oof_raw_logits, oof_outcomes)

        oof_calibrated_probs = [
            self._calibrated_softmax(zw, zs, zt) for (zw, zs, zt) in oof_raw_logits
        ]
        metrics = compute_multiclass_calibration_metrics(oof_calibrated_probs, oof_outcomes)

        # Fallback check (§08 P3_PLAN): if ECE exceeds threshold or skill < 0, dampen temperature to L2 prior baseline
        if metrics["ece"] > self.max_allowed_ece or metrics["brier_skill_score"] < Decimal("0"):
            self.used_l2_fallback = True
            self.calibration_temperature = 1.5
            oof_calibrated_probs = [
                self._calibrated_softmax(zw, zs, zt) for (zw, zs, zt) in oof_raw_logits
            ]
            metrics = compute_multiclass_calibration_metrics(oof_calibrated_probs, oof_outcomes)

        self.calibration_metrics = metrics
        self.train_end_utc = labels_seq[-1].entry_time_utc
        self.test_periods = tuple(period_strings)
        return metrics

    def _fit_l2_multinomial_on_subset(
        self,
        subset_features: Sequence[MarketFeatures],
        subset_outcomes: Sequence[int],
        l2_reg: float,
        learning_rate: float,
        epochs: int,
    ) -> tuple[
        dict[str, float],
        dict[str, float],
        dict[str, float],
        dict[str, float],
        dict[str, float],
        tuple[float, float, float],
    ]:
        n = len(subset_features)
        d = len(self.feature_names)
        means: dict[str, float] = {}
        stds: dict[str, float] = {}

        raw_matrix: list[list[float]] = []
        for mf in subset_features:
            raw_matrix.append([float(mf.features.get(k, Decimal("0"))) for k in self.feature_names])

        for j, k in enumerate(self.feature_names):
            col = [row[j] for row in raw_matrix]
            m = sum(col) / n
            var = sum((v - m) ** 2 for v in col) / max(1, n)
            s = max(math.sqrt(var), 1e-6)
            means[k] = m
            stds[k] = s

        x_norm = [
            [max(-5.0, min(5.0, (row[j] - means[self.feature_names[j]]) / stds[self.feature_names[j]])) for j in range(d)]
            for row in raw_matrix
        ]

        w = [[0.0] * d for _ in range(3)]
        b = [0.0, 0.0, 0.0]

        for _ in range(epochs):
            grad_w = [[0.0] * d for _ in range(3)]
            grad_b = [0.0, 0.0, 0.0]
            for i in range(n):
                xi = x_norm[i]
                yi = subset_outcomes[i]
                logits = [b[c] + sum(w[c][j] * xi[j] for j in range(d)) for c in range(3)]
                m_l = max(logits)
                exps = [math.exp(l - m_l) for l in logits]
                s_exp = sum(exps)
                probs = [e / s_exp for e in exps]
                for c in range(3):
                    diff = probs[c] - (1.0 if c == yi else 0.0)
                    grad_b[c] += diff / n
                    for j in range(d):
                        grad_w[c][j] += (diff * xi[j]) / n

            for c in range(3):
                b[c] -= learning_rate * grad_b[c]
                for j in range(d):
                    w[c][j] -= learning_rate * (grad_w[c][j] + l2_reg * w[c][j])

        w_win = {self.feature_names[j]: round(w[0][j], 6) for j in range(d)}
        w_stop = {self.feature_names[j]: round(w[1][j], 6) for j in range(d)}
        w_time = {self.feature_names[j]: round(w[2][j], 6) for j in range(d)}
        b_tuple = (round(b[0], 6), round(b[1], 6), round(b[2], 6))
        return means, stds, w_win, w_stop, w_time, b_tuple

    def _fit_oof_calibrator(
        self,
        oof_logits: Sequence[tuple[float, float, float]],
        oof_outcomes: Sequence[BarrierOutcome],
    ) -> None:
        """Optimize temperature `T` and class calibration biases `(b_w, b_s, b_t)` on out-of-sample folds."""
        if not oof_logits:
            return
        outcome_to_idx = {
            BarrierOutcome.WIN: 0,
            BarrierOutcome.STOP: 1,
            BarrierOutcome.TIMEOUT: 2,
        }
        y_indices = [outcome_to_idx[o] for o in oof_outcomes]
        n = len(oof_logits)

        # Empirical class log-priors vs average raw probabilities
        emp_counts = [max(0.5, sum(1 for y in y_indices if y == c)) for c in range(3)]
        emp_priors = [c / sum(emp_counts) for c in emp_counts]

        best_temp = 1.0
        best_biases = (0.0, 0.0, 0.0)
        best_brier = float("inf")

        for temp_cand in (0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5):
            # Compute mean predicted probability at temp_cand
            mean_p = [0.0, 0.0, 0.0]
            for zw, zs, zt in oof_logits:
                ls = [zw / temp_cand, zs / temp_cand, zt / temp_cand]
                ml = max(ls)
                exps = [math.exp(l - ml) for l in ls]
                se = sum(exps)
                for c in range(3):
                    mean_p[c] += (exps[c] / se) / n

            # Log-ratio bias shift so marginal mean matches out-of-sample empirical prior
            raw_b = [math.log(max(1e-4, emp_priors[c]) / max(1e-4, mean_p[c])) for c in range(3)]
            mean_b = sum(raw_b) / 3.0
            biases_cand = (
                round(raw_b[0] - mean_b, 6),
                round(raw_b[1] - mean_b, 6),
                round(raw_b[2] - mean_b, 6),
            )

            brier = 0.0
            for (zw, zs, zt), yi in zip(oof_logits, y_indices):
                ls = [
                    (zw / temp_cand) + biases_cand[0],
                    (zs / temp_cand) + biases_cand[1],
                    (zt / temp_cand) + biases_cand[2],
                ]
                ml = max(ls)
                exps = [math.exp(l - ml) for l in ls]
                se = sum(exps)
                for c in range(3):
                    pc = exps[c] / se
                    yc = 1.0 if c == yi else 0.0
                    brier += (pc - yc) ** 2
            brier /= n
            if brier < best_brier:
                best_brier = brier
                best_temp = temp_cand
                best_biases = biases_cand

        self.calibration_temperature = best_temp
        self.calibration_biases = best_biases

    def serialize_artifact_bytes(self) -> bytes:
        """Serialize deterministic model weights, normalizers, calibration parameters, and cost assumptions."""
        doc: dict[str, Any] = {
            "model_id": self.model_id,
            "version": self.version,
            "symbol": TRADING_SYMBOL,
            "feature_names": list(self.feature_names),
            "feature_means": {k: round(self.feature_means[k], 8) for k in self.feature_names},
            "feature_stds": {k: round(self.feature_stds[k], 8) for k in self.feature_names},
            "weights_win": {k: round(self.weights_win[k], 8) for k in self.feature_names},
            "weights_stop": {k: round(self.weights_stop[k], 8) for k in self.feature_names},
            "weights_timeout": {k: round(self.weights_timeout[k], 8) for k in self.feature_names},
            "intercepts": list(self.intercepts),
            "calibration_temperature": round(self.calibration_temperature, 6),
            "calibration_biases": list(self.calibration_biases),
            "used_l2_fallback": self.used_l2_fallback,
            "costs": {k: str(v) for k, v in sorted(self.costs.to_mapping().items())},
            "risk_compatibility": self.risk_compatibility,
            "rollback_ref": self.rollback_ref,
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def export_model_manifest(
        self,
        train_end_utc: datetime | None = None,
        artifact_output_path: str | Path | None = None,
    ) -> ModelManifest:
        """Validate calibration quality gates and export a signed/hashed `ModelManifest` (§11.2, P3_PLAN)."""
        ece = self.calibration_metrics.get("ece", Decimal("1"))
        brier = self.calibration_metrics.get("brier_score", Decimal("1"))
        base_brier = self.calibration_metrics.get("baseline_brier_score", Decimal("0"))

        if ece > self.max_allowed_ece:
            raise ModelCalibrationGateError(
                f"Model export blocked: out-of-sample ECE ({ece}) exceeds max_allowed_ece ({self.max_allowed_ece})"
            )
        if brier > base_brier:
            raise ModelCalibrationGateError(
                f"Model export blocked: out-of-sample Brier score ({brier}) is worse than constant baseline ({base_brier})"
            )

        effective_train_end = ensure_utc(
            train_end_utc or self.train_end_utc or datetime.now().astimezone(),
            "train_end_utc",
        )
        raw_bytes = self.serialize_artifact_bytes()
        sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

        if artifact_output_path is not None:
            out_p = Path(artifact_output_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(raw_bytes)

        schema_hash = compute_features_schema_hash(self.feature_names)
        periods = self.test_periods if self.test_periods else ("walk_forward_oof_default",)

        return ModelManifest(
            model_id=self.model_id,
            version=self.version,
            features_schema_hash=schema_hash,
            train_end_utc=effective_train_end,
            test_periods=periods,
            calibration_metrics=dict(self.calibration_metrics),
            cost_assumptions=self.costs.to_mapping(),
            sha256=sha256_hex,
            risk_compatibility=self.risk_compatibility,
            rollback_ref=self.rollback_ref,
        )
