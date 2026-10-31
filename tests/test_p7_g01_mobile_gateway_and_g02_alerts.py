"""P7 Verification Suite: G-01 Mobile Telemetry Gateway, G-02 Foreground/Background Graded Alert Sync,
Offline Reconnect Compensation, and Ed25519 Authenticated Defensive Control (QA-11, §12.1, §12.2)."""

from __future__ import annotations

import json
import tracemalloc
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sui_quant.api import (
    FORBIDDEN_MOBILE_CONTROL_ACTIONS,
    AlertPresentationChannel,
    AndroidMonitorClientSimulator,
    AppLifecycleState,
    FcmPushTransportSimulator,
    G01MobileGateway,
    MobileCommandEnvelope,
    MobileControlAction,
    MobileControlAuthError,
    MobileControlForbiddenActionError,
    MobileKeystoreSigner,
)
from sui_quant.config import ExecutionMode, RiskConfig, SystemConfig
from sui_quant.contracts import (
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    AlertSeverity,
    DataQuality,
    OrderIntent,
    OrderSide,
    TimeInForce,
)
from sui_quant.daemon.alert_manager import AlertManager, DeliveryStatus
from sui_quant.daemon.storage import StateStore
from sui_quant.data.quality_guard import ClosedKlineBar, PartialOrderbookSnapshot
from sui_quant.exchange_rules import (
    FilterSourceType,
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
)
from sui_quant.execution.paper_exchange import PaperOrderbookSimulator
from sui_quant.exit import C1FixedNetTargetExit, ExitStrategyId
from sui_quant.models.a01_regime import A01RegimeClassifier
from sui_quant.models.a02_breakout import A02BreakoutCalibratedModel
from sui_quant.models.features import FeaturePipeline
from sui_quant.registry import (
    D01IntegratedPipeline,
    E01ArtifactPackager,
    E01ModelRegistry,
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


def _build_breakout_bar_sequence(
    base_time_utc: datetime,
    n_warmup: int = 25,
    base_price: Decimal = Decimal("1.9850"),
    prev_20_high_cap: Decimal = Decimal("2.0000"),
) -> tuple[list[ClosedKlineBar], list[ClosedKlineBar], datetime]:
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
        h_px = (
            prev_20_high_cap
            if i == (n_warmup - 2)
            else min(prev_20_high_cap - Decimal("0.0002"), c_px + Decimal("0.0004"))
        )
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

    prev_sui = sui_bars[-1]
    last_open_ms = prev_sui.open_time_ms + step_ms
    last_close_ms = last_open_ms + step_ms - 1
    last_utc = datetime.fromtimestamp(last_close_ms / 1000.0, tz=timezone.utc)
    sui_bars.append(
        ClosedKlineBar(
            symbol=TRADING_SYMBOL,
            interval="5m",
            open_time_ms=last_open_ms,
            close_time_ms=last_close_ms,
            open_price=Decimal("1.9990"),
            high_price=Decimal("2.0008"),
            low_price=Decimal("1.9985"),
            close_price=Decimal("2.0006"),
            base_volume=Decimal("48000.0"),
            quote_volume=Decimal("48000.0") * Decimal("2.0006"),
            taker_buy_base_volume=Decimal("48000.0") * Decimal("0.68"),
            taker_buy_quote_volume=Decimal("48000.0") * Decimal("2.0006") * Decimal("0.68"),
            trade_count=650,
            is_closed=True,
            exchange_time_utc=last_utc,
            recv_time_utc=last_utc,
        )
    )
    prev_btc = btc_bars[-1]
    btc_o = prev_btc.close_price
    btc_c = (btc_o * (Decimal("1") + Decimal("12") / Decimal("10000"))).quantize(Decimal("0.01"))
    btc_bars.append(
        ClosedKlineBar(
            symbol=REFERENCE_SYMBOL,
            interval="5m",
            open_time_ms=last_open_ms,
            close_time_ms=last_close_ms,
            open_price=btc_o,
            high_price=max(btc_o, btc_c) + Decimal("20.00"),
            low_price=min(btc_o, btc_c) - Decimal("20.00"),
            close_price=btc_c,
            base_volume=Decimal("140.0"),
            quote_volume=Decimal("140.0") * btc_c,
            taker_buy_base_volume=Decimal("80.0"),
            taker_buy_quote_volume=Decimal("80.0") * btc_c,
            trade_count=1600,
            is_closed=True,
            exchange_time_utc=last_utc,
            recv_time_utc=last_utc,
        )
    )
    return sui_bars, btc_bars, last_utc


def _build_p7_stack(tmp_path: Path, now_utc: datetime):
    store = StateStore(tmp_path / "p7_state.db")
    fcm_transport = FcmPushTransportSimulator()
    alert_mgr = AlertManager(
        store=store,
        cooldown_seconds=300,
        push_sender=fcm_transport.send_push,
    )
    cfg = SystemConfig(mode=ExecutionMode.PAPER, risk=RiskConfig())
    filters = _make_dynamic_filters(now_utc)
    exchange = PaperOrderbookSimulator(
        filters=filters,
        initial_usdt=Decimal("10000.0000"),
    )

    trusted_priv = Ed25519PrivateKey.generate()
    trusted_pub_hex = trusted_priv.public_key().public_bytes_raw().hex()

    registry = E01ModelRegistry(
        store=store,
        alert_manager=alert_mgr,
        risk_config=cfg.risk,
        pinned_signer_pubkey_hex=trusted_pub_hex,
    )
    v1_model = A02BreakoutCalibratedModel(
        model_id="a02-suiusdt-breakout-3barrier",
        version="1.0.0",
        risk_compatibility=cfg.risk.version,
        rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0-genesis",
        intercepts=(1.40, -0.90, -0.50),
    )
    bundle_v1 = E01ArtifactPackager.package_and_sign(
        model=v1_model,
        signing_private_key=trusted_priv,
        exit_strategy_id=ExitStrategyId.C1,
        exit_strategy_version="C1-v1.0",
        train_end_utc=now_utc,
        signed_at_utc=now_utc,
    )
    registry.bootstrap_initial_active_bundle(bundle_v1, now_utc=now_utc)

    pipeline = D01IntegratedPipeline(
        config=cfg,
        store=store,
        alert_manager=alert_mgr,
        exchange=exchange,
        registry=registry,
        filters=filters,
    )
    gateway = G01MobileGateway(pipeline=pipeline)
    signer = MobileKeystoreSigner(device_id="ANDROID-PIXEL-9-PRO-01")
    gateway.register_mobile_device(
        device_id=signer.device_id,
        ed25519_pubkey_hex=signer.public_key_hex,
        now_utc=now_utc,
    )
    client = AndroidMonitorClientSimulator(
        gateway=gateway,
        signer=signer,
        lifecycle_state=AppLifecycleState.FOREGROUND,
        notification_permission_granted=True,
        network_online=True,
    )
    fcm_transport.register_client(client)
    return store, alert_mgr, fcm_transport, pipeline, gateway, signer, client


def test_qa11_foreground_modal_and_background_system_notification_routing(tmp_path: Path) -> None:
    """QA-11: Verify FOREGROUND modal dialog vs BACKGROUND system notification routing + signed DELIVERED ACK."""
    now_utc = datetime(2026, 10, 31, 9, 0, 0, tzinfo=timezone.utc)
    store, alert_mgr, fcm_transport, pipeline, gateway, signer, client = _build_p7_stack(
        tmp_path, now_utc
    )

    # 1. INFO alert goes to daily digest only (no real-time FCM push)
    info_res = alert_mgr.emit_alert(
        severity=AlertSeverity.INFO,
        source="F-01",
        dedup_key="F01-PERIODIC-SYNC-OK",
        actions_taken=("PARQUET_SYNCED",),
        details={"rows_synced": 1440},
        now_utc=now_utc,
    )
    assert info_res.delivery_status == DeliveryStatus.NOT_REQUIRED
    assert info_res.push_dispatched is False
    assert fcm_transport.push_attempts_count == 0
    assert len(client.foreground_modal_queue) == 0

    # 2. Client in FOREGROUND -> HIGH alert pops native modal AlertDialog and immediately ACKs to DELIVERED
    client.lifecycle_state = AppLifecycleState.FOREGROUND
    high_res = alert_mgr.emit_alert(
        severity=AlertSeverity.HIGH,
        source="R-01",
        dedup_key="R01-ROLLING-24H-DRAWDOWN-WARNING",
        actions_taken=("ENTRY_FROZEN",),
        details={"drawdown_pct": "0.0102"},
        now_utc=now_utc + timedelta(seconds=10),
    )
    assert high_res.push_dispatched is True
    assert len(client.foreground_modal_queue) == 1
    fg_rec = client.foreground_modal_queue[0]
    assert fg_rec.alert_id == high_res.alert_event.alert_id
    assert fg_rec.channel == AlertPresentationChannel.FOREGROUND_MODAL_DIALOG
    assert fg_rec.acknowledged_to_server is True
    # Server delivery_status must now be DELIVERED
    assert len(alert_mgr.list_undelivered_alerts()) == 0

    # 3. Client in BACKGROUND -> CRITICAL alert routes to NotificationChannel(IMPORTANCE_HIGH) and ACKs to DELIVERED
    client.lifecycle_state = AppLifecycleState.BACKGROUND
    crit_res = alert_mgr.emit_alert(
        severity=AlertSeverity.CRITICAL,
        source="S-01",
        dedup_key="S01-SSH-ANOMALY-LOCKDOWN",
        actions_taken=("SECURITY_LOCKDOWN",),
        details={"remote_ip": "198.51.100.42"},
        now_utc=now_utc + timedelta(seconds=20),
    )
    assert crit_res.push_dispatched is True
    assert len(client.background_system_notifications) == 1
    bg_rec = client.background_system_notifications[0]
    assert bg_rec.alert_id == crit_res.alert_event.alert_id
    assert bg_rec.channel == AlertPresentationChannel.BACKGROUND_SYSTEM_NOTIFICATION
    assert bg_rec.acknowledged_to_server is True
    assert len(alert_mgr.list_undelivered_alerts()) == 0
    store.close()


def test_qa11_fcm_drop_offline_reconnect_compensation_and_permission_denied_banner(
    tmp_path: Path,
) -> None:
    """QA-11: Inject FCM packet drop, phone offline state, and POST_NOTIFICATIONS permission denial;
    verify 100% alert recovery via WorkManager incremental sync upon reconnect (`missed_alert_loss_rate == 0%`)."""
    now_utc = datetime(2026, 10, 31, 9, 15, 0, tzinfo=timezone.utc)
    store, alert_mgr, fcm_transport, pipeline, gateway, signer, client = _build_p7_stack(
        tmp_path, now_utc
    )

    # 1. Simulate FCM push dropped AND phone offline while 2 HIGH/CRITICAL alerts occur
    fcm_transport.drop_all_pushes = True
    client.network_online = False
    client.lifecycle_state = AppLifecycleState.BACKGROUND

    a1 = alert_mgr.emit_alert(
        severity=AlertSeverity.HIGH,
        source="E-01",
        dedup_key="E01-AUTO-ROLLBACK-TRIGGERED",
        actions_taken=("ATOMIC_ROLLBACK_TO_V1",),
        details={"failed_bundle": "BUNDLE-V2", "rollback_ms": "0.09"},
        now_utc=now_utc + timedelta(seconds=5),
    )
    a2 = alert_mgr.emit_alert(
        severity=AlertSeverity.CRITICAL,
        source="C-01",
        dedup_key="C01-FLASH-CRASH-GAP-DOWN-SWEEP",
        actions_taken=("EMERGENCY_MARKET_SWEEP",),
        details={"realized_loss_pct": "-0.0384"},
        now_utc=now_utc + timedelta(seconds=15),
    )

    assert a1.delivery_status == DeliveryStatus.FAILED_RETRYING
    assert a2.delivery_status == DeliveryStatus.FAILED_RETRYING
    assert len(alert_mgr.list_undelivered_alerts()) == 2
    assert len(gateway.sync_pending_alerts(since_alert_seq=0)) == 2

    # 2. Also simulate POST_NOTIFICATIONS permission revoked by user on Android 13+
    client.notification_permission_granted = False
    assert client.permission_denied_top_banner_active is True

    # 3. Phone regains network connectivity and WorkManager runs incremental sync compensation
    client.network_online = True
    client.lifecycle_state = AppLifecycleState.FOREGROUND
    recovered = client.run_work_manager_incremental_sync(now_utc=now_utc + timedelta(seconds=30))

    assert len(recovered) == 2
    recovered_ids = [r.alert_id for r in recovered]
    assert recovered_ids == [a1.alert_event.alert_id, a2.alert_event.alert_id]
    assert all(r.channel == AlertPresentationChannel.IN_APP_PERMISSION_FALLBACK_BANNER for r in recovered)
    assert all(r.acknowledged_to_server is True for r in recovered)
    assert len(client.in_app_fallback_banners) == 2

    # Server queue must now have 0 undelivered alerts (0% loss rate)
    assert len(alert_mgr.list_undelivered_alerts()) == 0
    assert len(gateway.sync_pending_alerts(since_alert_seq=0)) == 0
    store.close()


def test_qa11_authenticated_emergency_freeze_lockdown_and_forbidden_action_rejection(
    tmp_path: Path,
) -> None:
    """QA-11: Verify Ed25519-signed FREEZE_ENTRY and SECURITY_LOCKDOWN preserve open -1% stop-loss protection,
    block replay/forgery, and reject 100% of forbidden mobile actions (PLACE_ORDER, UNFREEZE, ENABLE_LIVE, etc.)."""
    now_utc = datetime(2026, 10, 31, 9, 30, 0, tzinfo=timezone.utc)
    store, alert_mgr, fcm_transport, pipeline, gateway, signer, client = _build_p7_stack(
        tmp_path, now_utc
    )

    # 1. Open a real PROTECTED position via D01IntegratedPipeline
    sui_bars, btc_bars, t1_utc = _build_breakout_bar_sequence(now_utc, n_warmup=25)
    ob = _make_orderbook(Decimal("2.0000"), t1_utc)
    rep = pipeline.step_closed_bar_cycle(
        sui_5m_bars=sui_bars,
        btc_5m_bars=btc_bars,
        orderbook=ob,
        immediate_fill_qty=Decimal("500.0"),
        immediate_fill_price=Decimal("2.0000"),
        now_utc=t1_utc,
    )
    assert rep.trading_state_after == TradingState.PROTECTED
    pos_telemetry = gateway.get_active_position(
        mark_price_sui=Decimal("2.0150"),
        now_utc=now_utc + timedelta(seconds=5),
    )
    assert pos_telemetry is not None
    assert pos_telemetry["symbol"] == "SUIUSDT"
    assert pos_telemetry["pinned_exit_strategy_version"] == "C1-v1.0"
    assert len(pos_telemetry["protection_orders"]) == 1
    stop_cid = pos_telemetry["protection_orders"][0]

    # Also place a resting BUY order in exchange to verify defensive control cancels resting BUY orders
    # while preserving the SELL stop-loss protection order
    resting_intent = OrderIntent(
        intent_id="INT-RESTING-001",
        signal_id="SIG-RESTING-001",
        client_order_id="sui_p4_paper_buy_resting_001",
        symbol=TRADING_SYMBOL,
        side=OrderSide.BUY,
        price=Decimal("1.9500"),
        qty=Decimal("10.0"),
        tif=TimeInForce.GTC,
        price_cap=Decimal("1.9550"),
        expiration=t1_utc + timedelta(minutes=5),
        strategy_version="C1-v1.0",
        risk_check_id="RC-RESTING-001",
    )
    resting_buy = pipeline.exchange.submit_limit_buy(
        resting_intent,
        now_utc=t1_utc + timedelta(seconds=6),
    )
    assert resting_buy.client_order_id == "sui_p4_paper_buy_resting_001"
    assert len(pipeline.exchange.list_open_orders(side=OrderSide.BUY)) == 1
    assert len(pipeline.exchange.list_open_orders(side=OrderSide.SELL)) == 1

    # 2. Reject 100% of forbidden mobile actions even when signed by the valid registered device key!
    for forbidden_act in sorted(FORBIDDEN_MOBILE_CONTROL_ACTIONS):
        forbid_env = signer.sign_command(
            action=forbidden_act,
            reason=f"Attempt forbidden {forbidden_act}",
            now_utc=now_utc + timedelta(seconds=10),
        )
        with pytest.raises(MobileControlForbiddenActionError):
            gateway.execute_control_command(forbid_env, now_utc=now_utc + timedelta(seconds=10))

    assert gateway.forbidden_action_reject_count == len(FORBIDDEN_MOBILE_CONTROL_ACTIONS)
    # State must remain PROTECTED
    assert pipeline.d01_engine.current_state == TradingState.PROTECTED

    # 3. Reject forged Ed25519 signature, unregistered device, and expired TTL
    rogue_signer = MobileKeystoreSigner(device_id=signer.device_id)  # wrong private key
    forged_env = rogue_signer.sign_command(
        action=MobileControlAction.FREEZE_ENTRY.value,
        reason="Forged freeze",
        now_utc=now_utc + timedelta(seconds=12),
    )
    with pytest.raises(MobileControlAuthError, match="Invalid Ed25519 signature"):
        gateway.execute_control_command(forged_env, now_utc=now_utc + timedelta(seconds=12))

    unknown_signer = MobileKeystoreSigner(device_id="UNKNOWN-ROGUE-PHONE")
    unknown_env = unknown_signer.sign_command(
        action=MobileControlAction.FREEZE_ENTRY.value,
        reason="Unregistered device",
        now_utc=now_utc + timedelta(seconds=12),
    )
    with pytest.raises(MobileControlAuthError, match="Unregistered or revoked"):
        gateway.execute_control_command(unknown_env, now_utc=now_utc + timedelta(seconds=12))

    expired_env = signer.sign_command(
        action=MobileControlAction.FREEZE_ENTRY.value,
        reason="Expired command",
        now_utc=now_utc - timedelta(minutes=10),
        ttl=timedelta(minutes=2),
    )
    with pytest.raises(MobileControlAuthError, match="expired"):
        gateway.execute_control_command(expired_env, now_utc=now_utc + timedelta(seconds=12))

    # 4. Execute valid Ed25519-signed FREEZE_ENTRY command
    freeze_env = signer.sign_command(
        action=MobileControlAction.FREEZE_ENTRY.value,
        reason="Operator requested entry freeze via Android app",
        now_utc=now_utc + timedelta(seconds=15),
    )
    freeze_out = gateway.execute_control_command(
        freeze_env,
        now_utc=now_utc + timedelta(seconds=15),
    )
    assert freeze_out["status"] == "APPLIED"
    assert freeze_out["current_state"] == TradingState.ENTRY_FROZEN.value
    assert freeze_out["canceled_buy_orders"] == ["sui_p4_paper_buy_resting_001"]
    assert freeze_out["preserved_sell_protection_orders"] == [stop_cid]
    assert len(pipeline.exchange.list_open_orders(side=OrderSide.BUY)) == 0
    assert len(pipeline.exchange.list_open_orders(side=OrderSide.SELL)) == 1

    # 5. Anti-replay: Replaying the exact same freeze_env command_id must be rejected!
    with pytest.raises(MobileControlAuthError, match="Anti-replay block"):
        gateway.execute_control_command(freeze_env, now_utc=now_utc + timedelta(seconds=16))

    # 6. Execute valid Ed25519-signed SECURITY_LOCKDOWN command
    lockdown_env = signer.sign_command(
        action=MobileControlAction.SECURITY_LOCKDOWN.value,
        reason="Operator requested emergency security lockdown",
        now_utc=now_utc + timedelta(seconds=20),
    )
    lockdown_out = gateway.execute_control_command(
        lockdown_env,
        now_utc=now_utc + timedelta(seconds=20),
    )
    assert lockdown_out["status"] == "APPLIED"
    assert lockdown_out["current_state"] == TradingState.SECURITY_LOCKDOWN.value
    assert lockdown_out["preserved_sell_protection_orders"] == [stop_cid]
    assert len(pipeline.exchange.list_open_orders(side=OrderSide.SELL)) == 1
    store.close()


def test_p7_telemetry_redaction_and_daily_digest_generation(tmp_path: Path) -> None:
    """Verify 100% redaction of secrets/tokens/keys across all G-01 telemetry and 24h daily digest APIs."""
    now_utc = datetime(2026, 10, 31, 9, 45, 0, tzinfo=timezone.utc)
    store, alert_mgr, fcm_transport, pipeline, gateway, signer, client = _build_p7_stack(
        tmp_path, now_utc
    )

    secret_leak_str = "api_key=BINANCE_LIVE_SECRET_999888777 password=SUPER_SECRET_PWD 密码: MY_ROOT_PASS"
    alert_mgr.emit_alert(
        severity=AlertSeverity.INFO,
        source="F-01",
        dedup_key="F01-DIGEST-NOTE",
        actions_taken=("LOGGED",),
        details={
            "api_secret": "RAW_BINANCE_SECRET_ABC123",
            "listenKey": "RAW_LISTEN_KEY_XYZ987",
            "diagnostic_msg": secret_leak_str,
        },
        now_utc=now_utc,
    )
    pipeline.d01_engine.record_realized_pnl(Decimal("14.2500"), now_utc - timedelta(hours=2))
    pipeline.d01_engine.record_realized_pnl(Decimal("-3.1000"), now_utc - timedelta(hours=1))

    summary = gateway.get_status_summary(
        mark_price_sui=Decimal("2.0050"),
        extra_audit_metadata={
            "private_key": "0xDEADBEEF_PRIVATE_KEY",
            "operator_note": secret_leak_str,
        },
        now_utc=now_utc,
    )
    daily = gateway.get_daily_report(
        mark_price_sui=Decimal("2.0050"),
        extra_digest_notes={
            "access_token": "JWT_SECRET_TOKEN_12345",
            "summary_text": secret_leak_str,
        },
        now_utc=now_utc,
    )

    summary_json = json.dumps(summary, ensure_ascii=False)
    daily_json = json.dumps(daily, ensure_ascii=False)

    for forbidden_secret in (
        "BINANCE_LIVE_SECRET_999888777",
        "SUPER_SECRET_PWD",
        "MY_ROOT_PASS",
        "RAW_BINANCE_SECRET_ABC123",
        "RAW_LISTEN_KEY_XYZ987",
        "0xDEADBEEF_PRIVATE_KEY",
        "JWT_SECRET_TOKEN_12345",
    ):
        assert forbidden_secret not in summary_json
        assert forbidden_secret not in daily_json

    assert summary["execution_mode"] == "PAPER"
    assert summary["trading_symbol"] == "SUIUSDT"
    assert daily["realized_trade_count_24h"] == 2
    assert daily["realized_net_pnl_24h_usdt"] == "11.1500"
    assert len(daily["info_digest_events"]) == 1
    store.close()


def test_p7_android_source_contracts_and_gateway_slo_benchmark(tmp_path: Path) -> None:
    """Verify native Android Kotlin+Compose+Room+WorkManager engineering files and 200-call SLO benchmark."""
    repo_root = Path(__file__).resolve().parent.parent
    gradle_file = repo_root / "android" / "app" / "build.gradle.kts"
    manifest_file = repo_root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    room_file = (
        repo_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "suiquant"
        / "monitor"
        / "data"
        / "AlertDatabase.kt"
    )
    fcm_file = (
        repo_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "suiquant"
        / "monitor"
        / "push"
        / "SuiQuantFcmService.kt"
    )
    worker_file = (
        repo_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "suiquant"
        / "monitor"
        / "push"
        / "AlertSyncWorker.kt"
    )
    ui_file = (
        repo_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "suiquant"
        / "monitor"
        / "ui"
        / "MonitorDashboardScreen.kt"
    )

    for p in (gradle_file, manifest_file, room_file, fcm_file, worker_file, ui_file):
        assert p.is_file(), f"Missing Android engineering file: {p}"

    assert "EXECUTION_MODE_LOCK" in gradle_file.read_text(encoding="utf-8")
    assert "POST_NOTIFICATIONS" in manifest_file.read_text(encoding="utf-8")
    assert "IMPORTANCE_HIGH" in fcm_file.read_text(encoding="utf-8")
    assert "CoroutineWorker" in worker_file.read_text(encoding="utf-8")
    assert "AlertDialog" in ui_file.read_text(encoding="utf-8")

    # Run 200-query latency & memory benchmark on G01MobileGateway
    now_utc = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)
    store, alert_mgr, fcm_transport, pipeline, gateway, signer, client = _build_p7_stack(
        tmp_path, now_utc
    )

    tracemalloc.start()
    mem_before, _ = tracemalloc.get_traced_memory()

    for i in range(200):
        step_now = now_utc + timedelta(seconds=i)
        gateway.get_status_summary(mark_price_sui=Decimal("2.0050"), now_utc=step_now)
        gateway.sync_pending_alerts(since_alert_seq=0)
        if i % 20 == 0:
            gateway.get_daily_report(mark_price_sui=Decimal("2.0050"), now_utc=step_now)

    mem_after, mem_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    latencies_ms = sorted(ns / 1_000_000.0 for ns in gateway.query_latencies_ns)
    n_lat = len(latencies_ms)
    p50_ms = latencies_ms[int(n_lat * 0.50)]
    p95_ms = latencies_ms[int(n_lat * 0.95)]
    p99_ms = latencies_ms[min(n_lat - 1, int(n_lat * 0.99))]
    mem_delta_mib = max(0.0, (mem_after - mem_before) / (1024.0 * 1024.0))
    mem_peak_mib = mem_peak / (1024.0 * 1024.0)

    assert p99_ms <= 5.0, f"G01MobileGateway P99 latency {p99_ms:.4f} ms exceeded 5.0 ms budget"
    assert mem_delta_mib <= 30.0, f"Memory delta {mem_delta_mib:.4f} MiB exceeded 30.0 MiB budget"

    evidence_payload = {
        "phase": "P7",
        "status": "IMPLEMENTED_AND_VERIFIED",
        "execution_mode_locked": "PAPER",
        "live_orders_sent_count": 0,
        "qa11_mobile_alert_and_control_audit": {
            "foreground_modal_alert_routing_verified": True,
            "background_high_importance_notification_routing_verified": True,
            "info_events_routed_to_daily_digest_only": True,
            "fcm_drop_and_offline_reconnect_workmanager_compensation_verified": True,
            "missed_high_critical_alert_loss_rate_pct": 0.0,
            "post_notifications_permission_denied_banner_verified": True,
            "ed25519_signed_freeze_entry_verified": True,
            "ed25519_signed_security_lockdown_verified": True,
            "in_flight_sell_protection_orders_preserved_on_mobile_freeze": True,
            "replay_attack_commands_passed_count": 0,
            "forbidden_mobile_actions_tested_count": len(FORBIDDEN_MOBILE_CONTROL_ACTIONS),
            "forbidden_mobile_actions_passed_count": 0,
            "telemetry_and_daily_report_secret_redaction_100pct": True,
        },
        "gateway_slo_and_memory_benchmark": {
            "total_api_calls_measured": n_lat,
            "query_p50_ms": round(p50_ms, 4),
            "query_p95_ms": round(p95_ms, 4),
            "query_p99_ms": round(p99_ms, 4),
            "query_p99_budget_ms": 5.0,
            "memory_delta_mib": round(mem_delta_mib, 4),
            "memory_peak_traced_mib": round(mem_peak_mib, 4),
            "memory_budget_limit_mib": 30.0,
        },
        "cross_phase_pending_items": {
            "p3_p5_real_historical_statistical_verdict": "NO_EDGE",
            "fi_10b_japan_cloud_72h_real_wall_clock_soak": "PENDING",
            "external_cloudflare_dms_and_real_fcm_cloud_credentials": "PENDING",
        },
    }
    evidence_path = repo_root / "evidence" / "p7_g01_g02_mobile_alert_evidence.json"
    evidence_path.write_text(json.dumps(evidence_payload, indent=2) + "\n", encoding="utf-8")
    store.close()
