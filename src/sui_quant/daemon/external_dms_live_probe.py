"""Cross-Machine External Dead-Man's Switch Probe & Webhook Notification Sink (Task A.3 & A.4).

Runs on an independent external fault domain (Workstation outside Tencent Cloud Tokyo) to monitor
the Japan trading server (`<JAPAN_CLOUD_IP_REDACTED>`).

Provides verifiable, distinct detection and external notification receipts for:
1. `SIMULATED_BLACKOUT_DAEMON_SILENT`: Host `<JAPAN_CLOUD_IP_REDACTED>` is network-reachable, but the guardian/R-01
   daemon is stopped or its heartbeat age exceeds `daemon_stale_threshold_sec`.
2. `REAL_HOST_UNREACHABLE_BLACKOUT`: Host `<JAPAN_CLOUD_IP_REDACTED>` is genuinely unreachable over the network
   (TCP SYN packets dropped / timed out at the transport layer), evaluated strictly by the external
   machine's independent clock (`monotonic_ns()` / `utc_now()`).
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import socket
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExternalAlertWebhookSink:
    """Local HTTP webhook server on the external fault domain that receives Dead-Man's Switch alerts
    and issues signed JSON delivery receipts persisted to `receipts_jsonl_path`.
    """

    def __init__(
        self,
        receipts_jsonl_path: Path,
        receipt_signing_secret: str = "SUIQ-EXTERNAL-RECEIPT-SECRET-2026",
        host: str = "127.0.0.1",
        port: int = 18990,
    ) -> None:
        self.receipts_jsonl_path = receipts_jsonl_path
        self.receipts_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.receipt_signing_secret = receipt_signing_secret
        self.host = host
        self.port = port
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        sink_ref = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw_body)
                received_utc = _utc_now().isoformat()
                receipt_id = f"RCPT-{uuid.uuid4().hex[:12].upper()}"
                canonical = (
                    f"{receipt_id}|{received_utc}|{payload.get('incident_type', '')}|"
                    f"{payload.get('fault_classification', '')}|{payload.get('target_host', '')}"
                )
                sig = hmac.new(
                    sink_ref.receipt_signing_secret.encode("utf-8"),
                    canonical.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                receipt = {
                    "receipt_id": receipt_id,
                    "channel_type": "EXTERNAL_WORKSTATION_WEBHOOK_SINK",
                    "third_party_cloud_push_status": "PENDING_USER_CLOUDFLARE_AND_PUSH_CREDENTIALS",
                    "http_status": 200,
                    "received_at_utc": received_utc,
                    "incident_type": payload.get("incident_type"),
                    "fault_classification": payload.get("fault_classification"),
                    "target_host": payload.get("target_host"),
                    "independent_probe_host": socket.gethostname(),
                    "probe_details": payload.get("details", {}),
                    "receipt_hmac_sha256": sig,
                }
                with sink_ref.receipts_jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(receipt) + "\n")
                    f.flush()

                resp_bytes = json.dumps(receipt).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # Keep stdout clean

        self._server = http.server.HTTPServer((self.host, self.port), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{self.host}:{self.port}/alert-receipt"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def dispatch_webhook_alert(
    webhook_url: str,
    incident_type: str,
    fault_classification: str,
    target_host: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    body = json.dumps(
        {
            "incident_type": incident_type,
            "fault_classification": fault_classification,
            "target_host": target_host,
            "dispatched_at_utc": _utc_now().isoformat(),
            "details": details,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ExternalCrossMachineProbe:
    """Independent External Dead-Man's Switch evaluator running outside the Japan cloud server."""

    def __init__(
        self,
        target_host: str,
        target_port: int,
        webhook_url: str,
        daemon_stale_threshold_sec: float = 12.0,
        tcp_timeout_sec: float = 3.5,
    ) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.webhook_url = webhook_url
        self.daemon_stale_threshold_sec = daemon_stale_threshold_sec
        self.tcp_timeout_sec = tcp_timeout_sec

    def evaluate_remote_target(
        self,
        fetch_remote_daemon_status_fn: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Probe the remote server using the external machine's own network stack and clock.

        Step 1: Verify raw TCP reachability to `target_host:target_port` with strict `tcp_timeout_sec`.
                If TCP SYN times out or is refused, classify as `REAL_HOST_UNREACHABLE_BLACKOUT`.
        Step 2: If host is reachable over TCP, query the daemon heartbeat status. If `guardian` service
                is inactive or heartbeat age > `daemon_stale_threshold_sec`, classify as
                `SIMULATED_BLACKOUT_DAEMON_SILENT`.
        """
        probe_started_utc = _utc_now()
        t_start = datetime.now(timezone.utc)

        # Step 1: Raw TCP transport reachability check (skip duplicate pre-auth connect on port 22
        # so OpenSSH 9.8+ PerSourcePenalties does not penalize unauthenticated banner drops)
        if self.target_port != 22:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.tcp_timeout_sec)
            try:
                sock.connect((self.target_host, self.target_port))
                sock.close()
            except Exception as exc:
                elapsed_ms = round((_utc_now() - t_start).total_seconds() * 1000.0, 2)
                receipt = dispatch_webhook_alert(
                    webhook_url=self.webhook_url,
                    incident_type="EXTERNAL_HOST_BLACKOUT_CRITICAL",
                    fault_classification="REAL_HOST_UNREACHABLE_BLACKOUT",
                    target_host=f"{self.target_host}:{self.target_port}",
                    details={
                        "probe_started_utc": probe_started_utc.isoformat(),
                        "tcp_timeout_sec": self.tcp_timeout_sec,
                        "observed_wait_ms": elapsed_ms,
                        "transport_exception": f"{type(exc).__name__}: {exc}",
                        "independent_judgment": "Host failed to complete TCP 3-way handshake within timeout; classified as REAL_HOST_UNREACHABLE_BLACKOUT.",
                    },
                )
                return {
                    "healthy": False,
                    "fault_classification": "REAL_HOST_UNREACHABLE_BLACKOUT",
                    "receipt": receipt,
                }

        # Step 2: Execute remote transport + daemon status query
        try:
            remote_status = fetch_remote_daemon_status_fn()
        except Exception as exc:
            elapsed_ms = round((_utc_now() - t_start).total_seconds() * 1000.0, 2)
            receipt = dispatch_webhook_alert(
                webhook_url=self.webhook_url,
                incident_type="EXTERNAL_HOST_BLACKOUT_CRITICAL",
                fault_classification="REAL_HOST_UNREACHABLE_BLACKOUT",
                target_host=f"{self.target_host}:{self.target_port}",
                details={
                    "probe_started_utc": probe_started_utc.isoformat(),
                    "tcp_timeout_sec": self.tcp_timeout_sec,
                    "observed_wait_ms": elapsed_ms,
                    "transport_exception": f"{type(exc).__name__}: {exc}",
                    "independent_judgment": "Host failed to respond over network within timeout; classified as REAL_HOST_UNREACHABLE_BLACKOUT.",
                },
            )
            return {
                "healthy": False,
                "fault_classification": "REAL_HOST_UNREACHABLE_BLACKOUT",
                "receipt": receipt,
            }

        guardian_active = remote_status.get("guardian_active_state") == "active"
        guardian_age_sec = float(remote_status.get("guardian_heartbeat_age_sec", 999999.0))
        if (not guardian_active) or (guardian_age_sec > self.daemon_stale_threshold_sec):
            receipt = dispatch_webhook_alert(
                webhook_url=self.webhook_url,
                incident_type="EXTERNAL_DAEMON_HEARTBEAT_STALE",
                fault_classification="SIMULATED_BLACKOUT_DAEMON_SILENT",
                target_host=f"{self.target_host}:{self.target_port}",
                details={
                    "probe_started_utc": probe_started_utc.isoformat(),
                    "host_network_reachable": True,
                    "guardian_active_state": remote_status.get("guardian_active_state"),
                    "guardian_heartbeat_age_sec": guardian_age_sec,
                    "stale_threshold_sec": self.daemon_stale_threshold_sec,
                    "r01_active_state": remote_status.get("r01_active_state"),
                    "independent_judgment": "Host network is reachable, but guardian daemon is stopped/stale; classified as SIMULATED_BLACKOUT_DAEMON_SILENT.",
                },
            )
            return {
                "healthy": False,
                "fault_classification": "SIMULATED_BLACKOUT_DAEMON_SILENT",
                "receipt": receipt,
            }

        return {
            "healthy": True,
            "fault_classification": "NONE",
            "remote_status": remote_status,
        }
