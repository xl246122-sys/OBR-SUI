"""SQLite WAL state and audit storage for F-02, R-01, S-01, and G-02 (§1, §9, §10).

Enforces WAL mode, bounded busy_timeout, automatic WAL checkpointing, and strict UTC timestamps.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sui_quant.contracts import AlertEvent, AlertSeverity
from sui_quant.security.redaction import redact_mapping, redact_text
from sui_quant.state_machine import StateTransitionRecord, TradingState
from sui_quant.utils.clock import ensure_utc, utc_now


class StateStore:
    """Lightweight SQLite WAL store for heartbeats, state transitions, alerts, and security forensics."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._init_schema()

    def _configure_pragmas(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA busy_timeout=5000;")
        cur.execute("PRAGMA wal_autocheckpoint=1000;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS system_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_heartbeats (
                service_name TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                boot_id TEXT NOT NULL,
                monotonic_ns INTEGER NOT NULL,
                wall_time_utc TEXT NOT NULL,
                loop_tick_seq INTEGER NOT NULL,
                last_work_time_utc TEXT NOT NULL,
                rss_bytes INTEGER NOT NULL,
                service_state TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS state_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transition_id TEXT NOT NULL UNIQUE,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crash_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                crashed_at_utc TEXT NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alert_events (
                alert_id TEXT PRIMARY KEY,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                event_state TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                push_attempts INTEGER NOT NULL DEFAULT 0,
                actions_taken_json TEXT NOT NULL,
                details_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_alert_dedup ON alert_events(dedup_key, event_state);

            CREATE TABLE IF NOT EXISTS security_incidents (
                incident_id TEXT PRIMARY KEY,
                dimension TEXT NOT NULL,
                severity TEXT NOT NULL,
                detected_at_utc TEXT NOT NULL,
                evidence_path TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )
        cur.close()

    def set_trading_state(self, state: TradingState) -> None:
        now_str = utc_now().isoformat()
        self._conn.execute(
            """
            INSERT INTO system_kv (key, value, updated_at_utc)
            VALUES ('trading_state', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at_utc=excluded.updated_at_utc
            """,
            (state.value, now_str),
        )

    def get_trading_state(self) -> TradingState:
        row = self._conn.execute(
            "SELECT value FROM system_kv WHERE key = 'trading_state'"
        ).fetchone()
        if row is None:
            return TradingState.BOOT
        return TradingState(str(row["value"]))

    def record_transition(self, record: StateTransitionRecord) -> None:
        ensure_utc(record.occurred_at_utc, "occurred_at_utc")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO state_transitions (transition_id, from_state, to_state, reason, occurred_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.transition_id,
                    record.from_state.value,
                    record.to_state.value,
                    redact_text(record.reason),
                    record.occurred_at_utc.isoformat(),
                ),
            )
            self.set_trading_state(record.to_state)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def list_transitions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT transition_id, from_state, to_state, reason, occurred_at_utc FROM state_transitions ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_heartbeat(
        self,
        service_name: str,
        pid: int,
        boot_id: str,
        monotonic_ns: int,
        wall_time_utc_iso: str,
        loop_tick_seq: int,
        last_work_time_utc_iso: str,
        rss_bytes: int,
        service_state: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO service_heartbeats (
                service_name, pid, boot_id, monotonic_ns, wall_time_utc,
                loop_tick_seq, last_work_time_utc, rss_bytes, service_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_name) DO UPDATE SET
                pid=excluded.pid,
                boot_id=excluded.boot_id,
                monotonic_ns=excluded.monotonic_ns,
                wall_time_utc=excluded.wall_time_utc,
                loop_tick_seq=excluded.loop_tick_seq,
                last_work_time_utc=excluded.last_work_time_utc,
                rss_bytes=excluded.rss_bytes,
                service_state=excluded.service_state
            """,
            (
                service_name,
                pid,
                boot_id,
                monotonic_ns,
                wall_time_utc_iso,
                loop_tick_seq,
                last_work_time_utc_iso,
                rss_bytes,
                service_state,
            ),
        )

    def get_heartbeat(self, service_name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM service_heartbeats WHERE service_name = ?",
            (service_name,),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_crash(self, service_name: str, crashed_at_utc_iso: str, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO crash_events (service_name, crashed_at_utc, reason) VALUES (?, ?, ?)",
            (service_name, crashed_at_utc_iso, redact_text(reason)),
        )

    def count_recent_crashes(self, service_name: str, since_utc_iso: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM crash_events
            WHERE service_name = ? AND crashed_at_utc >= ?
            """,
            (service_name, since_utc_iso),
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def record_security_incident(
        self,
        incident_id: str,
        dimension: str,
        severity: AlertSeverity,
        detected_at_utc_iso: str,
        evidence_path: str,
        details: dict[str, Any],
    ) -> None:
        sanitized_details = redact_mapping(details)
        self._conn.execute(
            """
            INSERT INTO security_incidents (
                incident_id, dimension, severity, detected_at_utc, evidence_path, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                dimension,
                severity.value,
                detected_at_utc_iso,
                evidence_path,
                json.dumps(sanitized_details, ensure_ascii=False),
            ),
        )

    def list_security_incidents(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM security_incidents ORDER BY detected_at_utc ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
