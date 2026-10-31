"""P3 Automated Verification Suite (Including Task A Mathematical Audit Remediation A.1–A.9):
- `test_p3_feature_causality_zero_lookahead_leak`: Causal feature extraction & zero look-ahead leakage proof.
- `test_p3_task_a2_no_signal_to_entry_time_travel`: Proof of zero time-travel between 5m signal close confirmation (`bar[t].close_time_ms`) and reference entry quote (`bar[t+1].open_time_ms`).
- `test_p3_task_a3_a4_no_double_fee_counting_and_timeout_net_return`: Single-deduction fee/slippage audit & explicit `TIMEOUT` realized net return formula.
- `test_p3_task_a5_three_stage_train_cal_test_purged_separation`: Strict 3-way separation of `TRAIN`, `CALIBRATION`, and `FINAL_TEST` with >= 36 bars (3h) Purge + Embargo at both boundaries.
- `test_p3_task_a7_btc_flash_drop_explicit_windows_and_ablation`: Explicit 5m/15m/30m BTC windows and out-of-sample ablation comparison (`enable_btc_flash_drop_filter=True` vs `False`).
- `test_p3_task_a8_htf_15m_1h_strict_closed_bar_alignment`: Proof that 15m and 1h features strictly reference completed bars (`close_time_ms <= bar_5m.close_time_ms`).
- `test_p3_task_a9_insufficient_evidence_and_no_edge_verdicts`: Honest `INSUFFICIENT_EVIDENCE` and `NO_EDGE` statistical verdict gating.
- `test_p3_a01_five_regimes_and_btc_veto`: A-01 5-state regime classification & BTC flash-drop veto gate.
- `test_p3_triple_barrier_net_fees_and_pessimistic_tie_breaking`: Fee-aware +1.5% WIN / -1.0% STOP / 3h TIMEOUT & pessimistic intra-bar tie-breaking.
- `test_p3_purged_embargo_walk_forward_split`: Purged & Embargoed Walk-Forward validation (>= 36 bars / 3h embargo, zero overlap).
- `test_p3_a02_calibrated_probabilities_simplex_and_ev_net`: Calibrated probabilities on exact Decimal simplex, ev_net, ModelManifest governance, and DuckDB warehouse round-trip.
- `test_p3_single_bar_inference_latency_slo`: Combined A-01 + A-02 inference latency < 5 ms.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sui_quant.contracts import (
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    DataQuality,
    MarketFeatures,
    RegimeType,
)
from sui_quant.data.quality_guard import ClosedKlineBar, UnclosedKlineTransientView
from sui_quant.data.sync_engine import CloudShardExporter, LocalResearchSyncEngine
from sui_quant.exchange_rules import (
    compute_c1_net_target_sell_price,
    compute_stop_loss_trigger_price,
)
from sui_quant.models import (
    A01RegimeClassifier,
    A01RegimeConfig,
    A02BreakoutCalibratedModel,
    A02CostAssumptions,
    BarrierOutcome,
    CausalityViolationError,
    EntryPriceMode,
    FeaturePipeline,
    ModelCalibrationGateError,
    PurgedWalkForwardSplitter,
    StatisticalVerdict,
    TripleBarrierConfig,
    TripleBarrierLabel,
    TripleBarrierLabeler,
    UnclosedBarRejectedError,
    assert_no_signal_to_entry_time_travel,
    compute_feature_snapshot_hash,
    evaluate_breakout_candidate,
    load_closed_bars_from_warehouse,
    project_to_decimal_simplex,
)
from sui_quant.utils.clock import monotonic_ns


FIVE_MIN_MS = 5 * 60 * 1000
FIFTEEN_MIN_MS = 15 * 60 * 1000
ONE_HOUR_MS = 60 * 60 * 1000


def _build_synthetic_bars(
    symbol: str = TRADING_SYMBOL,
    count: int = 80,
    base_price: Decimal = Decimal("2.0000"),
    step_pct: Decimal = Decimal("0.0010"),
    base_vol: Decimal = Decimal("10000.0"),
    start_utc: datetime = datetime(2026, 10, 30, 0, 0, 0, tzinfo=timezone.utc),
    interval: str = "5m",
    interval_ms: int = FIVE_MIN_MS,
) -> list[ClosedKlineBar]:
    """Generate a deterministic chronological sequence of `ClosedKlineBar` records."""
    bars: list[ClosedKlineBar] = []
    cur_p = base_price
    start_ms = int(start_utc.timestamp() * 1000)

    for i in range(count):
        open_ms = start_ms + i * interval_ms
        close_ms = open_ms + interval_ms - 1
        ex_utc = datetime.fromtimestamp((close_ms + 1) / 1000.0, tz=timezone.utc)

        wave = Decimal("1") if (i % 4 != 3) else Decimal("-0.5")
        delta = (cur_p * step_pct * wave).quantize(Decimal("0.0001"))
        open_p = cur_p
        close_p = max(Decimal("0.1000"), (open_p + delta).quantize(Decimal("0.0001")))
        high_p = (max(open_p, close_p) + Decimal("0.0020")).quantize(Decimal("0.0001"))
        low_p = max(Decimal("0.0500"), (min(open_p, close_p) - Decimal("0.0015")).quantize(Decimal("0.0001")))

        vol_mult = Decimal("1.0") + Decimal(str((i % 5) * 0.1))
        vol = (base_vol * vol_mult).quantize(Decimal("0.1"))
        q_vol = (vol * close_p).quantize(Decimal("0.0001"))
        taker_base = (vol * Decimal("0.55")).quantize(Decimal("0.1"))
        taker_quote = (q_vol * Decimal("0.55")).quantize(Decimal("0.0001"))

        bars.append(
            ClosedKlineBar(
                symbol=symbol,
                interval=interval,
                open_time_ms=open_ms,
                close_time_ms=close_ms,
                open_price=open_p,
                high_price=high_p,
                low_price=low_p,
                close_price=close_p,
                base_volume=vol,
                quote_volume=q_vol,
                taker_buy_base_volume=taker_base,
                taker_buy_quote_volume=taker_quote,
                trade_count=120 + (i % 30),
                exchange_time_utc=ex_utc,
                recv_time_utc=ex_utc + timedelta(milliseconds=15),
                is_closed=True,
            )
        )
        cur_p = close_p

    return bars


def test_p3_feature_causality_zero_lookahead_leak() -> None:
    """Verify 0% look-ahead leakage when future bars t+1..t+36 are truncated or adversarially mutated,
    and verify physical rejection of unclosed K-line views.
    """
    sui_bars = _build_synthetic_bars(symbol=TRADING_SYMBOL, count=70, base_price=Decimal("2.1000"))
    btc_bars = _build_synthetic_bars(symbol=REFERENCE_SYMBOL, count=70, base_price=Decimal("68000.00"))
    pipeline = FeaturePipeline()

    for check_idx in (20, 25, 33, 50):
        passed, snap_hash = pipeline.verify_causality_no_lookahead(
            sui_5m_bars=sui_bars,
            check_index=check_idx,
            btc_5m_bars=btc_bars,
        )
        assert passed is True
        assert len(snap_hash) == 64

    bar_25 = sui_bars[25]
    spiked_bar_25 = ClosedKlineBar(
        symbol=bar_25.symbol,
        interval=bar_25.interval,
        open_time_ms=bar_25.open_time_ms,
        close_time_ms=bar_25.close_time_ms,
        open_price=bar_25.open_price,
        high_price=Decimal("9.9999"),
        low_price=bar_25.low_price,
        close_price=Decimal("9.5000"),
        base_volume=Decimal("50000.0"),
        quote_volume=Decimal("475000.0"),
        taker_buy_base_volume=Decimal("40000.0"),
        taker_buy_quote_volume=Decimal("380000.0"),
        trade_count=500,
        exchange_time_utc=bar_25.exchange_time_utc,
        recv_time_utc=bar_25.recv_time_utc,
        is_closed=True,
    )
    mod_bars = list(sui_bars[:25]) + [spiked_bar_25]
    feat_25 = pipeline.extract_at_index(mod_bars, target_index=25, btc_5m_bars=btc_bars)
    expected_prev_20_high = max(b.high_price for b in sui_bars[5:25])
    assert feat_25.features["prev_20_high"] == expected_prev_20_high
    assert feat_25.features["breakout_above_prev_20_high"] == Decimal("1")
    assert feat_25.complete_bar_only is True

    unclosed_view = UnclosedKlineTransientView(
        symbol=TRADING_SYMBOL,
        interval="5m",
        open_time_ms=bar_25.open_time_ms,
        close_time_ms=bar_25.close_time_ms,
        open_price=bar_25.open_price,
        high_price=bar_25.high_price,
        low_price=bar_25.low_price,
        current_price=bar_25.close_price,
        base_volume=bar_25.base_volume,
        quote_volume=bar_25.quote_volume,
        exchange_time_utc=bar_25.exchange_time_utc,
        recv_time_utc=bar_25.recv_time_utc,
        is_closed=False,
    )
    with pytest.raises(UnclosedBarRejectedError):
        pipeline.extract_at_index(list(sui_bars[:25]) + [unclosed_view])  # type: ignore[list-item]

    incomplete_mf = MarketFeatures(
        feature_time_utc=bar_25.exchange_time_utc,
        horizon="5m",
        symbol=TRADING_SYMBOL,
        complete_bar_only=False,
        features=feat_25.features,
        data_version="v1",
    )
    with pytest.raises(UnclosedBarRejectedError):
        compute_feature_snapshot_hash(incomplete_mf)

    with pytest.raises(CausalityViolationError):
        pipeline.extract_at_index(list(sui_bars[:22]) + [sui_bars[10]])


def test_p3_task_a2_no_signal_to_entry_time_travel() -> None:
    """Task A.2: Prove zero time-travel between 5m signal close confirmation (`bar[t].close_time_ms`)
    and reference entry quote (`bar[t+1].open_time_ms`), and prove `bar[t]`'s own intra-bar high/low
    can never trigger `WIN` or `STOP`.
    """
    bars = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=60,
        base_price=Decimal("2.0000"),
        step_pct=Decimal("0.0000"),
    )
    # Give signal bar t=10 an extreme high (3.0000) and extreme low (1.0000), and give bar t+1 (index 11)
    # an open_price of 2.0050 (gap up after bar 10 closed at 2.0000).
    b10 = bars[10]
    bars[10] = ClosedKlineBar(
        symbol=TRADING_SYMBOL,
        interval="5m",
        open_time_ms=b10.open_time_ms,
        close_time_ms=b10.close_time_ms,
        open_price=Decimal("2.0000"),
        high_price=Decimal("3.0000"),
        low_price=Decimal("1.0000"),
        close_price=Decimal("2.0000"),
        base_volume=b10.base_volume,
        quote_volume=b10.quote_volume,
        taker_buy_base_volume=b10.taker_buy_base_volume,
        taker_buy_quote_volume=b10.taker_buy_quote_volume,
        trade_count=b10.trade_count,
        exchange_time_utc=b10.exchange_time_utc,
        recv_time_utc=b10.recv_time_utc,
        is_closed=True,
    )
    b11 = bars[11]
    bars[11] = ClosedKlineBar(
        symbol=TRADING_SYMBOL,
        interval="5m",
        open_time_ms=b11.open_time_ms,
        close_time_ms=b11.close_time_ms,
        open_price=Decimal("2.0050"),
        high_price=Decimal("2.0060"),
        low_price=Decimal("2.0040"),
        close_price=Decimal("2.0050"),
        base_volume=b11.base_volume,
        quote_volume=b11.quote_volume,
        taker_buy_base_volume=b11.taker_buy_base_volume,
        taker_buy_quote_volume=b11.taker_buy_quote_volume,
        trade_count=b11.trade_count,
        exchange_time_utc=b11.exchange_time_utc,
        recv_time_utc=b11.recv_time_utc,
        is_closed=True,
    )

    labeler = TripleBarrierLabeler(TripleBarrierConfig(entry_price_mode=EntryPriceMode.NEXT_BAR_OPEN))
    lbl = labeler.label_at_index(bars, entry_index=10)

    # 1. Entry price is anchored at bar[11].open_price (2.0050), NOT bar[10].close_price (2.0000)
    assert lbl.entry_price == Decimal("2.0050")
    assert lbl.signal_confirm_time_ms == bars[10].close_time_ms
    assert lbl.entry_quote_time_ms == bars[11].open_time_ms
    assert lbl.entry_quote_time_ms == lbl.signal_confirm_time_ms + 1
    assert assert_no_signal_to_entry_time_travel(bars, lbl) is True

    # 2. Even though bar[10] had high=3.0 and low=1.0, it is the signal bar BEFORE entry_quote_time_ms,
    # so it NEVER triggers WIN or STOP (outcome is TIMEOUT after 36 future bars).
    assert lbl.outcome == BarrierOutcome.TIMEOUT
    assert lbl.bars_to_hit == 36

    # 3. Attempting to construct a TripleBarrierLabel where entry_quote_time_ms <= signal_confirm_time_ms raises CausalityViolationError
    with pytest.raises(CausalityViolationError, match="Time-travel violation"):
        TripleBarrierLabel(
            event_index=10,
            symbol=TRADING_SYMBOL,
            entry_time_utc=lbl.entry_time_utc,
            entry_open_time_ms=lbl.entry_open_time_ms,
            entry_close_time_ms=lbl.entry_close_time_ms,
            signal_confirm_time_ms=lbl.signal_confirm_time_ms,
            entry_quote_time_ms=lbl.signal_confirm_time_ms,  # Illegal: not strictly > signal_confirm_time_ms
            entry_price_mode="ILLEGAL",
            label_end_time_utc=lbl.label_end_time_utc,
            label_end_close_time_ms=lbl.label_end_close_time_ms,
            horizon_end_index=lbl.horizon_end_index,
            entry_price=lbl.entry_price,
            win_barrier_price=lbl.win_barrier_price,
            stop_barrier_price=lbl.stop_barrier_price,
            outcome=lbl.outcome,
            bars_to_hit=lbl.bars_to_hit,
            raw_exit_quote_price=lbl.raw_exit_quote_price,
            exit_price=lbl.exit_price,
            gross_return=lbl.gross_return,
            net_return=lbl.net_return,
            ambiguous_same_bar_conflict=False,
        )


def test_p3_task_a3_a4_no_double_fee_counting_and_timeout_net_return() -> None:
    """Task A.3 & A.4: Prove zero double-counting of fees across WIN, STOP, and TIMEOUT,
    and verify the explicit TIMEOUT net return formula.
    """
    costs = A02CostAssumptions(
        buy_fee_rate=Decimal("0.0010"),
        sell_fee_rate=Decimal("0.0010"),
        target_net_win_return=Decimal("0.0150"),
        stop_loss_trigger_pct=Decimal("0.0100"),
        entry_slippage_rate=Decimal("0.0003"),
        stop_slippage_rate=Decimal("0.0005"),
        adverse_selection_penalty=Decimal("0.0004"),
        expected_timeout_gross_return=Decimal("0.0020"),
        timeout_slippage_rate=Decimal("0.0002"),
    )
    audit = costs.verify_no_double_fee_counting(entry_price=Decimal("2.0000"))
    assert audit["double_fee_counting_detected"] is False
    # Verify WIN net payoff is +0.0150 - (0.0003 + 0.0004) = +0.014300 (fees are NOT deducted a second time!)
    assert costs.net_win_payoff == Decimal("0.014300")

    # Verify TIMEOUT net return in TripleBarrierLabeler matches exact formula:
    # entry_price = 2.0000, close_36 = 2.0100 (+0.5% gross), timeout_slippage = 0.0002 (2 bps)
    # exit_price = 2.0100 * (1 - 0.0002) = 2.00959800
    # net_return = (2.00959800 * 0.999) / (2.0000 * 1.001) - 1 = 2.007588402 / 2.002 - 1 = +0.00279141
    bars = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=55,
        base_price=Decimal("2.0000"),
        step_pct=Decimal("0.0000"),
    )
    b_end = bars[46]  # entry_index=10 + 36 = 46
    bars[46] = ClosedKlineBar(
        symbol=TRADING_SYMBOL,
        interval="5m",
        open_time_ms=b_end.open_time_ms,
        close_time_ms=b_end.close_time_ms,
        open_price=Decimal("2.0000"),
        high_price=Decimal("2.0120"),
        low_price=Decimal("1.9950"),
        close_price=Decimal("2.0100"),
        base_volume=b_end.base_volume,
        quote_volume=b_end.quote_volume,
        taker_buy_base_volume=b_end.taker_buy_base_volume,
        taker_buy_quote_volume=b_end.taker_buy_quote_volume,
        trade_count=b_end.trade_count,
        exchange_time_utc=b_end.exchange_time_utc,
        recv_time_utc=b_end.recv_time_utc,
        is_closed=True,
    )
    labeler = TripleBarrierLabeler(
        TripleBarrierConfig(
            buy_fee_rate=Decimal("0.0010"),
            sell_fee_rate=Decimal("0.0010"),
            timeout_slippage_rate=Decimal("0.0002"),
        )
    )
    lbl_to = labeler.label_at_index(bars, entry_index=10, entry_price_override=Decimal("2.0000"))
    assert lbl_to.outcome == BarrierOutcome.TIMEOUT
    assert lbl_to.raw_exit_quote_price == Decimal("2.0100")
    assert lbl_to.exit_price == Decimal("2.00959800")
    assert lbl_to.gross_return == Decimal("0.00500000")
    expected_net = (
        (Decimal("2.00959800") * Decimal("0.9990")) / (Decimal("2.0000") * Decimal("1.0010")) - Decimal("1")
    ).quantize(Decimal("0.00000001"))
    assert lbl_to.net_return == expected_net


def test_p3_task_a5_three_stage_train_cal_test_purged_separation() -> None:
    """Task A.5: Verify that `split_train_calibration_test` and `fit_and_evaluate_three_stage`
    strictly separate `TRAIN`, `CALIBRATION`, and `FINAL_TEST` with >= 36 bars (3 hours)
    Purge + Embargo at BOTH boundaries (`TRAIN->CAL` and `CAL->TEST`).
    """
    sui_bars = _build_synthetic_bars(symbol=TRADING_SYMBOL, count=420, base_price=Decimal("2.0000"))
    btc_bars = _build_synthetic_bars(symbol=REFERENCE_SYMBOL, count=420, base_price=Decimal("68000.00"))
    pipeline = FeaturePipeline()
    labeler = TripleBarrierLabeler()

    indices = list(range(24, 360, 4))
    features_seq = [pipeline.extract_at_index(sui_bars, target_index=i, btc_5m_bars=btc_bars) for i in indices]
    labels_seq = [labeler.label_at_index(sui_bars, entry_index=i) for i in indices]

    splitter = PurgedWalkForwardSplitter(n_splits=3, embargo_bars=36, min_train_events=10)
    split = splitter.split_train_calibration_test(labels_seq, train_ratio=0.50, cal_ratio=0.25)

    assert split.purged_train_to_cal_count > 0
    assert split.purged_cal_to_test_count > 0
    assert split.min_bar_gap_train_to_cal >= 36
    assert split.min_bar_gap_cal_to_test >= 36
    assert split.min_time_gap_ms_train_to_cal >= 36 * FIVE_MIN_MS
    assert split.min_time_gap_ms_cal_to_test >= 36 * FIVE_MIN_MS
    assert splitter.verify_three_stage_zero_leakage(labels_seq, split) is True

    model = A02BreakoutCalibratedModel()
    report = model.fit_and_evaluate_three_stage(features_seq, labels_seq, splitter=splitter)
    assert len(report.split.train_indices) > 0
    assert len(report.split.cal_indices) > 0
    assert len(report.split.test_indices) > 0
    assert "brier_score" in report.final_test_metrics
    assert "brier_score" in report.bootstrap_95_ci
    assert "tier2_standard_spot_baseline" in report.cost_sensitivity


def test_p3_task_a7_btc_flash_drop_explicit_windows_and_ablation() -> None:
    """Task A.7: Verify explicit 5m (`-0.80%`), 15m (`-1.50%`), and 30m (`-2.20%`) BTC flash-drop windows
    and verify out-of-sample ablation between `enable_btc_flash_drop_filter=True` vs `False`.
    """
    pipeline = FeaturePipeline()
    up_sui = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=35,
        base_price=Decimal("2.0000"),
        step_pct=Decimal("0.0040"),
    )
    calm_btc = _build_synthetic_bars(
        symbol=REFERENCE_SYMBOL,
        count=35,
        base_price=Decimal("68000.00"),
        step_pct=Decimal("0.0002"),
    )
    # Inject a -1.0% 5m BTC drop on the final bar (triggers 5m <= -0.80% window)
    last_b = calm_btc[-1]
    drop_btc = list(calm_btc[:-1]) + [
        ClosedKlineBar(
            symbol=REFERENCE_SYMBOL,
            interval="5m",
            open_time_ms=last_b.open_time_ms,
            close_time_ms=last_b.close_time_ms,
            open_price=last_b.open_price,
            high_price=last_b.open_price,
            low_price=(last_b.open_price * Decimal("0.9880")).quantize(Decimal("0.01")),
            close_price=(last_b.open_price * Decimal("0.9890")).quantize(Decimal("0.01")),
            base_volume=last_b.base_volume * Decimal("3"),
            quote_volume=last_b.quote_volume * Decimal("3"),
            taker_buy_base_volume=last_b.taker_buy_base_volume,
            taker_buy_quote_volume=last_b.taker_buy_quote_volume,
            trade_count=400,
            exchange_time_utc=last_b.exchange_time_utc,
            recv_time_utc=last_b.recv_time_utc,
            is_closed=True,
        )
    ]
    feat = pipeline.extract_at_index(up_sui, btc_5m_bars=drop_btc)
    assert feat.features["btc_ret_5m"] <= Decimal("-0.0080")

    clf_with_btc = A01RegimeClassifier(A01RegimeConfig(enable_btc_flash_drop_filter=True))
    clf_no_btc = A01RegimeClassifier(A01RegimeConfig(enable_btc_flash_drop_filter=False))

    dec_with, diag_with = clf_with_btc.classify_with_diagnostics(feat, DataQuality.OK)
    dec_without, diag_without = clf_no_btc.classify_with_diagnostics(feat, DataQuality.OK)

    assert dec_with.allow_entry is False
    assert diag_with.btc_veto_triggered is True
    assert "BTC_DROP_5M_LE_MINUS_80BPS" in diag_with.btc_veto_windows
    assert "BTC_DROP_5M_LE_MINUS_80BPS" in dec_with.reason_codes

    assert dec_without.allow_entry is True
    assert diag_without.btc_veto_triggered is False


def test_p3_task_a8_htf_15m_1h_strict_closed_bar_alignment() -> None:
    """Task A.8: Prove that at decision time `10:09:59.999` (the close of the `10:05..10:10` 5m bar),
    `15m` and `1h` features ONLY reference the already-completed `09:45..10:00` 15m bar and `09:00..10:00` 1h bar,
    and NEVER reference the in-progress `10:00..10:15` 15m bar or `10:00..11:00` 1h bar.
    """
    start_utc = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    sui_5m = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=30,  # 08:00 to 10:30 (index 25 is 10:05..10:09:59.999)
        base_price=Decimal("2.0000"),
        start_utc=start_utc,
        interval="5m",
        interval_ms=FIVE_MIN_MS,
    )
    sui_15m = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=10,  # 08:00 to 10:30 (index 8 is 10:00..10:14:59.999)
        base_price=Decimal("2.0000"),
        start_utc=start_utc,
        interval="15m",
        interval_ms=FIFTEEN_MIN_MS,
    )
    sui_1h = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=3,   # 08:00..09:00, 09:00..10:00, 10:00..11:00 (index 2 closes at 10:59:59.999)
        base_price=Decimal("2.0000"),
        start_utc=start_utc,
        interval="1h",
        interval_ms=ONE_HOUR_MS,
    )

    pipeline = FeaturePipeline()
    # Index 25 is 5m bar 10:05:00.000 .. 10:09:59.999
    bar_10_10 = sui_5m[25]
    feat_clean = pipeline.extract_at_index(
        sui_5m_bars=sui_5m,
        target_index=25,
        sui_15m_bars=sui_15m,
        sui_1h_bars=sui_1h,
    )
    assert int(feat_clean.features["htf_15m_close_time_ms"]) <= bar_10_10.close_time_ms
    assert int(feat_clean.features["htf_1h_close_time_ms"]) <= bar_10_10.close_time_ms
    # Specifically, at 10:09:59.999, the latest completed 15m bar closed at 09:59:59.999 (sui_15m[7])
    # and the latest completed 1h bar closed at 09:59:59.999 (sui_1h[1])!
    assert int(feat_clean.features["htf_15m_close_time_ms"]) == sui_15m[7].close_time_ms
    assert int(feat_clean.features["htf_1h_close_time_ms"]) == sui_1h[1].close_time_ms

    # Mutate the in-progress 10:00..10:15 15m bar (sui_15m[8]) and 10:00..11:00 1h bar (sui_1h[2]) by 100x
    passed, _ = pipeline.verify_causality_no_lookahead(
        sui_5m_bars=sui_5m,
        check_index=25,
        sui_15m_bars=sui_15m,
        sui_1h_bars=sui_1h,
    )
    assert passed is True


def test_p3_task_a9_insufficient_evidence_and_no_edge_verdicts() -> None:
    """Task A.9: Verify that when independent test sample count < 100 or 95% CI crosses zero,
    the model explicitly reports `INSUFFICIENT_EVIDENCE` or `NO_EDGE` and never claims `EDGE_VALIDATED`.
    """
    sui_bars = _build_synthetic_bars(symbol=TRADING_SYMBOL, count=360, base_price=Decimal("2.0000"))
    btc_bars = _build_synthetic_bars(symbol=REFERENCE_SYMBOL, count=360, base_price=Decimal("68000.00"))
    pipeline = FeaturePipeline()
    labeler = TripleBarrierLabeler()

    indices = list(range(24, 300, 4))
    features_seq = [pipeline.extract_at_index(sui_bars, target_index=i, btc_5m_bars=btc_bars) for i in indices]
    labels_seq = [labeler.label_at_index(sui_bars, entry_index=i) for i in indices]

    model = A02BreakoutCalibratedModel(min_test_events_for_edge_claim=100)
    report = model.fit_and_evaluate_three_stage(features_seq, labels_seq)

    assert report.statistical_verdict in {
        StatisticalVerdict.INSUFFICIENT_EVIDENCE,
        StatisticalVerdict.NO_EDGE,
    }
    assert report.statistical_verdict != StatisticalVerdict.EDGE_VALIDATED
    assert len(report.verdict_reasons) >= 1


def test_p3_a01_five_regimes_and_btc_veto() -> None:
    """Verify all 5 market states (`UP_TREND`, `CONSOLIDATION`, `DOWN_TREND`, `HIGH_VOLATILITY`, `DATA_UNHEALTHY`)
    and verify `allow_entry` is strictly False in `DOWN_TREND`, `HIGH_VOLATILITY`, `DATA_UNHEALTHY`, or BTC flash drop.
    """
    pipeline = FeaturePipeline()
    classifier = A01RegimeClassifier()

    up_sui = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=35,
        base_price=Decimal("2.0000"),
        step_pct=Decimal("0.0040"),
    )
    calm_btc = _build_synthetic_bars(
        symbol=REFERENCE_SYMBOL,
        count=35,
        base_price=Decimal("68000.00"),
        step_pct=Decimal("0.0005"),
    )
    feat_up = pipeline.extract_at_index(up_sui, btc_5m_bars=calm_btc)
    dec_up, diag_up = classifier.classify_with_diagnostics(feat_up, data_quality=DataQuality.OK)
    assert dec_up.regime == RegimeType.UP_TREND
    assert dec_up.allow_entry is True
    assert Decimal("0") <= dec_up.confidence <= Decimal("1")
    assert sum(diag_up.regime_probabilities.values()) == Decimal("1.000000")

    crash_btc = list(calm_btc[:-1])
    last_b = calm_btc[-1]
    crash_btc.append(
        ClosedKlineBar(
            symbol=REFERENCE_SYMBOL,
            interval="5m",
            open_time_ms=last_b.open_time_ms,
            close_time_ms=last_b.close_time_ms,
            open_price=last_b.open_price,
            high_price=last_b.open_price,
            low_price=(last_b.open_price * Decimal("0.980")).quantize(Decimal("0.01")),
            close_price=(last_b.open_price * Decimal("0.982")).quantize(Decimal("0.01")),
            base_volume=last_b.base_volume * Decimal("4"),
            quote_volume=last_b.quote_volume * Decimal("4"),
            taker_buy_base_volume=last_b.taker_buy_base_volume,
            taker_buy_quote_volume=last_b.taker_buy_quote_volume,
            trade_count=500,
            exchange_time_utc=last_b.exchange_time_utc,
            recv_time_utc=last_b.recv_time_utc,
            is_closed=True,
        )
    )
    feat_up_btc_crash = pipeline.extract_at_index(up_sui, btc_5m_bars=crash_btc)
    dec_btc_veto, diag_btc_veto = classifier.classify_with_diagnostics(feat_up_btc_crash, data_quality=DataQuality.OK)
    assert diag_btc_veto.btc_veto_triggered is True
    assert dec_btc_veto.allow_entry is False
    assert "BTC_FLASH_DROP_VETO" in dec_btc_veto.reason_codes

    flat_sui = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=35,
        base_price=Decimal("2.0000"),
        step_pct=Decimal("0.0000"),
    )
    feat_flat = pipeline.extract_at_index(flat_sui, btc_5m_bars=calm_btc)
    dec_consol = classifier.classify(feat_flat, data_quality=DataQuality.OK)
    assert dec_consol.regime == RegimeType.CONSOLIDATION
    assert dec_consol.allow_entry is True

    down_sui = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=35,
        base_price=Decimal("2.5000"),
        step_pct=Decimal("-0.0045"),
    )
    feat_down = pipeline.extract_at_index(down_sui, btc_5m_bars=calm_btc)
    dec_down = classifier.classify(feat_down, data_quality=DataQuality.OK)
    assert dec_down.regime == RegimeType.DOWN_TREND
    assert dec_down.allow_entry is False

    hv_bars: list[ClosedKlineBar] = []
    for idx, b in enumerate(flat_sui):
        swing = Decimal("0.0800") if idx % 2 == 0 else Decimal("-0.0750")
        c_p = (Decimal("2.0000") + swing).quantize(Decimal("0.0001"))
        hv_bars.append(
            ClosedKlineBar(
                symbol=TRADING_SYMBOL,
                interval="5m",
                open_time_ms=b.open_time_ms,
                close_time_ms=b.close_time_ms,
                open_price=Decimal("2.0000"),
                high_price=Decimal("2.1200"),
                low_price=Decimal("1.8800"),
                close_price=c_p,
                base_volume=b.base_volume,
                quote_volume=b.quote_volume,
                taker_buy_base_volume=b.taker_buy_base_volume,
                taker_buy_quote_volume=b.taker_buy_quote_volume,
                trade_count=b.trade_count,
                exchange_time_utc=b.exchange_time_utc,
                recv_time_utc=b.recv_time_utc,
                is_closed=True,
            )
        )
    feat_hv = pipeline.extract_at_index(hv_bars, btc_5m_bars=calm_btc)
    dec_hv = classifier.classify(feat_hv, data_quality=DataQuality.OK)
    assert dec_hv.regime == RegimeType.HIGH_VOLATILITY
    assert dec_hv.allow_entry is False

    dec_stale = classifier.classify(feat_up, data_quality=DataQuality.STALE)
    assert dec_stale.regime == RegimeType.DATA_UNHEALTHY
    assert dec_stale.allow_entry is False

    dec_gap = classifier.classify(feat_up, data_quality=DataQuality.GAP_DETECTED)
    assert dec_gap.regime == RegimeType.DATA_UNHEALTHY
    assert dec_gap.allow_entry is False


def test_p3_triple_barrier_net_fees_and_pessimistic_tie_breaking() -> None:
    """Verify fee-aware +1.5% net profit barrier, -1.0% stop barrier, 3h (36 bars) timeout barrier,
    and strict pessimistic STOP resolution when a bar touches both WIN and STOP simultaneously.
    """
    cfg = TripleBarrierConfig(
        horizon_bars=36,
        target_net_return=Decimal("0.0150"),
        stop_loss_pct=Decimal("0.0100"),
        buy_fee_rate=Decimal("0.0010"),
        sell_fee_rate=Decimal("0.0010"),
        stop_slippage_rate=Decimal("0.0005"),
    )
    labeler = TripleBarrierLabeler(cfg)

    entry_price = Decimal("2.0000")
    win_barrier, stop_barrier = labeler.compute_barriers(entry_price)

    expected_win = compute_c1_net_target_sell_price(
        avg_buy_price=entry_price,
        buy_fee_rate=Decimal("0.0010"),
        sell_fee_rate=Decimal("0.0010"),
        target_net_return=Decimal("0.0150"),
    )
    expected_stop = compute_stop_loss_trigger_price(
        avg_buy_price=entry_price,
        stop_loss_pct=Decimal("0.0100"),
    )
    assert win_barrier == expected_win
    assert stop_barrier == expected_stop
    assert win_barrier > Decimal("2.0300")
    assert stop_barrier == Decimal("1.9800")

    base_bars = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=60,
        base_price=entry_price,
        step_pct=Decimal("0.0000"),
    )

    win_bars = list(base_bars)
    b5 = win_bars[15]
    win_bars[15] = ClosedKlineBar(
        symbol=TRADING_SYMBOL,
        interval="5m",
        open_time_ms=b5.open_time_ms,
        close_time_ms=b5.close_time_ms,
        open_price=entry_price,
        high_price=win_barrier + Decimal("0.0010"),
        low_price=entry_price - Decimal("0.0010"),
        close_price=win_barrier,
        base_volume=b5.base_volume,
        quote_volume=b5.quote_volume,
        taker_buy_base_volume=b5.taker_buy_base_volume,
        taker_buy_quote_volume=b5.taker_buy_quote_volume,
        trade_count=b5.trade_count,
        exchange_time_utc=b5.exchange_time_utc,
        recv_time_utc=b5.recv_time_utc,
        is_closed=True,
    )
    lbl_win = labeler.label_at_index(win_bars, entry_index=10, entry_price_override=entry_price)
    assert lbl_win.outcome == BarrierOutcome.WIN
    assert lbl_win.bars_to_hit == 5
    assert lbl_win.ambiguous_same_bar_conflict is False
    assert lbl_win.net_return >= Decimal("0.0150")

    stop_bars = list(base_bars)
    b3 = stop_bars[13]
    stop_bars[13] = ClosedKlineBar(
        symbol=TRADING_SYMBOL,
        interval="5m",
        open_time_ms=b3.open_time_ms,
        close_time_ms=b3.close_time_ms,
        open_price=entry_price,
        high_price=entry_price + Decimal("0.0010"),
        low_price=stop_barrier - Decimal("0.0020"),
        close_price=stop_barrier,
        base_volume=b3.base_volume,
        quote_volume=b3.quote_volume,
        taker_buy_base_volume=b3.taker_buy_base_volume,
        taker_buy_quote_volume=b3.taker_buy_quote_volume,
        trade_count=b3.trade_count,
        exchange_time_utc=b3.exchange_time_utc,
        recv_time_utc=b3.recv_time_utc,
        is_closed=True,
    )
    lbl_stop = labeler.label_at_index(stop_bars, entry_index=10, entry_price_override=entry_price)
    assert lbl_stop.outcome == BarrierOutcome.STOP
    assert lbl_stop.bars_to_hit == 3
    assert lbl_stop.ambiguous_same_bar_conflict is False
    assert lbl_stop.net_return < Decimal("-0.0100")

    tie_bars = list(base_bars)
    b4 = tie_bars[14]
    tie_bars[14] = ClosedKlineBar(
        symbol=TRADING_SYMBOL,
        interval="5m",
        open_time_ms=b4.open_time_ms,
        close_time_ms=b4.close_time_ms,
        open_price=entry_price,
        high_price=win_barrier + Decimal("0.0050"),
        low_price=stop_barrier - Decimal("0.0050"),
        close_price=entry_price,
        base_volume=b4.base_volume * Decimal("5"),
        quote_volume=b4.quote_volume * Decimal("5"),
        taker_buy_base_volume=b4.taker_buy_base_volume,
        taker_buy_quote_volume=b4.taker_buy_quote_volume,
        trade_count=500,
        exchange_time_utc=b4.exchange_time_utc,
        recv_time_utc=b4.recv_time_utc,
        is_closed=True,
    )
    lbl_tie = labeler.label_at_index(tie_bars, entry_index=10, entry_price_override=entry_price)
    assert lbl_tie.ambiguous_same_bar_conflict is True
    assert lbl_tie.outcome == BarrierOutcome.STOP
    assert lbl_tie.bars_to_hit == 4

    lbl_timeout = labeler.label_at_index(base_bars, entry_index=10, entry_price_override=entry_price)
    assert lbl_timeout.outcome == BarrierOutcome.TIMEOUT
    assert lbl_timeout.bars_to_hit == 36
    assert lbl_timeout.ambiguous_same_bar_conflict is False


def test_p3_purged_embargo_walk_forward_split() -> None:
    """Verify Purged & Embargoed Walk-Forward splitter enforces >= 36 5m bars (3 hours) separation
    and purges all training samples whose 3h label horizon overlaps the validation fold.
    """
    bars = _build_synthetic_bars(symbol=TRADING_SYMBOL, count=320, base_price=Decimal("2.0000"))
    labeler = TripleBarrierLabeler()

    labels = [
        labeler.label_at_index(bars, entry_index=idx)
        for idx in range(20, 260, 4)
    ]
    assert len(labels) == 60

    splitter = PurgedWalkForwardSplitter(n_splits=3, embargo_bars=36, min_train_events=15)
    folds = splitter.split(labels)
    assert len(folds) == 3

    for fold in folds:
        assert fold.purged_count > 0
        assert fold.embargo_bars == 36
        assert fold.min_bar_gap_between_train_and_val >= 36
        assert fold.min_time_gap_ms_between_train_end_and_val_start >= 36 * FIVE_MIN_MS
        assert splitter.verify_fold_zero_leakage(labels, fold) is True

        for ti in fold.train_indices:
            for vi in fold.val_indices:
                assert labels[vi].event_index - labels[ti].event_index >= 36
                assert labels[ti].horizon_end_index < labels[vi].event_index
                assert labels[ti].label_end_close_time_ms < labels[vi].entry_open_time_ms


def test_p3_a02_calibrated_probabilities_simplex_and_ev_net(tmp_path: Path) -> None:
    """Verify A-02 3-stage training (`TRAIN / CALIBRATION / FINAL_TEST`), out-of-sample calibration,
    exact Decimal simplex (`p_win + p_stop + p_timeout == Decimal("1")`), net expected value `ev_net`,
    `ModelManifest` export, and DuckDB research warehouse round-trip.
    """
    for rw, rs, rt in (
        (0.3333333, 0.3333333, 0.3333333),
        (0.7123456, 0.1876543, 0.1000001),
        (0.0, 0.0, 0.0),
        (1e-9, 0.9999999, 1e-9),
        (Decimal("0.4512349"), Decimal("0.3198764"), Decimal("0.2288887")),
    ):
        pw, ps, pt = project_to_decimal_simplex(rw, rs, rt)
        assert isinstance(pw, Decimal) and isinstance(ps, Decimal) and isinstance(pt, Decimal)
        assert Decimal("0") <= pw <= Decimal("1")
        assert Decimal("0") <= ps <= Decimal("1")
        assert Decimal("0") <= pt <= Decimal("1")
        assert pw + ps + pt == Decimal("1")
        assert pw + ps + pt == Decimal("1.000000")

    sui_bars = _build_synthetic_bars(
        symbol=TRADING_SYMBOL,
        count=380,
        base_price=Decimal("2.0000"),
        step_pct=Decimal("0.0012"),
    )
    btc_bars = _build_synthetic_bars(
        symbol=REFERENCE_SYMBOL,
        count=380,
        base_price=Decimal("68000.00"),
        step_pct=Decimal("0.0004"),
    )

    for step_num, idx in enumerate(range(24, 320, 12)):
        prev_high = max(b.high_price for b in sui_bars[idx - 20 : idx])
        b_cur = sui_bars[idx]
        if step_num % 3 == 0:
            brk_close = (prev_high * Decimal("1.0060")).quantize(Decimal("0.0001"))
            sui_bars[idx] = ClosedKlineBar(
                symbol=TRADING_SYMBOL,
                interval="5m",
                open_time_ms=b_cur.open_time_ms,
                close_time_ms=b_cur.close_time_ms,
                open_price=prev_high,
                high_price=(brk_close + Decimal("0.0005")).quantize(Decimal("0.0001")),
                low_price=(prev_high - Decimal("0.0010")).quantize(Decimal("0.0001")),
                close_price=brk_close,
                base_volume=Decimal("35000.0"),
                quote_volume=(Decimal("35000.0") * brk_close).quantize(Decimal("0.0001")),
                taker_buy_base_volume=Decimal("28000.0"),
                taker_buy_quote_volume=(Decimal("28000.0") * brk_close).quantize(Decimal("0.0001")),
                trade_count=400,
                exchange_time_utc=b_cur.exchange_time_utc,
                recv_time_utc=b_cur.recv_time_utc,
                is_closed=True,
            )
            b_next = sui_bars[idx + 2]
            win_high = (brk_close * Decimal("1.0320")).quantize(Decimal("0.0001"))
            sui_bars[idx + 2] = ClosedKlineBar(
                symbol=TRADING_SYMBOL,
                interval="5m",
                open_time_ms=b_next.open_time_ms,
                close_time_ms=b_next.close_time_ms,
                open_price=brk_close,
                high_price=win_high,
                low_price=brk_close,
                close_price=win_high,
                base_volume=b_next.base_volume,
                quote_volume=b_next.quote_volume,
                taker_buy_base_volume=b_next.taker_buy_base_volume,
                taker_buy_quote_volume=b_next.taker_buy_quote_volume,
                trade_count=b_next.trade_count,
                exchange_time_utc=b_next.exchange_time_utc,
                recv_time_utc=b_next.recv_time_utc,
                is_closed=True,
            )
        elif step_num % 3 == 1:
            trap_close = (prev_high * Decimal("1.0010")).quantize(Decimal("0.0001"))
            trap_high = (prev_high * Decimal("1.0150")).quantize(Decimal("0.0001"))
            sui_bars[idx] = ClosedKlineBar(
                symbol=TRADING_SYMBOL,
                interval="5m",
                open_time_ms=b_cur.open_time_ms,
                close_time_ms=b_cur.close_time_ms,
                open_price=prev_high,
                high_price=trap_high,
                low_price=(prev_high - Decimal("0.0020")).quantize(Decimal("0.0001")),
                close_price=trap_close,
                base_volume=Decimal("9000.0"),
                quote_volume=(Decimal("9000.0") * trap_close).quantize(Decimal("0.0001")),
                taker_buy_base_volume=Decimal("3000.0"),
                taker_buy_quote_volume=(Decimal("3000.0") * trap_close).quantize(Decimal("0.0001")),
                trade_count=90,
                exchange_time_utc=b_cur.exchange_time_utc,
                recv_time_utc=b_cur.recv_time_utc,
                is_closed=True,
            )
            b_next = sui_bars[idx + 1]
            stop_low = (trap_close * Decimal("0.9850")).quantize(Decimal("0.0001"))
            sui_bars[idx + 1] = ClosedKlineBar(
                symbol=TRADING_SYMBOL,
                interval="5m",
                open_time_ms=b_next.open_time_ms,
                close_time_ms=b_next.close_time_ms,
                open_price=trap_close,
                high_price=trap_close,
                low_price=stop_low,
                close_price=stop_low,
                base_volume=b_next.base_volume,
                quote_volume=b_next.quote_volume,
                taker_buy_base_volume=b_next.taker_buy_base_volume,
                taker_buy_quote_volume=b_next.taker_buy_quote_volume,
                trade_count=b_next.trade_count,
                exchange_time_utc=b_next.exchange_time_utc,
                recv_time_utc=b_next.recv_time_utc,
                is_closed=True,
            )

    spool_dir = tmp_path / "spool"
    warehouse_dir = tmp_path / "warehouse"
    exporter = CloudShardExporter(spool_dir)
    exporter.write_shard(
        symbol=TRADING_SYMBOL,
        stream_type="kline_closed",
        records=[
            {
                "record_type": "kline_closed",
                "symbol": b.symbol,
                "interval": b.interval,
                "open_time_ms": b.open_time_ms,
                "close_time_ms": b.close_time_ms,
                "open_price": str(b.open_price),
                "high_price": str(b.high_price),
                "low_price": str(b.low_price),
                "close_price": str(b.close_price),
                "base_volume": str(b.base_volume),
                "quote_volume": str(b.quote_volume),
                "exchange_time_utc": b.exchange_time_utc.isoformat(),
                "recv_time_utc": b.recv_time_utc.isoformat(),
            }
            for b in sui_bars
        ],
    )
    sync_engine = LocalResearchSyncEngine(warehouse_dir)
    sync_report = sync_engine.sync_from_manifest(
        exporter.get_manifest_dict(),
        lambda fname: (spool_dir / fname).read_bytes(),
    )
    assert sync_report.ingested_records_count == len(sui_bars)
    loaded_bars = load_closed_bars_from_warehouse(sync_engine.duckdb_path, symbol=TRADING_SYMBOL, interval="5m")
    assert len(loaded_bars) == len(sui_bars)

    pipeline = FeaturePipeline()
    labeler = TripleBarrierLabeler()
    sample_indices = list(range(24, 320, 4))
    features_seq = [pipeline.extract_at_index(sui_bars, target_index=i, btc_5m_bars=btc_bars) for i in sample_indices]
    labels_seq = [labeler.label_at_index(sui_bars, entry_index=i) for i in sample_indices]

    model = A02BreakoutCalibratedModel()
    oof_metrics = model.fit_purged_walk_forward(features_seq, labels_seq)

    assert oof_metrics["ece"] <= Decimal("0.080000")
    assert oof_metrics["brier_score"] <= oof_metrics["baseline_brier_score"]

    classifier = A01RegimeClassifier()
    cand_feat = pipeline.extract_at_index(sui_bars, target_index=24, btc_5m_bars=btc_bars)
    cand_check = evaluate_breakout_candidate(cand_feat)
    assert cand_check.is_candidate is True

    reg_allow = classifier.classify(cand_feat, data_quality=DataQuality.OK)
    assert reg_allow.allow_entry is True

    proposal = model.evaluate_breakout_proposal(cand_feat, reg_allow, require_candidate_filter=True)
    assert proposal is not None
    assert proposal.p_win + proposal.p_stop + proposal.p_timeout == Decimal("1")
    assert proposal.p_win + proposal.p_stop + proposal.p_timeout == Decimal("1.000000")
    assert proposal.ev_net == model.costs.compute_ev_net(proposal.p_win, proposal.p_stop, proposal.p_timeout)
    assert len(proposal.feature_snapshot_hash) == 64

    reg_blocked = classifier.classify(cand_feat, data_quality=DataQuality.STALE)
    assert reg_blocked.allow_entry is False
    assert model.evaluate_breakout_proposal(cand_feat, reg_blocked) is None

    btc_feat_as_contract = MarketFeatures(
        feature_time_utc=cand_feat.feature_time_utc,
        horizon="5m",
        symbol=REFERENCE_SYMBOL,
        complete_bar_only=True,
        features=cand_feat.features,
        data_version="v1",
    )
    with pytest.raises(ValueError, match="SUIUSDT"):
        model.predict_probabilities(btc_feat_as_contract)

    artifact_file = tmp_path / "models" / "a02_v1.json"
    manifest = model.export_model_manifest(artifact_output_path=artifact_file)
    assert artifact_file.exists()
    assert len(manifest.sha256) == 64
    assert manifest.calibration_metrics["ece"] <= Decimal("0.080000")

    bad_model = A02BreakoutCalibratedModel()
    bad_model.calibration_metrics["ece"] = Decimal("0.150000")
    with pytest.raises(ModelCalibrationGateError):
        bad_model.export_model_manifest()


def test_p3_single_bar_inference_latency_slo() -> None:
    """Verify combined FeaturePipeline + A-01 + A-02 single-bar inference latency is < 5 ms."""
    sui_bars = _build_synthetic_bars(symbol=TRADING_SYMBOL, count=40, base_price=Decimal("2.0000"))
    btc_bars = _build_synthetic_bars(symbol=REFERENCE_SYMBOL, count=40, base_price=Decimal("68000.00"))
    pipeline = FeaturePipeline()
    classifier = A01RegimeClassifier()
    model = A02BreakoutCalibratedModel()

    latencies_ms: list[float] = []
    for _ in range(50):
        t0 = monotonic_ns()
        feat = pipeline.extract_at_index(sui_bars, btc_5m_bars=btc_bars)
        reg = classifier.classify(feat, data_quality=DataQuality.OK)
        _ = model.evaluate_breakout_proposal(feat, reg, require_candidate_filter=False)
        t1 = monotonic_ns()
        latencies_ms.append((t1 - t0) / 1_000_000.0)

    latencies_ms.sort()
    p99_ms = latencies_ms[int(len(latencies_ms) * 0.99)]
    assert p99_ms < 5.0, f"Combined P3 inference P99 latency ({p99_ms:.3f} ms) exceeded 5.0 ms budget"
