"""Real Continuous 72-Hour Wall-Clock Stability Observer (Task A.1 & A.2).

Strictly distinct from `soak_runner.run_accelerated_72h_soak()` (which is an accelerated virtual-clock
simulation). This module runs on the target Linux server (`<JAPAN_CLOUD_IP_REDACTED>`) in real wall-clock time,
sampling actual kernel, systemd, SQLite heartbeat, and disk telemetry into an append-only JSONL file
(`/var/log/sui-quant/real_72h_observation.jsonl`) and summary status file
(`/var/log/sui-quant/real_72h_status.json`).

Governance Rule (Task A.1 / A.7):
- `verification_status` MUST remain `"PENDING"` whenever `real_elapsed_seconds < 259200` (72.0 hours).
- Never fabricates or extrapolates wall-clock time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REQUIRED_WALLCLOCK_SECONDS = 72 * 3600  # 259,200 seconds (72.0 real hours)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_proc_loadavg() -> dict[str, float]:
    p = Path("/proc/loadavg")
    if not p.exists():
        return {"load_1m": 0.0, "load_5m": 0.0, "load_15m": 0.0}
    parts = p.read_text(encoding="utf-8").split()
    return {
        "load_1m": float(parts[0]),
        "load_5m": float(parts[1]),
        "load_15m": float(parts[2]),
    }


def _read_proc_meminfo() -> dict[str, float]:
    p = Path("/proc/meminfo")
    if not p.exists():
        return {
            "mem_total_mib": 0.0,
            "mem_available_mib": 0.0,
            "mem_used_mib": 0.0,
            "swap_total_mib": 0.0,
            "swap_used_mib": 0.0,
        }
    kv: dict[str, int] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            toks = v.strip().split()
            if toks and toks[0].isdigit():
                kv[k.strip()] = int(toks[0])  # kB
    mem_total_kb = kv.get("MemTotal", 0)
    mem_avail_kb = kv.get("MemAvailable", 0)
    mem_used_kb = max(0, mem_total_kb - mem_avail_kb)
    swap_total_kb = kv.get("SwapTotal", 0)
    swap_free_kb = kv.get("SwapFree", 0)
    swap_used_kb = max(0, swap_total_kb - swap_free_kb)
    return {
        "mem_total_mib": round(mem_total_kb / 1024.0, 2),
        "mem_available_mib": round(mem_avail_kb / 1024.0, 2),
        "mem_used_mib": round(mem_used_kb / 1024.0, 2),
        "swap_total_mib": round(swap_total_kb / 1024.0, 2),
        "swap_used_mib": round(swap_used_kb / 1024.0, 2),
    }


def _read_proc_oom_kill() -> int:
    p = Path("/proc/vmstat")
    if not p.exists():
        return 0
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "oom_kill":
            return int(parts[1])
    return 0


def _read_systemd_unit_metrics(unit_name: str) -> dict[str, Any]:
    try:
        out = subprocess.check_output(
            [
                "systemctl",
                "show",
                unit_name,
                "--property=ActiveState,SubState,MainPID,MemoryCurrent,NRestarts,ExecMainStartTimestampUTC",
            ],
            text=True,
            timeout=5.0,
        )
        props: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
        pid = int(props.get("MainPID", "0") or 0)
        mem_raw = props.get("MemoryCurrent", "0")
        mem_bytes = int(mem_raw) if mem_raw.isdigit() else 0
        # Fallback to /proc/<pid>/statm if MemoryCurrent is [not set]
        if mem_bytes == 0 and pid > 0:
            statm = Path(f"/proc/{pid}/statm")
            if statm.exists():
                parts = statm.read_text().split()
                if len(parts) >= 2:
                    mem_bytes = int(parts[1]) * 4096
        nrestarts = int(props.get("NRestarts", "0") or 0)
        return {
            "active_state": props.get("ActiveState", "unknown"),
            "sub_state": props.get("SubState", "unknown"),
            "main_pid": pid,
            "rss_mib": round(mem_bytes / (1024.0 * 1024.0), 2),
            "nrestarts": nrestarts,
            "started_utc": props.get("ExecMainStartTimestampUTC", ""),
        }
    except Exception as exc:
        return {
            "active_state": f"error:{exc}",
            "sub_state": "unknown",
            "main_pid": 0,
            "rss_mib": 0.0,
            "nrestarts": 0,
            "started_utc": "",
        }


def _read_sqlite_heartbeat_telemetry(db_path: Path, now_utc: datetime) -> dict[str, Any]:
    if not db_path.exists():
        return {"db_exists": False, "trading_state": "MISSING", "services": {}}
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        state_row = conn.execute(
            "SELECT value FROM system_kv WHERE key = 'trading_state'"
        ).fetchone()
        trading_state = state_row[0] if state_row else "UNKNOWN"
        services: dict[str, Any] = {}
        rows = conn.execute(
            """
            SELECT service_name, pid, boot_id, wall_time_utc, loop_tick_seq, rss_bytes
            FROM service_heartbeats
            """
        ).fetchall()
        for svc_name, pid, boot_id, wall_str, tick_seq, rss_bytes in rows:
            hb_dt = datetime.fromisoformat(wall_str)
            age_sec = round((now_utc - hb_dt).total_seconds(), 3)
            services[svc_name] = {
                "pid": pid,
                "boot_id": boot_id,
                "heartbeat_utc": wall_str,
                "age_sec": age_sec,
                "loop_tick_seq": tick_seq,
                "rss_mib": round(int(rss_bytes) / (1024.0 * 1024.0), 2),
            }
        return {
            "db_exists": True,
            "trading_state": trading_state,
            "services": services,
        }
    except Exception as exc:
        return {"db_exists": True, "trading_state": f"ERR:{exc}", "services": {}}
    finally:
        conn.close()


def _dir_size_bytes(directory: Path) -> int:
    if not directory.exists():
        return 0
    total = 0
    for p in directory.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _read_disk_metrics(state_dir: Path, log_dir: Path) -> dict[str, Any]:
    usage = shutil.disk_usage("/")
    used_pct = round((usage.used / usage.total) * 100.0, 2) if usage.total > 0 else 0.0
    return {
        "root_total_gib": round(usage.total / (1024.0**3), 3),
        "root_free_gib": round(usage.free / (1024.0**3), 3),
        "root_used_pct": used_pct,
        "state_dir_bytes": _dir_size_bytes(state_dir),
        "log_dir_bytes": _dir_size_bytes(log_dir),
    }


def collect_single_sample(
    sample_seq: int,
    started_at_utc: datetime,
    initial_oom: int,
    initial_state_bytes: int,
    initial_log_bytes: int,
    db_path: Path,
    log_dir: Path,
) -> dict[str, Any]:
    now_utc = _utc_now()
    elapsed_sec = round((now_utc - started_at_utc).total_seconds(), 2)
    load = _read_proc_loadavg()
    mem = _read_proc_meminfo()
    oom_now = _read_proc_oom_kill()
    r01_unit = _read_systemd_unit_metrics("sui-quant-r01.service")
    guardian_unit = _read_systemd_unit_metrics("sui-quant-guardian.service")
    hb = _read_sqlite_heartbeat_telemetry(db_path, now_utc)
    disk = _read_disk_metrics(db_path.parent, log_dir)

    return {
        "sample_seq": sample_seq,
        "sampled_at_utc": now_utc.isoformat(),
        "started_at_utc": started_at_utc.isoformat(),
        "real_elapsed_seconds": elapsed_sec,
        "real_elapsed_hours": round(elapsed_sec / 3600.0, 4),
        "cpu": load,
        "memory": mem,
        "oom": {
            "kernel_oom_kill_total": oom_now,
            "oom_kill_delta_since_start": oom_now - initial_oom,
        },
        "systemd_units": {
            "sui-quant-r01.service": r01_unit,
            "sui-quant-guardian.service": guardian_unit,
        },
        "heartbeats": hb,
        "disk": {
            **disk,
            "state_dir_growth_bytes": disk["state_dir_bytes"] - initial_state_bytes,
            "log_dir_growth_bytes": disk["log_dir_bytes"] - initial_log_bytes,
        },
    }


def run_real_72h_observation(
    db_path: Path,
    log_dir: Path,
    interval_sec: int = 60,
    max_samples: int = 0,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = log_dir / "real_72h_observation.jsonl"
    status_path = log_dir / "real_72h_status.json"

    # Resume existing run if jsonl_path already exists so we preserve true original start time
    started_at_utc: datetime | None = None
    initial_oom: int | None = None
    initial_state_bytes: int | None = None
    initial_log_bytes: int | None = None
    sample_seq = 0

    if jsonl_path.exists() and jsonl_path.stat().st_size > 0:
        lines = [ln for ln in jsonl_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            first_rec = json.loads(lines[0])
            last_rec = json.loads(lines[-1])
            started_at_utc = datetime.fromisoformat(first_rec["started_at_utc"])
            initial_oom = int(first_rec["oom"]["kernel_oom_kill_total"])
            initial_state_bytes = int(first_rec["disk"]["state_dir_bytes"])
            initial_log_bytes = int(first_rec["disk"]["log_dir_bytes"])
            sample_seq = int(last_rec["sample_seq"])

    if started_at_utc is None:
        started_at_utc = _utc_now()
    if initial_oom is None:
        initial_oom = _read_proc_oom_kill()
    if initial_state_bytes is None:
        initial_state_bytes = _dir_size_bytes(db_path.parent)
    if initial_log_bytes is None:
        initial_log_bytes = _dir_size_bytes(log_dir)

    target_completion_utc = started_at_utc + timedelta(seconds=REQUIRED_WALLCLOCK_SECONDS)
    samples_collected_this_invocation = 0

    while True:
        sample_seq += 1
        samples_collected_this_invocation += 1
        sample = collect_single_sample(
            sample_seq=sample_seq,
            started_at_utc=started_at_utc,
            initial_oom=initial_oom,
            initial_state_bytes=initial_state_bytes,
            initial_log_bytes=initial_log_bytes,
            db_path=db_path,
            log_dir=log_dir,
        )
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sample) + "\n")
            f.flush()
            os.fsync(f.fileno())

        elapsed_sec = sample["real_elapsed_seconds"]
        is_completed = elapsed_sec >= REQUIRED_WALLCLOCK_SECONDS
        status_doc = {
            "test_classification": "REAL_WALLCLOCK_72H_OBSERVATION",
            "is_accelerated_simulation": False,
            "verification_status": "PASS" if is_completed else "PENDING",
            "pending_reason": (
                None
                if is_completed
                else f"Real continuous 72h observation in progress: {elapsed_sec:.1f}s / {REQUIRED_WALLCLOCK_SECONDS}s elapsed"
            ),
            "started_at_utc": started_at_utc.isoformat(),
            "latest_sample_utc": sample["sampled_at_utc"],
            "target_completion_utc": target_completion_utc.isoformat(),
            "real_elapsed_seconds": elapsed_sec,
            "real_elapsed_hours": sample["real_elapsed_hours"],
            "required_wallclock_hours": 72.0,
            "completion_pct": round(min(100.0, (elapsed_sec / REQUIRED_WALLCLOCK_SECONDS) * 100.0), 4),
            "total_samples_recorded": sample_seq,
            "latest_sample": sample,
        }
        status_path.write_text(json.dumps(status_doc, indent=2), encoding="utf-8")

        if max_samples > 0 and samples_collected_this_invocation >= max_samples:
            break
        if is_completed:
            break
        time.sleep(interval_sec)


def print_remote_daemon_status(db_path: Path) -> None:
    now_utc = _utc_now()
    r01_unit = _read_systemd_unit_metrics("sui-quant-r01.service")
    guardian_unit = _read_systemd_unit_metrics("sui-quant-guardian.service")
    hb = _read_sqlite_heartbeat_telemetry(db_path, now_utc)
    g_hb = hb.get("services", {}).get("guardian", {})
    r_hb = hb.get("services", {}).get("r01", {})
    out = {
        "sampled_at_utc": now_utc.isoformat(),
        "trading_state": hb.get("trading_state", "UNKNOWN"),
        "guardian_active_state": guardian_unit["active_state"],
        "guardian_heartbeat_age_sec": g_hb.get("age_sec", 999999.0),
        "guardian_tick_seq": g_hb.get("loop_tick_seq", 0),
        "r01_active_state": r01_unit["active_state"],
        "r01_heartbeat_age_sec": r_hb.get("age_sec", 999999.0),
        "r01_tick_seq": r_hb.get("loop_tick_seq", 0),
    }
    print(json.dumps(out))


def main() -> None:
    parser = argparse.ArgumentParser(description="SUI-Quant Real 72-Hour Wall-Clock Observer")
    parser.add_argument("--db-path", default="/var/lib/sui-quant/state.db")
    parser.add_argument("--log-dir", default="/var/log/sui-quant")
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--print-remote-status", action="store_true")
    args = parser.parse_args()
    if args.print_remote_status:
        print_remote_daemon_status(Path(args.db_path))
        return
    run_real_72h_observation(
        db_path=Path(args.db_path),
        log_dir=Path(args.log_dir),
        interval_sec=args.interval_sec,
        max_samples=args.max_samples,
    )


if __name__ == "__main__":
    main()
