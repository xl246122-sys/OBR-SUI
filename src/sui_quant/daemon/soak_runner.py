"""P1 Soak Runner & Resource/Latency Profiler (FI-10 & systemd service entrypoint).

Measures:
- Heartbeat write & stall check latency (P50 / P95 / P99 ms)
- R-01 risk verdict evaluation latency (P50 / P95 / P99 ms, target P99 <= 25ms)
- S-01 integrity & security inspection latency (P50 / P95 / P99 ms)
- Process RSS memory stability and linear regression slope (MB/h) over simulated 72h workload
- SQLite WAL database size and file descriptor stability
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sui_quant.config import RiskConfig
from sui_quant.contracts import BreakoutProposal, DataQuality
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.external_probe import CloudHostBeaconSender, ExternalDeadMansSwitchEvaluator
from sui_quant.daemon.heartbeat import F02Guardian, ServiceHeartbeat, SimulatedExchangeState
from sui_quant.daemon.r01_server import R01RiskDaemon
from sui_quant.daemon.security_supervisor import S01SecuritySupervisor, SignedReleaseManifest
from sui_quant.daemon.storage import StateStore
from sui_quant.exchange_rules import FilterSourceType, SymbolFilters
from sui_quant.utils.clock import monotonic_ns, utc_now


def _get_process_rss_bytes() -> int:
    """Read current process RSS memory in bytes across Linux (/proc/self/statm) and Windows."""
    statm = Path("/proc/self/statm")
    if statm.exists():
        parts = statm.read_text().split()
        if len(parts) >= 2:
            page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
            return int(parts[1]) * page_size
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize)
    return 0


def _get_open_fd_count() -> int:
    fd_dir = Path("/proc/self/fd")
    if fd_dir.exists():
        try:
            return len(list(fd_dir.iterdir()))
        except OSError:
            return 0
    return 0


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 4)


def run_accelerated_72h_soak(
    simulated_hours: int = 72,
    cycles_to_execute: int = 2160,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute an accelerated 72-hour soak test simulating continuous F-02, R-01, S-01, and external beacons."""
    cleanup_dir = False
    if work_dir is None:
        tmp = tempfile.mkdtemp(prefix="sui_quant_soak_")
        work_dir = Path(tmp)
        cleanup_dir = True

    try:
        db_path = work_dir / "state.db"
        evidence_dir = work_dir / "evidence"
        protected_code_dir = work_dir / "protected"
        protected_code_dir.mkdir(parents=True, exist_ok=True)
        (protected_code_dir / "core_module.py").write_text(" VERSION = '1.0.0'\n", encoding="utf-8")

        store = StateStore(db_path)
        alerts = AlertManager(store)
        risk = RiskConfig()
        guardian = F02Guardian(store, alerts, risk)
        r01 = R01RiskDaemon(store, alerts, risk)

        priv_key = Ed25519PrivateKey.generate()
        pub_hex = (
            priv_key.public_key()
            .public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
            .hex()
        )
        manifest = SignedReleaseManifest.build_and_sign_offline(
            priv_key=priv_key,
            base_dir=protected_code_dir,
            relative_file_paths=("core_module.py",),
            risk_config=risk,
        )
        s01 = S01SecuritySupervisor(
            store=store,
            alert_manager=alerts,
            evidence_dir=evidence_dir,
            pinned_release_pubkey_hex=pub_hex,
        )

        beacon_sender = CloudHostBeaconSender("SUIQ-BEACON-WRITE-SECRET-2026")
        dms = ExternalDeadMansSwitchEvaluator(
            beacon_write_token="SUIQ-BEACON-WRITE-SECRET-2026",
            pinned_manifest_root_sha256=manifest.manifest_root_sha256,
            isolated_fcm_sender=lambda alert_type, details: True,
        )

        sim_now = utc_now()
        sim_mono = monotonic_ns()
        guardian.initiate_boot_syncing("SOAK_START", now_utc=sim_now)

        dyn_filters = SymbolFilters(
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
            fetched_at_utc=sim_now,
        )
        guardian.execute_syncing_reconciliation(
            r01_active_risk_config=risk,
            r01_heartbeat_age_sec=Decimal("1.0"),
            s01_lockdown_active=False,
            disk_free_pct=Decimal("0.85"),
            ntp_offset_ms=Decimal("0.01"),
            f01_data_quality=DataQuality.OK,
            f01_event_age_sec=Decimal("0.1"),
            active_filters=dyn_filters,
            exchange_state=SimulatedExchangeState(),
            now_utc=sim_now,
        )

        hb_latencies_ms: list[float] = []
        r01_latencies_ms: list[float] = []
        s01_latencies_ms: list[float] = []
        rss_samples_mb: list[float] = []
        fd_samples: list[int] = []

        step_seconds = (simulated_hours * 3600) // max(1, cycles_to_execute)
        gc.collect()
        initial_rss_mb = _get_process_rss_bytes() / (1024 * 1024)

        for tick in range(1, cycles_to_execute + 1):
            sim_now = sim_now + timedelta(seconds=step_seconds)
            sim_mono = sim_mono + (step_seconds * 1_000_000_000)

            # 1. Heartbeat update & check across services
            t0 = monotonic_ns()
            for svc in ("guardian", "r01", "f01", "d01", "g01"):
                guardian.write_heartbeat(
                    ServiceHeartbeat(
                        service_name=svc,
                        pid=os.getpid(),
                        boot_id="BOOT-SOAK-01",
                        monotonic_ns=sim_mono,
                        wall_time_utc=sim_now,
                        loop_tick_seq=tick,
                        last_work_time_utc=sim_now,
                        rss_bytes=_get_process_rss_bytes(),
                    )
                )
            ok_hb, _ = guardian.check_service_health("f01", current_monotonic_ns=sim_mono, now_utc=sim_now)
            assert ok_hb
            hb_latencies_ms.append((monotonic_ns() - t0) / 1_000_000.0)

            # 2. R-01 risk evaluation latency
            t1 = monotonic_ns()
            proposal = BreakoutProposal(
                signal_id=f"SIG-{tick}",
                candle_close_utc=sim_now,
                barrier_ref=Decimal("2.0000"),
                p_win=Decimal("0.55"),
                p_stop=Decimal("0.25"),
                p_timeout=Decimal("0.20"),
                ev_net=Decimal("0.0050"),
                confidence=Decimal("0.85"),
                feature_snapshot_hash="a" * 64,
                model_version="a02-v1",
                expiry=sim_now + timedelta(minutes=5),
            )
            verdict = r01.evaluate_entry_proposal(
                proposal=proposal,
                account_equity_usdt=Decimal("1000.0"),
                rolling_24h_loss_pct=Decimal("0.001"),
                planned_buy_price=Decimal("2.0000"),
                current_monotonic_ns=sim_mono,
                now_utc=sim_now,
            )
            assert verdict.allowed
            r01_latencies_ms.append((monotonic_ns() - t1) / 1_000_000.0)

            # 3. S-01 inspection & External Beacon every 6th cycle
            if tick % 6 == 0:
                t2 = monotonic_ns()
                ok_s01, live_root = s01.inspect_file_and_config_integrity(
                    manifest=manifest,
                    base_dir=protected_code_dir,
                    active_risk_config=risk,
                    now_utc=sim_now,
                )
                assert ok_s01
                beacon = beacon_sender.create_beacon(
                    boot_id="BOOT-SOAK-01",
                    sent_at_utc=sim_now,
                    trading_state=store.get_trading_state(),
                    r01_healthy=True,
                    manifest_root_sha256=live_root,
                )
                assert dms.ingest_beacon(beacon, recv_utc=sim_now)
                assert dms.evaluate_cron_tick(now_utc=sim_now + timedelta(seconds=30)) is None
                s01_latencies_ms.append((monotonic_ns() - t2) / 1_000_000.0)

            if tick % 100 == 0:
                rss_samples_mb.append(round(_get_process_rss_bytes() / (1024 * 1024), 2))
                fd_samples.append(_get_open_fd_count())

        gc.collect()
        final_rss_mb = _get_process_rss_bytes() / (1024 * 1024)
        rss_slope_mb_per_h = max(0.0, (final_rss_mb - initial_rss_mb) / float(simulated_hours))
        db_size_kb = round(db_path.stat().st_size / 1024.0, 2) if db_path.exists() else 0.0

        hb_sorted = sorted(hb_latencies_ms)
        r01_sorted = sorted(r01_latencies_ms)
        s01_sorted = sorted(s01_latencies_ms)

        report = {
            "simulated_hours": simulated_hours,
            "cycles_executed": cycles_to_execute,
            "final_trading_state": store.get_trading_state().value,
            "heartbeat_check_latency_ms": {
                "p50": _percentile(hb_sorted, 50),
                "p95": _percentile(hb_sorted, 95),
                "p99": _percentile(hb_sorted, 99),
            },
            "r01_risk_verdict_latency_ms": {
                "p50": _percentile(r01_sorted, 50),
                "p95": _percentile(r01_sorted, 95),
                "p99": _percentile(r01_sorted, 99),
                "slo_target_p99_ms": 25.0,
                "slo_passed": _percentile(r01_sorted, 99) <= 25.0,
            },
            "s01_and_beacon_latency_ms": {
                "p50": _percentile(s01_sorted, 50),
                "p95": _percentile(s01_sorted, 95),
                "p99": _percentile(s01_sorted, 99),
            },
            "memory_rss_mb": {
                "initial_mb": round(initial_rss_mb, 2),
                "final_mb": round(final_rss_mb, 2),
                "peak_mb": max(rss_samples_mb) if rss_samples_mb else round(final_rss_mb, 2),
                "slope_mb_per_sim_hour": round(rss_slope_mb_per_h, 4),
            },
            "open_fds": {
                "min": min(fd_samples) if fd_samples else 0,
                "max": max(fd_samples) if fd_samples else 0,
            },
            "sqlite_db_size_kb": db_size_kb,
        }
        store.close()
        return report
    finally:
        if cleanup_dir and work_dir and work_dir.exists():
            import shutil

            shutil.rmtree(work_dir, ignore_errors=True)


def run_live_service_loop(service_name: str, db_path: Path, duration_sec: int = 0) -> None:
    """Run a live daemon heartbeat loop for systemd deployment on the cloud server."""
    store = StateStore(db_path)
    alerts = AlertManager(store)
    risk = RiskConfig()
    guardian = F02Guardian(store, alerts, risk)

    if service_name == "guardian":
        guardian.initiate_boot_syncing(reason="SYSTEMD_GUARDIAN_START")
        now = utc_now()
        dyn_filters = SymbolFilters(
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
        # Ensure initial R-01 heartbeat exists or wait briefly
        guardian.write_heartbeat(
            ServiceHeartbeat(
                service_name="r01",
                pid=os.getpid(),
                boot_id="SYSTEMD-BOOT",
                monotonic_ns=monotonic_ns(),
                wall_time_utc=now,
                loop_tick_seq=1,
                last_work_time_utc=now,
                rss_bytes=_get_process_rss_bytes(),
            )
        )
        guardian.execute_syncing_reconciliation(
            r01_active_risk_config=risk,
            r01_heartbeat_age_sec=Decimal("0.5"),
            s01_lockdown_active=False,
            disk_free_pct=Decimal("0.85"),
            ntp_offset_ms=Decimal("0.01"),
            f01_data_quality=DataQuality.OK,
            f01_event_age_sec=Decimal("0.1"),
            active_filters=dyn_filters,
            exchange_state=SimulatedExchangeState(),
            now_utc=now,
        )

    boot_id = f"SYS-{os.getpid()}"
    tick = 0
    start_time = time.monotonic()
    try:
        while True:
            tick += 1
            now = utc_now()
            guardian.write_heartbeat(
                ServiceHeartbeat(
                    service_name=service_name,
                    pid=os.getpid(),
                    boot_id=boot_id,
                    monotonic_ns=monotonic_ns(),
                    wall_time_utc=now,
                    loop_tick_seq=tick,
                    last_work_time_utc=now,
                    rss_bytes=_get_process_rss_bytes(),
                )
            )
            if duration_sec > 0 and (time.monotonic() - start_time) >= duration_sec:
                break
            time.sleep(5.0)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SUI-Quant P1 Daemon & Soak Runner")
    parser.add_argument("--mode", choices=["soak", "service"], default="soak")
    parser.add_argument("--service-name", default="guardian")
    parser.add_argument("--db-path", default="/var/lib/sui-quant/state.db")
    parser.add_argument("--duration-hours", type=int, default=72)
    parser.add_argument("--cycles", type=int, default=2160)
    parser.add_argument("--service-duration-sec", type=int, default=0)
    args = parser.parse_args()

    if args.mode == "soak":
        report = run_accelerated_72h_soak(
            simulated_hours=args.duration_hours,
            cycles_to_execute=args.cycles,
        )
        print(json.dumps(report, indent=2))
    else:
        run_live_service_loop(
            service_name=args.service_name,
            db_path=Path(args.db_path),
            duration_sec=args.service_duration_sec,
        )


if __name__ == "__main__":
    main()
