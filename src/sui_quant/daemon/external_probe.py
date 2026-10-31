"""External Independent Fault-Domain Dead-Man's Switch Probe (§10, §12.2).

Separates:
1. `CloudHostBeaconSender` (runs on Japan cloud server; holds only `beacon_write_token`; cannot access FCM credentials).
2. `ExternalDeadMansSwitchEvaluator` (runs on external serverless edge e.g. Cloudflare Worker; holds isolated
   FCM push credentials and pinned `expected_manifest_root_sha256`; independently detects >90s server blackout
   or manifest hash tampering and dispatches CRITICAL notifications to Android).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sui_quant.contracts import AlertSeverity
from sui_quant.state_machine import TradingState
from sui_quant.utils.clock import ensure_utc


BEACON_INTERVAL_SEC = 30
EXTERNAL_BLACKOUT_TIMEOUT_SEC = 90  # 3 missed 30s beacons -> SERVER_UNREACHABLE


@dataclass(frozen=True)
class HeartbeatBeacon:
    boot_id: str
    sent_at_utc: datetime
    trading_state: TradingState
    r01_healthy: bool
    manifest_root_sha256: str
    hmac_sha256: str

    @staticmethod
    def compute_hmac(
        beacon_write_token: str,
        boot_id: str,
        sent_at_utc: datetime,
        trading_state: TradingState,
        r01_healthy: bool,
        manifest_root_sha256: str,
    ) -> str:
        ensure_utc(sent_at_utc, "sent_at_utc")
        msg = (
            f"SUIQ-BEACON|boot={boot_id}|ts={sent_at_utc.isoformat()}|"
            f"state={trading_state.value}|r01={r01_healthy}|root={manifest_root_sha256}"
        )
        return hmac.new(
            beacon_write_token.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


class CloudHostBeaconSender:
    """Runs inside `sui-quant-guardian` on the Japan server; holds ONLY `beacon_write_token`."""

    def __init__(self, beacon_write_token: str) -> None:
        if not beacon_write_token or len(beacon_write_token) < 16:
            raise ValueError("beacon_write_token must be at least 16 characters")
        self._beacon_write_token = beacon_write_token

    def create_beacon(
        self,
        boot_id: str,
        sent_at_utc: datetime,
        trading_state: TradingState,
        r01_healthy: bool,
        manifest_root_sha256: str,
    ) -> HeartbeatBeacon:
        ensure_utc(sent_at_utc, "sent_at_utc")
        sig = HeartbeatBeacon.compute_hmac(
            beacon_write_token=self._beacon_write_token,
            boot_id=boot_id,
            sent_at_utc=sent_at_utc,
            trading_state=trading_state,
            r01_healthy=r01_healthy,
            manifest_root_sha256=manifest_root_sha256,
        )
        return HeartbeatBeacon(
            boot_id=boot_id,
            sent_at_utc=sent_at_utc,
            trading_state=trading_state,
            r01_healthy=r01_healthy,
            manifest_root_sha256=manifest_root_sha256,
            hmac_sha256=sig,
        )


@dataclass(frozen=True)
class ExternalProbeAlertRecord:
    alert_type: str
    severity: AlertSeverity
    triggered_at_utc: datetime
    details: dict[str, Any]
    fcm_dispatched: bool


class ExternalDeadMansSwitchEvaluator:
    """Runs in an external fault domain (e.g., Cloudflare Worker) with isolated FCM credentials.

    Even if the Japan server suffers complete power loss, network blackout, or root compromise,
    this evaluator independently detects the missing beacon (>90s) or tampered `manifest_root_sha256`
    and pushes a CRITICAL alert to the user's Android phone.
    """

    def __init__(
        self,
        beacon_write_token: str,
        pinned_manifest_root_sha256: str,
        isolated_fcm_sender: Callable[[str, dict[str, Any]], bool],
        timeout_seconds: int = EXTERNAL_BLACKOUT_TIMEOUT_SEC,
    ) -> None:
        self._beacon_write_token = beacon_write_token
        self.pinned_manifest_root_sha256 = pinned_manifest_root_sha256
        self.isolated_fcm_sender = isolated_fcm_sender
        self.timeout_seconds = timeout_seconds
        self.last_valid_beacon: HeartbeatBeacon | None = None
        self.server_currently_unreachable: bool = False
        self.audit_log: list[ExternalProbeAlertRecord] = []

    def ingest_beacon(self, beacon: HeartbeatBeacon, recv_utc: datetime) -> bool:
        ensure_utc(recv_utc, "recv_utc")
        expected_hmac = HeartbeatBeacon.compute_hmac(
            beacon_write_token=self._beacon_write_token,
            boot_id=beacon.boot_id,
            sent_at_utc=beacon.sent_at_utc,
            trading_state=beacon.trading_state,
            r01_healthy=beacon.r01_healthy,
            manifest_root_sha256=beacon.manifest_root_sha256,
        )
        if not hmac.compare_digest(expected_hmac, beacon.hmac_sha256):
            self._emit_external_alert(
                alert_type="INVALID_BEACON_SIGNATURE",
                severity=AlertSeverity.CRITICAL,
                now_utc=recv_utc,
                details={"boot_id": beacon.boot_id},
            )
            return False

        if beacon.manifest_root_sha256 != self.pinned_manifest_root_sha256:
            self._emit_external_alert(
                alert_type="REMOTE_MANIFEST_TAMPERED",
                severity=AlertSeverity.CRITICAL,
                now_utc=recv_utc,
                details={
                    "reported_root_sha256": beacon.manifest_root_sha256,
                    "pinned_root_sha256": self.pinned_manifest_root_sha256,
                },
            )
            return False

        was_unreachable = self.server_currently_unreachable
        self.last_valid_beacon = beacon
        self.server_currently_unreachable = False

        if was_unreachable:
            self._emit_external_alert(
                alert_type="SERVER_RECOVERY_SYNCING",
                severity=AlertSeverity.HIGH,
                now_utc=recv_utc,
                details={
                    "boot_id": beacon.boot_id,
                    "trading_state": beacon.trading_state.value,
                },
            )
        return True

    def evaluate_cron_tick(self, now_utc: datetime) -> ExternalProbeAlertRecord | None:
        """Executed every 60s by the external Cron Trigger independent of the Japan server."""
        ensure_utc(now_utc, "now_utc")
        if self.last_valid_beacon is None:
            self.server_currently_unreachable = True
            return self._emit_external_alert(
                alert_type="SERVER_UNREACHABLE_NO_INITIAL_BEACON",
                severity=AlertSeverity.CRITICAL,
                now_utc=now_utc,
                details={"reason": "No initial heartbeat beacon received"},
            )

        elapsed = (now_utc - self.last_valid_beacon.sent_at_utc).total_seconds()
        if elapsed > self.timeout_seconds:
            self.server_currently_unreachable = True
            return self._emit_external_alert(
                alert_type="SERVER_UNREACHABLE",
                severity=AlertSeverity.CRITICAL,
                now_utc=now_utc,
                details={
                    "silence_seconds": round(elapsed, 1),
                    "timeout_threshold_seconds": self.timeout_seconds,
                    "last_boot_id": self.last_valid_beacon.boot_id,
                    "last_state": self.last_valid_beacon.trading_state.value,
                },
            )
        return None

    def _emit_external_alert(
        self,
        alert_type: str,
        severity: AlertSeverity,
        now_utc: datetime,
        details: dict[str, Any],
    ) -> ExternalProbeAlertRecord:
        dispatched = bool(self.isolated_fcm_sender(alert_type, details))
        rec = ExternalProbeAlertRecord(
            alert_type=alert_type,
            severity=severity,
            triggered_at_utc=now_utc,
            details=details,
            fcm_dispatched=dispatched,
        )
        self.audit_log.append(rec)
        return rec
