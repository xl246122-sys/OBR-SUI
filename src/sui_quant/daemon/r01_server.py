"""R-01 Independent Risk Process & Mutual Watchdog (§8, §9).

- Zero dependency on ML/strategy inference libraries; executes pure Decimal boundary checks in microseconds.
- Enforces 25ms P99 IPC latency gate (`MAX_IPC_LATENCY_NS = 25_000_000`).
- Fails closed (`ENTRY_FROZEN`) if R-01 heartbeat is stale (>10s), IPC times out (>25ms), or guardian
  (`F-02`) heartbeat is lost (>15s), while preserving verified risk-reduction/exit paths.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sui_quant.config import RiskConfig
from sui_quant.contracts import AlertSeverity, BreakoutProposal, OrderSide, RiskVerdict
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.exchange_rules import (
    P0_SNAPSHOT_SUIUSDT_FILTERS,
    SymbolFilters,
    validate_pre_buy_protectability,
)
from sui_quant.state_machine import StateTransitionRecord, TradingState
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


MAX_R01_HEARTBEAT_AGE_NS = 10 * 1_000_000_000   # 10 seconds
MAX_GUARDIAN_HEARTBEAT_AGE_NS = 15 * 1_000_000_000  # 15 seconds mutual watchdog
MAX_R01_DECISION_LATENCY_NS = 25 * 1_000_000    # 25 milliseconds SLO (§13)


class R01RiskDaemon:
    """Independent R-01 risk supervisor with mutual watchdog and fail-closed entry veto."""

    def __init__(
        self,
        store: StateStore,
        alert_manager: AlertManager,
        risk_config: RiskConfig,
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
    ) -> None:
        self.store = store
        self.alert_manager = alert_manager
        self.risk_config = risk_config
        self.filters = filters

    def check_guardian_mutual_watchdog(
        self,
        current_monotonic_ns: int | None = None,
        now_utc: datetime | None = None,
    ) -> bool:
        """Mutual Watchdog: R-01 verifies that `guardian` (F-02) is alive (<= 15s).

        If `guardian` is dead or stalled, R-01 directly freezes new entries (`ENTRY_FROZEN`)
        and dispatches a CRITICAL alert.
        """
        ref_mono = current_monotonic_ns if current_monotonic_ns is not None else monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        guardian_hb = self.store.get_heartbeat("guardian")
        if guardian_hb is None or (ref_mono - int(guardian_hb["monotonic_ns"])) > MAX_GUARDIAN_HEARTBEAT_AGE_NS:
            self._force_entry_frozen("GUARDIAN_HEARTBEAT_LOST_BY_R01", ref_now)
            self.alert_manager.emit_alert(
                severity=AlertSeverity.CRITICAL,
                source="R-01",
                dedup_key="R01-GUARDIAN-WATCHDOG-TIMEOUT",
                actions_taken=("ENTRY_FROZEN", "R01_MUTUAL_WATCHDOG_TRIGGERED"),
                details={"guardian_heartbeat_present": guardian_hb is not None},
                now_utc=ref_now,
            )
            return False
        return True

    def evaluate_entry_proposal(
        self,
        proposal: BreakoutProposal,
        account_equity_usdt: Decimal,
        rolling_24h_loss_pct: Decimal,
        planned_buy_price: Decimal,
        current_monotonic_ns: int | None = None,
        simulated_ipc_latency_ns: int = 0,
        now_utc: datetime | None = None,
    ) -> RiskVerdict:
        """Evaluate whether a new SUIUSDT buy entry is allowed under R-01 non-relaxable boundaries."""
        t0 = monotonic_ns()
        ref_mono = current_monotonic_ns if current_monotonic_ns is not None else t0
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        reject_reasons: list[str] = []

        # 1. Check current trading state
        current_state = self.store.get_trading_state()
        if current_state in {TradingState.ENTRY_FROZEN, TradingState.SECURITY_LOCKDOWN}:
            reject_reasons.append(f"SYSTEM_STATE_{current_state.value}")

        # 2. Check R-01 own heartbeat freshness and Guardian mutual watchdog
        r01_hb = self.store.get_heartbeat("r01")
        if r01_hb is None or (ref_mono - int(r01_hb["monotonic_ns"])) > MAX_R01_HEARTBEAT_AGE_NS:
            reject_reasons.append("R01_HEARTBEAT_STALE_OR_MISSING")
            self._force_entry_frozen("R01_HEARTBEAT_LOST", ref_now)

        if not self.check_guardian_mutual_watchdog(current_monotonic_ns=ref_mono, now_utc=ref_now):
            reject_reasons.append("GUARDIAN_WATCHDOG_FAILED")

        # 3. Check 24h rolling equity loss circuit breaker (<= 1.0%)
        if rolling_24h_loss_pct >= self.risk_config.max_rolling_24h_loss_pct:
            reject_reasons.append("ROLLING_24H_LOSS_LIMIT_REACHED")
            self._force_entry_frozen("ROLLING_24H_LOSS_BREACH", ref_now)

        # 4. Compute maximum position size respecting both 10% allocation cap and 0.25% risk budget
        #    assuming 1.5% stressed stop loss (1% trigger + 0.5% stressed slippage/fees)
        stressed_per_unit_loss_pct = self.risk_config.stop_loss_trigger_pct + Decimal("0.005")
        max_notional_by_alloc = account_equity_usdt * self.risk_config.max_capital_allocation_pct
        max_notional_by_risk = (
            account_equity_usdt * self.risk_config.max_single_trade_risk_pct
        ) / stressed_per_unit_loss_pct
        allowed_notional = min(max_notional_by_alloc, max_notional_by_risk)
        raw_qty = allowed_notional / planned_buy_price
        max_qty = self.filters.quantize_qty_down(raw_qty)

        if max_qty < self.filters.min_qty:
            reject_reasons.append("MAX_QTY_BELOW_MIN_LOT_SIZE")
        else:
            try:
                validate_pre_buy_protectability(
                    planned_buy_price=planned_buy_price,
                    planned_buy_qty=max_qty,
                    filters=self.filters,
                )
            except Exception as exc:
                reject_reasons.append(f"PRE_BUY_PROTECTABILITY_FAILED:{exc}")

        elapsed_ns = (monotonic_ns() - t0) + simulated_ipc_latency_ns
        if elapsed_ns > MAX_R01_DECISION_LATENCY_NS:
            reject_reasons.append("R01_IPC_LATENCY_SLO_EXCEEDED")
            self._force_entry_frozen("R01_LATENCY_TIMEOUT", ref_now)

        if reject_reasons:
            return RiskVerdict(
                risk_check_id=f"RV-{uuid.uuid4().hex[:10]}",
                signal_id=proposal.signal_id,
                allowed=False,
                max_qty=Decimal("0"),
                max_notional=Decimal("0"),
                risk_config_version=self.risk_config.version,
                reject_reasons=tuple(reject_reasons),
                evaluated_at=ref_now,
            )

        return RiskVerdict(
            risk_check_id=f"RV-{uuid.uuid4().hex[:10]}",
            signal_id=proposal.signal_id,
            allowed=True,
            max_qty=max_qty,
            max_notional=(max_qty * planned_buy_price).quantize(Decimal("0.0001")),
            risk_config_version=self.risk_config.version,
            reject_reasons=(),
            evaluated_at=ref_now,
        )

    def is_protective_exit_permitted(self, side: OrderSide, reduce_only_spot_qty: Decimal, held_qty: Decimal) -> bool:
        """Protective stop-loss / risk-reducing SELL exits are ALWAYS permitted even during ENTRY_FROZEN
        or SECURITY_LOCKDOWN, provided `reduce_only_spot_qty <= held_qty` (§7, §8.2).
        """
        if side != OrderSide.SELL:
            return False
        if reduce_only_spot_qty <= Decimal("0") or reduce_only_spot_qty > held_qty:
            return False
        return True

    def _force_entry_frozen(self, reason: str, now_utc: datetime) -> None:
        current = self.store.get_trading_state()
        if current not in {TradingState.ENTRY_FROZEN, TradingState.SECURITY_LOCKDOWN}:
            rec = StateTransitionRecord(
                transition_id=f"TR-{uuid.uuid4().hex[:12]}",
                from_state=current,
                to_state=TradingState.ENTRY_FROZEN,
                reason=reason,
                occurred_at_utc=now_utc,
            )
            self.store.record_transition(rec)
