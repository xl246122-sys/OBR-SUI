"""P8 Final Verification Suite: QA-01~QA-14 Honest Evidence Aggregation, Full-Stack Multi-Fault
PAPER Soak Rehearsal, and Non-Bypassable §16 GO/NO-GO Live Gate Verification (§14, §15, §16)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sui_quant.audit import (
    P8FinalSystemAuditor,
    QaVerificationStatus,
    ReleaseDecision,
    build_live_authorization_draft_manifest,
)
from sui_quant.config import (
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
from sui_quant.contracts import TRADING_SYMBOL
from sui_quant.utils.clock import utc_now


def test_p8_honest_qa01_to_qa14_evidence_aggregation_and_no_go_verdict() -> None:
    """Verify P8FinalSystemAuditor honestly aggregates P0~P7 evidence without whitewashing NO_EDGE or PENDING."""
    auditor = P8FinalSystemAuditor()
    report = auditor.evaluate_all_evidence_and_gates(
        now_utc=datetime(2026, 10, 31, 10, 15, 0, tzinfo=timezone.utc),
    )

    assert report.phase == "P8"
    assert report.execution_mode_locked == "PAPER"
    assert report.live_orders_sent_count == 0
    assert report.real_account_api_keys_present is False
    assert report.p3_p5_statistical_verdict == "NO_EDGE"
    assert report.fi_10b_72h_wallclock_status == "PENDING"
    assert report.fi_10b_elapsed_hours < 72.0
    assert report.overall_release_decision == ReleaseDecision.NO_GO_STAY_IN_PAPER
    assert report.live_gate_blocked_verified is True

    # Verify all 14 QA items are present and accurately classified
    qa_map = {r.qa_id: r for r in report.qa_records}
    assert len(qa_map) == 14
    for i in range(1, 15):
        assert f"QA-{i:02d}" in qa_map

    # Strictly PASS items (proven in deterministic unit/fault-injection/paper suites)
    for pass_id in ("QA-01", "QA-03", "QA-04", "QA-05", "QA-06", "QA-07", "QA-08", "QA-12", "QA-14"):
        assert qa_map[pass_id].status == QaVerificationStatus.PASS, f"{pass_id} expected PASS"
        assert qa_map[pass_id].engineering_verified is True

    # Strictly PARTIAL_PASS_PENDING items (engineering proven, external cloud creds / 72h wallclock pending)
    for pending_id in ("QA-02", "QA-09", "QA-10", "QA-11"):
        assert qa_map[pending_id].status == QaVerificationStatus.PARTIAL_PASS_PENDING
        assert qa_map[pending_id].engineering_verified is True

    # Strictly NO_EDGE item (60d real historical out-of-sample net expectancy is negative)
    assert qa_map["QA-13"].status == QaVerificationStatus.NO_EDGE
    assert qa_map["QA-13"].engineering_verified is True
    assert "NO_EDGE" in qa_map["QA-13"].statistical_or_external_status

    # Verify §16 7-Point GoNoGoChecklist
    chk = report.go_no_go_checklist
    assert chk.protection_orders_fault_proven is True
    assert chk.anomaly_freeze_and_reconciliation_proven is True
    assert chk.account_spot_suiusdt_verified is False
    assert chk.user_risk_limits_explicitly_approved is False
    assert chk.out_of_sample_net_positive_proven is False
    assert chk.security_and_external_heartbeat_proven is False
    assert chk.independent_audit_record_signed is False
    assert chk.all_passed is False
    assert len(report.go_no_go_gate_audits) == 7
    assert len(report.blocking_reasons) >= 4


def test_p8_live_gate_non_bypassable_under_current_system_state_qa14(tmp_path: Path) -> None:
    """Verify QA-14 & §16: LIVE mode is physically non-bypassable if any of the 7 GO/NO-GO gates is False
    or if IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX remains None."""
    assert IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX is None

    risk = RiskConfig()
    priv_key = Ed25519PrivateKey.generate()
    pub_hex = (
        priv_key.public_key()
        .public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
        .hex()
    )
    now = utc_now() - timedelta(minutes=1)
    exp = now + timedelta(days=3)
    code_sha = compute_runtime_code_tree_sha256()

    payload = LiveAuthorizationToken.canonical_Signing_payload(
        token_id="P8-TEST-NONCE-0001",
        approver_id="INVESTOR-P8",
        authorized_symbol=TRADING_SYMBOL,
        max_capital_usdt=Decimal("1000.00"),
        max_single_trade_risk_pct=Decimal("0.0025"),
        max_rolling_24h_loss_pct=Decimal("0.01"),
        risk_config_version=risk.version,
        risk_config_sha256=risk.canonical_sha256(),
        git_commit_sha=code_sha,
        issued_at_utc=now,
        expires_at_utc=exp,
    )
    sig_hex = priv_key.sign(payload).hex()
    signed_token = LiveAuthorizationToken(
        token_id="P8-TEST-NONCE-0001",
        approver_id="INVESTOR-P8",
        authorized_symbol=TRADING_SYMBOL,
        max_capital_usdt=Decimal("1000.00"),
        max_single_trade_risk_pct=Decimal("0.0025"),
        max_rolling_24h_loss_pct=Decimal("0.01"),
        risk_config_version=risk.version,
        risk_config_sha256=risk.canonical_sha256(),
        git_commit_sha=code_sha,
        issued_at_utc=now,
        expires_at_utc=exp,
        ed25519_signature_hex=sig_hex,
    )

    all_true_checklist = GoNoGoChecklist(
        account_spot_suiusdt_verified=True,
        protection_orders_fault_proven=True,
        user_risk_limits_explicitly_approved=True,
        anomaly_freeze_and_reconciliation_proven=True,
        out_of_sample_net_positive_proven=True,
        security_and_external_heartbeat_proven=True,
        independent_audit_record_signed=True,
    )

    # 1. Every single one of the 7 GoNoGoChecklist fields individually blocks LIVE when False!
    for gate_field in (
        "account_spot_suiusdt_verified",
        "protection_orders_fault_proven",
        "user_risk_limits_explicitly_approved",
        "anomaly_freeze_and_reconciliation_proven",
        "out_of_sample_net_positive_proven",
        "security_and_external_heartbeat_proven",
        "independent_audit_record_signed",
    ):
        incomplete_checklist = replace(all_true_checklist, **{gate_field: False})
        assert incomplete_checklist.all_passed is False
        with pytest.raises(LiveAuthorizationError, match="GO/NO-GO checklist is incomplete"):
            SystemConfig(
                mode=ExecutionMode.LIVE,
                risk=risk,
                allow_live_trading=True,
                go_no_go=incomplete_checklist,
                live_auth_token=signed_token,
                pinned_approver_pubkey_hex=pub_hex,
                verify_runtime_code_tree=True,
            )

    # 2. Even if someone passes `all_true_checklist` + `signed_token` through `from_untrusted_mapping`,
    #    because `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX` in `config.py` is `None`, LIVE is STILL blocked!
    nonce_reg = PersistentNonceRegistry(tmp_path / "p8_nonces.db")
    with pytest.raises(LiveAuthorizationError, match="No valid 32-byte Ed25519 approver public key is pinned"):
        SystemConfig.from_untrusted_mapping(
            raw_config={
                "mode": "LIVE",
                "allow_live_trading": True,
                "go_no_go": all_true_checklist,
                "live_auth_token": signed_token,
            },
            trusted_nonce_registry=nonce_reg,
        )

    # 3. Verify unsigned draft manifest cannot be used as a token and contains explicit warning
    draft = build_live_authorization_draft_manifest(risk_config=risk)
    assert draft["manifest_approval_state"] == "UNSIGNED_DRAFT_DO_NOT_SIGN_WHILE_NO_EDGE"
    assert draft["cryptographic_binding_fields"]["ed25519_sig_hex_placeholder"] == ""
    assert draft["cryptographic_binding_fields"]["runtime_code_tree_sha256"] == code_sha
    assert draft["cryptographic_binding_fields"]["risk_config_sha256"] == risk.canonical_sha256()


def test_p8_full_stack_multi_fault_paper_soak_rehearsal_and_archive_evidence(tmp_path: Path) -> None:
    """Run full-stack multi-fault PAPER soak rehearsal across P0~P7 modules and archive P8 final evidence."""
    auditor = P8FinalSystemAuditor()
    now_utc = datetime(2026, 10, 31, 10, 30, 0, tzinfo=timezone.utc)

    soak_report = auditor.run_full_stack_multi_fault_paper_soak_rehearsal(
        work_dir=tmp_path / "p8_soak_rehearsal",
        now_utc=now_utc,
    )

    assert soak_report.total_bars_rehearsed >= 30
    assert soak_report.duplicate_and_out_of_order_events_rejected == 2
    assert soak_report.unclosed_kline_bars_isolated == 1
    assert soak_report.entries_opened_count == 2
    assert soak_report.partial_fill_immediately_protected_verified is True
    assert soak_report.in_flight_model_hot_swap_and_version_pin_verified is True
    assert soak_report.auto_rollback_on_faulty_model_verified is True
    assert soak_report.auto_rollback_latency_ms <= 5.0
    assert soak_report.c01_exits_completed_count == 1
    assert soak_report.over_reservation_violations == 0
    assert soak_report.naked_position_incidents == 0
    assert soak_report.version_crosstalk_violations == 0
    assert soak_report.mobile_fcm_drop_offline_sync_zero_loss_verified is True
    assert soak_report.mobile_signed_freeze_preserved_stop_orders_verified is True
    assert soak_report.mobile_forbidden_actions_blocked_count == 4
    assert soak_report.crash_and_boot_syncing_reconciliation_verified is True
    assert soak_report.live_orders_sent_count == 0
    assert soak_report.cycle_p99_ms <= 50.0
    assert soak_report.memory_delta_mib <= 15.0

    final_report = auditor.evaluate_all_evidence_and_gates(
        soak_rehearsal_report=soak_report,
        now_utc=now_utc,
    )
    assert final_report.overall_release_decision == ReleaseDecision.NO_GO_STAY_IN_PAPER
    assert final_report.live_orders_sent_count == 0

    evidence_path = Path(__file__).resolve().parents[1] / "evidence" / "p8_final_go_no_go_audit_evidence.json"
    evidence_payload = final_report.to_dict()
    evidence_path.write_text(
        json.dumps(evidence_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert evidence_path.exists()
