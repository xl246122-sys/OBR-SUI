"""G-01 / G-02 Mobile Monitoring Gateway, Graded Alert Sync, and Authenticated Defensive Control (§12, QA-11).

Responsibilities:
1. Read-Only Telemetry & Daily Audit Digest (`GET /api/v1/status/summary`, `GET /api/v1/positions/active`,
   `GET /api/v1/daily-report`):
   - Exposes `PAPER` execution state, `D-01` state machine, `A-01` regime, `E-01` active/shadow model bundles,
     in-flight pinned exit strategy (`C1/C2/C3`), `R-01` 24h drawdown and risk headroom, and `F-01/F-02` health.
   - Guarantees 100% credential/secret redaction via `redact_mapping` and `redact_text` (`QA-10`, `QA-11`).
2. G-02 Graded Alert Routing, Offline Reconnect Compensation, and Signed ACK Loop (`QA-11`):
   - `INFO` alerts are stored with `DeliveryStatus.NOT_REQUIRED` for the daily digest only.
   - `HIGH` and `CRITICAL` alerts trigger immediate high-priority push and remain in `undelivered` state
     (`!= DELIVERED`) until the Android client explicitly sends an authenticated `ACK_ALERT`.
   - When FCM drops/delays a push or the phone is offline, the Android client's `WorkManager` / reconnect sync
     (`sync_pending_alerts`) pulls all unacknowledged `HIGH / CRITICAL` alerts by monotonic sequence and completes
     the `DELIVERED` loop (`missed_alert_loss_rate == 0%`).
3. Ed25519 Hardware-Key Signed & Anti-Replay Protected Defensive Control (`POST /api/v1/control/*`):
   - Permits strictly defensive actions (`FREEZE_ENTRY -> ENTRY_FROZEN`, `SECURITY_LOCKDOWN -> SECURITY_LOCKDOWN`,
     and `ACK_ALERT`).
   - Cancels any resting unfilled `BUY` orders while strictly preserving existing `-1%` stop-loss protection
     `SELL` orders on open `PROTECTED` positions.
   - Physically blocks any mobile attempt to `PLACE_ORDER`, `UNFREEZE`, `UNLOCK_SECURITY_LOCKDOWN`,
     `RELAX_RISK`, or `ENABLE_LIVE`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from sui_quant.config import ExecutionMode
from sui_quant.contracts import (
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    AlertEvent,
    AlertSeverity,
    OrderSide,
    RegimeDecision,
    require_decimal,
)
from sui_quant.daemon.alert_manager import AlertManager, DeliveryStatus
from sui_quant.daemon.storage import StateStore
from sui_quant.execution.d01_engine import D01DecisionEngine
from sui_quant.registry.e01_model_registry import E01ModelRegistry
from sui_quant.registry.integrated_pipeline import D01IntegratedPipeline
from sui_quant.security.redaction import redact_mapping, redact_text
from sui_quant.state_machine import TradingState
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now

MAX_MOBILE_COMMAND_TTL = timedelta(minutes=5)


class MobileControlError(PermissionError):
    """Base exception for mobile gateway control rejections."""


class MobileControlAuthError(MobileControlError):
    """Raised when a mobile command fails Ed25519 signature, TTL, payload hash, or anti-replay verification."""


class MobileControlForbiddenActionError(MobileControlError):
    """Raised when a mobile command attempts a forbidden action (e.g., manual order, unfreeze, live enable)."""


class MobileControlAction(str, Enum):
    FREEZE_ENTRY = "FREEZE_ENTRY"
    SECURITY_LOCKDOWN = "SECURITY_LOCKDOWN"
    ACK_ALERT = "ACK_ALERT"


ALLOWED_MOBILE_CONTROL_ACTIONS: frozenset[str] = frozenset(
    {
        MobileControlAction.FREEZE_ENTRY.value,
        MobileControlAction.SECURITY_LOCKDOWN.value,
        MobileControlAction.ACK_ALERT.value,
    }
)

FORBIDDEN_MOBILE_CONTROL_ACTIONS: frozenset[str] = frozenset(
    {
        "PLACE_ORDER",
        "MANUAL_BUY",
        "MANUAL_SELL",
        "CANCEL_PROTECTION_ORDER",
        "UNFREEZE",
        "RESUME_TRADING",
        "UNLOCK_SECURITY_LOCKDOWN",
        "RELAX_RISK",
        "ENABLE_LIVE",
        "SWITCH_TO_LIVE",
        "CHANGE_SYMBOL",
    }
)


class AppLifecycleState(str, Enum):
    FOREGROUND = "FOREGROUND"
    BACKGROUND = "BACKGROUND"


class AlertPresentationChannel(str, Enum):
    FOREGROUND_MODAL_DIALOG = "FOREGROUND_MODAL_DIALOG"
    BACKGROUND_SYSTEM_NOTIFICATION = "BACKGROUND_SYSTEM_NOTIFICATION"
    IN_APP_PERMISSION_FALLBACK_BANNER = "IN_APP_PERMISSION_FALLBACK_BANNER"


@dataclass(frozen=True)
class MobileCommandEnvelope:
    """Ed25519-signed command envelope sent by the Android G-01 client (§12.1, QA-11)."""

    command_id: str
    device_id: str
    action: str
    issued_at_utc: datetime
    expires_at_utc: datetime
    reason: str
    target_alert_id: str | None = None
    presentation_channel: str | None = None
    payload_sha256: str = ""
    signature_hex: str = ""

    def __post_init__(self) -> None:
        ensure_utc(self.issued_at_utc, "issued_at_utc")
        ensure_utc(self.expires_at_utc, "expires_at_utc")
        if not self.command_id.strip():
            raise ValueError("command_id cannot be empty")
        if not self.device_id.strip():
            raise ValueError("device_id cannot be empty")
        if not self.action.strip():
            raise ValueError("action cannot be empty")

    def compute_payload_sha256(self) -> str:
        canonical_payload = json.dumps(
            {
                "action": self.action,
                "reason": self.reason,
                "target_alert_id": self.target_alert_id or "",
                "presentation_channel": self.presentation_channel or "",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    def canonical_signing_bytes(self) -> bytes:
        canonical = (
            f"G01MobileCommand|v=1|cmd={self.command_id}|dev={self.device_id}|"
            f"act={self.action}|iat={self.issued_at_utc.isoformat()}|"
            f"exp={self.expires_at_utc.isoformat()}|alert={self.target_alert_id or ''}|"
            f"chan={self.presentation_channel or ''}|sha256={self.payload_sha256}"
        )
        return canonical.encode("utf-8")


class MobileKeystoreSigner:
    """Represents the Android Keystore hardware-backed Ed25519 keypair used solely for mobile command signing.

    Strictly holds NO exchange API keys or trading secrets (§1, §12.1).
    """

    def __init__(self, device_id: str, private_key: Ed25519PrivateKey | None = None) -> None:
        if not device_id.strip():
            raise ValueError("device_id cannot be empty")
        self.device_id = device_id
        self._private_key = private_key or Ed25519PrivateKey.generate()
        pub_bytes = self._private_key.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )
        self.public_key_hex = pub_bytes.hex()

    def sign_command(
        self,
        *,
        action: str,
        reason: str,
        command_id: str | None = None,
        target_alert_id: str | None = None,
        presentation_channel: str | None = None,
        now_utc: datetime | None = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> MobileCommandEnvelope:
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        cmd_id = command_id or f"MCMD-{ref_now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:10]}"
        exp_utc = ref_now + ttl
        unsigned = MobileCommandEnvelope(
            command_id=cmd_id,
            device_id=self.device_id,
            action=action,
            issued_at_utc=ref_now,
            expires_at_utc=exp_utc,
            reason=reason,
            target_alert_id=target_alert_id,
            presentation_channel=presentation_channel,
        )
        payload_hash = unsigned.compute_payload_sha256()
        with_hash = MobileCommandEnvelope(
            command_id=cmd_id,
            device_id=self.device_id,
            action=action,
            issued_at_utc=ref_now,
            expires_at_utc=exp_utc,
            reason=reason,
            target_alert_id=target_alert_id,
            presentation_channel=presentation_channel,
            payload_sha256=payload_hash,
        )
        sig_bytes = self._private_key.sign(with_hash.canonical_signing_bytes())
        return MobileCommandEnvelope(
            command_id=cmd_id,
            device_id=self.device_id,
            action=action,
            issued_at_utc=ref_now,
            expires_at_utc=exp_utc,
            reason=reason,
            target_alert_id=target_alert_id,
            presentation_channel=presentation_channel,
            payload_sha256=payload_hash,
            signature_hex=sig_bytes.hex(),
        )


@dataclass(frozen=True)
class SyncedAlertItem:
    """Alert record returned by the incremental sync API (`GET /api/v1/alerts/sync`, QA-11)."""

    alert_seq: int
    alert_id: str
    severity: AlertSeverity
    source: str
    dedup_key: str
    first_seen_utc: datetime
    last_seen_utc: datetime
    occurrence_count: int
    delivery_status: DeliveryStatus
    actions_taken: tuple[str, ...]
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "alert_seq": self.alert_seq,
                "alert_id": self.alert_id,
                "severity": self.severity.value,
                "source": redact_text(self.source),
                "dedup_key": redact_text(self.dedup_key),
                "first_seen_utc": self.first_seen_utc.isoformat(),
                "last_seen_utc": self.last_seen_utc.isoformat(),
                "occurrence_count": self.occurrence_count,
                "delivery_status": self.delivery_status.value,
                "actions_taken": [redact_text(a) for a in self.actions_taken],
                "details": dict(self.details),
            }
        )


@dataclass(frozen=True)
class ActivePositionTelemetry:
    """Read-only active position snapshot for the Android dashboard (`GET /api/v1/positions/active`)."""

    position_id: str
    symbol: str
    actual_qty: str
    sellable_qty: str
    reserved_qty: str
    dust_qty: str
    avg_fill_price: str
    protection_orders: tuple[str, ...]
    active_stop_trigger_price: str | None
    pinned_exit_strategy_version: str
    risk_config_version: str
    bars_held: int
    high_water_mark_price: str | None
    filled_tranche_indices: tuple[int, ...]
    unrealized_pnl_usdt: str

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "position_id": self.position_id,
                "symbol": self.symbol,
                "actual_qty": self.actual_qty,
                "sellable_qty": self.sellable_qty,
                "reserved_qty": self.reserved_qty,
                "dust_qty": self.dust_qty,
                "avg_fill_price": self.avg_fill_price,
                "protection_orders": list(self.protection_orders),
                "active_stop_trigger_price": self.active_stop_trigger_price,
                "pinned_exit_strategy_version": self.pinned_exit_strategy_version,
                "risk_config_version": self.risk_config_version,
                "bars_held": self.bars_held,
                "high_water_mark_price": self.high_water_mark_price,
                "filled_tranche_indices": list(self.filled_tranche_indices),
                "unrealized_pnl_usdt": self.unrealized_pnl_usdt,
            }
        )


@dataclass(frozen=True)
class MobileStatusSummarySnapshot:
    """Read-only system telemetry snapshot (`GET /api/v1/status/summary`, §12.1)."""

    generated_at_utc: datetime
    execution_mode: str
    trading_symbol: str
    reference_symbol: str
    trading_state: TradingState
    allow_new_entry: bool
    latest_regime: str
    latest_regime_confidence: str
    active_paper_bundle_id: str
    active_paper_exit_version: str
    shadow_bundle_id: str | None
    pinned_position_exit_version: str | None
    has_active_position: bool
    rolling_24h_drawdown_pct: str
    max_rolling_24h_loss_limit_pct: str
    risk_config_version: str
    risk_config_sha256: str
    undelivered_high_critical_alerts_count: int
    ws_stream_healthy: bool
    f02_heartbeat_healthy: bool
    sanitized_audit_summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "generated_at_utc": self.generated_at_utc.isoformat(),
                "execution_mode": self.execution_mode,
                "trading_symbol": self.trading_symbol,
                "reference_symbol": self.reference_symbol,
                "trading_state": self.trading_state.value,
                "allow_new_entry": self.allow_new_entry,
                "latest_regime": self.latest_regime,
                "latest_regime_confidence": self.latest_regime_confidence,
                "active_paper_bundle_id": self.active_paper_bundle_id,
                "active_paper_exit_version": self.active_paper_exit_version,
                "shadow_bundle_id": self.shadow_bundle_id,
                "pinned_position_exit_version": self.pinned_position_exit_version,
                "has_active_position": self.has_active_position,
                "rolling_24h_drawdown_pct": self.rolling_24h_drawdown_pct,
                "max_rolling_24h_loss_limit_pct": self.max_rolling_24h_loss_limit_pct,
                "risk_config_version": self.risk_config_version,
                "risk_config_sha256": self.risk_config_sha256,
                "undelivered_high_critical_alerts_count": self.undelivered_high_critical_alerts_count,
                "ws_stream_healthy": self.ws_stream_healthy,
                "f02_heartbeat_healthy": self.f02_heartbeat_healthy,
                "sanitized_audit_summary": dict(self.sanitized_audit_summary),
            }
        )


@dataclass(frozen=True)
class DailyAuditDigestReport:
    """24-hour daily audit digest including `INFO` events that do not require real-time push (§10, §12.2)."""

    report_date_utc: str
    generated_at_utc: datetime
    execution_mode: str
    trading_state: str
    realized_trade_count_24h: int
    realized_net_pnl_24h_usdt: str
    rolling_24h_drawdown_pct: str
    info_digest_events: tuple[dict[str, Any], ...]
    high_critical_alerts_summary: tuple[dict[str, Any], ...]
    state_transitions_count: int
    model_rollback_events_count: int
    security_incidents_count: int
    extra_digest_notes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "report_date_utc": self.report_date_utc,
                "generated_at_utc": self.generated_at_utc.isoformat(),
                "execution_mode": self.execution_mode,
                "trading_state": self.trading_state,
                "realized_trade_count_24h": self.realized_trade_count_24h,
                "realized_net_pnl_24h_usdt": self.realized_net_pnl_24h_usdt,
                "rolling_24h_drawdown_pct": self.rolling_24h_drawdown_pct,
                "info_digest_events": list(self.info_digest_events),
                "high_critical_alerts_summary": list(self.high_critical_alerts_summary),
                "state_transitions_count": self.state_transitions_count,
                "model_rollback_events_count": self.model_rollback_events_count,
                "security_incidents_count": self.security_incidents_count,
                "extra_digest_notes": dict(self.extra_digest_notes),
            }
        )


class G01MobileGateway:
    """Cloud G-01 / G-02 Mobile Telemetry, Alert Sync, and Authenticated Emergency Control Gateway (§12, QA-11)."""

    def __init__(
        self,
        *,
        pipeline: D01IntegratedPipeline | None = None,
        d01_engine: D01DecisionEngine | None = None,
        registry: E01ModelRegistry | None = None,
        store: StateStore | None = None,
        alert_manager: AlertManager | None = None,
    ) -> None:
        if pipeline is not None:
            self.pipeline = pipeline
            self.d01_engine = pipeline.d01_engine
            self.registry = pipeline.registry
            self.store = pipeline.store
            self.alert_manager = pipeline.alert_manager
        else:
            if d01_engine is None or store is None or alert_manager is None:
                raise ValueError(
                    "Either `pipeline` or (`d01_engine`, `store`, `alert_manager`) must be provided"
                )
            self.pipeline = None
            self.d01_engine = d01_engine
            self.registry = registry
            self.store = store
            self.alert_manager = alert_manager

        if self.d01_engine.config.mode not in {ExecutionMode.PAPER, ExecutionMode.RESEARCH}:
            raise PermissionError(
                f"P7 strictly locks G01MobileGateway to PAPER/RESEARCH, got {self.d01_engine.config.mode}"
            )

        self._init_p7_tables()
        self._query_latencies_ns: list[int] = []
        self._forbidden_action_reject_count = 0
        self._replay_or_auth_reject_count = 0

    def _init_p7_tables(self) -> None:
        self.store._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS p7_registered_mobile_devices (
                device_id TEXT PRIMARY KEY,
                ed25519_pubkey_hex TEXT NOT NULL,
                registered_at_utc TEXT NOT NULL,
                is_revoked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS p7_consumed_mobile_nonces (
                command_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                consumed_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS p7_mobile_alert_acks (
                alert_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                presentation_channel TEXT NOT NULL,
                acked_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS p7_mobile_control_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL
            );
            """
        )

    @property
    def query_latencies_ns(self) -> tuple[int, ...]:
        return tuple(self._query_latencies_ns)

    @property
    def forbidden_action_reject_count(self) -> int:
        return self._forbidden_action_reject_count

    @property
    def replay_or_auth_reject_count(self) -> int:
        return self._replay_or_auth_reject_count

    def register_mobile_device(
        self,
        *,
        device_id: str,
        ed25519_pubkey_hex: str,
        now_utc: datetime | None = None,
    ) -> None:
        """Pin an Android device's Keystore Ed25519 public key for authenticated defensive commands."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if not device_id.strip():
            raise ValueError("device_id cannot be empty")
        raw_pub = bytes.fromhex(ed25519_pubkey_hex)
        if len(raw_pub) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes (64 hex chars)")
        Ed25519PublicKey.from_public_bytes(raw_pub)
        self.store._conn.execute(
            """
            INSERT INTO p7_registered_mobile_devices (device_id, ed25519_pubkey_hex, registered_at_utc, is_revoked)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(device_id) DO UPDATE SET
                ed25519_pubkey_hex=excluded.ed25519_pubkey_hex,
                is_revoked=0
            """,
            (device_id, ed25519_pubkey_hex, ref_now.isoformat()),
        )

    def get_status_summary(
        self,
        *,
        mark_price_sui: Decimal | None = None,
        latest_regime: RegimeDecision | None = None,
        ws_stream_healthy: bool = True,
        f02_heartbeat_healthy: bool = True,
        extra_audit_metadata: Mapping[str, Any] | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Return sanitized read-only status summary (`GET /api/v1/status/summary`, §12.1)."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        state = self.d01_engine.current_state
        ledger = self.d01_engine.active_position_ledger
        dd_24h = self.d01_engine.current_rolling_24h_drawdown_pct(
            mark_price_sui=mark_price_sui,
            now_utc=ref_now,
        )
        undelivered = self.alert_manager.list_undelivered_alerts()

        active_bundle_id = (
            self.registry.active_paper_bundle.bundle_id
            if self.registry is not None
            else "B01-STATIC-PAPER"
        )
        active_exit_ver = (
            self.registry.active_paper_exit_strategy_version
            if self.registry is not None
            else "C1-v1.0"
        )
        shadow_bundle_id = (
            self.registry.shadow_bundle.bundle_id
            if (self.registry is not None and self.registry.shadow_bundle is not None)
            else None
        )
        pinned_exit_ver = ledger.exit_strategy_version if ledger is not None else None

        allow_entry = (
            state == TradingState.READY_NO_POSITION
            and ws_stream_healthy
            and f02_heartbeat_healthy
            and (latest_regime.allow_entry if latest_regime is not None else True)
        )

        snapshot = MobileStatusSummarySnapshot(
            generated_at_utc=ref_now,
            execution_mode=self.d01_engine.config.mode.value,
            trading_symbol=TRADING_SYMBOL,
            reference_symbol=REFERENCE_SYMBOL,
            trading_state=state,
            allow_new_entry=allow_entry,
            latest_regime=latest_regime.regime.value if latest_regime is not None else "CONSOLIDATION",
            latest_regime_confidence=(
                str(latest_regime.confidence) if latest_regime is not None else "0.0000"
            ),
            active_paper_bundle_id=active_bundle_id,
            active_paper_exit_version=active_exit_ver,
            shadow_bundle_id=shadow_bundle_id,
            pinned_position_exit_version=pinned_exit_ver,
            has_active_position=ledger is not None,
            rolling_24h_drawdown_pct=f"{dd_24h:.6f}",
            max_rolling_24h_loss_limit_pct=str(self.d01_engine.config.risk.max_rolling_24h_loss_pct),
            risk_config_version=self.d01_engine.config.risk.version,
            risk_config_sha256=self.d01_engine.config.risk.canonical_sha256(),
            undelivered_high_critical_alerts_count=len(undelivered),
            ws_stream_healthy=ws_stream_healthy,
            f02_heartbeat_healthy=f02_heartbeat_healthy,
            sanitized_audit_summary=redact_mapping(dict(extra_audit_metadata or {})),
        )
        res = snapshot.to_dict()
        self._query_latencies_ns.append(monotonic_ns() - t0_ns)
        return res

    def get_active_position(
        self,
        *,
        mark_price_sui: Decimal | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return read-only telemetry for the currently open `PositionLedger` (`GET /api/v1/positions/active`)."""
        t0_ns = monotonic_ns()
        ensure_utc(now_utc or utc_now(), "now_utc")
        ledger = self.d01_engine.active_position_ledger
        if ledger is None:
            self._query_latencies_ns.append(monotonic_ns() - t0_ns)
            return None

        sellable_qty = self.d01_engine.filters.quantize_qty_down(ledger.actual_qty)
        stop_price_str: str | None = None
        for cid in ledger.protection_orders:
            snap = self.d01_engine.exchange._orders.get(cid)
            if snap is not None and snap.stop_price is not None:
                stop_price_str = str(snap.stop_price)
                break

        bars_held = 0
        hwm_str: str | None = None
        filled_tranches: tuple[int, ...] = ()
        if self.registry is not None:
            pin_state = self.registry.pin_registry._pinned_positions.get(ledger.position_id)
            if pin_state is not None:
                bars_held = pin_state.bars_held
                hwm_str = str(pin_state.high_water_mark_price)
                filled_tranches = tuple(pin_state.filled_tranche_indices)

        ref_mark = require_decimal(mark_price_sui, "mark_price_sui") if mark_price_sui is not None else ledger.avg_fill_price
        unrealized = (ref_mark - ledger.avg_fill_price) * ledger.actual_qty

        telemetry = ActivePositionTelemetry(
            position_id=ledger.position_id,
            symbol=ledger.symbol,
            actual_qty=str(ledger.actual_qty),
            sellable_qty=str(sellable_qty),
            reserved_qty=str(ledger.reserved_qty),
            dust_qty=str(self.d01_engine.active_dust_qty),
            avg_fill_price=str(ledger.avg_fill_price),
            protection_orders=ledger.protection_orders,
            active_stop_trigger_price=stop_price_str,
            pinned_exit_strategy_version=ledger.exit_strategy_version,
            risk_config_version=ledger.risk_config_version,
            bars_held=bars_held,
            high_water_mark_price=hwm_str,
            filled_tranche_indices=filled_tranches,
            unrealized_pnl_usdt=f"{unrealized:.4f}",
        )
        res = telemetry.to_dict()
        self._query_latencies_ns.append(monotonic_ns() - t0_ns)
        return res

    def sync_pending_alerts(self, *, since_alert_seq: int = 0) -> list[SyncedAlertItem]:
        """Return all unacknowledged (`!= DELIVERED`) HIGH/CRITICAL alerts with `rowid > since_alert_seq` (QA-11)."""
        t0_ns = monotonic_ns()
        if since_alert_seq < 0:
            raise ValueError("since_alert_seq must be >= 0")
        rows = self.store._conn.execute(
            """
            SELECT rowid AS alert_seq, *
            FROM alert_events
            WHERE severity IN ('HIGH', 'CRITICAL')
              AND delivery_status != ?
              AND rowid > ?
            ORDER BY rowid ASC
            """,
            (DeliveryStatus.DELIVERED.value, since_alert_seq),
        ).fetchall()

        items: list[SyncedAlertItem] = []
        for r in rows:
            raw_details = json.loads(str(r["details_json"]))
            raw_actions = json.loads(str(r["actions_taken_json"]))
            items.append(
                SyncedAlertItem(
                    alert_seq=int(r["alert_seq"]),
                    alert_id=str(r["alert_id"]),
                    severity=AlertSeverity(str(r["severity"])),
                    source=redact_text(str(r["source"])),
                    dedup_key=redact_text(str(r["dedup_key"])),
                    first_seen_utc=datetime.fromisoformat(str(r["first_seen_utc"])),
                    last_seen_utc=datetime.fromisoformat(str(r["last_seen_utc"])),
                    occurrence_count=int(r["occurrence_count"]),
                    delivery_status=DeliveryStatus(str(r["delivery_status"])),
                    actions_taken=tuple(redact_text(str(a)) for a in raw_actions),
                    details=redact_mapping(raw_details if isinstance(raw_details, Mapping) else {}),
                )
            )
        self._query_latencies_ns.append(monotonic_ns() - t0_ns)
        return items

    def get_daily_report(
        self,
        *,
        mark_price_sui: Decimal | None = None,
        extra_digest_notes: Mapping[str, Any] | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Generate 24-hour sanitized daily audit report including `INFO` events (`GET /api/v1/daily-report`)."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        window_start = ref_now - timedelta(hours=24)

        pnl_entries = [
            e for e in self.d01_engine.get_realized_pnl_entries() if e.closed_at_utc >= window_start
        ]
        net_pnl_24h = sum((e.net_pnl_usdt for e in pnl_entries), Decimal("0.0000"))
        dd_24h = self.d01_engine.current_rolling_24h_drawdown_pct(
            mark_price_sui=mark_price_sui,
            now_utc=ref_now,
        )

        info_rows = self.store._conn.execute(
            """
            SELECT alert_id, source, dedup_key, first_seen_utc, last_seen_utc,
                   occurrence_count, delivery_status, actions_taken_json, details_json
            FROM alert_events
            WHERE severity = 'INFO'
            ORDER BY rowid ASC
            """
        ).fetchall()
        info_digest = tuple(
            redact_mapping(
                {
                    "alert_id": str(r["alert_id"]),
                    "source": redact_text(str(r["source"])),
                    "dedup_key": redact_text(str(r["dedup_key"])),
                    "first_seen_utc": str(r["first_seen_utc"]),
                    "last_seen_utc": str(r["last_seen_utc"]),
                    "occurrence_count": int(r["occurrence_count"]),
                    "delivery_status": str(r["delivery_status"]),
                    "actions_taken": json.loads(str(r["actions_taken_json"])),
                    "details": json.loads(str(r["details_json"])),
                }
            )
            for r in info_rows
        )

        hc_rows = self.store._conn.execute(
            """
            SELECT alert_id, severity, source, dedup_key, first_seen_utc,
                   occurrence_count, delivery_status
            FROM alert_events
            WHERE severity IN ('HIGH', 'CRITICAL')
            ORDER BY rowid ASC
            """
        ).fetchall()
        hc_summary = tuple(
            redact_mapping(
                {
                    "alert_id": str(r["alert_id"]),
                    "severity": str(r["severity"]),
                    "source": redact_text(str(r["source"])),
                    "dedup_key": redact_text(str(r["dedup_key"])),
                    "first_seen_utc": str(r["first_seen_utc"]),
                    "occurrence_count": int(r["occurrence_count"]),
                    "delivery_status": str(r["delivery_status"]),
                }
            )
            for r in hc_rows
        )

        transitions = self.store.list_transitions()
        sec_incidents = self.store.list_security_incidents()
        rollback_count = (
            len(self.registry.rollback_history) if self.registry is not None else 0
        )

        report = DailyAuditDigestReport(
            report_date_utc=ref_now.strftime("%Y-%m-%d"),
            generated_at_utc=ref_now,
            execution_mode=self.d01_engine.config.mode.value,
            trading_state=self.d01_engine.current_state.value,
            realized_trade_count_24h=len(pnl_entries),
            realized_net_pnl_24h_usdt=f"{net_pnl_24h:.4f}",
            rolling_24h_drawdown_pct=f"{dd_24h:.6f}",
            info_digest_events=info_digest,
            high_critical_alerts_summary=hc_summary,
            state_transitions_count=len(transitions),
            model_rollback_events_count=rollback_count,
            security_incidents_count=len(sec_incidents),
            extra_digest_notes=redact_mapping(dict(extra_digest_notes or {})),
        )
        res = report.to_dict()
        self._query_latencies_ns.append(monotonic_ns() - t0_ns)
        return res

    def _record_control_audit(
        self,
        *,
        envelope: MobileCommandEnvelope,
        allowed: bool,
        from_state: TradingState,
        to_state: TradingState,
        reason: str,
        now_utc: datetime,
    ) -> None:
        self.store._conn.execute(
            """
            INSERT INTO p7_mobile_control_audit (
                command_id, device_id, action, allowed, from_state, to_state, reason, occurred_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope.command_id,
                envelope.device_id,
                envelope.action,
                1 if allowed else 0,
                from_state.value,
                to_state.value,
                redact_text(reason),
                now_utc.isoformat(),
            ),
        )

    def _verify_and_consume_command_envelope(
        self,
        envelope: MobileCommandEnvelope,
        now_utc: datetime,
    ) -> None:
        """Verify action whitelist, device Ed25519 signature, payload hash, TTL, and anti-replay nonce."""
        state_before = self.d01_engine.current_state

        # 1. Hard Action Whitelist Check (Forbid PLACE_ORDER, UNFREEZE, ENABLE_LIVE, RELAX_RISK, etc.)
        if (
            envelope.action in FORBIDDEN_MOBILE_CONTROL_ACTIONS
            or envelope.action not in ALLOWED_MOBILE_CONTROL_ACTIONS
        ):
            self._forbidden_action_reject_count += 1
            self._record_control_audit(
                envelope=envelope,
                allowed=False,
                from_state=state_before,
                to_state=state_before,
                reason=f"FORBIDDEN_MOBILE_ACTION:{envelope.action}",
                now_utc=now_utc,
            )
            self.alert_manager.emit_alert(
                severity=AlertSeverity.HIGH,
                source="G-01-GATEWAY",
                dedup_key=f"G01-FORBIDDEN-MOBILE-ACTION-{envelope.action}",
                actions_taken=("REJECT_FORBIDDEN_MOBILE_COMMAND",),
                details={
                    "device_id": envelope.device_id,
                    "command_id": envelope.command_id,
                    "attempted_action": envelope.action,
                },
                now_utc=now_utc,
            )
            raise MobileControlForbiddenActionError(
                f"Mobile action '{envelope.action}' is strictly forbidden by G-01 security boundary"
            )

        # 2. Verify registered device public key
        dev_row = self.store._conn.execute(
            """
            SELECT ed25519_pubkey_hex, is_revoked
            FROM p7_registered_mobile_devices
            WHERE device_id = ?
            """,
            (envelope.device_id,),
        ).fetchone()
        if dev_row is None or int(dev_row["is_revoked"]) != 0:
            self._replay_or_auth_reject_count += 1
            self._record_control_audit(
                envelope=envelope,
                allowed=False,
                from_state=state_before,
                to_state=state_before,
                reason="UNREGISTERED_OR_REVOKED_DEVICE",
                now_utc=now_utc,
            )
            raise MobileControlAuthError(
                f"Unregistered or revoked mobile device_id '{envelope.device_id}'"
            )

        # 3. Verify TTL window
        ttl = envelope.expires_at_utc - envelope.issued_at_utc
        if ttl <= timedelta(0) or ttl > MAX_MOBILE_COMMAND_TTL:
            self._replay_or_auth_reject_count += 1
            raise MobileControlAuthError(
                f"Mobile command TTL ({ttl}) exceeds max allowed {MAX_MOBILE_COMMAND_TTL}"
            )
        if now_utc < envelope.issued_at_utc - timedelta(seconds=5) or now_utc > envelope.expires_at_utc:
            self._replay_or_auth_reject_count += 1
            raise MobileControlAuthError("Mobile command timestamp expired or outside validity window")

        # 4. Verify payload SHA-256
        expected_sha = envelope.compute_payload_sha256()
        if envelope.payload_sha256 != expected_sha:
            self._replay_or_auth_reject_count += 1
            raise MobileControlAuthError("Mobile command payload_sha256 mismatch")

        # 5. Verify Ed25519 signature
        try:
            pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(str(dev_row["ed25519_pubkey_hex"])))
            sig_bytes = bytes.fromhex(envelope.signature_hex)
            pub_key.verify(sig_bytes, envelope.canonical_signing_bytes())
        except (ValueError, InvalidSignature) as exc:
            self._replay_or_auth_reject_count += 1
            self._record_control_audit(
                envelope=envelope,
                allowed=False,
                from_state=state_before,
                to_state=state_before,
                reason="INVALID_ED25519_SIGNATURE",
                now_utc=now_utc,
            )
            raise MobileControlAuthError("Invalid Ed25519 signature on mobile command") from exc

        # 6. Atomic Anti-Replay Nonce Consumption in SQLite WAL
        conn = self.store._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT consumed_at_utc FROM p7_consumed_mobile_nonces WHERE command_id = ?",
                (envelope.command_id,),
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                self._replay_or_auth_reject_count += 1
                self._record_control_audit(
                    envelope=envelope,
                    allowed=False,
                    from_state=state_before,
                    to_state=state_before,
                    reason=f"REPLAYED_COMMAND_NONCE:{envelope.command_id}",
                    now_utc=now_utc,
                )
                raise MobileControlAuthError(
                    f"Anti-replay block: mobile command_id '{envelope.command_id}' already consumed at {existing['consumed_at_utc']}"
                )
            conn.execute(
                """
                INSERT INTO p7_consumed_mobile_nonces (
                    command_id, device_id, action, payload_sha256, consumed_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    envelope.command_id,
                    envelope.device_id,
                    envelope.action,
                    envelope.payload_sha256,
                    now_utc.isoformat(),
                ),
            )
            conn.execute("COMMIT")
        except MobileControlAuthError:
            raise
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise MobileControlAuthError(f"Failed to persist mobile command nonce: {exc}") from exc

    def _cancel_open_buy_orders_preserving_sell_protection(self, now_utc: datetime) -> list[str]:
        """Cancel any resting open BUY orders while strictly preserving open SELL protection orders (§12.1)."""
        canceled_buy_cids: list[str] = []
        open_buys = self.d01_engine.exchange.list_open_orders(side=OrderSide.BUY)
        for buy_snap in open_buys:
            self.d01_engine.exchange.cancel_order(
                symbol=TRADING_SYMBOL,
                client_order_id=buy_snap.client_order_id,
                now_utc=now_utc,
            )
            self.d01_engine._update_intent_status(
                buy_snap.client_order_id,
                "CANCELED_BY_MOBILE_DEFENSIVE_CONTROL",
            )
            canceled_buy_cids.append(buy_snap.client_order_id)
        return canceled_buy_cids

    def execute_control_command(
        self,
        envelope: MobileCommandEnvelope,
        *,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify and execute an authenticated mobile command (`FREEZE_ENTRY`, `SECURITY_LOCKDOWN`, or `ACK_ALERT`)."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        self._verify_and_consume_command_envelope(envelope, ref_now)

        state_before = self.d01_engine.current_state

        if envelope.action == MobileControlAction.ACK_ALERT.value:
            if not envelope.target_alert_id:
                raise ValueError("ACK_ALERT command requires target_alert_id")
            channel = envelope.presentation_channel or AlertPresentationChannel.FOREGROUND_MODAL_DIALOG.value
            self.alert_manager.acknowledge_alert(envelope.target_alert_id)
            self.store._conn.execute(
                """
                INSERT INTO p7_mobile_alert_acks (
                    alert_id, device_id, command_id, presentation_channel, acked_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(alert_id) DO UPDATE SET
                    device_id=excluded.device_id,
                    command_id=excluded.command_id,
                    presentation_channel=excluded.presentation_channel,
                    acked_at_utc=excluded.acked_at_utc
                """,
                (
                    envelope.target_alert_id,
                    envelope.device_id,
                    envelope.command_id,
                    channel,
                    ref_now.isoformat(),
                ),
            )
            self._record_control_audit(
                envelope=envelope,
                allowed=True,
                from_state=state_before,
                to_state=state_before,
                reason=f"ACK_ALERT:{envelope.target_alert_id}:{channel}",
                now_utc=ref_now,
            )
            self._query_latencies_ns.append(monotonic_ns() - t0_ns)
            return {
                "status": "ACK_DELIVERED",
                "command_id": envelope.command_id,
                "alert_id": envelope.target_alert_id,
                "delivery_status": DeliveryStatus.DELIVERED.value,
                "presentation_channel": channel,
                "acked_at_utc": ref_now.isoformat(),
            }

        canceled_buys = self._cancel_open_buy_orders_preserving_sell_protection(ref_now)
        open_sell_protections = [
            s.client_order_id for s in self.d01_engine.exchange.list_open_orders(side=OrderSide.SELL)
        ]

        if envelope.action == MobileControlAction.FREEZE_ENTRY.value:
            target_state = TradingState.ENTRY_FROZEN
            self.d01_engine._transition_to(
                target_state,
                f"MOBILE_FREEZE_ENTRY:{envelope.device_id}:{envelope.reason}",
                now_utc=ref_now,
            )
            self.alert_manager.emit_alert(
                severity=AlertSeverity.HIGH,
                source="G-01-MOBILE",
                dedup_key=f"G01-MOBILE-FREEZE-{envelope.command_id}",
                actions_taken=("ENTRY_FROZEN", "PRESERVE_STOP_PROTECTION"),
                details={
                    "device_id": envelope.device_id,
                    "command_id": envelope.command_id,
                    "canceled_buy_orders": canceled_buys,
                    "preserved_sell_protection_orders": open_sell_protections,
                    "reason": envelope.reason,
                },
                now_utc=ref_now,
            )
        elif envelope.action == MobileControlAction.SECURITY_LOCKDOWN.value:
            target_state = TradingState.SECURITY_LOCKDOWN
            self.d01_engine._transition_to(
                target_state,
                f"MOBILE_SECURITY_LOCKDOWN:{envelope.device_id}:{envelope.reason}",
                now_utc=ref_now,
            )
            self.alert_manager.emit_alert(
                severity=AlertSeverity.CRITICAL,
                source="G-01-MOBILE",
                dedup_key=f"G01-MOBILE-LOCKDOWN-{envelope.command_id}",
                actions_taken=("SECURITY_LOCKDOWN", "PRESERVE_STOP_PROTECTION"),
                details={
                    "device_id": envelope.device_id,
                    "command_id": envelope.command_id,
                    "canceled_buy_orders": canceled_buys,
                    "preserved_sell_protection_orders": open_sell_protections,
                    "reason": envelope.reason,
                },
                now_utc=ref_now,
            )
        else:
            raise MobileControlForbiddenActionError(f"Unsupported action: {envelope.action}")

        self._record_control_audit(
            envelope=envelope,
            allowed=True,
            from_state=state_before,
            to_state=target_state,
            reason=envelope.reason,
            now_utc=ref_now,
        )
        self._query_latencies_ns.append(monotonic_ns() - t0_ns)
        return {
            "status": "APPLIED",
            "command_id": envelope.command_id,
            "action": envelope.action,
            "previous_state": state_before.value,
            "current_state": self.d01_engine.current_state.value,
            "canceled_buy_orders": canceled_buys,
            "preserved_sell_protection_orders": open_sell_protections,
            "applied_at_utc": ref_now.isoformat(),
        }


class FcmPushTransportSimulator:
    """Simulates Google FCM High-Priority Data+Notification transport with injectable packet drops (`QA-11`)."""

    def __init__(self) -> None:
        self.clients: list[AndroidMonitorClientSimulator] = []
        self.drop_all_pushes: bool = False
        self.push_attempts_count: int = 0
        self.push_dropped_count: int = 0
        self.push_delivered_to_client_count: int = 0

    def register_client(self, client: AndroidMonitorClientSimulator) -> None:
        if client not in self.clients:
            self.clients.append(client)

    def send_push(self, alert_event: AlertEvent, details: Mapping[str, Any]) -> bool:
        """Callback wired into `AlertManager(push_sender=...)`."""
        self.push_attempts_count += 1
        if self.drop_all_pushes:
            self.push_dropped_count += 1
            return False

        any_reachable = False
        for client in self.clients:
            if client.network_online:
                any_reachable = True
                self.push_delivered_to_client_count += 1
                client.on_fcm_push_received(
                    alert_event=alert_event,
                    details=details,
                    now_utc=alert_event.first_seen_utc,
                )
        if not any_reachable:
            self.push_dropped_count += 1
            return False
        return True


@dataclass
class ClientAlertPresentationRecord:
    """Audit record of how an alert was surfaced on the Android client (`QA-11`)."""

    alert_id: str
    severity: AlertSeverity
    channel: AlertPresentationChannel
    presented_at_utc: datetime
    acknowledged_to_server: bool
    source_transport: str  # "FCM_REALTIME_PUSH" or "WORKMANAGER_INCREMENTAL_SYNC"


class AndroidMonitorClientSimulator:
    """Deterministic state machine modeling the Kotlin + Jetpack Compose + Room + WorkManager Android client (`QA-11`).

    Verifies:
    1. Foreground state (`AppLifecycleState.FOREGROUND`) -> immediate modal `AlertDialog` + signed ACK.
    2. Background state (`AppLifecycleState.BACKGROUND`) with `POST_NOTIFICATIONS` granted -> high-priority
       system `NotificationChannel (IMPORTANCE_HIGH)` + signed ACK.
    3. `POST_NOTIFICATIONS` permission denied -> activates persistent top red warning banner in UI and recovers
       unacknowledged alerts via `WorkManager` / foreground sync.
    4. FCM drop or phone offline -> upon reconnect / `WorkManager` periodic run, pulls all `!= DELIVERED`
       `HIGH / CRITICAL` alerts via `gateway.sync_pending_alerts(since_alert_seq=...)`, stores in Room cache,
       displays to user, and sends signed `ACK_ALERT` (`missed_alert_loss_rate == 0%`).
    """

    def __init__(
        self,
        *,
        gateway: G01MobileGateway,
        signer: MobileKeystoreSigner,
        lifecycle_state: AppLifecycleState = AppLifecycleState.FOREGROUND,
        notification_permission_granted: bool = True,
        network_online: bool = True,
    ) -> None:
        self.gateway = gateway
        self.signer = signer
        self.lifecycle_state = lifecycle_state
        self.notification_permission_granted = notification_permission_granted
        self.network_online = network_online

        # Local Room database state equivalents
        self.room_cached_alerts: dict[str, dict[str, Any]] = {}
        self.last_synced_alert_seq: int = 0

        # UI presentation state
        self.foreground_modal_queue: list[ClientAlertPresentationRecord] = []
        self.background_system_notifications: list[ClientAlertPresentationRecord] = []
        self.in_app_fallback_banners: list[ClientAlertPresentationRecord] = []

    @property
    def permission_denied_top_banner_active(self) -> bool:
        """True whenever Android `POST_NOTIFICATIONS` permission is denied (`QA-11`)."""
        return not self.notification_permission_granted

    def _send_signed_ack(
        self,
        *,
        alert_id: str,
        channel: AlertPresentationChannel,
        now_utc: datetime,
    ) -> bool:
        if not self.network_online:
            return False
        env = self.signer.sign_command(
            action=MobileControlAction.ACK_ALERT.value,
            reason=f"Client presented alert via {channel.value}",
            target_alert_id=alert_id,
            presentation_channel=channel.value,
            now_utc=now_utc,
        )
        self.gateway.execute_control_command(env, now_utc=now_utc)
        return True

    def on_fcm_push_received(
        self,
        *,
        alert_event: AlertEvent,
        details: Mapping[str, Any],
        now_utc: datetime | None = None,
    ) -> ClientAlertPresentationRecord | None:
        """Handle incoming FCM High-Priority push message (`SuiQuantFcmService.onMessageReceived`)."""
        if not self.network_online:
            return None
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")

        if self.lifecycle_state == AppLifecycleState.FOREGROUND:
            channel = AlertPresentationChannel.FOREGROUND_MODAL_DIALOG
            acked = self._send_signed_ack(
                alert_id=alert_event.alert_id,
                channel=channel,
                now_utc=ref_now,
            )
            rec = ClientAlertPresentationRecord(
                alert_id=alert_event.alert_id,
                severity=alert_event.severity,
                channel=channel,
                presented_at_utc=ref_now,
                acknowledged_to_server=acked,
                source_transport="FCM_REALTIME_PUSH",
            )
            self.room_cached_alerts[alert_event.alert_id] = {
                "alert_id": alert_event.alert_id,
                "severity": alert_event.severity.value,
                "channel": channel.value,
                "details": redact_mapping(dict(details)),
                "acked": acked,
            }
            self.foreground_modal_queue.append(rec)
            return rec

        # App is in BACKGROUND
        if not self.notification_permission_granted:
            # OS blocks background system notification when POST_NOTIFICATIONS is denied;
            # alert remains unacknowledged on server so WorkManager / foreground resume will recover it!
            return None

        channel = AlertPresentationChannel.BACKGROUND_SYSTEM_NOTIFICATION
        acked = self._send_signed_ack(
            alert_id=alert_event.alert_id,
            channel=channel,
            now_utc=ref_now,
        )
        rec = ClientAlertPresentationRecord(
            alert_id=alert_event.alert_id,
            severity=alert_event.severity,
            channel=channel,
            presented_at_utc=ref_now,
            acknowledged_to_server=acked,
            source_transport="FCM_REALTIME_PUSH",
        )
        self.room_cached_alerts[alert_event.alert_id] = {
            "alert_id": alert_event.alert_id,
            "severity": alert_event.severity.value,
            "channel": channel.value,
            "details": redact_mapping(dict(details)),
            "acked": acked,
        }
        self.background_system_notifications.append(rec)
        return rec

    def run_work_manager_incremental_sync(
        self,
        *,
        now_utc: datetime | None = None,
    ) -> list[ClientAlertPresentationRecord]:
        """Execute `AlertSyncWorker` incremental pull to recover missed alerts after FCM drop or offline reconnect."""
        if not self.network_online:
            return []
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        pending_items = self.gateway.sync_pending_alerts(since_alert_seq=0)
        recovered: list[ClientAlertPresentationRecord] = []

        for item in pending_items:
            self.last_synced_alert_seq = max(self.last_synced_alert_seq, item.alert_seq)
            if self.lifecycle_state == AppLifecycleState.FOREGROUND:
                channel = (
                    AlertPresentationChannel.FOREGROUND_MODAL_DIALOG
                    if self.notification_permission_granted
                    else AlertPresentationChannel.IN_APP_PERMISSION_FALLBACK_BANNER
                )
            else:
                if self.notification_permission_granted:
                    channel = AlertPresentationChannel.BACKGROUND_SYSTEM_NOTIFICATION
                else:
                    channel = AlertPresentationChannel.IN_APP_PERMISSION_FALLBACK_BANNER

            acked = self._send_signed_ack(
                alert_id=item.alert_id,
                channel=channel,
                now_utc=ref_now,
            )
            rec = ClientAlertPresentationRecord(
                alert_id=item.alert_id,
                severity=item.severity,
                channel=channel,
                presented_at_utc=ref_now,
                acknowledged_to_server=acked,
                source_transport="WORKMANAGER_INCREMENTAL_SYNC",
            )
            self.room_cached_alerts[item.alert_id] = {
                "alert_id": item.alert_id,
                "severity": item.severity.value,
                "channel": channel.value,
                "details": dict(item.details),
                "acked": acked,
            }
            if channel == AlertPresentationChannel.FOREGROUND_MODAL_DIALOG:
                self.foreground_modal_queue.append(rec)
            elif channel == AlertPresentationChannel.BACKGROUND_SYSTEM_NOTIFICATION:
                self.background_system_notifications.append(rec)
            else:
                self.in_app_fallback_banners.append(rec)
                self.foreground_modal_queue.append(rec)
            recovered.append(rec)

        return recovered
