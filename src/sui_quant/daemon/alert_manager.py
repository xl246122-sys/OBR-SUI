"""G-02 Alert classification, deduplication, storm suppression, persistence, and retry state machine (§10, §12.2)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Mapping

from sui_quant.contracts import AlertEvent, AlertSeverity
from sui_quant.daemon.storage import StateStore
from sui_quant.security.redaction import redact_mapping
from sui_quant.utils.clock import ensure_utc, utc_now


class DeliveryStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"          # INFO events stored for daily report only
    PENDING = "PENDING"
    SENT_AWAITING_ACK = "SENT_AWAITING_ACK"
    DELIVERED = "DELIVERED"
    FAILED_RETRYING = "FAILED_RETRYING"


@dataclass(frozen=True)
class AlertDispatchResult:
    alert_event: AlertEvent
    occurrence_count: int
    was_deduplicated: bool
    push_dispatched: bool
    delivery_status: DeliveryStatus


class AlertManager:
    """Manages INFO/HIGH/CRITICAL alerts with cooldown deduplication, persistence, and honest delivery tracking."""

    def __init__(
        self,
        store: StateStore,
        cooldown_seconds: int = 300,
        push_sender: Callable[[AlertEvent, Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self.store = store
        self.cooldown_seconds = cooldown_seconds
        self.push_sender = push_sender

    def emit_alert(
        self,
        severity: AlertSeverity,
        source: str,
        dedup_key: str,
        actions_taken: tuple[str, ...] = (),
        details: Mapping[str, Any] | None = None,
        now_utc: datetime | None = None,
    ) -> AlertDispatchResult:
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        sanitized_details = redact_mapping(dict(details or {}))
        conn = self.store._conn

        existing = conn.execute(
            """
            SELECT * FROM alert_events
            WHERE dedup_key = ? AND event_state = 'OPEN'
            ORDER BY last_seen_utc DESC LIMIT 1
            """,
            (dedup_key,),
        ).fetchone()

        if existing is not None:
            last_seen = datetime.fromisoformat(str(existing["last_seen_utc"]))
            new_count = int(existing["occurrence_count"]) + 1
            within_cooldown = (ref_now - last_seen) < timedelta(seconds=self.cooldown_seconds)
            if within_cooldown:
                conn.execute(
                    """
                    UPDATE alert_events
                    SET last_seen_utc = ?, occurrence_count = ?
                    WHERE alert_id = ?
                    """,
                    (ref_now.isoformat(), new_count, str(existing["alert_id"])),
                )
                alert_obj = AlertEvent(
                    alert_id=str(existing["alert_id"]),
                    severity=AlertSeverity(str(existing["severity"])),
                    source=str(existing["source"]),
                    first_seen_utc=datetime.fromisoformat(str(existing["first_seen_utc"])),
                    dedup_key=dedup_key,
                    event_state="OPEN",
                    actions_taken=tuple(json.loads(str(existing["actions_taken_json"]))),
                )
                return AlertDispatchResult(
                    alert_event=alert_obj,
                    occurrence_count=new_count,
                    was_deduplicated=True,
                    push_dispatched=False,
                    delivery_status=DeliveryStatus(str(existing["delivery_status"])),
                )

        alert_id = f"ALT-{ref_now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        alert_obj = AlertEvent(
            alert_id=alert_id,
            severity=severity,
            source=source,
            first_seen_utc=ref_now,
            dedup_key=dedup_key,
            event_state="OPEN",
            actions_taken=actions_taken,
        )

        # INFO events go to daily digest; HIGH and CRITICAL require push notification
        if severity == AlertSeverity.INFO:
            delivery_status = DeliveryStatus.NOT_REQUIRED
            push_dispatched = False
            push_attempts = 0
        else:
            push_attempts = 1
            if self.push_sender is not None:
                try:
                    ok = bool(self.push_sender(alert_obj, sanitized_details))
                except Exception:
                    ok = False
                if ok:
                    delivery_status = DeliveryStatus.SENT_AWAITING_ACK
                    push_dispatched = True
                else:
                    delivery_status = DeliveryStatus.FAILED_RETRYING
                    push_dispatched = False
            else:
                delivery_status = DeliveryStatus.PENDING
                push_dispatched = False

        conn.execute(
            """
            INSERT INTO alert_events (
                alert_id, severity, source, dedup_key, first_seen_utc, last_seen_utc,
                occurrence_count, event_state, delivery_status, push_attempts,
                actions_taken_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 'OPEN', ?, ?, ?, ?)
            """,
            (
                alert_id,
                severity.value,
                source,
                dedup_key,
                ref_now.isoformat(),
                ref_now.isoformat(),
                delivery_status.value,
                push_attempts,
                json.dumps(list(actions_taken), ensure_ascii=False),
                json.dumps(sanitized_details, ensure_ascii=False),
            ),
        )

        return AlertDispatchResult(
            alert_event=alert_obj,
            occurrence_count=1,
            was_deduplicated=False,
            push_dispatched=push_dispatched,
            delivery_status=delivery_status,
        )

    def acknowledge_alert(self, alert_id: str) -> None:
        """Mark an alert as DELIVERED when Android App acknowledges receipt."""
        self.store._conn.execute(
            "UPDATE alert_events SET delivery_status = ? WHERE alert_id = ?",
            (DeliveryStatus.DELIVERED.value, alert_id),
        )

    def list_undelivered_alerts(self) -> list[dict[str, Any]]:
        """Return all HIGH/CRITICAL alerts not yet acknowledged as DELIVERED (for offline reconnect sync)."""
        rows = self.store._conn.execute(
            """
            SELECT * FROM alert_events
            WHERE severity IN ('HIGH', 'CRITICAL')
              AND delivery_status != ?
            ORDER BY first_seen_utc ASC
            """,
            (DeliveryStatus.DELIVERED.value,),
        ).fetchall()
        return [dict(r) for r in rows]
