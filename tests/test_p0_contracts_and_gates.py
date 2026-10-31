"""P0 Unit and Acceptance Tests for SUI-Quant V1.0 contracts, filters, risk boundaries, and LIVE gates.

Includes P0 Remediation verification:
- Ed25519 asymmetric signature verification, replay protection, version/SHA256 binding on `LiveAuthorizationToken`
- Proof that `PAPER` default and `LIVE` gates cannot be bypassed via env vars, malformed configs, or parameter tampering
- Boundary tests for `step_size`, fee asset (`SUI` vs `BNB` vs `USDT`), residual dust, `min_notional`, and insufficient sellable quantity
- Distinction between `P0_STATIC_SNAPSHOT` filters and `DYNAMIC_EXCHANGE_INFO` runtime refresh enforcement
"""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sui_quant.config import (
    ExecutionMode,
    GoNoGoChecklist,
    LiveAuthorizationError,
    LiveAuthorizationToken,
    RiskBoundaryViolationError,
    RiskConfig,
    SystemConfig,
)
from sui_quant.contracts import (
    AlertEvent,
    AlertSeverity,
    BreakoutProposal,
    MarketEvent,
    MarketFeatures,
    ModelManifest,
    OrderEvent,
    OrderIntent,
    OrderSide,
    PositionLedger,
    RegimeDecision,
    RegimeType,
    RiskVerdict,
    TimeInForce,
)
from sui_quant.exchange_rules import (
    ExchangeFilterError,
    FilterSourceType,
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SUIUSDT_FILTERS,
    StaleExchangeFilterError,
    SymbolFilters,
    compute_c1_net_target_sell_price,
    compute_post_fill_sellable_position,
    compute_stop_loss_trigger_price,
    validate_pre_buy_protectability,
)
from sui_quant.security.redaction import RedactingLogFilter, redact_mapping, redact_text
from sui_quant.state_machine import (
    InvalidStateTransitionError,
    TradingState,
    assert_valid_transition,
    can_transition,
)
from sui_quant.utils.clock import monotonic_ns, utc_now
from sui_quant.utils.ids import generate_client_order_id


def _make_signed_live_token(
    priv_key: Ed25519PrivateKey,
    risk: RiskConfig,
    token_id: str = "NONCE-20261031-0001",
    git_sha: str = "26b008fe283f00c6e55de0b38c52f7f8adda1d99",
    issued_offset_min: int = -5,
    ttl_days: int = 3,
) -> tuple[LiveAuthorizationToken, str]:
    pub_hex = (
        priv_key.public_key()
        .public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
        .hex()
    )
    issued = utc_now() + timedelta(minutes=issued_offset_min)
    expires = issued + timedelta(days=ttl_days)
    risk_sha = risk.canonical_sha256()
    payload = LiveAuthorizationToken.canonical_Signing_payload(
        token_id=token_id,
        approver_id="investor-owner",
        authorized_symbol="SUIUSDT",
        max_capital_usdt=Decimal("1000.00"),
        max_single_trade_risk_pct=Decimal("0.0025"),
        max_rolling_24h_loss_pct=Decimal("0.01"),
        risk_config_version=risk.version,
        risk_config_sha256=risk_sha,
        git_commit_sha=git_sha,
        issued_at_utc=issued,
        expires_at_utc=expires,
    )
    sig_hex = priv_key.sign(payload).hex()
    token = LiveAuthorizationToken(
        token_id=token_id,
        approver_id="investor-owner",
        authorized_symbol="SUIUSDT",
        max_capital_usdt=Decimal("1000.00"),
        max_single_trade_risk_pct=Decimal("0.0025"),
        max_rolling_24h_loss_pct=Decimal("0.01"),
        risk_config_version=risk.version,
        risk_config_sha256=risk_sha,
        git_commit_sha=git_sha,
        issued_at_utc=issued,
        expires_at_utc=expires,
        ed25519_signature_hex=sig_hex,
    )
    return token, pub_hex


def test_decimal_enforcement_rejects_binary_float() -> None:
    now = utc_now()
    with pytest.raises(TypeError, match="binary float"):
        OrderIntent(
            intent_id="INT-1",
            signal_id="SIG-1",
            client_order_id="SUIQ-ENT-1-abc",
            symbol="SUIUSDT",
            side=OrderSide.BUY,
            price=2.1500,  # type: ignore[arg-type]  # float must be rejected!
            qty=Decimal("10.0"),
            tif=TimeInForce.GTC,
            price_cap=Decimal("2.1600"),
            expiration=now + timedelta(seconds=30),
            strategy_version="v1",
            risk_check_id="RC-1",
        )


def test_utc_timestamp_enforcement_rejects_naive_and_non_utc() -> None:
    naive_dt = datetime(2026, 10, 31, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        MarketEvent(
            event_id="EVT-1",
            symbol="SUIUSDT",
            source="ws:aggTrade",
            exchange_time_utc=naive_dt,
            recv_time_utc=utc_now(),
            recv_monotonic_ns=monotonic_ns(),
            schema_version="1.0",
            payload={},
        )

    tz_plus8 = timezone(timedelta(hours=8))
    non_utc_dt = datetime(2026, 10, 31, 12, 0, 0, tzinfo=tz_plus8)
    with pytest.raises(ValueError, match="strictly UTC"):
        MarketEvent(
            event_id="EVT-2",
            symbol="SUIUSDT",
            source="ws:aggTrade",
            exchange_time_utc=non_utc_dt,
            recv_time_utc=utc_now(),
            recv_monotonic_ns=monotonic_ns(),
            schema_version="1.0",
            payload={},
        )


def test_breakout_proposal_probability_simplex_and_regime_guard() -> None:
    now = utc_now()
    proposal = BreakoutProposal(
        signal_id="SIG-100",
        candle_close_utc=now,
        barrier_ref=Decimal("2.0000"),
        p_win=Decimal("0.52"),
        p_stop=Decimal("0.28"),
        p_timeout=Decimal("0.20"),
        ev_net=Decimal("0.0042"),
        confidence=Decimal("0.85"),
        feature_snapshot_hash="a" * 64,
        model_version="a02-v1",
        expiry=now + timedelta(minutes=5),
    )
    assert proposal.p_win + proposal.p_stop + proposal.p_timeout == Decimal("1.00")

    with pytest.raises(ValueError, match="must equal 1"):
        BreakoutProposal(
            signal_id="SIG-BAD",
            candle_close_utc=now,
            barrier_ref=Decimal("2.0000"),
            p_win=Decimal("0.60"),
            p_stop=Decimal("0.30"),
            p_timeout=Decimal("0.20"),
            ev_net=Decimal("0.0042"),
            confidence=Decimal("0.85"),
            feature_snapshot_hash="a" * 64,
            model_version="a02-v1",
            expiry=now + timedelta(minutes=5),
        )

    with pytest.raises(ValueError, match="must never allow new entry"):
        RegimeDecision(
            decision_id="REG-1",
            regime=RegimeType.DATA_UNHEALTHY,
            confidence=Decimal("0.99"),
            allow_entry=True,
            reason_codes=("WS_GAP",),
            valid_until=now + timedelta(minutes=15),
            model_version="a01-v1",
        )


def test_suiusdt_exchange_filters_and_dust_rounding_qa03_qa06() -> None:
    SUIUSDT_FILTERS.validate_order(price=Decimal("2.1234"), qty=Decimal("10.5"))

    with pytest.raises(ExchangeFilterError, match="tick_size"):
        SUIUSDT_FILTERS.validate_order(price=Decimal("2.12345"), qty=Decimal("10.0"))

    with pytest.raises(ExchangeFilterError, match="step_size"):
        SUIUSDT_FILTERS.validate_order(price=Decimal("2.1234"), qty=Decimal("10.05"))

    with pytest.raises(ExchangeFilterError, match="notional"):
        SUIUSDT_FILTERS.validate_order(price=Decimal("2.0000"), qty=Decimal("2.0"))

    with pytest.raises(ValueError, match="reserved_qty .* cannot exceed actual_qty"):
        PositionLedger(
            position_id="POS-1",
            symbol="SUIUSDT",
            actual_qty=Decimal("9.9"),
            avg_fill_price=Decimal("2.0000"),
            fees=Decimal("0.02"),
            reserved_qty=Decimal("10.0"),
            protection_orders=("OCO-1",),
            exit_strategy_version="C1-v1",
            risk_config_version="1.0.0-candidate",
        )


def test_fee_asset_dust_and_insufficient_sellable_qty_boundaries_task1_5() -> None:
    # 1. Base asset (SUI) fee deduction creates 0.09 SUI residual dust when 10.0 SUI bought with 0.1% fee
    alloc_sui_fee = compute_post_fill_sellable_position(
        gross_filled_qty=Decimal("10.0"),
        avg_fill_price=Decimal("2.0000"),
        fee_asset="SUI",
        fee_amount=Decimal("0.0100"),
    )
    assert alloc_sui_fee.net_base_qty == Decimal("9.9900")
    assert alloc_sui_fee.sellable_qty == Decimal("9.9")
    assert alloc_sui_fee.dust_qty == Decimal("0.0900")
    assert alloc_sui_fee.is_protectable is True

    # 2. BNB fee deduction leaves 100% of base asset intact (0 dust)
    alloc_bnb_fee = compute_post_fill_sellable_position(
        gross_filled_qty=Decimal("10.0"),
        avg_fill_price=Decimal("2.0000"),
        fee_asset="BNB",
        fee_amount=Decimal("0.00005"),
    )
    assert alloc_bnb_fee.net_base_qty == Decimal("10.0")
    assert alloc_bnb_fee.sellable_qty == Decimal("10.0")
    assert alloc_bnb_fee.dust_qty == Decimal("0.0")
    assert alloc_bnb_fee.is_protectable is True

    # 3. Micro partial fill of 0.1 SUI with SUI fee -> net 0.0999 SUI -> sellable_qty = 0.0 < min_qty (0.1)
    alloc_micro = compute_post_fill_sellable_position(
        gross_filled_qty=Decimal("0.1"),
        avg_fill_price=Decimal("2.0000"),
        fee_asset="SUI",
        fee_amount=Decimal("0.0001"),
    )
    assert alloc_micro.sellable_qty == Decimal("0.0")
    assert alloc_micro.dust_qty == Decimal("0.0999")
    assert alloc_micro.is_protectable is False
    assert alloc_micro.unprotectable_reason == "SELLABLE_QTY_BELOW_MIN_QTY"

    # 4. Borderline order: buy 2.5 SUI at $2.01 ($5.025 buy notional >= $5.0),
    #    after 0.1% SUI fee -> net 2.4975 -> sellable 2.4 SUI; at -1% stop ($1.9899),
    #    stop_notional = 2.4 * 1.9899 = $4.77576 < $5.0 min_notional!
    alloc_borderline = compute_post_fill_sellable_position(
        gross_filled_qty=Decimal("2.5"),
        avg_fill_price=Decimal("2.0100"),
        fee_asset="SUI",
        fee_amount=Decimal("0.0025"),
    )
    assert alloc_borderline.sellable_qty == Decimal("2.4")
    assert alloc_borderline.stop_notional_usdt < Decimal("5.0")
    assert alloc_borderline.is_protectable is False
    assert alloc_borderline.unprotectable_reason == "STOP_NOTIONAL_BELOW_MIN_NOTIONAL"

    # 5. Pre-buy protectability guard blocks borderline 2.5 SUI order BEFORE submission!
    with pytest.raises(ExchangeFilterError, match="STOP_NOTIONAL_BELOW_MIN_NOTIONAL"):
        validate_pre_buy_protectability(
            planned_buy_price=Decimal("2.0100"),
            planned_buy_qty=Decimal("2.5"),
        )

    # 6. Pre-buy protectability guard allows 3.0 SUI at $2.0100 (sellable 2.9 * 1.9899 = $5.77071 >= $5.25 buffer)
    safe_alloc = validate_pre_buy_protectability(
        planned_buy_price=Decimal("2.0100"),
        planned_buy_qty=Decimal("3.0"),
    )
    assert safe_alloc.is_protectable is True
    assert safe_alloc.sellable_qty == Decimal("2.9")


def test_static_p0_snapshot_vs_dynamic_runtime_filters_task1_6() -> None:
    now = utc_now()
    # Static P0 snapshot is blocked when require_dynamic_freshness=True (runtime execution guard)
    assert P0_SNAPSHOT_SUIUSDT_FILTERS.source == FilterSourceType.P0_STATIC_SNAPSHOT
    with pytest.raises(StaleExchangeFilterError, match="P0_STATIC_SNAPSHOT"):
        P0_SNAPSHOT_SUIUSDT_FILTERS.validate_order(
            price=Decimal("2.0000"),
            qty=Decimal("10.0"),
            require_dynamic_freshness=True,
            now_utc=now,
        )

    # Dynamic filters parsed from exchangeInfo payload succeed when fresh
    sample_exchange_info_symbol = {
        "symbol": "SUIUSDT",
        "status": "TRADING",
        "ocoAllowed": True,
        "otoAllowed": True,
        "allowTrailingStop": True,
        "cancelReplaceAllowed": True,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.00010000", "maxPrice": "1000.00000000", "tickSize": "0.00010000"},
            {"filterType": "LOT_SIZE", "minQty": "0.10000000", "maxQty": "92141578.00000000", "stepSize": "0.10000000"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000", "maxNotional": "9000000.00000000"},
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
            {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 5},
            {"filterType": "MAX_NUM_ORDER_LISTS", "maxNumOrderLists": 20},
        ],
    }
    dyn_filters = SymbolFilters.from_binance_exchange_info_symbol(
        symbol_payload=sample_exchange_info_symbol,
        fetched_at_utc=now - timedelta(seconds=30),
    )
    assert dyn_filters.source == FilterSourceType.DYNAMIC_EXCHANGE_INFO
    dyn_filters.validate_order(
        price=Decimal("2.0000"),
        qty=Decimal("10.0"),
        require_dynamic_freshness=True,
        now_utc=now,
    )

    # Dynamic filters older than max_age_seconds (e.g. 2 hours old) are rejected as stale
    with pytest.raises(StaleExchangeFilterError, match="stale"):
        dyn_filters.validate_order(
            price=Decimal("2.0000"),
            qty=Decimal("10.0"),
            require_dynamic_freshness=True,
            now_utc=now + timedelta(hours=2),
            max_age_seconds=3600,
        )


def test_c1_net_target_and_stop_loss_trigger_math_qa07() -> None:
    avg_buy = Decimal("2.0000")
    stop_trigger = compute_stop_loss_trigger_price(avg_buy)
    assert stop_trigger == Decimal("1.9800")

    p_sell = compute_c1_net_target_sell_price(
        avg_buy_price=avg_buy,
        buy_fee_rate=Decimal("0.001"),
        sell_fee_rate=Decimal("0.001"),
        target_net_return=Decimal("0.015"),
    )
    buy_cost = avg_buy * (Decimal("1") + Decimal("0.001"))
    sell_proceeds = p_sell * (Decimal("1") - Decimal("0.001"))
    net_return = (sell_proceeds - buy_cost) / buy_cost
    assert net_return >= Decimal("0.015")
    assert (p_sell % SUIUSDT_FILTERS.tick_size) == Decimal("0")


def test_risk_config_immutable_boundaries_qa08() -> None:
    default_risk = RiskConfig()
    assert default_risk.trading_symbol == "SUIUSDT"
    assert default_risk.stop_loss_trigger_pct == Decimal("0.01")

    with pytest.raises(FrozenInstanceError):
        default_risk.allow_leverage = True  # type: ignore[misc]

    with pytest.raises(RiskBoundaryViolationError, match="Only 'SUIUSDT'"):
        RiskConfig(trading_symbol="BTCUSDT")

    with pytest.raises(RiskBoundaryViolationError, match="Spot-only"):
        RiskConfig(allow_leverage=True)

    with pytest.raises(RiskBoundaryViolationError, match="stop_loss_trigger_pct"):
        RiskConfig(stop_loss_trigger_pct=Decimal("0.02"))

    with pytest.raises(RiskBoundaryViolationError, match="max_single_trade_risk_pct"):
        RiskConfig(max_single_trade_risk_pct=Decimal("0.005"))

    with pytest.raises(RiskBoundaryViolationError, match="max_capital_allocation_pct"):
        RiskConfig(max_capital_allocation_pct=Decimal("0.20"))


def test_ed25519_live_token_and_non_bypassable_live_gates_task1_2_1_3_qa14() -> None:
    # 1. Default mode is strictly PAPER
    cfg = SystemConfig()
    assert cfg.mode == ExecutionMode.PAPER
    assert not cfg.allow_live_trading

    all_go = GoNoGoChecklist(
        account_spot_suiusdt_verified=True,
        protection_orders_fault_proven=True,
        user_risk_limits_explicitly_approved=True,
        anomaly_freeze_and_reconciliation_proven=True,
        out_of_sample_net_positive_proven=True,
        security_and_external_heartbeat_proven=True,
        independent_audit_record_signed=True,
    )
    risk = RiskConfig()
    priv_key = Ed25519PrivateKey.generate()
    attacker_key = Ed25519PrivateKey.generate()
    git_sha = "26b008fe283f00c6e55de0b38c52f7f8adda1d99"

    valid_token, pub_hex = _make_signed_live_token(priv_key=priv_key, risk=risk, git_sha=git_sha)

    # 2. Valid Ed25519 signature + all 7 GO/NO-GO gates + matching risk SHA + matching git SHA unlocks LIVE
    live_cfg = SystemConfig(
        mode=ExecutionMode.LIVE,
        risk=risk,
        allow_live_trading=True,
        go_no_go=all_go,
        live_auth_token=valid_token,
        pinned_approver_pubkey_hex=pub_hex,
        audited_git_commit_sha=git_sha,
    )
    assert live_cfg.mode == ExecutionMode.LIVE

    # 3. Forged signature (signed by attacker's Ed25519 key instead of pinned investor key) -> BLOCKED
    forged_token, _ = _make_signed_live_token(priv_key=attacker_key, risk=risk, git_sha=git_sha)
    with pytest.raises(LiveAuthorizationError, match="Ed25519 signature verification failed"):
        SystemConfig(
            mode=ExecutionMode.LIVE,
            risk=risk,
            allow_live_trading=True,
            go_no_go=all_go,
            live_auth_token=forged_token,
            pinned_approver_pubkey_hex=pub_hex,
            audited_git_commit_sha=git_sha,
        )

    # 4. Replay attack (token_id already in consumed_token_ids after restart) -> BLOCKED
    with pytest.raises(LiveAuthorizationError, match="Replay attack"):
        SystemConfig(
            mode=ExecutionMode.LIVE,
            risk=risk,
            allow_live_trading=True,
            go_no_go=all_go,
            live_auth_token=valid_token,
            pinned_approver_pubkey_hex=pub_hex,
            consumed_token_ids=("NONCE-20261031-0001",),
            audited_git_commit_sha=git_sha,
        )

    # 5. Post-signing RiskConfig parameter tampering (even tightening or changing any parameter after signing) -> BLOCKED
    modified_risk = RiskConfig(max_single_trade_risk_pct=Decimal("0.0020"))
    with pytest.raises(LiveAuthorizationError, match="RiskConfig parameter tampering detected"):
        SystemConfig(
            mode=ExecutionMode.LIVE,
            risk=modified_risk,
            allow_live_trading=True,
            go_no_go=all_go,
            live_auth_token=valid_token,
            pinned_approver_pubkey_hex=pub_hex,
            audited_git_commit_sha=git_sha,
        )

    # 6. Code version mismatch (git_commit_sha tampering) -> BLOCKED
    with pytest.raises(LiveAuthorizationError, match="git_commit_sha .* does not match"):
        SystemConfig(
            mode=ExecutionMode.LIVE,
            risk=risk,
            allow_live_trading=True,
            go_no_go=all_go,
            live_auth_token=valid_token,
            pinned_approver_pubkey_hex=pub_hex,
            audited_git_commit_sha="deadbeef1234567",
        )

    # 7. Excessive token TTL (> 7 days) -> BLOCKED
    long_ttl_token, _ = _make_signed_live_token(
        priv_key=priv_key, risk=risk, git_sha=git_sha, ttl_days=14
    )
    with pytest.raises(LiveAuthorizationError, match="Token TTL"):
        SystemConfig(
            mode=ExecutionMode.LIVE,
            risk=risk,
            allow_live_trading=True,
            go_no_go=all_go,
            live_auth_token=long_ttl_token,
            pinned_approver_pubkey_hex=pub_hex,
            audited_git_commit_sha=git_sha,
        )

    # 8. Environment variable injection (`SUI_QUANT_MODE=LIVE` or `ALLOW_LIVE=1`) -> BLOCKED
    with pytest.raises(LiveAuthorizationError, match="Environment variables are strictly forbidden"):
        SystemConfig.from_untrusted_mapping(
            raw_config={},
            env_vars={"SUI_QUANT_MODE": "LIVE"},
        )
    with pytest.raises(LiveAuthorizationError, match="Environment variables are strictly forbidden"):
        SystemConfig.from_untrusted_mapping(
            raw_config={},
            env_vars={"ALLOW_LIVE": "true"},
        )

    # 9. Untrusted dict with truthy string `allow_live_trading="true"` -> BLOCKED
    with pytest.raises(LiveAuthorizationError, match="strict boolean"):
        SystemConfig.from_untrusted_mapping(
            raw_config={"mode": "PAPER", "allow_live_trading": "true"},
        )


def test_state_machine_valid_and_invalid_transitions_qa04_qa05() -> None:
    assert can_transition(TradingState.BOOT, TradingState.SYNCING)
    assert not can_transition(TradingState.BOOT, TradingState.READY_NO_POSITION)
    with pytest.raises(InvalidStateTransitionError):
        assert_valid_transition(TradingState.BOOT, TradingState.READY_NO_POSITION)

    assert not can_transition(TradingState.SIGNAL_CANDIDATE, TradingState.ORDER_SUBMITTED)
    assert can_transition(TradingState.SIGNAL_CANDIDATE, TradingState.RISK_CHECK)
    assert can_transition(TradingState.RISK_CHECK, TradingState.ORDER_SUBMITTED)

    assert not can_transition(TradingState.FILLED, TradingState.PROTECTED)
    assert can_transition(TradingState.FILLED, TradingState.PROTECTION_PENDING)
    assert can_transition(TradingState.PROTECTION_PENDING, TradingState.PROTECTED)

    for state in TradingState:
        assert can_transition(state, TradingState.ENTRY_FROZEN)
        assert can_transition(state, TradingState.SECURITY_LOCKDOWN)

    assert not can_transition(TradingState.SECURITY_LOCKDOWN, TradingState.READY_NO_POSITION)
    assert can_transition(TradingState.SECURITY_LOCKDOWN, TradingState.SYNCING)


def test_idempotent_client_order_id_and_log_redaction_qa10() -> None:
    cid1 = generate_client_order_id("SUIQ", "SIG-20261031-001", leg="ENTRY", attempt=1)
    cid2 = generate_client_order_id("SUIQ", "SIG-20261031-001", leg="ENTRY", attempt=1)
    cid_stop = generate_client_order_id("SUIQ", "SIG-20261031-001", leg="STOP", attempt=1)
    assert cid1 == cid2
    assert cid1 != cid_stop
    assert len(cid1) <= 36

    raw_msg = "Connecting with api_key=ABCDEFG123456 and password=SuperSecret999; 密码： MyPass123;"
    sanitized = redact_text(raw_msg)
    assert "ABCDEFG123456" not in sanitized
    assert "SuperSecret999" not in sanitized
    assert "MyPass123" not in sanitized
    assert "[REDACTED]" in sanitized

    sanitized_dict = redact_mapping(
        {
            "symbol": "SUIUSDT",
            "X-MBX-APIKEY": "secretKeyVal",
            "nested": {"api_secret": "topSecret", "qty": "10.0"},
        }
    )
    assert sanitized_dict["X-MBX-APIKEY"] == "[REDACTED]"
    assert sanitized_dict["nested"]["api_secret"] == "[REDACTED]"
    assert sanitized_dict["nested"]["qty"] == "10.0"

    record = logging.LogRecord(
        name="sui_quant",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Header X-MBX-APIKEY: REAL_SECRET_KEY",
        args=(),
        exc_info=None,
    )
    f = RedactingLogFilter()
    f.filter(record)
    assert "REAL_SECRET_KEY" not in record.msg
    assert "[REDACTED]" in record.msg


def test_other_core_contracts_instantiation() -> None:
    now = utc_now()
    mf = MarketFeatures(
        feature_time_utc=now,
        horizon="5m",
        symbol="SUIUSDT",
        complete_bar_only=True,
        features={"rvol_20": Decimal("2.45"), "atr_14": Decimal("0.0185")},
        data_version="v1",
    )
    assert mf.complete_bar_only is True

    rv = RiskVerdict(
        risk_check_id="RC-1",
        signal_id="SIG-1",
        allowed=False,
        max_qty=Decimal("0"),
        max_notional=Decimal("0"),
        risk_config_version="1.0.0-candidate",
        reject_reasons=("BTC_FLASH_DROP",),
        evaluated_at=now,
    )
    assert not rv.allowed

    oe = OrderEvent(
        client_order_id="SUIQ-ENT-1-abc",
        exchange_status="FILLED",
        filled_qty=Decimal("10.0"),
        cumulative_quote_qty=Decimal("20.0000"),
        fee_asset="SUI",
        fee_amount=Decimal("0.0100"),
        trade_ids=(1001, 1002),
        source_timestamp=now,
        exchange_order_id=999888,
    )
    assert oe.filled_qty == Decimal("10.0")

    ae = AlertEvent(
        alert_id="ALT-1",
        severity=AlertSeverity.CRITICAL,
        source="S-01",
        first_seen_utc=now,
        dedup_key="S01-SSH-ANOMALY",
        event_state="OPEN",
        actions_taken=("SECURITY_LOCKDOWN",),
    )
    assert ae.severity == AlertSeverity.CRITICAL

    mm = ModelManifest(
        model_id="A02-LGBM",
        version="1.0.0",
        features_schema_hash="b" * 64,
        train_end_utc=now,
        test_periods=("2026-09", "2026-10"),
        calibration_metrics={"brier_score": Decimal("0.142")},
        cost_assumptions={"taker_fee": Decimal("0.001")},
        sha256="c" * 64,
        risk_compatibility="1.0.0-candidate",
        rollback_ref="A02-LOGREG-0.9.0",
    )
    assert mm.rollback_ref == "A02-LOGREG-0.9.0"
