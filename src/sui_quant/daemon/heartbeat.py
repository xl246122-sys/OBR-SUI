"""F-02 System Guardian: 5s application heartbeat, 30s stall detector, crash-loop circuit breaker,
and mandatory BOOT -> SYNCING 5-gate recovery coordinator (§7, §8.2, §9).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sui_quant.config import RiskConfig
from sui_quant.contracts import AlertSeverity, DataQuality, PositionLedger
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.exchange_rules import (
    FilterSourceType,
    SymbolFilters,
    compute_post_fill_sellable_position,
)
from sui_quant.state_machine import StateTransitionRecord, TradingState, assert_valid_transition
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


HEARTBEAT_INTERVAL_SEC = 5
STALL_TIMEOUT_SEC = 30
CRASH_LOOP_WINDOW_SEC = 300
CRASH_LOOP_MAX_BURST = 5


@dataclass(frozen=True)
class ServiceHeartbeat:
    service_name: str
    pid: int
    boot_id: str
    monotonic_ns: int
    wall_time_utc: datetime
    loop_tick_seq: int
    last_work_time_utc: datetime
    rss_bytes: int
    service_state: str = "RUNNING"

    def __post_init__(self) -> None:
        ensure_utc(self.wall_time_utc, "wall_time_utc")
        ensure_utc(self.last_work_time_utc, "last_work_time_utc")


@dataclass(frozen=True)
class SimulatedExchangeState:
    """Deterministic simulated exchange state used in P1 to verify BOOT -> SYNCING recovery without API keys."""

    reachable: bool = True
    actual_sui_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("2.0000")
    fee_asset: str = "BNB"
    fee_amount: Decimal = Decimal("0")
    active_protection_qty: Decimal = Decimal("0")
    protection_order_ids: tuple[str, ...] = ()
    can_place_protection_order: bool = True


@dataclass(frozen=True)
class RecoveryGateReport:
    passed: bool
    target_state: TradingState
    failed_gates: tuple[str, ...]
    reconciled_ledger: PositionLedger | None = None


class F02Guardian:
    """F-02 Guardian coordinating service heartbeats, stall detection, crash circuit-breaking, and SYNCING recovery."""

    def __init__(
        self,
        store: StateStore,
        alert_manager: AlertManager,
        expected_risk_config: RiskConfig,
        stall_timeout_sec: int = STALL_TIMEOUT_SEC,
        crash_window_sec: int = CRASH_LOOP_WINDOW_SEC,
        crash_max_burst: int = CRASH_LOOP_MAX_BURST,
    ) -> None:
        self.store = store
        self.alert_manager = alert_manager
        self.expected_risk_config = expected_risk_config
        self.stall_timeout_sec = stall_timeout_sec
        self.crash_window_sec = crash_window_sec
        self.crash_max_burst = crash_max_burst
        self._last_observed_tick: dict[str, tuple[int, int]] = {}  # service -> (loop_tick_seq, observed_mono_ns)

    def write_heartbeat(self, hb: ServiceHeartbeat) -> None:
        self.store.upsert_heartbeat(
            service_name=hb.service_name,
            pid=hb.pid,
            boot_id=hb.boot_id,
            monotonic_ns=hb.monotonic_ns,
            wall_time_utc_iso=hb.wall_time_utc.isoformat(),
            loop_tick_seq=hb.loop_tick_seq,
            last_work_time_utc_iso=hb.last_work_time_utc.isoformat(),
            rss_bytes=hb.rss_bytes,
            service_state=hb.service_state,
        )

    def check_service_health(
        self,
        service_name: str,
        current_monotonic_ns: int | None = None,
        now_utc: datetime | None = None,
    ) -> tuple[bool, str]:
        """Check whether a service is healthy or stalled (process alive but loop_tick_seq frozen >= 30s)."""
        ref_mono = current_monotonic_ns if current_monotonic_ns is not None else monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        row = self.store.get_heartbeat(service_name)
        if row is None:
            self._freeze_on_guardian_fault(
                reason=f"MISSING_HEARTBEAT:{service_name}",
                dedup_key=f"F02-MISSING-HB-{service_name}",
                now_utc=ref_now,
            )
            return False, "MISSING_HEARTBEAT"

        hb_mono = int(row["monotonic_ns"])
        hb_tick = int(row["loop_tick_seq"])
        age_sec = (ref_mono - hb_mono) / 1_000_000_000.0

        # Track progression of loop_tick_seq
        prev = self._last_observed_tick.get(service_name)
        if prev is None or hb_tick > prev[0]:
            self._last_observed_tick[service_name] = (hb_tick, hb_mono)
            effective_stall_sec = age_sec
        else:
            _, first_stuck_mono = prev
            effective_stall_sec = (ref_mono - first_stuck_mono) / 1_000_000_000.0

        if age_sec >= self.stall_timeout_sec or effective_stall_sec >= self.stall_timeout_sec:
            self._freeze_on_guardian_fault(
                reason=f"STALL_DETECTED:{service_name}:stall_sec={max(age_sec, effective_stall_sec):.1f}s:tick={hb_tick}",
                dedup_key=f"F02-STALL-{service_name}",
                now_utc=ref_now,
            )
            return False, "STALL_DETECTED"

        return True, "HEALTHY"

    def register_service_crash_and_should_restart(
        self,
        service_name: str,
        reason: str,
        now_utc: datetime | None = None,
    ) -> bool:
        """Record a service crash (e.g. kill -9 / unhandled exception) and enforce Crash-Loop Circuit Breaker.

        Returns True if the service is allowed to auto-restart (entering BOOT -> SYNCING),
        or False if it hit `crash_max_burst` within `crash_window_sec` (circuit breaker tripped).
        """
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        self.store.record_crash(service_name, ref_now.isoformat(), reason)

        # Force state to BOOT -> SYNCING or ENTRY_FROZEN immediately upon crash
        current_state = self.store.get_trading_state()
        if current_state != TradingState.SECURITY_LOCKDOWN:
            self._transition_to(
                TradingState.ENTRY_FROZEN,
                reason=f"PROCESS_CRASH:{service_name}:{reason}",
                now_utc=ref_now,
            )

        window_start_iso = (ref_now - timedelta(seconds=self.crash_window_sec)).isoformat()
        recent_crashes = self.store.count_recent_crashes(service_name, window_start_iso)

        if recent_crashes >= self.crash_max_burst:
            self.alert_manager.emit_alert(
                severity=AlertSeverity.CRITICAL,
                source="F-02",
                dedup_key=f"F02-CRASH-LOOP-{service_name}",
                actions_taken=("CIRCUIT_BREAKER_TRIPPED", "ENTRY_FROZEN", "STOP_AUTO_RESTART"),
                details={
                    "service_name": service_name,
                    "recent_crashes": recent_crashes,
                    "window_seconds": self.crash_window_sec,
                    "last_reason": reason,
                },
                now_utc=ref_now,
            )
            return False

        self.alert_manager.emit_alert(
            severity=AlertSeverity.HIGH,
            source="F-02",
            dedup_key=f"F02-SERVICE-RESTART-{service_name}",
            actions_taken=("AUTO_RESTART_TO_SYNCING",),
            details={
                "service_name": service_name,
                "recent_crashes": recent_crashes,
                "reason": reason,
            },
            now_utc=ref_now,
        )
        return True

    def initiate_boot_syncing(self, reason: str = "PROCESS_STARTUP", now_utc: datetime | None = None) -> TradingState:
        """Enforce mandatory startup invariant: every boot/restart starts in BOOT and moves to SYNCING."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        current = self.store.get_trading_state()
        if current == TradingState.SECURITY_LOCKDOWN:
            # Never auto-clear SECURITY_LOCKDOWN on simple restart! (§10)
            return TradingState.SECURITY_LOCKDOWN

        self.store.set_trading_state(TradingState.BOOT)
        self._transition_to(TradingState.SYNCING, reason=f"BOOT_TO_SYNCING:{reason}", now_utc=ref_now)
        return self.store.get_trading_state()

    def execute_syncing_reconciliation(
        self,
        r01_active_risk_config: RiskConfig,
        r01_heartbeat_age_sec: Decimal,
        s01_lockdown_active: bool,
        disk_free_pct: Decimal,
        ntp_offset_ms: Decimal,
        f01_data_quality: DataQuality,
        f01_event_age_sec: Decimal,
        active_filters: SymbolFilters,
        exchange_state: SimulatedExchangeState,
        now_utc: datetime | None = None,
    ) -> RecoveryGateReport:
        """Evaluate all 5 mandatory recovery gates before allowing SYNCING -> READY_NO_POSITION / PROTECTED."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        current_state = self.store.get_trading_state()
        if current_state == TradingState.SECURITY_LOCKDOWN or s01_lockdown_active:
            if current_state != TradingState.SECURITY_LOCKDOWN:
                self._transition_to(TradingState.SECURITY_LOCKDOWN, "S01_LOCKDOWN_ACTIVE", ref_now)
            return RecoveryGateReport(
                passed=False,
                target_state=TradingState.SECURITY_LOCKDOWN,
                failed_gates=("GATE2_SECURITY_LOCKDOWN",),
            )

        if current_state != TradingState.SYNCING:
            self._transition_to(TradingState.SYNCING, "ENTER_SYNCING_FOR_RECONCILIATION", ref_now)

        failed: list[str] = []

        # Gate 1: R-01 independent risk daemon alive and RiskConfig hash matches
        if r01_heartbeat_age_sec > Decimal("10"):
            failed.append("GATE1_R01_HEARTBEAT_STALE")
        if r01_active_risk_config.canonical_sha256() != self.expected_risk_config.canonical_sha256():
            failed.append("GATE1_RISK_CONFIG_SHA256_MISMATCH")

        # Gate 2: S-01 host security, disk watermark (>= 15%), and NTP clock offset (< 100ms)
        if disk_free_pct < Decimal("0.15"):
            failed.append("GATE2_DISK_WATERMARK_CRITICAL")
        if abs(ntp_offset_ms) >= Decimal("100"):
            failed.append("GATE2_CLOCK_DRIFT_EXCEEDED")

        # Gate 3: F-01 market stream connectivity and data quality
        if not exchange_state.reachable or f01_data_quality != DataQuality.OK or f01_event_age_sec > Decimal("5"):
            failed.append("GATE3_MARKET_STREAM_UNHEALTHY")

        # Gate 4: Dynamic exchangeInfo filters freshness
        try:
            active_filters.assert_fresh_for_execution(now_utc=ref_now, max_age_seconds=3600)
        except Exception:
            failed.append("GATE4_STALE_OR_STATIC_EXCHANGE_FILTERS")

        if failed:
            self._transition_to(
                TradingState.ENTRY_FROZEN,
                reason=f"SYNCING_GATES_FAILED:{','.join(failed)}",
                now_utc=ref_now,
            )
            self.alert_manager.emit_alert(
                severity=AlertSeverity.CRITICAL,
                source="F-02",
                dedup_key="F02-SYNCING-FAILED",
                actions_taken=("ENTRY_FROZEN",),
                details={"failed_gates": failed},
                now_utc=ref_now,
            )
            return RecoveryGateReport(
                passed=False,
                target_state=TradingState.ENTRY_FROZEN,
                failed_gates=tuple(failed),
            )

        # Gate 5: Position & protection order reconciliation
        if exchange_state.actual_sui_qty == Decimal("0"):
            self._transition_to(
                TradingState.READY_NO_POSITION,
                reason="SYNCING_PASSED_FLAT",
                now_utc=ref_now,
            )
            return RecoveryGateReport(
                passed=True,
                target_state=TradingState.READY_NO_POSITION,
                failed_gates=(),
            )

        alloc = compute_post_fill_sellable_position(
            gross_filled_qty=exchange_state.actual_sui_qty,
            avg_fill_price=exchange_state.avg_fill_price,
            fee_asset=exchange_state.fee_asset,
            fee_amount=exchange_state.fee_amount,
            stop_loss_pct=self.expected_risk_config.stop_loss_trigger_pct,
            filters=active_filters,
        )

        if not alloc.is_protectable:
            self._transition_to(
                TradingState.ENTRY_FROZEN,
                reason=f"UNPROTECTABLE_POSITION:{alloc.unprotectable_reason}",
                now_utc=ref_now,
            )
            self.alert_manager.emit_alert(
                severity=AlertSeverity.CRITICAL,
                source="F-02",
                dedup_key="F02-PROTECTION-DEGRADED",
                actions_taken=("ENTRY_FROZEN", "PROTECTION_DEGRADED"),
                details={"reason": alloc.unprotectable_reason, "sellable_qty": str(alloc.sellable_qty)},
                now_utc=ref_now,
            )
            return RecoveryGateReport(
                passed=False,
                target_state=TradingState.ENTRY_FROZEN,
                failed_gates=("GATE5_UNPROTECTABLE_POSITION",),
            )

        # If position exists and is already fully protected on the exchange
        if exchange_state.active_protection_qty == alloc.sellable_qty and exchange_state.protection_order_ids:
            ledger = PositionLedger(
                position_id="POS-RECONCILED",
                symbol="SUIUSDT",
                actual_qty=alloc.net_base_qty,
                avg_fill_price=exchange_state.avg_fill_price,
                fees=exchange_state.fee_amount,
                reserved_qty=alloc.sellable_qty,
                protection_orders=exchange_state.protection_order_ids,
                exit_strategy_version="C1-v1",
                risk_config_version=self.expected_risk_config.version,
            )
            self._transition_to(TradingState.PROTECTED, "SYNCING_EXISTING_PROTECTED_POSITION", ref_now)
            return RecoveryGateReport(
                passed=True,
                target_state=TradingState.PROTECTED,
                failed_gates=(),
                reconciled_ledger=ledger,
            )

        # Unprotected position discovered on restart -> enter PROTECTION_PENDING first! (QA-05)
        self._transition_to(
            TradingState.PROTECTION_PENDING,
            reason="UNPROTECTED_POSITION_DISCOVERED_ON_SYNC",
            now_utc=ref_now,
        )
        if exchange_state.can_place_protection_order:
            ledger = PositionLedger(
                position_id="POS-RESTORED",
                symbol="SUIUSDT",
                actual_qty=alloc.net_base_qty,
                avg_fill_price=exchange_state.avg_fill_price,
                fees=exchange_state.fee_amount,
                reserved_qty=alloc.sellable_qty,
                protection_orders=("RESTORED-STOP-001",),
                exit_strategy_version="C1-v1",
                risk_config_version=self.expected_risk_config.version,
            )
            self._transition_to(
                TradingState.PROTECTED,
                reason="PROTECTION_RESTORED_AFTER_RESTART",
                now_utc=ref_now,
            )
            return RecoveryGateReport(
                passed=True,
                target_state=TradingState.PROTECTED,
                failed_gates=(),
                reconciled_ledger=ledger,
            )

        # Failed to place protection order -> ENTRY_FROZEN + CRITICAL alert
        self._transition_to(
            TradingState.ENTRY_FROZEN,
            reason="PROTECTION_RESTORE_FAILED",
            now_utc=ref_now,
        )
        self.alert_manager.emit_alert(
            severity=AlertSeverity.CRITICAL,
            source="F-02",
            dedup_key="F02-PROTECTION-RESTORE-FAILED",
            actions_taken=("ENTRY_FROZEN", "PROTECTION_DEGRADED"),
            details={"net_base_qty": str(alloc.net_base_qty)},
            now_utc=ref_now,
        )
        return RecoveryGateReport(
            passed=False,
            target_state=TradingState.ENTRY_FROZEN,
            failed_gates=("GATE5_PROTECTION_RESTORE_FAILED",),
        )

    def _freeze_on_guardian_fault(self, reason: str, dedup_key: str, now_utc: datetime) -> None:
        current = self.store.get_trading_state()
        if current != TradingState.SECURITY_LOCKDOWN:
            self._transition_to(TradingState.ENTRY_FROZEN, reason=reason, now_utc=now_utc)
        self.alert_manager.emit_alert(
            severity=AlertSeverity.CRITICAL,
            source="F-02",
            dedup_key=dedup_key,
            actions_taken=("ENTRY_FROZEN",),
            details={"reason": reason},
            now_utc=now_utc,
        )

    def _transition_to(self, to_state: TradingState, reason: str, now_utc: datetime) -> None:
        from_state = self.store.get_trading_state()
        if from_state == to_state:
            return
        assert_valid_transition(from_state, to_state)
        rec = StateTransitionRecord(
            transition_id=f"TR-{uuid.uuid4().hex[:12]}",
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            occurred_at_utc=now_utc,
        )
        self.store.record_transition(rec)
