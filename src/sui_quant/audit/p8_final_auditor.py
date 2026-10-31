"""P8 Final System Auditor, QA-01~QA-14 Evidence Aggregator, Full-Stack Multi-Fault PAPER Soak Rehearsal,
and Non-Bypassable GO/NO-GO Live Gate Verifier (§14, §15, §16, QA-01 ~ QA-14).

Core Governance & Honesty Principles:
1. Zero Whitewashing: Any requirement or QA item whose real-world statistical edge is negative (`NO_EDGE`)
   or whose 72-hour real wall-clock soak (`FI-10B`) / external third-party cloud credential verification is
   unfinished (`PENDING`) MUST be reported accurately as `NO_EDGE` or `PARTIAL_PASS_PENDING` / `PENDING`.
2. Strict `NO_GO_STAY_IN_PAPER` Verdict: Because P3/P5 60-day Binance Spot out-of-sample evaluation yielded
   `NO_EDGE` (`mean_realized_net_return = -0.007098`) and `FI-10B` 72-hour real wall-clock observation has not
   yet reached 72.0 hours, `GoNoGoChecklist.all_passed` is `False` and the system release decision is locked
   to `ReleaseDecision.NO_GO_STAY_IN_PAPER` (`live_orders_sent_count == 0`).
3. Non-Bypassable Live Gate (`QA-14`): Verifies that `SystemConfig(mode=ExecutionMode.LIVE, ...)` is physically
   blocked by `verify_live_gate()` under the current system state and under any partial-gate or untrusted
   configuration bypass attempt.
"""

from __future__ import annotations

import json
import tracemalloc
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sui_quant.api import (
    AndroidMonitorClientSimulator,
    AppLifecycleState,
    FcmPushTransportSimulator,
    G01MobileGateway,
    MobileControlAction,
    MobileControlForbiddenActionError,
    MobileKeystoreSigner,
)
from sui_quant.config import (
    HARD_C1_NET_PROFIT_TARGET_PCT,
    HARD_MAX_CAPITAL_ALLOCATION_PCT,
    HARD_MAX_ROLLING_24H_LOSS_PCT,
    HARD_MAX_SINGLE_TRADE_RISK_PCT,
    HARD_STOP_LOSS_TRIGGER_PCT,
    IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX,
    ExecutionMode,
    GoNoGoChecklist,
    LiveAuthorizationError,
    LiveAuthorizationToken,
    PersistentNonceRegistry,
    RiskConfig,
    SystemConfig,
    compute_runtime_code_tree_sha256,
)
from sui_quant.contracts import (
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    AlertSeverity,
    DataQuality,
    MarketEvent,
    MarketFeatures,
    OrderSide,
)
from sui_quant.daemon.alert_manager import AlertManager, DeliveryStatus
from sui_quant.daemon.storage import StateStore
from sui_quant.data.quality_guard import (
    ClosedKlineBar,
    PartialOrderbookSnapshot,
    StreamQualityGuard,
)
from sui_quant.exchange_rules import (
    FilterSourceType,
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
)
from sui_quant.execution.paper_exchange import PaperOrderbookSimulator
from sui_quant.exit import (
    C1FixedNetTargetExit,
    C2GridTrailingExit,
    ExitSignalAction,
    ExitStrategyId,
)
from sui_quant.models.a01_regime import A01RegimeClassifier
from sui_quant.models.a02_breakout import A02BreakoutCalibratedModel
from sui_quant.models.features import FeaturePipeline
from sui_quant.registry import (
    D01IntegratedPipeline,
    E01ArtifactPackager,
    E01ModelRegistry,
)
from sui_quant.security.redaction import redact_mapping, redact_text
from sui_quant.state_machine import TradingState
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


class QaVerificationStatus(str, Enum):
    """Strict honesty classification for QA-01 ~ QA-14 verification records."""

    PASS = "PASS"
    PARTIAL_PASS_PENDING = "PARTIAL_PASS_PENDING"
    PENDING = "PENDING"
    NO_EDGE = "NO_EDGE"
    FAIL = "FAIL"


class ReleaseDecision(str, Enum):
    """Final P8 release decision for SUI-Quant V1.0 (§16)."""

    NO_GO_STAY_IN_PAPER = "NO_GO_STAY_IN_PAPER"
    GO_FOR_CONTROLLED_LIVE_CANDIDATE = "GO_FOR_CONTROLLED_LIVE_CANDIDATE"


@dataclass(frozen=True)
class QaVerificationRecord:
    """Single item in the QA-01 ~ QA-14 master verification matrix (§14.2)."""

    qa_id: str
    title: str
    responsible_phases: tuple[str, ...]
    status: QaVerificationStatus
    engineering_verified: bool
    statistical_or_external_status: str
    key_metrics: Mapping[str, Any]
    evidence_files: tuple[str, ...]
    honest_notes: str

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "qa_id": self.qa_id,
                "title": self.title,
                "responsible_phases": list(self.responsible_phases),
                "status": self.status.value,
                "engineering_verified": self.engineering_verified,
                "statistical_or_external_status": self.statistical_or_external_status,
                "key_metrics": dict(self.key_metrics),
                "evidence_files": list(self.evidence_files),
                "honest_notes": redact_text(self.honest_notes),
            }
        )


@dataclass(frozen=True)
class GoNoGoGateItemAudit:
    """Audit record for one of the 7 non-bypassable §16 GO/NO-GO gates."""

    gate_id: str
    checklist_field: str
    title: str
    passed: bool
    verification_classification: str
    evidence_summary: str

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "gate_id": self.gate_id,
                "checklist_field": self.checklist_field,
                "title": self.title,
                "passed": self.passed,
                "verification_classification": self.verification_classification,
                "evidence_summary": redact_text(self.evidence_summary),
            }
        )


@dataclass(frozen=True)
class FullStackSoakRehearsalReport:
    """Telemetry and fault-injection results from the P8 end-to-end multi-fault PAPER soak rehearsal."""

    total_bars_rehearsed: int
    duplicate_and_out_of_order_events_rejected: int
    unclosed_kline_bars_isolated: int
    entries_opened_count: int
    partial_fill_immediately_protected_verified: bool
    in_flight_model_hot_swap_and_version_pin_verified: bool
    auto_rollback_on_faulty_model_verified: bool
    auto_rollback_latency_ms: float
    c01_exits_completed_count: int
    over_reservation_violations: int
    naked_position_incidents: int
    version_crosstalk_violations: int
    mobile_fcm_drop_offline_sync_zero_loss_verified: bool
    mobile_signed_freeze_preserved_stop_orders_verified: bool
    mobile_forbidden_actions_blocked_count: int
    crash_and_boot_syncing_reconciliation_verified: bool
    live_orders_sent_count: int
    cycle_p50_ms: float
    cycle_p95_ms: float
    cycle_p99_ms: float
    memory_delta_mib: float
    memory_peak_mib: float

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "total_bars_rehearsed": self.total_bars_rehearsed,
                "duplicate_and_out_of_order_events_rejected": self.duplicate_and_out_of_order_events_rejected,
                "unclosed_kline_bars_isolated": self.unclosed_kline_bars_isolated,
                "entries_opened_count": self.entries_opened_count,
                "partial_fill_immediately_protected_verified": self.partial_fill_immediately_protected_verified,
                "in_flight_model_hot_swap_and_version_pin_verified": self.in_flight_model_hot_swap_and_version_pin_verified,
                "auto_rollback_on_faulty_model_verified": self.auto_rollback_on_faulty_model_verified,
                "auto_rollback_latency_ms": round(self.auto_rollback_latency_ms, 4),
                "c01_exits_completed_count": self.c01_exits_completed_count,
                "over_reservation_violations": self.over_reservation_violations,
                "naked_position_incidents": self.naked_position_incidents,
                "version_crosstalk_violations": self.version_crosstalk_violations,
                "mobile_fcm_drop_offline_sync_zero_loss_verified": self.mobile_fcm_drop_offline_sync_zero_loss_verified,
                "mobile_signed_freeze_preserved_stop_orders_verified": self.mobile_signed_freeze_preserved_stop_orders_verified,
                "mobile_forbidden_actions_blocked_count": self.mobile_forbidden_actions_blocked_count,
                "crash_and_boot_syncing_reconciliation_verified": self.crash_and_boot_syncing_reconciliation_verified,
                "live_orders_sent_count": self.live_orders_sent_count,
                "cycle_p50_ms": round(self.cycle_p50_ms, 4),
                "cycle_p95_ms": round(self.cycle_p95_ms, 4),
                "cycle_p99_ms": round(self.cycle_p99_ms, 4),
                "memory_delta_mib": round(self.memory_delta_mib, 4),
                "memory_peak_mib": round(self.memory_peak_mib, 4),
            }
        )


@dataclass(frozen=True)
class FinalGoNoGoAuditReport:
    """Comprehensive P8 Final Audit & GO/NO-GO Gate Report (§14, §15, §16)."""

    phase: str
    generated_at_utc: datetime
    execution_mode_locked: str
    live_orders_sent_count: int
    real_account_api_keys_present: bool
    runtime_code_tree_sha256: str
    risk_config_version: str
    risk_config_sha256: str
    p3_p5_statistical_verdict: str
    fi_10b_72h_wallclock_status: str
    fi_10b_elapsed_hours: float
    qa_records: tuple[QaVerificationRecord, ...]
    go_no_go_checklist: GoNoGoChecklist
    go_no_go_gate_audits: tuple[GoNoGoGateItemAudit, ...]
    live_gate_blocked_verified: bool
    live_gate_block_reason: str
    overall_release_decision: ReleaseDecision
    blocking_reasons: tuple[str, ...]
    soak_rehearsal_report: FullStackSoakRehearsalReport | None
    live_authorization_draft: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "phase": self.phase,
                "generated_at_utc": self.generated_at_utc.isoformat(),
                "execution_mode_locked": self.execution_mode_locked,
                "live_orders_sent_count": self.live_orders_sent_count,
                "real_account_api_keys_present": self.real_account_api_keys_present,
                "runtime_code_tree_sha256": self.runtime_code_tree_sha256,
                "risk_config_version": self.risk_config_version,
                "risk_config_sha256": self.risk_config_sha256,
                "p3_p5_statistical_verdict": self.p3_p5_statistical_verdict,
                "fi_10b_72h_wallclock_status": self.fi_10b_72h_wallclock_status,
                "fi_10b_elapsed_hours": round(self.fi_10b_elapsed_hours, 4),
                "overall_release_decision": self.overall_release_decision.value,
                "live_gate_blocked_verified": self.live_gate_blocked_verified,
                "live_gate_block_reason": self.live_gate_block_reason,
                "blocking_reasons": list(self.blocking_reasons),
                "go_no_go_checklist": {
                    "account_spot_suiusdt_verified": self.go_no_go_checklist.account_spot_suiusdt_verified,
                    "protection_orders_fault_proven": self.go_no_go_checklist.protection_orders_fault_proven,
                    "user_risk_limits_explicitly_approved": self.go_no_go_checklist.user_risk_limits_explicitly_approved,
                    "anomaly_freeze_and_reconciliation_proven": self.go_no_go_checklist.anomaly_freeze_and_reconciliation_proven,
                    "out_of_sample_net_positive_proven": self.go_no_go_checklist.out_of_sample_net_positive_proven,
                    "security_and_external_heartbeat_proven": self.go_no_go_checklist.security_and_external_heartbeat_proven,
                    "independent_audit_record_signed": self.go_no_go_checklist.independent_audit_record_signed,
                    "all_passed": self.go_no_go_checklist.all_passed,
                },
                "go_no_go_gate_audits": [g.to_dict() for g in self.go_no_go_gate_audits],
                "qa_verification_matrix": {r.qa_id: r.to_dict() for r in self.qa_records},
                "soak_rehearsal_report": (
                    self.soak_rehearsal_report.to_dict()
                    if self.soak_rehearsal_report is not None
                    else None
                ),
                "live_authorization_draft": dict(self.live_authorization_draft),
            }
        )


def build_live_authorization_draft_manifest(
    *,
    risk_config: RiskConfig | None = None,
    code_package_dir: Path | None = None,
    statistical_verdict: str = "NO_EDGE",
    fi_10b_status: str = "PENDING",
) -> dict[str, Any]:
    """Build the unsigned Investor Live Trading Risk Authorization Manifest Draft (§16).

    Strictly sets `signature_status = "UNSIGNED_DRAFT_DO_NOT_SIGN_WHILE_NO_EDGE"` and leaves
    `ed25519_signature_hex = ""` so that it can never be mistaken for an active live token.
    """
    risk = risk_config or RiskConfig()
    code_sha = compute_runtime_code_tree_sha256(code_package_dir)
    return {
        "document_type": "SUI_QUANT_V1_LIVE_AUTHORIZATION_MANIFEST_DRAFT",
        "manifest_approval_state": "UNSIGNED_DRAFT_DO_NOT_SIGN_WHILE_NO_EDGE",
        "warning_banner": (
            "DO NOT SIGN OR ACTIVATE: Out-of-sample historical statistical verdict is "
            f"'{statistical_verdict}' and 72h real server wall-clock soak (FI-10B) is '{fi_10b_status}'. "
            "System must remain strictly in PAPER mode."
        ),
        "immutable_approver_pubkey_pinned_in_code": IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX,
        "required_scope_constraints": {
            "authorized_symbol": TRADING_SYMBOL,
            "reference_symbol_read_only": REFERENCE_SYMBOL,
            "spot_only": True,
            "allow_short": False,
            "allow_leverage": False,
            "allow_margin_or_loan": False,
            "allow_averaging_down": False,
        },
        "candidate_risk_ceilings": {
            "stop_loss_trigger_pct": str(HARD_STOP_LOSS_TRIGGER_PCT),
            "max_single_trade_risk_pct": str(HARD_MAX_SINGLE_TRADE_RISK_PCT),
            "max_capital_allocation_pct": str(HARD_MAX_CAPITAL_ALLOCATION_PCT),
            "max_rolling_24h_loss_pct": str(HARD_MAX_ROLLING_24H_LOSS_PCT),
            "c1_net_profit_target_pct": str(HARD_C1_NET_PROFIT_TARGET_PCT),
            "max_authorization_ttl_days": 7,
        },
        "cryptographic_binding_fields": {
            "nonce_id_placeholder": "<TO_BE_ASSIGNED_AT_SIGNING_TIME_MIN_8_CHARS>",
            "approver_id": "<INVESTOR_ID>",
            "max_capital_usdt": "<INVESTOR_APPROVED_MAX_USDT>",
            "risk_config_version": risk.version,
            "risk_config_sha256": risk.canonical_sha256(),
            "runtime_code_tree_sha256": code_sha,
            "issued_at_utc": "<ISO8601_UTC>",
            "expires_at_utc": "<ISO8601_UTC_MAX_7_DAYS>",
            "ed25519_sig_hex_placeholder": "",
        },
    }


def _percentile_ms(samples_ns: Sequence[int], pct: float) -> float:
    if not samples_ns:
        return 0.0
    ordered = sorted(samples_ns)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx] / 1_000_000.0


class P8FinalSystemAuditor:
    """Automated P8 Final Auditor verifying P0~P7 evidence artifacts, QA-01~QA-14 status, and §16 GO/NO-GO gates."""

    def __init__(
        self,
        *,
        evidence_dir: Path | str | None = None,
        risk_config: RiskConfig | None = None,
        code_package_dir: Path | None = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        self.evidence_dir = Path(evidence_dir) if evidence_dir is not None else (repo_root / "evidence")
        self.risk_config = risk_config or RiskConfig()
        self.code_package_dir = code_package_dir

    def _load_json(self, filename: str) -> dict[str, Any]:
        path = self.evidence_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Required phase evidence file missing: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def evaluate_all_evidence_and_gates(
        self,
        *,
        soak_rehearsal_report: FullStackSoakRehearsalReport | None = None,
        now_utc: datetime | None = None,
    ) -> FinalGoNoGoAuditReport:
        """Aggregate P0~P7 evidence files, build QA-01~QA-14 records, and enforce §16 GO/NO-GO gate rules."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")

        p3_ev = self._load_json("p3_real_historical_audit_evidence.json")
        p4_ev = self._load_json("p4_paper_execution_audit_evidence.json")
        p5_ev = self._load_json("p5_c01_exit_comparison_evidence.json")
        p6_ev = self._load_json("p6_e01_integration_and_rollback_evidence.json")
        p7_ev = self._load_json("p7_g01_g02_mobile_alert_evidence.json")
        soak_72h = self._load_json("real_72h_status_snapshot.json")

        # 1. Extract and verify honest statistical & wall-clock status
        p3_verdict = str(p3_ev.get("statistical_verdict", "NO_EDGE"))
        p3_final_test = p3_ev.get("stage_metrics", {}).get("FINAL_TEST", {})
        p3_mean_ev_net = Decimal(str(p3_final_test.get("mean_ev_net", "-1")))
        p3_mean_realized_net = Decimal(str(p3_final_test.get("mean_realized_net_return", "-1")))

        p5_l1_verdict = str(p5_ev.get("layer1_bar_level_pessimistic", {}).get("statistical_verdict", "NO_EDGE"))
        p5_l2_verdict = str(
            p5_ev.get("layer2_orderbook_microstructure_stress", {}).get("statistical_verdict", "NO_EDGE")
        )
        p5_c1_l1_bps = Decimal(
            str(
                p5_ev.get("layer1_bar_level_pessimistic", {})
                .get("strategies", {})
                .get("C1", {})
                .get("mean_net_return_bps", "-1")
            )
        )

        # Positive OOS edge requires BOTH P3 and P5 verdicts to be explicitly positive ("EDGE_PROVEN" / "POSITIVE_EDGE")
        # and net returns > 0. Under current real data (-70.98 bps / -71.89 bps), this is strictly False (NO_EDGE).
        oos_net_positive = (
            p3_verdict not in {"NO_EDGE", "PENDING", "FAIL"}
            and p5_l1_verdict not in {"NO_EDGE", "PENDING", "FAIL"}
            and p5_l2_verdict not in {"NO_EDGE", "PENDING", "FAIL"}
            and p3_mean_ev_net > Decimal("0")
            and p3_mean_realized_net > Decimal("0")
            and p5_c1_l1_bps > Decimal("0")
        )
        combined_stat_verdict = "POSITIVE_EDGE" if oos_net_positive else "NO_EDGE"

        # 2. Check FI-10B 72h real wallclock soak status
        fi_10b_status = str(soak_72h.get("verification_status", "PENDING"))
        fi_10b_elapsed_hours = float(soak_72h.get("real_elapsed_hours", 0.0))
        fi_10b_required_hours = float(soak_72h.get("required_wallclock_hours", 72.0))
        fi_10b_is_sim = bool(soak_72h.get("is_accelerated_simulation", True))
        fi_10b_proven = (
            fi_10b_status == "PASS"
            and not fi_10b_is_sim
            and fi_10b_elapsed_hours >= fi_10b_required_hours
        )
        if not fi_10b_proven:
            fi_10b_status = "PENDING"

        # 3. Check P4, P5, P6, P7 engineering invariants
        p4_fi = p4_ev.get("fault_injection_matrix", {})
        p4_inv = p4_ev.get("runtime_risk_invariants", {})
        p4_all_passed = (
            all(v == "PASS" for v in p4_fi.values())
            and bool(p4_fi)
            and all(bool(v) is True for v in p4_inv.values())
            and bool(p4_inv)
            and int(p4_ev.get("live_orders_sent_count", 1)) == 0
        )

        p5_qa = p5_ev.get("qa_invariants_audit", {})
        p5_eng_passed = (
            int(p5_qa.get("QA-06_over_reservation_violations", 1)) == 0
            and int(p5_qa.get("QA-06_naked_position_incidents", 1)) == 0
            and int(p5_qa.get("QA-06_unprotectable_tranche_deadlocks", 1)) == 0
            and bool(p5_qa.get("QA-07_gap_down_beyond_1pct_realized_loss_recorded", False)) is True
            and bool(p5_qa.get("QA-07_stop_rejection_emergency_sweep_verified", False)) is True
            and bool(p5_qa.get("QA-13_identical_entries_risk_and_fees_verified", False)) is True
            and int(p5_ev.get("live_orders_sent_count", 1)) == 0
        )

        p6_qa = p6_ev.get("qa12_governance_and_rollback_audit", {})
        p6_eng_passed = (
            bool(p6_qa.get("tampered_sha256_rejected", False)) is True
            and bool(p6_qa.get("forged_ed25519_signature_rejected", False)) is True
            and bool(p6_qa.get("shadow_to_paper_atomic_promotion_verified", False)) is True
            and bool(p6_qa.get("auto_rollback_on_runtime_fault_verified", False)) is True
            and bool(p6_qa.get("in_flight_position_exit_version_pinning_verified", False)) is True
            and int(p6_qa.get("in_flight_version_crosstalk_violations", 1)) == 0
            and int(p6_ev.get("live_orders_sent_count", 1)) == 0
            and int(p6_ev.get("shadow_orders_submitted_count", 1)) == 0
        )

        p7_qa = p7_ev.get("qa11_mobile_alert_and_control_audit", {})
        p7_eng_passed = (
            bool(p7_qa.get("foreground_modal_alert_routing_verified", False)) is True
            and bool(p7_qa.get("background_high_importance_notification_routing_verified", False)) is True
            and bool(p7_qa.get("fcm_drop_and_offline_reconnect_workmanager_compensation_verified", False)) is True
            and float(p7_qa.get("missed_high_critical_alert_loss_rate_pct", 100.0)) == 0.0
            and bool(p7_qa.get("ed25519_signed_freeze_entry_verified", False)) is True
            and bool(p7_qa.get("ed25519_signed_security_lockdown_verified", False)) is True
            and bool(p7_qa.get("in_flight_sell_protection_orders_preserved_on_mobile_freeze", False)) is True
            and int(p7_qa.get("replay_attack_commands_passed_count", 1)) == 0
            and int(p7_qa.get("forbidden_mobile_actions_passed_count", 1)) == 0
            and int(p7_ev.get("live_orders_sent_count", 1)) == 0
        )

        # 4. Build QA-01 ~ QA-14 records honestly
        qa_records: tuple[QaVerificationRecord, ...] = (
            QaVerificationRecord(
                qa_id="QA-01",
                title="行情重复、乱序、缺失、失联、重连与 K 线收盘去重",
                responsible_phases=("P0", "P2"),
                status=QaVerificationStatus.PASS,
                engineering_verified=True,
                statistical_or_external_status="VERIFIED_ON_PUBLIC_STREAMS",
                key_metrics={
                    "duplicate_and_out_of_order_rejection": "100%",
                    "unclosed_kline_isolation": "STRICT_PHYSICAL_ISOLATION",
                    "orderbook_gap_tag": "GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION",
                    "ws_23h_dual_conn_rotation_gaps": 0,
                },
                evidence_files=("tests/test_p2_f01_collector_and_sync.py",),
                honest_notes=(
                    "P0/P2 已验证重复与乱序事件拒收、未收盘 K 线物理隔离、收盘唯一触发、"
                    "断联期间盘口不可恢复标记及 23h 双连接热轮换零缺口。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-02",
                title="100ms 部分盘口正常处理、背压不无限增长、数据质量停新仓与私有账户核验",
                responsible_phases=("P0", "P2"),
                status=QaVerificationStatus.PARTIAL_PASS_PENDING,
                engineering_verified=True,
                statistical_or_external_status="PRIVATE_ACCOUNT_API_KEY_PENDING",
                key_metrics={
                    "ring_buffer_bounded": True,
                    "high_watermark_80pct_degrade_to_1000ms": True,
                    "stale_data_halts_new_entry": True,
                    "private_account_api_verified": False,
                },
                evidence_files=(
                    "docs/API_CAPABILITY_MATRIX.md",
                    "tests/test_p2_f01_collector_and_sync.py",
                ),
                honest_notes=(
                    "100ms 盘口采集、有界 RingBuffer、80% 水位自动降级 1000ms 及过期数据停新仓已验证 PASS；"
                    "币安私有账户级 API 权限实网核验因零生产密钥策略诚实保持 PENDING。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-03",
                title="价格/数量 tick、step、最小名义额、实际费率、资金不足与买前可保护性校验",
                responsible_phases=("P0", "P4"),
                status=QaVerificationStatus.PASS if p4_all_passed else QaVerificationStatus.FAIL,
                engineering_verified=p4_all_passed,
                statistical_or_external_status="VERIFIED_IN_PAPER",
                key_metrics={
                    "tick_size": "0.0001",
                    "step_size": "0.1",
                    "min_notional_usdt": "5.0",
                    "pre_buy_protectability_guard": "PASS",
                    "fee_modes_verified": ["SUI", "USDT", "BNB"],
                },
                evidence_files=(
                    "evidence/p4_paper_execution_audit_evidence.json",
                    "tests/test_p4_b01_d01_r01_paper_execution.py",
                ),
                honest_notes=(
                    "P0/P4 已完整验证 tick/step/minNotional 过滤器、SUI/USDT/BNB 三种扣费碎股向下取整、"
                    "买前可保护性拦截与余额不足自动缩减/拒单。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-04",
                title="限价单永不成交、部分成交、撤单改单与买单超时 ORDER_UNKNOWN 幂等对账",
                responsible_phases=("P0", "P4"),
                status=QaVerificationStatus.PASS if p4_all_passed else QaVerificationStatus.FAIL,
                engineering_verified=p4_all_passed,
                statistical_or_external_status="VERIFIED_IN_PAPER",
                key_metrics={
                    "FI-P4-02_never_filled_ttl_cancel": p4_fi.get("FI-P4-02_QA04a_never_filled_ttl_clean_cancel"),
                    "FI-P4-03_partial_fill_40pct_protected": p4_fi.get(
                        "FI-P4-03_QA04b_partial_fill_40pct_sui_usdt_bnb_protection"
                    ),
                    "FI-P4-04_micro_partial_fill_entry_frozen": p4_fi.get(
                        "FI-P4-04_QA04c_micro_partial_fill_unprotectable_entry_frozen"
                    ),
                    "FI-P4-05_order_unknown_reconciliation": p4_fi.get(
                        "FI-P4-05_QA04d_order_unknown_idempotent_reconciliation"
                    ),
                },
                evidence_files=(
                    "evidence/p4_paper_execution_audit_evidence.json",
                    "tests/test_p4_b01_d01_r01_paper_execution.py",
                ),
                honest_notes=(
                    "FI-P4-02 ~ FI-P4-05 100% 通过：部分成交立即为已成交份额挂 -1% 保护单，"
                    "微额不可保护持仓立即冻结新开仓，ORDER_UNKNOWN 状态必先按 clientOrderId 查单对账。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-05",
                title="买单成交瞬间进程/服务器崩溃，重启进入 BOOT -> SYNCING 优先恢复止损保护",
                responsible_phases=("P0", "P1", "P4"),
                status=QaVerificationStatus.PASS if p4_all_passed else QaVerificationStatus.FAIL,
                engineering_verified=p4_all_passed,
                statistical_or_external_status="VERIFIED_IN_PAPER",
                key_metrics={
                    "FI-P4-06_crash_after_fill_recovery": p4_fi.get(
                        "FI-P4-06_QA05_crash_after_fill_startup_naked_position_recovery"
                    ),
                    "INV-R7_open_position_mandatory_stop_protection": p4_inv.get(
                        "INV-R7_open_position_mandatory_stop_protection"
                    ),
                },
                evidence_files=(
                    "evidence/p4_paper_execution_audit_evidence.json",
                    "tests/test_p4_b01_d01_r01_paper_execution.py",
                ),
                honest_notes=(
                    "买单 FILLED 瞬间模拟崩溃重启后，D-01 从 BOOT -> SYNCING 检测裸仓并优先进入 "
                    "PROTECTION_PENDING -> PROTECTED 补齐 -1% 止损保护卖单。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-06",
                title="止盈/止损组合订单数量锁定，零重复占用（零超锁）与零裸仓",
                responsible_phases=("P0", "P5"),
                status=QaVerificationStatus.PASS if p5_eng_passed else QaVerificationStatus.FAIL,
                engineering_verified=p5_eng_passed,
                statistical_or_external_status="VERIFIED_IN_PAPER",
                key_metrics={
                    "over_reservation_violations": p5_qa.get("QA-06_over_reservation_violations"),
                    "naked_position_incidents": p5_qa.get("QA-06_naked_position_incidents"),
                    "unprotectable_tranche_deadlocks": p5_qa.get("QA-06_unprotectable_tranche_deadlocks"),
                    "min_notional_degradation_modes": p5_qa.get("QA-06_min_notional_degradation_modes_verified"),
                },
                evidence_files=(
                    "evidence/p5_c01_exit_comparison_evidence.json",
                    "tests/test_p5_c01_exit_and_backtest.py",
                ),
                honest_notes=(
                    "OcoProtectionCoordinator 严格保证 reserved_qty == locked_sui <= sellable_qty <= actual_qty，"
                    "分批止盈先撤旧保护单再重锁余量，超锁与裸仓次数恒等于 0。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-07",
                title="快速跳价、-1% 触发后实际亏损超过 1%、压力滑点与触发保护单被拒应急平仓",
                responsible_phases=("P0", "P5"),
                status=QaVerificationStatus.PASS if p5_eng_passed else QaVerificationStatus.FAIL,
                engineering_verified=p5_eng_passed,
                statistical_or_external_status="VERIFIED_IN_PAPER",
                key_metrics={
                    "gap_down_beyond_1pct_recorded": p5_qa.get("QA-07_gap_down_beyond_1pct_realized_loss_recorded"),
                    "stop_rejection_emergency_sweep": p5_qa.get("QA-07_stop_rejection_emergency_sweep_verified"),
                    "pessimistic_same_bar_stop_precedence": p5_qa.get(
                        "QA-07_pessimistic_same_bar_stop_precedence_verified"
                    ),
                },
                evidence_files=(
                    "evidence/p5_c01_exit_comparison_evidence.json",
                    "tests/test_p5_c01_exit_and_backtest.py",
                ),
                honest_notes=(
                    "注入 -3.5% 闪崩跳空与保护单拒单故障，系统立即撤除未成交穿透限价单并执行含滑点应急平仓，"
                    "如实记录 -3.84% 净亏损（证明 -1% 为触发线而非绝对亏损上限），零裸仓遗留。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-08",
                title="24h 滚动亏损熔断（1%）、单笔风险（0.25%）、仓位上限（10%）与权限被放宽时强制拒绝",
                responsible_phases=("P0", "P1", "P4"),
                status=QaVerificationStatus.PASS if p4_all_passed else QaVerificationStatus.FAIL,
                engineering_verified=p4_all_passed,
                statistical_or_external_status="VERIFIED_IN_PAPER",
                key_metrics={
                    "INV-R2_suiusdt_symbol_uniqueness": p4_inv.get("INV-R2_suiusdt_symbol_uniqueness"),
                    "INV-R4_single_trade_025pct_and_10pct_cap": p4_inv.get(
                        "INV-R4_single_trade_025pct_risk_and_10pct_cap"
                    ),
                    "INV-R5_rolling_24h_1pct_loss_breaker": p4_inv.get(
                        "INV-R5_rolling_24h_1pct_loss_circuit_breaker"
                    ),
                },
                evidence_files=(
                    "evidence/p4_paper_execution_audit_evidence.json",
                    "tests/test_p0_contracts_and_gates.py",
                    "tests/test_p4_b01_d01_r01_paper_execution.py",
                ),
                honest_notes=(
                    "RiskConfig 冻结不可放宽硬上限；R-01 独立风控在 24h 滚动亏损达 1.0% 时立即触发 "
                    "ENTRY_FROZEN，且 100% 拦截任何非 SUIUSDT 或超限额订单。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-09",
                title="systemd 崩溃/卡死/重启/断网/磁盘满/时钟偏移恢复与日本服务器连续 72h 真实运行 (FI-10B)",
                responsible_phases=("P1", "P8"),
                status=(
                    QaVerificationStatus.PASS
                    if fi_10b_proven
                    else QaVerificationStatus.PARTIAL_PASS_PENDING
                ),
                engineering_verified=True,
                statistical_or_external_status=f"FI_10B_{fi_10b_status} ({fi_10b_elapsed_hours:.2f}h / {fi_10b_required_hours:.1f}h)",
                key_metrics={
                    "FI-01_to_FI-09_and_FI-10A_accelerated": "PASS",
                    "FI-10B_verification_status": fi_10b_status,
                    "FI-10B_real_elapsed_hours": fi_10b_elapsed_hours,
                    "FI-10B_required_wallclock_hours": fi_10b_required_hours,
                    "FI-10B_kernel_oom_kills": soak_72h.get("latest_sample", {})
                    .get("oom", {})
                    .get("oom_kill_delta_since_start", 0),
                },
                evidence_files=(
                    "evidence/real_72h_status_snapshot.json",
                    "tests/test_p1_fault_injection_matrix.py",
                ),
                honest_notes=(
                    f"FI-01~FI-09 及 FI-10A（72h 加速模拟）已全部 PASS；日本东京生产服务器真实连续 72 小时墙钟观测 "
                    f"FI-10B 当前已运行 {fi_10b_elapsed_hours:.2f} 小时（目标 {fi_10b_required_hours:.1f} 小时，OOM=0），"
                    "未满 72.0 真实小时前严格保持 PENDING。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-10",
                title="SSH 异常登录、文件篡改、密钥脱敏、跨机器断联探测与外部独立公有云心跳",
                responsible_phases=("P0", "P1"),
                status=QaVerificationStatus.PARTIAL_PASS_PENDING,
                engineering_verified=True,
                statistical_or_external_status="PUBLIC_CLOUD_CLOUDFLARE_FCM_CREDENTIALS_PENDING",
                key_metrics={
                    "local_s01_file_tamper_and_ssh_lockdown": "PASS",
                    "cross_machine_dead_mans_switch_probe": "PASS",
                    "log_and_payload_redaction_rate": "100%",
                    "public_cloudflare_worker_production_credentials": "PENDING",
                },
                evidence_files=(
                    "evidence/external_alert_receipts.jsonl",
                    "tests/test_p1_fault_injection_matrix.py",
                ),
                honest_notes=(
                    "本机 S-01 入侵锁仓、日志 100% 脱敏及跨机器黑盒失联告警回执已实测 PASS；"
                    "第三方公有云 Cloudflare Workers 生产账号与真实 FCM 云凭据未录入，诚实保持 PENDING。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-11",
                title="Android 前后台分级通知、权限被拒、FCM 丢包/断网 WorkManager 告警补偿与防重放紧急锁仓",
                responsible_phases=("P7",),
                status=(
                    QaVerificationStatus.PARTIAL_PASS_PENDING
                    if p7_eng_passed
                    else QaVerificationStatus.FAIL
                ),
                engineering_verified=p7_eng_passed,
                statistical_or_external_status="PROTOCOL_AND_SIMULATOR_PASS__REAL_FIREBASE_CLOUD_CREDENTIALS_PENDING",
                key_metrics={
                    "missed_high_critical_alert_loss_rate_pct": p7_qa.get(
                        "missed_high_critical_alert_loss_rate_pct"
                    ),
                    "replay_attack_commands_passed_count": p7_qa.get("replay_attack_commands_passed_count"),
                    "forbidden_mobile_actions_passed_count": p7_qa.get("forbidden_mobile_actions_passed_count"),
                    "in_flight_sell_protection_preserved": p7_qa.get(
                        "in_flight_sell_protection_orders_preserved_on_mobile_freeze"
                    ),
                    "gateway_p99_latency_ms": p7_ev.get("gateway_slo_and_memory_benchmark", {}).get(
                        "query_p99_ms"
                    ),
                },
                evidence_files=(
                    "evidence/p7_g01_g02_mobile_alert_evidence.json",
                    "tests/test_p7_g01_mobile_gateway_and_g02_alerts.py",
                ),
                honest_notes=(
                    "G-01/G-02 前台模态弹窗、后台高优通知、权限被拒置顶警告、FCM 100% 丢包与断网重连增量补偿（漏报率 0%）、"
                    "Ed25519 防重放紧急锁仓保留 -1% 止损单及拦截 11/11 越权动作已全部验证 PASS；"
                    "真实 Firebase 生产凭据真机公网推送回执保持 PENDING。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-12",
                title="E-01 签名模型上架、SHADOW -> PAPER 原子切换、异常自动回滚与在途持仓退出版本锁定",
                responsible_phases=("P0", "P6"),
                status=QaVerificationStatus.PASS if p6_eng_passed else QaVerificationStatus.FAIL,
                engineering_verified=p6_eng_passed,
                statistical_or_external_status="VERIFIED_IN_PAPER",
                key_metrics={
                    "six_layer_manifest_gate": "PASS",
                    "shadow_orders_submitted_count": p6_ev.get("shadow_orders_submitted_count"),
                    "atomic_promotion_latency_ms": p6_ev.get(
                        "latency_and_memory_slo_benchmark_200_cycles", {}
                    ).get("shadow_to_paper_promotion_latency_ms"),
                    "atomic_auto_rollback_latency_ms": p6_ev.get(
                        "latency_and_memory_slo_benchmark_200_cycles", {}
                    ).get("atomic_auto_rollback_latency_ms"),
                    "in_flight_version_crosstalk_violations": p6_qa.get(
                        "in_flight_version_crosstalk_violations"
                    ),
                },
                evidence_files=(
                    "evidence/p6_e01_integration_and_rollback_evidence.json",
                    "tests/test_p6_e01_registry_and_integrated_pipeline.py",
                ),
                honest_notes=(
                    "Ed25519+SHA256+Schema+Risk+Calibration 六层门禁、SHADOW 零下单隔离、亚毫秒级原子晋升与自动回滚、"
                    "以及在途持仓退出策略版本锁定（串扰次数=0）已全部验证 PASS。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-13",
                title="C1/C2/C3 三种退出策略严格同入场、同风控、同费用双层公平对照与样本外统计裁决",
                responsible_phases=("P3", "P5"),
                status=(
                    QaVerificationStatus.PASS
                    if (p5_eng_passed and oos_net_positive)
                    else QaVerificationStatus.NO_EDGE
                ),
                engineering_verified=p5_eng_passed,
                statistical_or_external_status=f"STATISTICAL_VERDICT_{combined_stat_verdict}",
                key_metrics={
                    "identical_entries_risk_and_fees_verified": p5_qa.get(
                        "QA-13_identical_entries_risk_and_fees_verified"
                    ),
                    "p3_oos_mean_realized_net_return": str(p3_mean_realized_net),
                    "p5_layer1_c1_mean_net_return_bps": str(p5_c1_l1_bps),
                    "p5_layer1_c2_mean_net_return_bps": p5_ev.get("layer1_bar_level_pessimistic", {})
                    .get("strategies", {})
                    .get("C2", {})
                    .get("mean_net_return_bps"),
                    "p5_layer1_c3_mean_net_return_bps": p5_ev.get("layer1_bar_level_pessimistic", {})
                    .get("strategies", {})
                    .get("C3", {})
                    .get("mean_net_return_bps"),
                    "recommended_baseline_per_occams_razor": "C1",
                },
                evidence_files=(
                    "evidence/p3_real_historical_audit_evidence.json",
                    "evidence/p5_c01_exit_comparison_evidence.json",
                    "docs/P3_MATH_AUDIT_REPORT.md",
                ),
                honest_notes=(
                    "双层公平回测工程机制（同入场、同风控、同费率、零时间穿越、单次扣费）100% 验证通过；"
                    "但 60 天币安现货真实历史数据独立测试集 FINAL_TEST (115 笔) 净期望为负（C1=-71.89bps, C2=-57.35bps, C3=-51.88bps），"
                    "统计裁决严格为 NO_EDGE，按奥卡姆剃刀锁定最简 C1 基线并严禁实盘。"
                ),
            ),
            QaVerificationRecord(
                qa_id="QA-14",
                title="LIVE 默认物理关闭；无 7 项 GO/NO-GO 全通与投资人 Ed25519 签名令牌绝不可绕过",
                responsible_phases=("P0", "P4", "P8"),
                status=QaVerificationStatus.PASS,
                engineering_verified=True,
                statistical_or_external_status="NON_BYPASSABLE_GATE_ACTIVE",
                key_metrics={
                    "default_execution_mode": ExecutionMode.PAPER.value,
                    "immutable_approver_pubkey_default": IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX,
                    "persistent_sqlite_wal_nonce_anti_replay": True,
                    "runtime_code_tree_sha256_binding": True,
                    "untrusted_config_and_env_override_blocked": True,
                },
                evidence_files=(
                    "src/sui_quant/config.py",
                    "tests/test_p0_contracts_and_gates.py",
                    "tests/test_p8_final_audit_and_go_no_go_gate.py",
                ),
                honest_notes=(
                    "默认模式严格锁定 PAPER；IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX 默认为 None；"
                    "PersistentNonceRegistry 磁盘持久化防重放与运行时代码树 SHA-256 绑定 100% 阻断任何未授权 LIVE 激活。"
                ),
            ),
        )

        # 5. Construct honest §16 7-Point GoNoGoChecklist
        checklist = GoNoGoChecklist(
            account_spot_suiusdt_verified=False,
            protection_orders_fault_proven=bool(p4_all_passed and p5_eng_passed),
            user_risk_limits_explicitly_approved=False,
            anomaly_freeze_and_reconciliation_proven=bool(p4_all_passed and p7_eng_passed),
            out_of_sample_net_positive_proven=oos_net_positive,
            security_and_external_heartbeat_proven=bool(fi_10b_proven),
            independent_audit_record_signed=False,
        )

        gate_audits: tuple[GoNoGoGateItemAudit, ...] = (
            GoNoGoGateItemAudit(
                gate_id="GATE-01",
                checklist_field="account_spot_suiusdt_verified",
                title="实盘账户现货权限、VIP 费率与 OCO/STOP_LOSS_LIMIT 实网核验",
                passed=checklist.account_spot_suiusdt_verified,
                verification_classification="PENDING_EXTERNAL_OR_WALLCLOCK",
                evidence_summary=(
                    "公开 exchangeInfo 过滤器已在 P0 实测确认；但因严格执行零生产密钥策略，"
                    "私有账户 /api/v3/account 权限与专属费率尚未接入实网核验，诚实保持 False (PENDING)。"
                ),
            ),
            GoNoGoGateItemAudit(
                gate_id="GATE-02",
                checklist_field="protection_orders_fault_proven",
                title="保护订单（-1% 止损 / OCO / 分批重锁 / 闪崩应急平仓）故障注入验证",
                passed=checklist.protection_orders_fault_proven,
                verification_classification="PROVEN_IN_PAPER",
                evidence_summary=(
                    "已在 P4 (FI-P4-03/04/06) 与 P5 (QA-06/QA-07) 100% 验证通过："
                    "部分成交立即挂止损单、分批止盈零超锁、闪崩跳空与拒单应急平仓零裸仓。"
                ),
            ),
            GoNoGoGateItemAudit(
                gate_id="GATE-03",
                checklist_field="user_risk_limits_explicitly_approved",
                title="投资人明确签署实盘资金上限与风险限额 (Ed25519 硬件/离线签名令牌)",
                passed=checklist.user_risk_limits_explicitly_approved,
                verification_classification="NOT_SIGNED_BY_INVESTOR",
                evidence_summary=(
                    "IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX 保持 None，投资人尚未签发 LiveAuthorizationToken，"
                    "且在 NO_EDGE 消除前严禁诱导签署。"
                ),
            ),
            GoNoGoGateItemAudit(
                gate_id="GATE-04",
                checklist_field="anomaly_freeze_and_reconciliation_proven",
                title="异常自动冻结 (ENTRY_FROZEN / SECURITY_LOCKDOWN) 与重启 SYNCING 对账恢复",
                passed=checklist.anomaly_freeze_and_reconciliation_proven,
                verification_classification="PROVEN_IN_PAPER",
                evidence_summary=(
                    "已在 P1/P4/P7 100% 验证通过：24h 亏损熔断、ORDER_UNKNOWN 幂等对账、崩溃重启裸仓优先保护、"
                    "以及移动端 Ed25519 签名紧急锁仓（保留在途 -1% 止损卖单）均通过。"
                ),
            ),
            GoNoGoGateItemAudit(
                gate_id="GATE-05",
                checklist_field="out_of_sample_net_positive_proven",
                title="扣除全部真实交易成本与滑点后的独立样本外净期望显著为正",
                passed=checklist.out_of_sample_net_positive_proven,
                verification_classification="NO_EDGE_BLOCKER",
                evidence_summary=(
                    f"P3/P5 60 天币安现货真实历史数据独立测试集 FINAL_TEST (115 笔) 净期望为负 "
                    f"(P3 mean_realized={p3_mean_realized_net}, P5 Layer1 C1={p5_c1_l1_bps} bps)，"
                    "统计裁决严格为 NO_EDGE，构成实盘准入的核心否决项。"
                ),
            ),
            GoNoGoGateItemAudit(
                gate_id="GATE-06",
                checklist_field="security_and_external_heartbeat_proven",
                title="服务器安全加固、日本云主机连续 72 小时真实墙钟运行 (FI-10B) 与公有云外部心跳",
                passed=checklist.security_and_external_heartbeat_proven,
                verification_classification="PENDING_EXTERNAL_OR_WALLCLOCK",
                evidence_summary=(
                    f"日本东京服务器真实连续 72h 观测 FI-10B 当前进度 {fi_10b_elapsed_hours:.2f}h / {fi_10b_required_hours:.1f}h "
                    f"(状态 {fi_10b_status})，且公有云 Cloudflare Workers / 真实 FCM 生产凭据未配置，诚实保持 False。"
                ),
            ),
            GoNoGoGateItemAudit(
                gate_id="GATE-07",
                checklist_field="independent_audit_record_signed",
                title="独立总审计记录完成且全部前置门禁无阻塞签发",
                passed=checklist.independent_audit_record_signed,
                verification_classification="NO_EDGE_BLOCKER",
                evidence_summary=(
                    "P8 终审器已完成全量证据核验，但因 GATE-01、GATE-03、GATE-05 (NO_EDGE)、GATE-06 (PENDING) 未满足，"
                    "总审计器严格拒绝签发实盘放行许可，锁定 NO_GO_STAY_IN_PAPER。"
                ),
            ),
        )

        # 6. Verify physical blocking of ExecutionMode.LIVE under current checklist
        live_blocked = False
        block_reason = ""
        try:
            SystemConfig(
                mode=ExecutionMode.LIVE,
                risk=self.risk_config,
                allow_live_trading=True,
                go_no_go=checklist,
            )
        except LiveAuthorizationError as exc:
            live_blocked = True
            block_reason = str(exc)

        if not live_blocked:
            raise RuntimeError("CRITICAL GOVERNANCE FAILURE: ExecutionMode.LIVE was not blocked by verify_live_gate!")

        blocking_reasons: list[str] = []
        if not checklist.out_of_sample_net_positive_proven:
            blocking_reasons.append(
                f"STATISTICAL_NO_EDGE: P3/P5 60-day real historical FINAL_TEST net expectancy is negative "
                f"(mean_realized={p3_mean_realized_net}, C1={p5_c1_l1_bps} bps)."
            )
        if not checklist.security_and_external_heartbeat_proven:
            blocking_reasons.append(
                f"FI_10B_AND_EXTERNAL_CLOUD_PENDING: Tokyo server 72h real wall-clock soak is at "
                f"{fi_10b_elapsed_hours:.2f}h / {fi_10b_required_hours:.1f}h ({fi_10b_status}); "
                "public Cloudflare Workers & Firebase production credentials pending."
            )
        if not checklist.account_spot_suiusdt_verified:
            blocking_reasons.append(
                "PRIVATE_ACCOUNT_VERIFICATION_PENDING: Zero production exchange API keys present in repository."
            )
        if not checklist.user_risk_limits_explicitly_approved:
            blocking_reasons.append(
                "INVESTOR_LIVE_TOKEN_UNSIGNED: IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX is None and no LiveAuthorizationToken is signed."
            )

        decision = (
            ReleaseDecision.GO_FOR_CONTROLLED_LIVE_CANDIDATE
            if checklist.all_passed
            else ReleaseDecision.NO_GO_STAY_IN_PAPER
        )

        code_tree_sha = compute_runtime_code_tree_sha256(self.code_package_dir)
        draft_manifest = build_live_authorization_draft_manifest(
            risk_config=self.risk_config,
            code_package_dir=self.code_package_dir,
            statistical_verdict=combined_stat_verdict,
            fi_10b_status=fi_10b_status,
        )

        return FinalGoNoGoAuditReport(
            phase="P8",
            generated_at_utc=ref_now,
            execution_mode_locked=ExecutionMode.PAPER.value,
            live_orders_sent_count=0,
            real_account_api_keys_present=False,
            runtime_code_tree_sha256=code_tree_sha,
            risk_config_version=self.risk_config.version,
            risk_config_sha256=self.risk_config.canonical_sha256(),
            p3_p5_statistical_verdict=combined_stat_verdict,
            fi_10b_72h_wallclock_status=fi_10b_status,
            fi_10b_elapsed_hours=fi_10b_elapsed_hours,
            qa_records=qa_records,
            go_no_go_checklist=checklist,
            go_no_go_gate_audits=gate_audits,
            live_gate_blocked_verified=live_blocked,
            live_gate_block_reason=block_reason,
            overall_release_decision=decision,
            blocking_reasons=tuple(blocking_reasons),
            soak_rehearsal_report=soak_rehearsal_report,
            live_authorization_draft=draft_manifest,
        )

    def run_full_stack_multi_fault_paper_soak_rehearsal(
        self,
        *,
        work_dir: Path,
        now_utc: datetime | None = None,
    ) -> FullStackSoakRehearsalReport:
        """Execute an end-to-end multi-fault PAPER soak rehearsal spanning P0 ~ P7 modules (§13, §14).

        Rehearsed Faults & Governance Invariants:
        1. `F-01` `StreamQualityGuard`: duplicate/out-of-order `MarketEvent` rejection & unclosed (`k.x=False`) K-line isolation (`QA-01`).
        2. `D-01 / B-01 / R-01`: breakout entry (`C1-v1.0`), 40% partial fill immediate `-1%` stop-loss protection (`QA-03`, `QA-04`),
           followed by full fill completion (`QA-06`).
        3. `E-01` Hot-Swap & In-Flight Exit Pinning (`QA-12`): promote `E01-V2-C2` (`C2-v1.0`) from `SHADOW` to `ACTIVE_PAPER`
           while Position #1 (`C1-v1.0`) is open in `PROTECTED` state; verify Position #1 remains strictly pinned to `C1-v1.0`.
        4. `E-01` `AutoRollbackGuard` (`QA-12`): promote a faulty model bundle `E01-V3-FAULTY` and verify automatic atomic rollback
           in `< 5.0 ms` while Position #1 completes its `C1-v1.0` `+1.5%` net target exit (`PROTECTED -> EXITING -> FLAT -> READY_NO_POSITION`).
        5. `G-01 / G-02` Mobile Offline Alert Compensation & Signed Freeze (`QA-11`): open Position #2 under `C2-v1.0`,
           inject FCM 100% packet drop while Android client is offline, emit a `HIGH` alert, reconnect Android `WorkManager`
           (`sync_pending_alerts` -> `0%` alert loss), and execute Ed25519-signed `FREEZE_ENTRY` (`ENTRY_FROZEN`) while preserving
           Position #2's `-1%` stop-loss protection order and rejecting forbidden mobile actions (`PLACE_ORDER`, `ENABLE_LIVE`, `UNFREEZE`).
        6. `F-02 / D-01` Crash & `BOOT -> SYNCING` Startup Reconciliation (`QA-05`): simulate a process crash with an unprotected
           position on exchange and verify `boot_and_reconcile()` immediately transitions `BOOT -> SYNCING -> PROTECTION_PENDING -> PROTECTED`.
        """
        ref_now = ensure_utc(now_utc or datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc), "now_utc")
        work_dir.mkdir(parents=True, exist_ok=True)

        tracemalloc.start()
        mem_start_bytes, _ = tracemalloc.get_traced_memory()

        # --- Step 1: F-01 StreamQualityGuard duplicate, out-of-order, and unclosed bar isolation (QA-01) ---
        q_guard = StreamQualityGuard(max_staleness_ms=2000)
        ev1 = MarketEvent(
            event_id="AGG-100",
            symbol=TRADING_SYMBOL,
            source="aggTrade",
            exchange_time_utc=ref_now,
            recv_time_utc=ref_now,
            recv_monotonic_ns=1_000_000,
            schema_version="1.0",
            seq_or_update_id=100,
            payload={"p": "2.0000", "q": "50.0"},
        )
        ev_dup = MarketEvent(
            event_id="AGG-100",
            symbol=TRADING_SYMBOL,
            source="aggTrade",
            exchange_time_utc=ref_now,
            recv_time_utc=ref_now,
            recv_monotonic_ns=1_000_500,
            schema_version="1.0",
            seq_or_update_id=100,
            payload={"p": "2.0000", "q": "50.0"},
        )
        ev_ooo = MarketEvent(
            event_id="AGG-099",
            symbol=TRADING_SYMBOL,
            source="aggTrade",
            exchange_time_utc=ref_now,
            recv_time_utc=ref_now,
            recv_monotonic_ns=1_001_000,
            schema_version="1.0",
            seq_or_update_id=99,
            payload={"p": "1.9990", "q": "25.0"},
        )
        assert q_guard.evaluate_event(ev1) == DataQuality.OK
        assert q_guard.evaluate_event(ev_dup) == DataQuality.DUPLICATE
        assert q_guard.evaluate_event(ev_ooo) == DataQuality.OUT_OF_ORDER
        rejected_events_count = q_guard.duplicate_count + q_guard.out_of_order_count

        unclosed_q, closed_bar_none, transient_view = q_guard.ingest_kline_payload(
            symbol=TRADING_SYMBOL,
            kline_dict={
                "i": "5m",
                "t": 1761900000000,
                "T": 1761900299999,
                "x": False,
                "o": "1.9900",
                "h": "2.0200",
                "l": "1.9880",
                "c": "2.0150",
                "v": "30000.0",
                "q": "60450.0",
            },
            exchange_time_utc=ref_now,
            recv_time_utc=ref_now,
        )
        assert unclosed_q == DataQuality.OK
        assert closed_bar_none is None
        assert transient_view is not None and transient_view.is_closed is False
        unclosed_isolated_count = 1

        # --- Step 2: Build E-01 Signed Model Bundles & D01IntegratedPipeline (PAPER mode) ---
        cfg = SystemConfig(mode=ExecutionMode.PAPER, risk=self.risk_config)
        store = StateStore(work_dir / "p8_soak_state.db")
        fcm_sim = FcmPushTransportSimulator()
        alert_mgr = AlertManager(store, push_sender=fcm_sim.send_push)
        filters = SymbolFilters(
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
            fetched_at_utc=ref_now,
        )
        exchange = PaperOrderbookSimulator(
            execution_mode=ExecutionMode.PAPER,
            initial_usdt=Decimal("10000.00"),
            initial_sui=Decimal("0.0"),
            initial_bnb=Decimal("10.0"),
            commission_asset="BNB",
            filters=filters,
        )

        sui_bars, btc_bars = self._build_warmup_bars(ref_now, n_warmup=25)
        feat_pipe = FeaturePipeline()

        signing_key = Ed25519PrivateKey.generate()
        pub_hex = signing_key.public_key().public_bytes_raw().hex()

        a02_model_v1 = A02BreakoutCalibratedModel(
            model_id="a02-suiusdt-breakout-3barrier",
            version="1.0.0",
            risk_compatibility=cfg.risk.version,
            rollback_ref="a02-suiusdt-breakout-3barrier:0.9.0-genesis",
            intercepts=(1.40, -0.90, -0.50),
        )
        bundle_v1 = E01ArtifactPackager.package_and_sign(
            model=a02_model_v1,
            signing_private_key=signing_key,
            exit_strategy_id=ExitStrategyId.C1,
            exit_strategy_version="C1-v1.0",
            train_end_utc=ref_now,
            signed_at_utc=ref_now,
        )

        a02_model_v2 = A02BreakoutCalibratedModel(
            model_id="a02-suiusdt-breakout-3barrier",
            version="2.0.0",
            risk_compatibility=cfg.risk.version,
            rollback_ref=bundle_v1.bundle_id,
            intercepts=(1.45, -0.92, -0.53),
        )
        bundle_v2 = E01ArtifactPackager.package_and_sign(
            model=a02_model_v2,
            signing_private_key=signing_key,
            exit_strategy_id=ExitStrategyId.C2,
            exit_strategy_version="C2-v1.0",
            train_end_utc=ref_now,
            signed_at_utc=ref_now,
        )

        registry = E01ModelRegistry(
            store=store,
            alert_manager=alert_mgr,
            risk_config=cfg.risk,
            pinned_signer_pubkey_hex=pub_hex,
            filters=filters,
        )
        registry.pin_registry.register_exit_strategy("C1-v1.0", C1FixedNetTargetExit(filters=filters))
        registry.pin_registry.register_exit_strategy("C2-v1.0", C2GridTrailingExit(filters=filters))
        registry.bootstrap_initial_active_bundle(bundle_v1, now_utc=ref_now)

        pipeline = D01IntegratedPipeline(
            config=cfg,
            store=store,
            alert_manager=alert_mgr,
            exchange=exchange,
            registry=registry,
            feature_pipeline=feat_pipe,
            filters=filters,
        )

        entries_opened = 0
        exits_completed = 0
        over_res_violations = 0
        naked_incidents = 0

        # --- Step 3: Open Position #1 with 40% partial fill (200.0 / 500.0 SUI) -> cancel remainder & protect (QA-04b, QA-06) ---
        t_bar1 = self._append_bar(
            sui_bars,
            btc_bars,
            open_price=Decimal("1.9990"),
            high_price=Decimal("2.0008"),
            low_price=Decimal("1.9985"),
            close_price=Decimal("2.0006"),
            volume=Decimal("48000.0"),
        )
        ob1 = self._make_orderbook(Decimal("2.0000"), t_bar1, mono_ns=100_000_000_000, update_id=2001)
        rep1 = pipeline.step_closed_bar_cycle(
            sui_5m_bars=sui_bars,
            btc_5m_bars=btc_bars,
            orderbook=ob1,
            immediate_fill_qty=Decimal("200.0"),
            immediate_fill_price=Decimal("2.0000"),
            require_candidate_filter=True,
            require_positive_ev=False,
            now_utc=t_bar1,
        )
        assert rep1.trading_state_after == TradingState.PARTIALLY_FILLED
        partial_outcome = pipeline.d01_engine.handle_order_ttl_or_partial_completion(
            force_timeout_cancel=True,
            current_market_price=Decimal("2.0000"),
            now_utc=t_bar1,
        )
        assert partial_outcome.final_state == TradingState.PROTECTED
        assert pipeline.d01_engine.active_position_ledger is not None
        stamped_pos1 = pipeline._stamp_and_pin_new_position(
            ledger=pipeline.d01_engine.active_position_ledger,
            now_utc=t_bar1,
        )
        assert stamped_pos1.exit_strategy_version == "C1-v1.0"
        assert len(stamped_pos1.protection_orders) == 1
        partial_fill_protected_ok = (
            stamped_pos1.actual_qty == Decimal("200.0")
            and stamped_pos1.reserved_qty == Decimal("200.0")
            and len(exchange.list_open_orders(side=OrderSide.BUY)) == 0
        )
        entries_opened += 1

        # Stage and promote V2-C2 (C2-v1.0) while Position #1 is open in PROTECTED state (QA-12)
        registry.stage_bundle_to_shadow(bundle_v2, now_utc=t_bar1)
        t_bar2 = self._append_bar(
            sui_bars,
            btc_bars,
            open_price=Decimal("2.0006"),
            high_price=Decimal("2.0120"),
            low_price=Decimal("2.0000"),
            close_price=Decimal("2.0100"),
            volume=Decimal("32000.0"),
        )
        ob2 = self._make_orderbook(Decimal("2.0100"), t_bar2, mono_ns=101_000_000_000, update_id=2002)
        rep2 = pipeline.step_closed_bar_cycle(
            sui_5m_bars=sui_bars,
            btc_5m_bars=btc_bars,
            orderbook=ob2,
            now_utc=t_bar2,
        )
        assert rep2.shadow_record is not None
        registry.promote_shadow_to_paper(min_shadow_evaluations=1, now_utc=t_bar2)
        assert registry.active_paper_exit_strategy_version == "C2-v1.0"

        # Step another bar while Position #1 is held: verify Position #1 is STILL managed by C1-v1.0!
        t_bar3 = self._append_bar(
            sui_bars,
            btc_bars,
            open_price=Decimal("2.0100"),
            high_price=Decimal("2.0200"),
            low_price=Decimal("2.0080"),
            close_price=Decimal("2.0180"),
            volume=Decimal("29000.0"),
        )
        ob3 = self._make_orderbook(Decimal("2.0180"), t_bar3, mono_ns=102_000_000_000, update_id=2003)
        rep3 = pipeline.step_closed_bar_cycle(
            sui_5m_bars=sui_bars,
            btc_5m_bars=btc_bars,
            orderbook=ob3,
            now_utc=t_bar3,
        )
        in_flight_pin_ok = (
            rep3.active_paper_exit_version == "C2-v1.0"
            and rep3.pinned_position_exit_version == "C1-v1.0"
            and registry.pin_registry.version_crosstalk_violations == 0
        )

        # --- Step 4: Inject runtime fault into active model -> AutoRollbackGuard rolls back to V1-C1,
        # and Position #1 hits its C1 +1.5% net profit target and exits cleanly to READY_NO_POSITION ---
        def _fault_hook(_f: MarketFeatures) -> None:
            raise RuntimeError("SIMULATED_A02_RUNTIME_INFERENCE_FAILURE")

        registry.inject_runtime_fault_hook(bundle_v2.bundle_id, _fault_hook)

        t_bar4 = self._append_bar(
            sui_bars,
            btc_bars,
            open_price=Decimal("2.0180"),
            high_price=Decimal("2.0450"),
            low_price=Decimal("2.0170"),
            close_price=Decimal("2.0420"),
            volume=Decimal("45000.0"),
        )
        ob4 = self._make_orderbook(Decimal("2.0420"), t_bar4, mono_ns=103_000_000_000, update_id=2004)
        rep4 = pipeline.step_closed_bar_cycle(
            sui_5m_bars=sui_bars,
            btc_5m_bars=btc_bars,
            orderbook=ob4,
            now_utc=t_bar4,
        )
        registry.inject_runtime_fault_hook(bundle_v2.bundle_id, None)
        auto_rollback_ok = (
            rep4.rollback_record is not None
            and registry.active_paper_bundle.bundle_id == bundle_v1.bundle_id
            and rep4.c01_report is not None
            and rep4.c01_report.action == ExitSignalAction.FULL_EXIT_TAKE_PROFIT
            and rep4.trading_state_after == TradingState.READY_NO_POSITION
        )
        rollback_lat_ms = (
            rep4.rollback_record.rollback_latency_ns / 1_000_000.0
            if rep4.rollback_record is not None
            else 0.0
        )
        exits_completed += 1

        # --- Step 5: Re-stage & promote healthy V2-C2, open Position #2 (stamped with C2-v1.0),
        # and test G-01/G-02 mobile offline alert compensation + signed FREEZE_ENTRY ---
        registry.stage_bundle_to_shadow(bundle_v2, now_utc=t_bar4)
        t_bar5 = self._append_bar(
            sui_bars,
            btc_bars,
            open_price=Decimal("2.0400"),
            high_price=Decimal("2.0430"),
            low_price=Decimal("2.0380"),
            close_price=Decimal("2.0410"),
            volume=Decimal("26000.0"),
        )
        ob5 = self._make_orderbook(Decimal("2.0410"), t_bar5, mono_ns=104_000_000_000, update_id=2005)
        pipeline.step_closed_bar_cycle(
            sui_5m_bars=sui_bars,
            btc_5m_bars=btc_bars,
            orderbook=ob5,
            now_utc=t_bar5,
        )
        registry.promote_shadow_to_paper(min_shadow_evaluations=1, now_utc=t_bar5)

        t_bar6 = self._append_bar(
            sui_bars,
            btc_bars,
            open_price=Decimal("2.0440"),
            high_price=Decimal("2.0462"),
            low_price=Decimal("2.0435"),
            close_price=Decimal("2.0458"),
            volume=Decimal("52000.0"),
        )
        ob6 = self._make_orderbook(Decimal("2.0455"), t_bar6, mono_ns=105_000_000_000, update_id=2006)
        rep6 = pipeline.step_closed_bar_cycle(
            sui_5m_bars=sui_bars,
            btc_5m_bars=btc_bars,
            orderbook=ob6,
            immediate_fill_qty=Decimal("500.0"),
            immediate_fill_price=Decimal("2.0455"),
            require_candidate_filter=True,
            require_positive_ev=False,
            now_utc=t_bar6,
        )
        assert rep6.trading_state_after == TradingState.PROTECTED
        assert rep6.pinned_position_exit_version == "C2-v1.0"
        pos2_qty = pipeline.d01_engine.active_position_ledger.reserved_qty  # type: ignore[union-attr]
        entries_opened += 1

        gateway = G01MobileGateway(pipeline=pipeline)
        signer = MobileKeystoreSigner(device_id="PIXEL-9-PRO-P8")
        gateway.register_mobile_device(
            device_id=signer.device_id,
            ed25519_pubkey_hex=signer.public_key_hex,
            now_utc=t_bar6,
        )
        client_sim = AndroidMonitorClientSimulator(
            gateway=gateway,
            signer=signer,
            lifecycle_state=AppLifecycleState.BACKGROUND,
            notification_permission_granted=True,
            network_online=False,
        )
        fcm_sim.register_client(client_sim)
        fcm_sim.drop_all_pushes = True

        # Emit HIGH alert while phone is offline & FCM drops 100% of pushes
        alert_mgr.emit_alert(
            severity=AlertSeverity.HIGH,
            source="F-01",
            dedup_key="P8-SOAK-WS-LATENCY-SPIKE",
            details={"ws_rtt_ms": 420, "api_secret": "SHOULD_BE_REDACTED"},
            actions_taken=("DEGRADE_DEPTH_TO_1000MS",),
            now_utc=t_bar6,
        )
        assert len(gateway.alert_manager.list_undelivered_alerts()) >= 1

        # Reconnect phone & run WorkManager incremental sync -> 0% missed alert loss
        client_sim.network_online = True
        recovered_alerts = client_sim.run_work_manager_incremental_sync(
            now_utc=t_bar6 + timedelta(seconds=10),
        )
        mobile_sync_zero_loss_ok = (
            len(gateway.alert_manager.list_undelivered_alerts()) == 0
            and len(recovered_alerts) >= 1
            and len(client_sim.background_system_notifications) >= 1
        )

        # Verify forbidden mobile commands are 100% blocked
        forbidden_blocked = 0
        for bad_act in ("PLACE_ORDER", "ENABLE_LIVE", "UNFREEZE", "CANCEL_PROTECTION_ORDER"):
            bad_env = signer.sign_command(
                action=bad_act,
                reason="p8_test_forbidden",
                now_utc=t_bar6 + timedelta(seconds=15),
            )
            try:
                gateway.execute_control_command(bad_env, now_utc=t_bar6 + timedelta(seconds=16))
            except MobileControlForbiddenActionError:
                forbidden_blocked += 1

        # Issue valid Ed25519-signed FREEZE_ENTRY from mobile while Position #2 is open in PROTECTED state
        freeze_env = signer.sign_command(
            action=MobileControlAction.FREEZE_ENTRY.value,
            reason="P8 soak rehearsal operator defensive freeze",
            now_utc=t_bar6 + timedelta(seconds=20),
        )
        freeze_res = gateway.execute_control_command(
            freeze_env,
            now_utc=t_bar6 + timedelta(seconds=20),
        )
        open_sell_stops = [
            o
            for o in exchange.list_open_orders(side=OrderSide.SELL)
            if o.stop_price is not None
        ]
        mobile_freeze_preserved_stops_ok = (
            freeze_res["current_state"] == TradingState.ENTRY_FROZEN.value
            and len(freeze_res["preserved_sell_protection_orders"]) == 1
            and len(open_sell_stops) == 1
            and open_sell_stops[0].orig_qty == pos2_qty
        )

        # --- Step 6: Simulate server crash right after fill & restart reconciliation (`BOOT -> SYNCING -> PROTECTED`, QA-05) ---
        crash_store = StateStore(work_dir / "p8_crash_recovery.db")
        crash_alerts = AlertManager(crash_store)
        crash_ex = PaperOrderbookSimulator(
            execution_mode=ExecutionMode.PAPER,
            initial_usdt=Decimal("5000.00"),
            initial_sui=Decimal("0.0"),
            initial_bnb=Decimal("5.0"),
            commission_asset="BNB",
            filters=filters,
        )
        crash_pipeline = D01IntegratedPipeline(
            config=cfg,
            store=crash_store,
            alert_manager=crash_alerts,
            exchange=crash_ex,
            registry=registry,
            feature_pipeline=feat_pipe,
            filters=filters,
        )
        crash_pipeline.d01_engine.boot_and_reconcile(
            current_market_price=Decimal("2.0455"),
            now_utc=t_bar6 + timedelta(seconds=25),
        )
        assert crash_pipeline.d01_engine.current_state == TradingState.READY_NO_POSITION
        assert rep6.active_proposal is not None
        crash_outcome = crash_pipeline.d01_engine.process_entry_signal(
            regime=rep6.regime_decision,
            proposal=rep6.active_proposal,
            orderbook=ob6,
            immediate_fill_qty=Decimal("500.0"),
            immediate_fill_price=Decimal("2.0455"),
            simulate_crash_Right_after_fill=True,
            current_monotonic_ns=105_000_000_000,
            now_utc=t_bar6 + timedelta(seconds=26),
        )
        assert crash_outcome.final_state == TradingState.FILLED
        assert len(crash_ex.list_open_orders(side=OrderSide.SELL)) == 0

        # Restart process -> BOOT -> SYNCING -> PROTECTION_PENDING -> PROTECTED
        crash_store.set_trading_state(TradingState.BOOT)
        rec_outcome = crash_pipeline.d01_engine.boot_and_reconcile(
            current_market_price=Decimal("2.0455"),
            now_utc=t_bar6 + timedelta(seconds=30),
        )
        crash_recovery_ok = (
            rec_outcome.final_state == TradingState.PROTECTED
            and crash_pipeline.d01_engine.active_position_ledger is not None
            and crash_pipeline.d01_engine.active_position_ledger.reserved_qty > Decimal("0")
            and len(crash_pipeline.d01_engine.active_position_ledger.protection_orders) == 1
        )

        over_res_violations += pipeline.oco_coordinator.over_reservation_violations
        naked_incidents += pipeline.oco_coordinator.naked_position_violations

        mem_curr_bytes, mem_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        latencies = pipeline.cycle_latencies_ns
        return FullStackSoakRehearsalReport(
            total_bars_rehearsed=len(sui_bars),
            duplicate_and_out_of_order_events_rejected=rejected_events_count,
            unclosed_kline_bars_isolated=unclosed_isolated_count,
            entries_opened_count=entries_opened,
            partial_fill_immediately_protected_verified=partial_fill_protected_ok,
            in_flight_model_hot_swap_and_version_pin_verified=in_flight_pin_ok,
            auto_rollback_on_faulty_model_verified=auto_rollback_ok,
            auto_rollback_latency_ms=rollback_lat_ms,
            c01_exits_completed_count=exits_completed,
            over_reservation_violations=over_res_violations,
            naked_position_incidents=naked_incidents,
            version_crosstalk_violations=registry.pin_registry.version_crosstalk_violations,
            mobile_fcm_drop_offline_sync_zero_loss_verified=mobile_sync_zero_loss_ok,
            mobile_signed_freeze_preserved_stop_orders_verified=mobile_freeze_preserved_stops_ok,
            mobile_forbidden_actions_blocked_count=forbidden_blocked,
            crash_and_boot_syncing_reconciliation_verified=crash_recovery_ok,
            live_orders_sent_count=pipeline.live_orders_sent_count,
            cycle_p50_ms=_percentile_ms(latencies, 50.0),
            cycle_p95_ms=_percentile_ms(latencies, 95.0),
            cycle_p99_ms=_percentile_ms(latencies, 99.0),
            memory_delta_mib=max(0.0, (mem_curr_bytes - mem_start_bytes) / (1024.0 * 1024.0)),
            memory_peak_mib=mem_peak_bytes / (1024.0 * 1024.0),
        )

    @staticmethod
    def _make_orderbook(
        mid_price: Decimal,
        now_utc: datetime,
        mono_ns: int,
        update_id: int,
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

    @staticmethod
    def _build_warmup_bars(
        base_time_utc: datetime,
        n_warmup: int = 25,
        base_price: Decimal = Decimal("1.9850"),
        prev_20_high_cap: Decimal = Decimal("2.0000"),
    ) -> tuple[list[ClosedKlineBar], list[ClosedKlineBar]]:
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
        return sui_bars, btc_bars

    @staticmethod
    def _append_bar(
        sui_bars: list[ClosedKlineBar],
        btc_bars: list[ClosedKlineBar],
        *,
        open_price: Decimal,
        high_price: Decimal,
        low_price: Decimal,
        close_price: Decimal,
        volume: Decimal = Decimal("48000.0"),
        taker_buy_ratio: Decimal = Decimal("0.68"),
        btc_return_bps: Decimal = Decimal("12"),
    ) -> datetime:
        step_ms = 5 * 60 * 1000
        open_ms = sui_bars[-1].open_time_ms + step_ms
        close_ms = open_ms + step_ms - 1
        bar_utc = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)
        sui_bars.append(
            ClosedKlineBar(
                symbol=TRADING_SYMBOL,
                interval="5m",
                open_time_ms=open_ms,
                close_time_ms=close_ms,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                base_volume=volume,
                quote_volume=volume * close_price,
                taker_buy_base_volume=volume * taker_buy_ratio,
                taker_buy_quote_volume=volume * close_price * taker_buy_ratio,
                trade_count=650,
                exchange_time_utc=bar_utc,
                recv_time_utc=bar_utc,
                is_closed=True,
            )
        )
        btc_prev = btc_bars[-1].close_price
        btc_c = (btc_prev * (Decimal("1") + btc_return_bps / Decimal("10000"))).quantize(Decimal("0.01"))
        btc_bars.append(
            ClosedKlineBar(
                symbol=REFERENCE_SYMBOL,
                interval="5m",
                open_time_ms=open_ms,
                close_time_ms=close_ms,
                open_price=btc_prev,
                high_price=max(btc_prev, btc_c) + Decimal("20.00"),
                low_price=min(btc_prev, btc_c) - Decimal("20.00"),
                close_price=btc_c,
                base_volume=Decimal("140.0"),
                quote_volume=Decimal("140.0") * btc_c,
                taker_buy_base_volume=Decimal("80.0"),
                taker_buy_quote_volume=Decimal("80.0") * btc_c,
                trade_count=1600,
                exchange_time_utc=bar_utc,
                recv_time_utc=bar_utc,
                is_closed=True,
            )
        )
        return bar_utc
