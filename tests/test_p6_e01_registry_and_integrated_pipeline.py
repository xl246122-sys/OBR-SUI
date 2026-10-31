"""P6 Verification Suite: E-01 Signed Model Artifact Registry, SHADOW -> PAPER Promotion,
AutoRollbackGuard, In-Flight Position Exit Version Pinning, and D-01 Integrated Pipeline (QA-12, §11.2, §13)."""

from __future__ import annotations

import hashlib
import json
import time
import tracemalloc
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sui_quant.config import ExecutionMode, RiskConfig, SystemConfig
from sui_quant.contracts import (
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    AlertSeverity,
    DataQuality,
    MarketFeatures,
    ModelManifest,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.data.quality_guard import ClosedKlineBar, PartialOrderbookSnapshot
from sui_quant.exchange_rules import (
    FilterSourceType,
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
)
from sui_quant.execution.paper_exchange import PaperOrderbookSimulator
from sui_quant.exit import (
    C1FixedNetTargetExit,
    C2GridTrailingExit,
    C3AdaptiveHybridExit,
    ExitSignalAction,
    ExitStrategyId,
)
from sui_quant.models.a01_regime import A01RegimeClassifier
from sui_quant.models.a02_breakout import A02BreakoutCalibratedModel
from sui_quant.models.features import FeaturePipeline
from sui_quant.registry import (
    D01IntegratedPipeline,
    E01ArtifactPackager,
    E01ModelRegistry,
    ModelRegistryVerificationError,
    SignedModelArtifactBundle,
)
from sui_quant.state_machine import TradingState


def _make_dynamic_filters(now_utc: datetime) -> SymbolFilters:
    return SymbolFilters(
        symbol=P0_SNAPSHOT_SUIUSDT_FILTERS.symbol,
        min_price=P0_SNAPSHOT_SUIUSDT_FILTERS.min_price,
        max_price=P0_SNAPSHOT_SUIUSDT_FILTERS.max_price,
        tick_size=P0_SNAPSHOT_SUIUSDT_FILTERS.tick_size,
        min_qty=P0_SNAPSHOT_SUIUSDT_FILTERS.min_qty,
        max_qty=P0_SNAPSHOT_SUIUSDT_FILTERS.max_qty,
        step_size=P0_SNAPSHOT_SUIUSDT_FILTERS.step_size,
        min_notional=P0_SNAPSHOT_SUIUSDT_FILTERS.min_notional,
        max_notional=P0_SNAPSHOT_SUIUSDT_FILTERS.max_notional,
        max_num_orders=P0_SNAPSHOT_SUIUSDT_FILTERS.max_num_orders,
        max_num_algo_orders=P0_SNAPSHOT_SUIUSDT_FILTERS.max_num_algo_orders,
        max_num_order_lists=P0_SNAPSHOT_SUIUSDT_FILTERS.max_num_order_lists,
        source=FilterSourceType.DYNAMIC_EXCHANGE_INFO,
        fetched_at_utc=now_utc,
    )


def _make_orderbook(
    mid_price: Decimal,
    now_utc: datetime,
    mono_ns: int = 100_000_000_000,
    update_id: int = 1000,
) -> PartialOrderbookSnapshot:
    tick = Decimal("0.0001")
    best_bid = (mid_price // tick) * tick
    best_ask = best_bid + tick
    bids = tuple(
        (best_bid - tick * Decimal(i), Decimal("2500.0") + Decimal(i * 200))
        for i in range(10)
    )
    asks = tuple(
        (best_ask + tick * Decimal(i), Decimal("1800.0") + Decimal(i * 150))
        for i in range(10)
    )
    return PartialOrderbookSnapshot(
        symbol=TRADING_SYMBOL,
        last_update_id=update_id,
        bids=bids,
        asks=asks,
        exchange_time_utc=now_utc,
        recv_time_utc=now_utc,
        recv_monotonic_ns=mono_ns,
        sampling_interval_ms=100,
    )


def _build_sui_and_btc_bar_history(
    base_time_utc: datetime,
    n_warmup: int = 25,
    base_price: Decimal = Decimal("1.9850"),
    prev_20_high_cap: Decimal = Decimal("2.0000"),
) -> tuple[list[ClosedKlineBar], list[ClosedKlineBar]]:
    """Construct causal 5m `SUIUSDT` and `BTCUSDT` closed bar history leading up to a breakout bar."""
    sui_bars: list[ClosedKlineBar] = []
    btc_bars: list[ClosedKlineBar] = []
    step_ms = 5 * 60 * 1000
    t0_ms = int((base_time_utc - timedelta(minutes=5 * n_warmup)).timestamp() * 1000)

    for i in range(n_warmup):
        open_ms = t0_ms + i * step_ms
        close_ms = open_ms + step_ms - 1
        bar_utc = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)
        drift = Decimal(i) * Decimal("0.0005")
        o_px = min(prev_20_high_cap - Decimal("0.0010"), base_price + drift)
        c_px = min(prev_20_high_cap - Decimal("0.0004"), o_px + Decimal("0.0004"))
        h_px = prev_20_high_cap if i == (n_warmup - 2) else min(prev_20_high_cap - Decimal("0.0002"), c_px + Decimal("0.0004"))
        l_px = o_px - Decimal("0.0004")
        vol = Decimal("15000.0") + Decimal((i % 5) * 800)

        sui_bars.append(
            ClosedKlineBar(
                symbol=TRADING_SYMBOL,
                interval="5m",
                open_time_ms=open_ms,
                close_time_ms=close_ms,
                open_price=o_px,
                high_price=h_px,
                low_price=l_px,
                close_price=c_px,
                base_volume=vol,
                quote_volume=vol * c_px,
                taker_buy_base_volume=vol * Decimal("0.56"),
                taker_buy_quote_volume=vol * c_px * Decimal("0.56"),
                trade_count=220,
                exchange_time_utc=bar_utc,
                recv_time_utc=bar_utc,
                is_closed=True,
            )
        )

        btc_o = Decimal("70000.00") + Decimal(i * 15)
        btc_c = btc_o + Decimal("18.00")
        btc_bars.append(
            ClosedKlineBar(
                symbol=REFERENCE_SYMBOL,
                interval="5m",
                open_time_ms=open_ms,
                close_time_ms=close_ms,
                open_price=btc_o,
                high_price=btc_c + Decimal("25.00"),
                low_price=btc_o - Decimal("20.00"),
                close_price=btc_c,
                base_volume=Decimal("120.0"),
                quote_volume=Decimal("120.0") * btc_c,
                taker_buy_base_volume=Decimal("68.0"),
                taker_buy_quote_volume=Decimal("68.0") * btc_c,
                trade_count=1500,
                exchange_time_utc=bar_utc,
                recv_time_utc=bar_utc,
                is_closed=True,
            )
        )

    return sui_bars, btc_bars


def _append_sui_and_btc_bar(
    sui_bars: list[ClosedKlineBar],
    btc_bars: list[ClosedKlineBar],
    *,
    open_price: Decimal,
    high_price: Decimal,
    low_price: Decimal,
    close_price: Decimal,
    base_volume: Decimal = Decimal("48000.0"),
    taker_buy_ratio: Decimal = Decimal("0.68"),
    btc_return_bps: Decimal = Decimal("12"),
) -> datetime:
    step_ms = 5 * 60 * 1000
    prev_sui = sui_bars[-1]
    open_ms = prev_sui.open_time_ms + step_ms
    close_ms = open_ms + step_ms - 1
    bar_utc = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)

    sui_bars.append(
        ClosedKlineBar(
            symbol=TRADING_SYMBOL,
            interval="5m",
            open_time_ms=open_ms,
            close_time_ms=close_ms,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            base_volume=base_volume,
            quote_volume=base_volume * close_price,
            taker_buy_base_volume=base_volume * taker_buy_ratio,
            taker_buy_quote_volume=base_volume * close_price * taker_buy_ratio,
            trade_count=650,
            exchange_time_utc=bar_utc,
            recv_time_utc=bar_utc,
            is_closed=True,
        )
    )

    prev_btc = btc_bars[-1]
    btc_o = prev_btc.close_price
    btc_c = (btc_o * (Decimal("1") + btc_return_bps / Decimal("10000"))).quantize(Decimal("0.01"))
    btc_bars.append(
        ClosedKlineBar(
            symbol=REFERENCE_SYMBOL,
            interval="5m",
            open_time_ms=open_ms,
            close_time_ms=close_ms,
            open_price=btc_o,
            high_price=max(btc_o, btc_c) + Decimal("20.00"),
            low_price=min(btc_o, btc_c) - Decimal("20.00"),
            close_price=btc_c,
            base_volume=Decimal("140.0"),
            quote_volume=Decimal("140.0") * btc_c,
            taker_buy_base_volume=Decimal("80.0"),
            taker_buy_quote_volume=Decimal("80.0") * btc_c,
            trade_count=1600,
            exchange_time_utc=bar_utc,
            recv_time_utc=bar_utc,
            is_closed=True,
        )
    )
    return bar_utc


def test_qa12_model_manifest_signature_sha256_schema_and_risk_compatibility_gates(tmp_path: Path) -> None:
    """QA-12: Verify E-01 100% blocks tampered model weights, forged Ed25519 signatures,
    schema hash mismatches, risk compatibility mismatches, calibration failures, and missing rollback targets."""
    now_utc = datetime(2026, 10, 31, 9, 0, 0, tzinfo=timezone.utc)
    store = StateStore(tmp_path / "p6_qa12_gates.db")
    alert_mgr = AlertManager(store=store)
    risk_cfg = RiskConfig()

    trusted_priv = Ed25519PrivateKey.generate()
    trusted_pub_hex = trusted_priv.public_key().public_bytes_raw().hex()
    attacker_priv = Ed25519PrivateKey.generate()

    registry = E01ModelRegistry(
        store=store,
        alert_manager=alert_mgr,
        risk_config=risk_cfg,
        pinned_signer_pubkey_hex=trusted_pub_hex,
    )

    # 1. Bootstrap legitimate v1.0.0 baseline bundle
    v1_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="1.0.0",
        risk_compatibility=risk_cfg.version,
        rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0-genesis",
    )
    v1_bundle = E01ArtifactPackager.package_and_sign(
        model=v1_model,
        signing_private_key=trusted_priv,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    registry.bootstrap_initial_active_bundle(v1_bundle, now_utc=now_utc)
    assert registry.active_paper_bundle.bundle_id == "a02-suiusdt-breakout-3barrier:1.0.0"

    # 2. Attack A: Tamper with serialized model weights in artifact_bytes after signing
    v2_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="2.0.0",
        risk_compatibility=risk_cfg.version,
        rollback_ref=v1_bundle.bundle_id,
    )
    v2_bundle = E01ArtifactPackager.package_and_sign(
        model=v2_model,
        signing_private_key=trusted_priv,
        exit_strategy_id=ExitStrategyId.C2,
        exit_strategy_version="C2-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    tampered_bytes = v2_bundle.artifact_bytes.replace(b'"breakout_pct":0.65', b'"breakout_pct":9.99')
    tampered_bundle = replace(v2_bundle, artifact_bytes=tampered_bytes)
    with pytest.raises(ModelRegistryVerificationError, match="ARTIFACT_SHA256_TAMPERED"):
        registry.stage_bundle_to_shadow(tampered_bundle, now_utc=now_utc)

    # 3. Attack B: Forged Ed25519 signature (signed by untrusted key)
    forged_bundle = E01ArtifactPackager.package_and_sign(
        model=v2_model,
        signing_private_key=attacker_priv,
        exit_strategy_id=ExitStrategyId.C2,
        exit_strategy_version="C2-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    with pytest.raises(ModelRegistryVerificationError, match="SIGNATURE_INVALID"):
        registry.stage_bundle_to_shadow(forged_bundle, now_utc=now_utc)

    # 4. Attack C: Feature schema mismatch
    bad_schema_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="2.0.1-bad-schema",
        feature_names=("breakout_pct", "rvol_20"),
        risk_compatibility=risk_cfg.version,
        rollback_ref=v1_bundle.bundle_id,
    )
    bad_schema_bundle = E01ArtifactPackager.package_and_sign(
        model=bad_schema_model,
        signing_private_key=trusted_priv,
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    with pytest.raises(ModelRegistryVerificationError, match="FEATURES_SCHEMA_MISMATCH"):
        registry.stage_bundle_to_shadow(bad_schema_bundle, now_utc=now_utc)

    # 5. Attack D: RiskConfig version compatibility mismatch
    bad_risk_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="2.0.2-bad-risk",
        risk_compatibility="99.9.9-relaxed-risk",
        rollback_ref=v1_bundle.bundle_id,
    )
    bad_risk_bundle = E01ArtifactPackager.package_and_sign(
        model=bad_risk_model,
        signing_private_key=trusted_priv,
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    with pytest.raises(ModelRegistryVerificationError, match="RISK_COMPATIBILITY_MISMATCH"):
        registry.stage_bundle_to_shadow(bad_risk_bundle, now_utc=now_utc)

    # 6. Attack E: Missing rollback target in registry
    missing_rb_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="2.0.3-missing-rb",
        risk_compatibility=risk_cfg.version,
        rollback_ref="a02-suiusdt-breakout-3barrier:nonexistent-v0",
    )
    missing_rb_bundle = E01ArtifactPackager.package_and_sign(
        model=missing_rb_model,
        signing_private_key=trusted_priv,
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    with pytest.raises(ModelRegistryVerificationError, match="MISSING_ROLLBACK_TARGET"):
        registry.stage_bundle_to_shadow(missing_rb_bundle, now_utc=now_utc)

    # 7. Attack F: Embedded BTCUSDT symbol or LIVE override inside signed payload
    malicious_doc = json.loads(v2_bundle.artifact_bytes.decode("utf-8"))
    malicious_doc["symbol"] = "BTCUSDT"
    malicious_bytes = json.dumps(malicious_doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    malicious_sha = hashlib.sha256(malicious_bytes).hexdigest()
    malicious_manifest = replace(v2_bundle.manifest, sha256=malicious_sha)
    malicious_sig = trusted_priv.sign(
        SignedModelArtifactBundle.canonical_signing_payload(
            manifest=malicious_manifest,
            exit_strategy_id=v2_bundle.exit_strategy_id,
            exit_strategy_version=v2_bundle.exit_strategy_version,
            signer_id=v2_bundle.signer_id,
            signed_at_utc=now_utc,
        )
    ).hex()
    malicious_bundle = SignedModelArtifactBundle(
        manifest=malicious_manifest,
        artifact_bytes=malicious_bytes,
        exit_strategy_id=v2_bundle.exit_strategy_id,
        exit_strategy_version=v2_bundle.exit_strategy_version,
        signer_id=v2_bundle.signer_id,
        signed_at_utc=now_utc,
        ed25519_signature_hex=malicious_sig,
    )
    with pytest.raises(ModelRegistryVerificationError, match="SYMBOL_GOVERNANCE_VIOLATION"):
        registry.stage_bundle_to_shadow(malicious_bundle, now_utc=now_utc)

    assert registry.tampered_models_rejected_count == 6
    assert registry.active_paper_bundle.bundle_id == v1_bundle.bundle_id
    assert registry.shadow_bundle is None
    store.close()


def test_qa12_shadow_to_paper_atomic_promotion_and_auto_rollback_on_degradation(tmp_path: Path) -> None:
    """QA-12: Verify `v1.0.0 -> v2.0.0 (SHADOW) -> v2.0.0 (ACTIVE_PAPER)` atomic promotion
    and `< 5.0 ms` automatic atomic rollback to `v1.0.0` upon runtime exception or latency breach."""
    now_utc = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
    store = StateStore(tmp_path / "p6_qa12_rollback.db")
    alert_mgr = AlertManager(store=store)
    risk_cfg = RiskConfig()

    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes_raw().hex()

    registry = E01ModelRegistry(
        store=store,
        alert_manager=alert_mgr,
        risk_config=risk_cfg,
        pinned_signer_pubkey_hex=pub_hex,
        max_inference_latency_ms=Decimal("5.0000"),
    )

    v1_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="1.0.0",
        risk_compatibility=risk_cfg.version,
        rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0",
    )
    v1_bundle = E01ArtifactPackager.package_and_sign(
        model=v1_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    registry.bootstrap_initial_active_bundle(v1_bundle, now_utc=now_utc)

    v2_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="2.0.0",
        risk_compatibility=risk_cfg.version,
        rollback_ref=v1_bundle.bundle_id,
    )
    v2_bundle = E01ArtifactPackager.package_and_sign(
        model=v2_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C2,
        exit_strategy_version="C2-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    registry.stage_bundle_to_shadow(v2_bundle, now_utc=now_utc)
    assert registry.shadow_bundle is not None
    assert registry.shadow_bundle.bundle_id == v2_bundle.bundle_id

    # Promoting before running min_shadow_evaluations must be blocked
    with pytest.raises(ModelRegistryVerificationError, match="PROMOTION_BLOCKED"):
        registry.promote_shadow_to_paper(min_shadow_evaluations=3, now_utc=now_utc)

    # Run 3 closed-bar shadow evaluations
    sui_bars, btc_bars = _build_sui_and_btc_bar_history(now_utc)
    _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("1.9940"),
        high_price=Decimal("2.0120"),
        low_price=Decimal("1.9930"),
        close_price=Decimal("2.0100"),
    )
    fp = FeaturePipeline()
    a01 = A01RegimeClassifier()
    feat = fp.extract_at_index(sui_bars, btc_5m_bars=btc_bars)
    regime = a01.classify(feat, now_utc=feat.feature_time_utc)

    for _ in range(3):
        s_rec = registry.evaluate_shadow_inference(feat, regime)
        assert s_rec is not None
        assert s_rec.healthy
        assert s_rec.p_win + s_rec.p_stop + s_rec.p_timeout == Decimal("1.000000")

    assert registry.shadow_orders_submitted_count == 0

    # Promote v2.0.0 to ACTIVE_PAPER
    promoted_id, promo_lat_ns = registry.promote_shadow_to_paper(
        min_shadow_evaluations=3,
        now_utc=now_utc,
    )
    assert promoted_id == v2_bundle.bundle_id
    assert promo_lat_ns / 1e6 <= 5.0
    assert registry.active_paper_bundle.bundle_id == v2_bundle.bundle_id
    assert registry.shadow_bundle is None

    # Inject a runtime fault into v2.0.0 and verify AutoRollbackGuard atomically reverts to v1.0.0
    def _corrupt_v2_hook(_mf: MarketFeatures) -> None:
        raise RuntimeError("SIMULATED_V2_NUMERICAL_INSTABILITY_NAN")

    registry.inject_runtime_fault_hook(v2_bundle.bundle_id, _corrupt_v2_hook)
    proposal, eval_lat_ns, rb_rec = registry.evaluate_active_paper_proposal(
        sui_features=feat,
        regime_decision=regime,
        now_utc=feat.feature_time_utc,
    )

    assert rb_rec is not None
    assert rb_rec.failed_bundle_id == v2_bundle.bundle_id
    assert rb_rec.restored_bundle_id == v1_bundle.bundle_id
    assert rb_rec.rollback_latency_ns / 1e6 <= 5.0
    assert registry.active_paper_bundle.bundle_id == v1_bundle.bundle_id
    assert proposal is not None
    assert proposal.model_version == "a02-suiusdt-breakout-3barrier:1.0.0"

    # Verify HIGH alert was emitted and persisted for the automatic rollback
    undelivered = alert_mgr.list_undelivered_alerts()
    assert any(a["source"] == "E-01" and a["severity"] == "HIGH" for a in undelivered)
    store.close()


def test_qa12_in_flight_position_exit_strategy_version_pinning_during_hot_swap(tmp_path: Path) -> None:
    """QA-12: Verify that when a position is opened under `v1.0.0 (C2-v1.0)` and has completed
    Tranche 1 (`40%`), hot-swapping active PAPER to `v2.0.0 (C1-v2.0)` leaves the in-flight
    position strictly pinned to `C2-v1.0` for Tranches 2 & 3 (`version_crosstalk_violations == 0`),
    and only the NEXT newly opened trade switches to `C1-v2.0`."""
    base_utc = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    filters = _make_dynamic_filters(base_utc)
    store = StateStore(tmp_path / "p6_qa12_pinning.db")
    alert_mgr = AlertManager(store=store)
    sys_cfg = SystemConfig(mode=ExecutionMode.PAPER)

    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes_raw().hex()

    registry = E01ModelRegistry(
        store=store,
        alert_manager=alert_mgr,
        risk_config=sys_cfg.risk,
        pinned_signer_pubkey_hex=pub_hex,
        filters=filters,
    )
    registry.pin_registry.register_exit_strategy("C2-v1.0", C2GridTrailingExit(filters=filters))
    registry.pin_registry.register_exit_strategy("C1-v2.0", C1FixedNetTargetExit(filters=filters))

    # v1.0.0 uses C2-v1.0 (3-tranche 40%/30%/30% exit)
    v1_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="1.0.0",
        risk_compatibility=sys_cfg.risk.version,
        rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0",
        intercepts=(1.40, -0.90, -0.50),
    )
    v1_bundle = E01ArtifactPackager.package_and_sign(
        model=v1_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C2,
        exit_strategy_version="C2-v1.0",
        train_end_utc=base_utc,
        signed_at_utc=base_utc,
    )
    registry.bootstrap_initial_active_bundle(v1_bundle, now_utc=base_utc)

    # v2.0.0 uses C1-v2.0 (single fixed +1.5% exit)
    v2_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="2.0.0",
        risk_compatibility=sys_cfg.risk.version,
        rollback_ref=v1_bundle.bundle_id,
        intercepts=(1.40, -0.90, -0.50),
    )
    v2_bundle = E01ArtifactPackager.package_and_sign(
        model=v2_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v2.0",
        train_end_utc=base_utc,
        signed_at_utc=base_utc,
    )
    registry.stage_bundle_to_shadow(v2_bundle, now_utc=base_utc)

    exchange = PaperOrderbookSimulator(
        initial_usdt=Decimal("10000.0000"),
        initial_sui=Decimal("0.0"),
        commission_asset="SUI",
        filters=filters,
        execution_mode=ExecutionMode.PAPER,
    )
    pipeline = D01IntegratedPipeline(
        config=sys_cfg,
        store=store,
        alert_manager=alert_mgr,
        exchange=exchange,
        registry=registry,
        filters=filters,
    )

    sui_bars, btc_bars = _build_sui_and_btc_bar_history(base_utc)

    # Cycle 1: Upward breakout bar closes at 2.0006 (> prev_20_high 2.0000) -> opens Position #1 under v1.0.0 (C2-v1.0)
    t1_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("1.9990"),
        high_price=Decimal("2.0008"),
        low_price=Decimal("1.9985"),
        close_price=Decimal("2.0006"),
    )
    rep1 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("2.0000"), t1_utc),
        immediate_fill_qty=Decimal("500.0"),
        immediate_fill_price=Decimal("2.0000"),
        now_utc=t1_utc,
    )
    assert rep1.trading_state_after == TradingState.PROTECTED
    assert rep1.position_ledger is not None
    pos1_id = rep1.position_ledger.position_id
    assert rep1.position_ledger.exit_strategy_version == "C2-v1.0"
    assert rep1.pinned_position_exit_version == "C2-v1.0"

    # Cycle 2: Price reaches 2.0250 (>= C2 Tranche 1 +1.0% net target 2.0241) -> fills Tranche 1 (40%)
    t2_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("2.0000"),
        high_price=Decimal("2.0250"),
        low_price=Decimal("1.9990"),
        close_price=Decimal("2.0220"),
    )
    rep2 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("2.0220"), t2_utc),
        now_utc=t2_utc,
    )
    assert rep2.trading_state_after == TradingState.PROTECTED
    assert rep2.c01_report is not None
    assert rep2.c01_report.action == ExitSignalAction.PARTIAL_TAKE_PROFIT_AND_RATCHET_STOP
    assert rep2.c01_report.active_protection_stop_price == Decimal("2.0041")  # fee-covered breakeven

    # Hot-Swap while Position #1 is still in-flight (60% remaining)!
    # Promote v2.0.0 (C1-v2.0) from SHADOW to ACTIVE_PAPER
    promoted_id, _ = registry.promote_shadow_to_paper(min_shadow_evaluations=2, now_utc=t2_utc)
    assert promoted_id == v2_bundle.bundle_id
    assert registry.active_paper_exit_strategy_version == "C1-v2.0"

    # Verify Position #1 pin state survived the hot-swap and remains strictly C2-v1.0
    _, pin_state = registry.pin_registry.resolve_pinned_exit_strategy(
        pipeline.d01_engine.active_position_ledger  # type: ignore[arg-type]
    )
    assert pin_state.pinned_exit_strategy_version == "C2-v1.0"
    assert pin_state.hot_swaps_survived == 1
    assert pin_state.filled_tranche_indices == [1]

    # Cycle 3: Price reaches 2.0350 (above C1-v2.0's +1.5% target 2.0341, but below C2-v1.0's T2 target 2.0401)
    # Because Position #1 is pinned to C2-v1.0, it MUST NOT full-exit at C1's +1.5% target; it holds protected!
    t3_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("2.0220"),
        high_price=Decimal("2.0350"),
        low_price=Decimal("2.0200"),
        close_price=Decimal("2.0330"),
    )
    rep3 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("2.0330"), t3_utc),
        now_utc=t3_utc,
    )
    assert rep3.trading_state_after == TradingState.PROTECTED
    assert rep3.c01_report is not None
    assert rep3.c01_report.action == ExitSignalAction.HOLD_PROTECTED
    assert rep3.pinned_position_exit_version == "C2-v1.0"
    assert rep3.active_paper_exit_version == "C1-v2.0"

    # Cycle 4: Price reaches 2.0420 (>= C2 Tranche 2 +1.8% net target 2.0401) -> fills Tranche 2 (30%)
    t4_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("2.0330"),
        high_price=Decimal("2.0420"),
        low_price=Decimal("2.0310"),
        close_price=Decimal("2.0410"),
    )
    rep4 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("2.0410"), t4_utc),
        now_utc=t4_utc,
    )
    assert rep4.trading_state_after == TradingState.PROTECTED
    assert rep4.c01_report is not None
    assert rep4.c01_report.action == ExitSignalAction.PARTIAL_TAKE_PROFIT_AND_RATCHET_STOP
    assert rep4.c01_report.active_protection_stop_price == Decimal("2.0241")

    # Cycle 5: Price peaks at 2.0600 and pulls back to 2.0450 (<= 2.0600 * (1 - 0.006) = 2.0476)
    # -> triggers Tranche 3 trailing stop full exit, closing Position #1 to FLAT -> READY_NO_POSITION!
    t5_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("2.0410"),
        high_price=Decimal("2.0600"),
        low_price=Decimal("2.0440"),
        close_price=Decimal("2.0450"),
    )
    rep5 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("2.0450"), t5_utc),
        now_utc=t5_utc,
    )
    assert rep5.trading_state_after == TradingState.READY_NO_POSITION
    assert rep5.c01_report is not None
    assert rep5.c01_report.action == ExitSignalAction.FULL_EXIT_TRAILING_STOP
    assert registry.pin_registry.active_pinned_count == 0

    # Cycle 6: New upward breakout above prior 20-bar high (2.0600) at 2.0606 -> opens Position #2
    # Verify Position #2 now uses the newly promoted C1-v2.0 exit strategy!
    t6_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("2.0580"),
        high_price=Decimal("2.0608"),
        low_price=Decimal("2.0575"),
        close_price=Decimal("2.0606"),
    )
    rep6 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("2.0600"), t6_utc),
        immediate_fill_qty=Decimal("500.0"),
        immediate_fill_price=Decimal("2.0600"),
        now_utc=t6_utc,
    )
    assert rep6.trading_state_after == TradingState.PROTECTED
    assert rep6.position_ledger is not None
    assert rep6.position_ledger.position_id != pos1_id
    assert rep6.position_ledger.exit_strategy_version == "C1-v2.0"
    assert rep6.pinned_position_exit_version == "C1-v2.0"
    assert registry.pin_registry.version_crosstalk_violations == 0
    assert pipeline.oco_coordinator.over_reservation_violations == 0
    assert pipeline.oco_coordinator.naked_position_violations == 0
    store.close()


def test_qa12_auto_rollback_on_latency_slo_breach(tmp_path: Path) -> None:
    """QA-12: Verify that when an active PAPER model exceeds `max_inference_latency_ms`,
    `AutoRollbackGuard` immediately triggers atomic rollback to `rollback_ref` and emits a HIGH alert."""
    now_utc = datetime(2026, 10, 31, 13, 0, 0, tzinfo=timezone.utc)
    store = StateStore(tmp_path / "p6_qa12_lat_rb.db")
    alert_mgr = AlertManager(store=store)
    risk_cfg = RiskConfig()

    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes_raw().hex()

    registry = E01ModelRegistry(
        store=store,
        alert_manager=alert_mgr,
        risk_config=risk_cfg,
        pinned_signer_pubkey_hex=pub_hex,
        max_inference_latency_ms=Decimal("2.0000"),
    )

    v1_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="1.0.0",
        risk_compatibility=risk_cfg.version,
        rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0",
        intercepts=(1.40, -0.90, -0.50),
    )
    v1_bundle = E01ArtifactPackager.package_and_sign(
        model=v1_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    registry.bootstrap_initial_active_bundle(v1_bundle, now_utc=now_utc)

    v2_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="2.0.0-slow",
        risk_compatibility=risk_cfg.version,
        rollback_ref=v1_bundle.bundle_id,
        intercepts=(1.40, -0.90, -0.50),
    )
    v2_bundle = E01ArtifactPackager.package_and_sign(
        model=v2_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    registry.stage_bundle_to_shadow(v2_bundle, now_utc=now_utc)

    sui_bars, btc_bars = _build_sui_and_btc_bar_history(now_utc)
    _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("1.9990"),
        high_price=Decimal("2.0008"),
        low_price=Decimal("1.9985"),
        close_price=Decimal("2.0006"),
    )
    feat = FeaturePipeline().extract_at_index(sui_bars, btc_5m_bars=btc_bars)
    regime = A01RegimeClassifier().classify(feat, now_utc=feat.feature_time_utc)

    registry.evaluate_shadow_inference(feat, regime)
    registry.promote_shadow_to_paper(min_shadow_evaluations=1, now_utc=now_utc)
    assert registry.active_paper_bundle.bundle_id == v2_bundle.bundle_id

    # Inject a 6ms latency stall (> 2.0ms max_inference_latency_ms) on v2_bundle
    registry.inject_runtime_fault_hook(v2_bundle.bundle_id, lambda _mf: time.sleep(0.006))
    prop, _, rb_rec = registry.evaluate_active_paper_proposal(
        sui_features=feat,
        regime_decision=regime,
        now_utc=feat.feature_time_utc,
    )
    assert rb_rec is not None
    assert "INFERENCE_LATENCY_SLO_BREACH" in rb_rec.trigger_reason
    assert rb_rec.restored_bundle_id == v1_bundle.bundle_id
    assert registry.active_paper_bundle.bundle_id == v1_bundle.bundle_id
    assert prop is not None
    assert prop.model_version == "a02-suiusdt-breakout-3barrier:1.0.0"
    store.close()


def test_qa12_in_flight_c3_adaptive_hybrid_version_pinning_and_btc_flash_drop_exit(tmp_path: Path) -> None:
    """QA-12: Verify an in-flight position opened under `C3-v1.0` continues to execute `C3-v1.0`
    emergency risk-off exit on a `BTCUSDT` flash drop even after active PAPER hot-swaps to `C1-v2.0`."""
    base_utc = datetime(2026, 10, 31, 13, 30, 0, tzinfo=timezone.utc)
    filters = _make_dynamic_filters(base_utc)
    store = StateStore(tmp_path / "p6_qa12_c3_pin.db")
    alert_mgr = AlertManager(store=store)
    sys_cfg = SystemConfig(mode=ExecutionMode.PAPER)

    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes_raw().hex()

    registry = E01ModelRegistry(
        store=store,
        alert_manager=alert_mgr,
        risk_config=sys_cfg.risk,
        pinned_signer_pubkey_hex=pub_hex,
        filters=filters,
    )
    registry.pin_registry.register_exit_strategy(
        "C3-v1.0",
        C3AdaptiveHybridExit(
            c1_delegate=C1FixedNetTargetExit(filters=filters),
            c2_delegate=C2GridTrailingExit(filters=filters),
        ),
    )
    registry.pin_registry.register_exit_strategy("C1-v2.0", C1FixedNetTargetExit(filters=filters))

    v1_bundle = E01ArtifactPackager.package_and_sign(
        model=A02BreakoutCalibratedModel(
            version="1.0.0-c3",
            risk_compatibility=sys_cfg.risk.version,
            rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0",
            intercepts=(1.40, -0.90, -0.50),
        ),
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C3,
        exit_strategy_version="C3-v1.0",
        train_end_utc=base_utc,
        signed_at_utc=base_utc,
    )
    registry.bootstrap_initial_active_bundle(v1_bundle, now_utc=base_utc)

    v2_bundle = E01ArtifactPackager.package_and_sign(
        model=A02BreakoutCalibratedModel(
            version="2.0.0-c1",
            risk_compatibility=sys_cfg.risk.version,
            rollback_ref=v1_bundle.bundle_id,
            intercepts=(1.40, -0.90, -0.50),
        ),
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v2.0",
        train_end_utc=base_utc,
        signed_at_utc=base_utc,
    )
    registry.stage_bundle_to_shadow(v2_bundle, now_utc=base_utc)

    exchange = PaperOrderbookSimulator(
        initial_usdt=Decimal("10000.0000"),
        initial_sui=Decimal("0.0"),
        commission_asset="USDT",
        filters=filters,
        execution_mode=ExecutionMode.PAPER,
    )
    pipeline = D01IntegratedPipeline(
        config=sys_cfg,
        store=store,
        alert_manager=alert_mgr,
        exchange=exchange,
        registry=registry,
        filters=filters,
    )

    sui_bars, btc_bars = _build_sui_and_btc_bar_history(base_utc)
    t1_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("1.9990"),
        high_price=Decimal("2.0008"),
        low_price=Decimal("1.9985"),
        close_price=Decimal("2.0006"),
    )
    rep1 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("2.0000"), t1_utc),
        immediate_fill_qty=Decimal("500.0"),
        immediate_fill_price=Decimal("2.0000"),
        now_utc=t1_utc,
    )
    assert rep1.trading_state_after == TradingState.PROTECTED
    assert rep1.pinned_position_exit_version == "C3-v1.0"

    # Promote v2.0.0 (C1-v2.0) while position is open
    registry.promote_shadow_to_paper(min_shadow_evaluations=1, now_utc=t1_utc)
    assert registry.active_paper_exit_strategy_version == "C1-v2.0"

    # Next bar: BTC flash drops -1.20% (-120 bps <= -80 bps threshold), while SUI price is 1.9960 (above -1% stop 1.9800).
    # C1-v2.0 would hold until 1.9800, whereas pinned C3-v1.0 immediately exits via FULL_EXIT_DEFENSIVE_RISK_OFF!
    t2_utc = _append_sui_and_btc_bar(
        sui_bars,
        btc_bars,
        open_price=Decimal("2.0006"),
        high_price=Decimal("2.0010"),
        low_price=Decimal("1.9950"),
        close_price=Decimal("1.9960"),
        btc_return_bps=Decimal("-120"),
    )
    rep2 = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=_make_orderbook(Decimal("1.9960"), t2_utc),
        now_utc=t2_utc,
    )
    assert rep2.trading_state_after == TradingState.READY_NO_POSITION
    assert rep2.c01_report is not None
    assert rep2.c01_report.action == ExitSignalAction.FULL_EXIT_DEFENSIVE_RISK_OFF
    assert rep2.pinned_position_exit_version == "C3-v1.0"
    store.close()


def test_p6_end_to_end_integrated_pipeline_slo_and_memory_budget(tmp_path: Path) -> None:
    """P6 End-to-End Stress & Governance Benchmark: Run 200 closed-bar cycles through
    `F-01 -> FeaturePipeline -> A-01 -> E-01 (ACTIVE + SHADOW) -> R-01 -> B-01 -> D-01 -> C-01`,
    verifying P99 cycle latency <= 50.0 ms (SLO <= 150.0 ms), memory delta <= 15.0 MiB,
    and archiving `evidence/p6_e01_integration_and_rollback_evidence.json`."""
    base_utc = datetime(2026, 10, 31, 14, 0, 0, tzinfo=timezone.utc)
    filters = _make_dynamic_filters(base_utc)
    store = StateStore(tmp_path / "p6_e2e_stress.db")
    alert_mgr = AlertManager(store=store)
    sys_cfg = SystemConfig(mode=ExecutionMode.PAPER)

    priv_key = Ed25519PrivateKey.generate()
    pub_hex = priv_key.public_key().public_bytes_raw().hex()

    registry = E01ModelRegistry(
        store=store,
        alert_manager=alert_mgr,
        risk_config=sys_cfg.risk,
        pinned_signer_pubkey_hex=pub_hex,
        filters=filters,
    )
    registry.pin_registry.register_exit_strategy("C1-v1.0", C1FixedNetTargetExit(filters=filters))
    registry.pin_registry.register_exit_strategy("C2-v1.1", C2GridTrailingExit(filters=filters))
    registry.pin_registry.register_exit_strategy(
        "C3-v1.2",
        C3AdaptiveHybridExit(
            c1_delegate=C1FixedNetTargetExit(filters=filters),
            c2_delegate=C2GridTrailingExit(filters=filters),
        ),
    )

    v1_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="1.0.0-baseline",
        risk_compatibility=sys_cfg.risk.version,
        rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0-genesis",
        intercepts=(1.40, -0.90, -0.50),
    )
    v1_bundle = E01ArtifactPackager.package_and_sign(
        model=v1_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v1.0",
        train_end_utc=base_utc,
        signed_at_utc=base_utc,
    )
    registry.bootstrap_initial_active_bundle(v1_bundle, now_utc=base_utc)

    v2_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="1.1.0-c2-candidate",
        risk_compatibility=sys_cfg.risk.version,
        rollback_ref=v1_bundle.bundle_id,
        intercepts=(1.40, -0.90, -0.50),
    )
    v2_bundle = E01ArtifactPackager.package_and_sign(
        model=v2_model,
        signing_private_key=priv_key,
        exit_strategy_id=ExitStrategyId.C2,
        exit_strategy_version="C2-v1.1",
        train_end_utc=base_utc,
        signed_at_utc=base_utc,
    )
    registry.stage_bundle_to_shadow(v2_bundle, now_utc=base_utc)

    exchange = PaperOrderbookSimulator(
        initial_usdt=Decimal("10000.0000"),
        initial_sui=Decimal("0.0"),
        commission_asset="USDT",
        filters=filters,
        execution_mode=ExecutionMode.PAPER,
    )
    pipeline = D01IntegratedPipeline(
        config=sys_cfg,
        store=store,
        alert_manager=alert_mgr,
        exchange=exchange,
        registry=registry,
        filters=filters,
    )

    sui_bars, btc_bars = _build_sui_and_btc_bar_history(base_utc, n_warmup=25)

    tracemalloc.start()
    mem_before, _ = tracemalloc.get_traced_memory()

    promotion_latency_ms = 0.0
    rollback_latency_ms = 0.0
    entries_opened = 0
    exits_completed = 0

    for i in range(200):
        prev_c = sui_bars[-1].close_price
        if i % 25 == 5 and pipeline.d01_engine.current_state == TradingState.READY_NO_POSITION:
            # Generate an upward volume breakout bar just above prev_20_high
            prev_20_high = max(b.high_price for b in sui_bars[-20:])
            c_px = prev_20_high + Decimal("0.0006")
            o_px = prev_20_high - Decimal("0.0008")
            h_px = c_px + Decimal("0.0002")
            l_px = o_px - Decimal("0.0004")
            vol = Decimal("52000.0")
            ob_mid = prev_20_high
            fill_q: Decimal | None = Decimal("500.0")
        elif i % 25 == 12 and pipeline.d01_engine.current_state == TradingState.PROTECTED:
            # Generate a strong take-profit rally bar (+2.2% above entry)
            c_px = prev_c * Decimal("1.0220")
            c_px = (c_px // Decimal("0.0001")) * Decimal("0.0001")
            o_px = prev_c
            h_px = c_px + Decimal("0.0020")
            l_px = o_px - Decimal("0.0005")
            vol = Decimal("35000.0")
            ob_mid = c_px
            fill_q = None
        else:
            # Mild consolidation bar
            delta = Decimal("0.0002") if (i % 2 == 0) else Decimal("-0.0001")
            c_px = max(Decimal("1.8000"), prev_c + delta)
            o_px = prev_c
            h_px = max(o_px, c_px) + Decimal("0.0003")
            l_px = min(o_px, c_px) - Decimal("0.0003")
            vol = Decimal("14000.0")
            ob_mid = c_px
            fill_q = None

        bar_utc = _append_sui_and_btc_bar(
            sui_bars,
            btc_bars,
            open_price=o_px,
            high_price=h_px,
            low_price=l_px,
            close_price=c_px,
            base_volume=vol,
        )

        # Keep rolling buffer bounded to 60 bars as on the 2GB cloud node
        if len(sui_bars) > 60:
            del sui_bars[0]
            del btc_bars[0]

        # Refresh dynamic exchangeInfo filters every 6 bars (30 minutes) as in production F-01/D-01
        if i % 6 == 0:
            fresh_f = _make_dynamic_filters(bar_utc)
            pipeline.filters = fresh_f
            pipeline.d01_engine.filters = fresh_f

        if i == 40 and registry.shadow_bundle is not None:
            _, promo_ns = registry.promote_shadow_to_paper(min_shadow_evaluations=10, now_utc=bar_utc)
            promotion_latency_ms = round(promo_ns / 1e6, 4)

        if i == 110:
            # Inject runtime failure on v2_bundle to trigger automatic rollback during live cycle
            registry.inject_runtime_fault_hook(
                v2_bundle.bundle_id,
                lambda _mf: (_ for _ in ()).throw(RuntimeError("INJECTED_STRESS_CYCLE_110_FAULT")),
            )

        rep = pipeline.step_closed_bar_cycle(
            sui_5m_bars=sui_bars,
            btc_5m_bars=btc_bars,
            orderbook=_make_orderbook(ob_mid, bar_utc, update_id=2000 + i),
            immediate_fill_qty=fill_q,
            immediate_fill_price=ob_mid if fill_q is not None else None,
            now_utc=bar_utc,
        )

        if rep.trading_state_before == TradingState.READY_NO_POSITION and rep.trading_state_after == TradingState.PROTECTED:
            entries_opened += 1
        if rep.trading_state_before == TradingState.PROTECTED and rep.trading_state_after == TradingState.READY_NO_POSITION:
            exits_completed += 1
        if rep.rollback_record is not None:
            rollback_latency_ms = round(rep.rollback_record.rollback_latency_ns / 1e6, 4)

    mem_after, mem_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    mem_delta_mib = round((mem_after - mem_before) / (1024 * 1024), 4)
    mem_peak_mib = round(mem_peak / (1024 * 1024), 4)

    lats_ms = sorted(ns / 1e6 for ns in pipeline.cycle_latencies_ns)
    n_lats = len(lats_ms)
    p50_ms = round(lats_ms[int(0.50 * n_lats)], 4)
    p95_ms = round(lats_ms[int(0.95 * n_lats)], 4)
    p99_ms = round(lats_ms[min(n_lats - 1, int(0.99 * n_lats))], 4)

    assert p99_ms <= 50.0  # Strictly below 50 ms budget (and 150 ms SLO)
    assert promotion_latency_ms <= 5.0
    assert rollback_latency_ms > 0.0 and rollback_latency_ms <= 5.0
    assert mem_delta_mib <= 15.0
    assert entries_opened >= 2
    assert exits_completed >= 1
    assert pipeline.live_orders_sent_count == 0
    assert registry.shadow_orders_submitted_count == 0
    assert registry.pin_registry.version_crosstalk_violations == 0
    assert pipeline.oco_coordinator.over_reservation_violations == 0
    assert pipeline.oco_coordinator.naked_position_violations == 0

    evidence_doc = {
        "phase": "P6",
        "status": "IMPLEMENTED_AND_VERIFIED",
        "execution_mode_locked": "PAPER",
        "live_orders_sent_count": 0,
        "shadow_orders_submitted_count": 0,
        "qa12_governance_and_rollback_audit": {
            "tampered_sha256_rejected": True,
            "forged_ed25519_signature_rejected": True,
            "features_schema_mismatch_rejected": True,
            "risk_compatibility_mismatch_rejected": True,
            "symbol_and_live_gate_override_rejected": True,
            "missing_rollback_target_rejected": True,
            "shadow_to_paper_atomic_promotion_verified": True,
            "auto_rollback_on_runtime_fault_verified": True,
            "in_flight_position_exit_version_pinning_verified": True,
            "in_flight_version_crosstalk_violations": registry.pin_registry.version_crosstalk_violations,
            "over_reservation_violations": pipeline.oco_coordinator.over_reservation_violations,
            "naked_position_violations": pipeline.oco_coordinator.naked_position_violations,
        },
        "latency_and_memory_slo_benchmark_200_cycles": {
            "cycles_evaluated": n_lats,
            "entries_opened": entries_opened,
            "exits_completed": exits_completed,
            "cycle_p50_ms": p50_ms,
            "cycle_p95_ms": p95_ms,
            "cycle_p99_ms": p99_ms,
            "cycle_slo_limit_ms": 150.0,
            "cycle_p6_target_ms": 50.0,
            "shadow_to_paper_promotion_latency_ms": promotion_latency_ms,
            "atomic_auto_rollback_latency_ms": rollback_latency_ms,
            "rollback_slo_limit_ms": 5.0,
            "memory_delta_mib": mem_delta_mib,
            "memory_peak_traced_mib": mem_peak_mib,
            "memory_budget_limit_mib": 15.0,
        },
        "cross_phase_pending_items": {
            "p3_p5_real_historical_statistical_verdict": "NO_EDGE",
            "fi_10b_japan_cloud_72h_real_wall_clock_soak": "PENDING",
            "external_cloudflare_dms_and_fcm_real_push": "PENDING",
        },
    }
    evidence_path = Path(__file__).resolve().parents[1] / "evidence" / "p6_e01_integration_and_rollback_evidence.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence_doc, indent=2), encoding="utf-8")
    store.close()
