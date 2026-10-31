"""P0 Unit and Acceptance Tests for SUI-Quant V1.0 contracts, filters, risk boundaries, and LIVE gates."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

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
    DataQuality,
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
    SUIUSDT_FILTERS,
    compute_c1_net_target_sell_price,
    compute_stop_loss_trigger_price,
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

    # DOWN_TREND / HIGH_VOLATILITY / DATA_UNHEALTHY must never allow entry
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
    # Valid SUIUSDT order: price tick 0.0001, qty step 0.1, notional >= 5.0
    SUIUSDT_FILTERS.validate_order(price=Decimal("2.1234"), qty=Decimal("10.5"))

    # Invalid tick size (0.00001)
    with pytest.raises(ExchangeFilterError, match="tick_size"):
        SUIUSDT_FILTERS.validate_order(price=Decimal("2.12345"), qty=Decimal("10.0"))

    # Invalid step size (0.05 < 0.1 step)
    with pytest.raises(ExchangeFilterError, match="step_size"):
        SUIUSDT_FILTERS.validate_order(price=Decimal("2.1234"), qty=Decimal("10.05"))

    # Invalid notional (< 5.0 USDT)
    with pytest.raises(ExchangeFilterError, match="notional"):
        SUIUSDT_FILTERS.validate_order(price=Decimal("2.0000"), qty=Decimal("2.0"))

    # Dust handling when 10.0 SUI bought with 0.1% fee deducted in SUI -> 9.99 SUI net
    net_sui_after_fee = Decimal("10.0") * Decimal("0.999")
    assert net_sui_after_fee == Decimal("9.990")
    sellable_qty = SUIUSDT_FILTERS.quantize_qty_down(net_sui_after_fee)
    assert sellable_qty == Decimal("9.9")
    SUIUSDT_FILTERS.validate_order(price=Decimal("2.0000"), qty=sellable_qty)

    # PositionLedger must reject duplicate/over-reserved protection qty (QA-06)
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


def test_c1_net_target_and_stop_loss_trigger_math_qa07() -> None:
    avg_buy = Decimal("2.0000")
    stop_trigger = compute_stop_loss_trigger_price(avg_buy)
    assert stop_trigger == Decimal("1.9800")

    # C1 target sell price with 0.1% buy fee and 0.1% sell fee must yield >= +1.5% NET return
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

    # Attempt to trade BTCUSDT or other symbol -> rejected
    with pytest.raises(RiskBoundaryViolationError, match="Only 'SUIUSDT'"):
        RiskConfig(trading_symbol="BTCUSDT")

    # Attempt to enable leverage or shorting -> rejected
    with pytest.raises(RiskBoundaryViolationError, match="Spot-only"):
        RiskConfig(allow_leverage=True)

    # Attempt to relax -1% stop-loss trigger to -2% -> rejected
    with pytest.raises(RiskBoundaryViolationError, match="stop_loss_trigger_pct"):
        RiskConfig(stop_loss_trigger_pct=Decimal("0.02"))

    # Attempt to relax single-trade risk above 0.25% -> rejected
    with pytest.raises(RiskBoundaryViolationError, match="max_single_trade_risk_pct"):
        RiskConfig(max_single_trade_risk_pct=Decimal("0.005"))

    # Attempt to relax capital allocation above 10% -> rejected
    with pytest.raises(RiskBoundaryViolationError, match="max_capital_allocation_pct"):
        RiskConfig(max_capital_allocation_pct=Decimal("0.20"))


def test_live_mode_strictly_blocked_by_default_qa14() -> None:
    # Default config is strictly PAPER
    cfg = SystemConfig()
    assert cfg.mode == ExecutionMode.PAPER
    assert not cfg.allow_live_trading

    # Attempting to instantiate LIVE mode without flags/gates/token -> blocked
    with pytest.raises(LiveAuthorizationError, match="strictly disabled by default"):
        SystemConfig(mode=ExecutionMode.LIVE)

    # Attempting LIVE with allow_live_trading=True but incomplete Go/No-Go checklist -> blocked
    with pytest.raises(LiveAuthorizationError, match="GO/NO-GO checklist is incomplete"):
        SystemConfig(mode=ExecutionMode.LIVE, allow_live_trading=True)

    # Attempting LIVE with complete checklist but missing LiveAuthorizationToken -> blocked
    all_go = GoNoGoChecklist(
        account_spot_suiusdt_verified=True,
        protection_orders_fault_proven=True,
        user_risk_limits_explicitly_approved=True,
        anomaly_freeze_and_reconciliation_proven=True,
        out_of_sample_net_positive_proven=True,
        security_and_external_heartbeat_proven=True,
        independent_audit_record_signed=True,
    )
    with pytest.raises(LiveAuthorizationError, match="LiveAuthorizationToken is missing"):
        SystemConfig(mode=ExecutionMode.LIVE, allow_live_trading=True, go_no_go=all_go)

    # Valid token unlocks when all 7 GO/NO-GO gates and digest match
    issued = utc_now() - timedelta(minutes=5)
    expires = issued + timedelta(days=7)
    digest = LiveAuthorizationToken.compute_digest(
        approver_id="investor-owner",
        authorized_symbol="SUIUSDT",
        max_capital_usdt=Decimal("1000.00"),
        risk_config_version="1.0.0-candidate",
        issued_at_utc=issued,
        expires_at_utc=expires,
    )
    valid_token = LiveAuthorizationToken(
        approver_id="investor-owner",
        authorized_symbol="SUIUSDT",
        max_capital_usdt=Decimal("1000.00"),
        risk_config_version="1.0.0-candidate",
        issued_at_utc=issued,
        expires_at_utc=expires,
        approval_statement_sha256=digest,
    )
    live_cfg = SystemConfig(
        mode=ExecutionMode.LIVE,
        allow_live_trading=True,
        go_no_go=all_go,
        live_auth_token=valid_token,
    )
    assert live_cfg.mode == ExecutionMode.LIVE


def test_state_machine_valid_and_invalid_transitions_qa04_qa05() -> None:
    # Boot must go through SYNCING first, never straight to READY_NO_POSITION
    assert can_transition(TradingState.BOOT, TradingState.SYNCING)
    assert not can_transition(TradingState.BOOT, TradingState.READY_NO_POSITION)
    with pytest.raises(InvalidStateTransitionError):
        assert_valid_transition(TradingState.BOOT, TradingState.READY_NO_POSITION)

    # SIGNAL_CANDIDATE must go through RISK_CHECK, never straight to ORDER_SUBMITTED
    assert not can_transition(TradingState.SIGNAL_CANDIDATE, TradingState.ORDER_SUBMITTED)
    assert can_transition(TradingState.SIGNAL_CANDIDATE, TradingState.RISK_CHECK)
    assert can_transition(TradingState.RISK_CHECK, TradingState.ORDER_SUBMITTED)

    # FILLED must go through PROTECTION_PENDING before PROTECTED
    assert not can_transition(TradingState.FILLED, TradingState.PROTECTED)
    assert can_transition(TradingState.FILLED, TradingState.PROTECTION_PENDING)
    assert can_transition(TradingState.PROTECTION_PENDING, TradingState.PROTECTED)

    # Emergency states reachable from any state
    for state in TradingState:
        assert can_transition(state, TradingState.ENTRY_FROZEN)
        assert can_transition(state, TradingState.SECURITY_LOCKDOWN)

    # Leaving SECURITY_LOCKDOWN cannot jump straight to READY_NO_POSITION without SYNCING
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
