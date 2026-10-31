"""E-01 Signed Model Artifact Registry, SHADOW -> PAPER Atomic Promotion, AutoRollbackGuard,
and In-Flight Position Exit Version Pinning (§11.2, §7, §14.2, QA-12).

Core Invariants Enforced:
1. Multi-Gate Cryptographic & Schema Verification (`verify_bundle`, QA-12):
   - Verifies Ed25519 digital signature against the pinned model-signer public key.
   - Verifies exact SHA-256 digest of serialized `artifact_bytes` against `ModelManifest.sha256`.
   - Verifies `ModelManifest.features_schema_hash` against `FeaturePipeline` / `A02_FEATURE_NAMES`.
   - Verifies `ModelManifest.risk_compatibility` against active `RiskConfig.version` and blocks any
     attempt to alter `R-01` risk boundaries, trade non-`SUIUSDT` symbols, or bypass `LIVE` gates.
   - Verifies `ModelManifest.calibration_metrics` (`ece <= max_allowed_ece`, `brier_score <= baseline_brier_score`).
   - Verifies `ModelManifest.rollback_ref` points to a valid registered stable version (for non-genesis models).
2. Zero-Order `SHADOW` Slot & Atomic `SHADOW -> PAPER` Promotion (§11.2, QA-12):
   - Candidate models are staged in `SHADOW` mode first, running parallel inference on closed-bar
     `MarketFeatures` with zero order submission authority (`shadow_orders_submitted == 0`).
   - Promotion to `ACTIVE_PAPER` swaps the active bundle pointer atomically in `< 5.0 ms` only after
     passing shadow health & latency gates.
3. Automatic Atomic Rollback (`AutoRollbackGuard`, QA-12):
   - Monitors every `ACTIVE_PAPER` inference for runtime exceptions, schema mismatches, non-finite
     outputs, simplex violations, or latency breaches (`> max_inference_latency_ms`).
   - Atomically reverts `ACTIVE_PAPER` to `manifest.rollback_ref` in `< 5.0 ms` and dispatches a
     `HIGH` severity alert via `AlertManager`.
4. In-Flight Position Exit Strategy Version Pinning (`InFlightVersionPinRegistry`, QA-12):
   - While a position is open (`PROTECTED`, `PROTECTION_PENDING`, `PARTIALLY_FILLED`, `EXITING`),
     its exit logic is strictly pinned to the `PositionLedger.exit_strategy_version` recorded at entry.
   - Model hot-swaps or rollbacks affect ONLY subsequent new entries after the current position closes
     to `FLAT -> READY_NO_POSITION` (`version_crosstalk_violations == 0`).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sui_quant.config import ExecutionMode, RiskConfig
from sui_quant.contracts import (
    TRADING_SYMBOL,
    AlertSeverity,
    BreakoutProposal,
    MarketFeatures,
    ModelManifest,
    PositionLedger,
    RegimeDecision,
    require_decimal,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.exchange_rules import P0_SNAPSHOT_SUIUSDT_FILTERS, SymbolFilters
from sui_quant.exit.c01_strategies import (
    C1FixedNetTargetExit,
    C2GridTrailingExit,
    C3AdaptiveHybridExit,
    ExitStrategyId,
    TranchePlanResult,
)
from sui_quant.models.a02_breakout import (
    A02_FEATURE_NAMES,
    A02BreakoutCalibratedModel,
    A02CostAssumptions,
    StatisticalVerdict,
)
from sui_quant.models.features import compute_features_schema_hash
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


ExitStrategyInstance = C1FixedNetTargetExit | C2GridTrailingExit | C3AdaptiveHybridExit


class ModelRegistryVerificationError(PermissionError):
    """Raised when a model artifact bundle fails cryptographic, schema, risk, or calibration verification."""


class ModelSlotRole(str, Enum):
    ACTIVE_PAPER = "ACTIVE_PAPER"
    SHADOW = "SHADOW"
    REGISTERED_STANDBY = "REGISTERED_STANDBY"
    ROLLED_BACK = "ROLLED_BACK"


def make_bundle_id(model_id: str, version: str) -> str:
    return f"{model_id}:{version}"


@dataclass(frozen=True)
class SignedModelArtifactBundle:
    """Immutable, Ed25519-signed model artifact bundle exported by the local research workstation (§11.2)."""

    manifest: ModelManifest
    artifact_bytes: bytes
    exit_strategy_id: ExitStrategyId
    exit_strategy_version: str
    signer_id: str
    signed_at_utc: datetime
    ed25519_signature_hex: str

    def __post_init__(self) -> None:
        ensure_utc(self.signed_at_utc, "signed_at_utc")
        if not isinstance(self.manifest, ModelManifest):
            raise TypeError("manifest must be a ModelManifest instance")
        if not isinstance(self.artifact_bytes, bytes) or len(self.artifact_bytes) == 0:
            raise ValueError("artifact_bytes must be non-empty bytes")
        if not isinstance(self.exit_strategy_id, ExitStrategyId):
            raise TypeError("exit_strategy_id must be an ExitStrategyId enum")
        if not self.exit_strategy_version.strip():
            raise ValueError("exit_strategy_version cannot be empty")
        if not self.signer_id.strip():
            raise ValueError("signer_id cannot be empty")
        if len(self.ed25519_signature_hex) != 128:
            raise ValueError("ed25519_signature_hex must be a 128-character hex string (64 bytes)")

    @property
    def bundle_id(self) -> str:
        return make_bundle_id(self.manifest.model_id, self.manifest.version)

    @staticmethod
    def canonical_signing_payload(
        *,
        manifest: ModelManifest,
        exit_strategy_id: ExitStrategyId,
        exit_strategy_version: str,
        signer_id: str,
        signed_at_utc: datetime,
    ) -> bytes:
        ensure_utc(signed_at_utc, "signed_at_utc")
        cal_str = ",".join(
            f"{k}={require_decimal(v, k).normalize()}"
            for k, v in sorted(manifest.calibration_metrics.items())
        )
        cost_str = ",".join(
            f"{k}={require_decimal(v, k).normalize()}"
            for k, v in sorted(manifest.cost_assumptions.items())
        )
        periods_str = ",".join(manifest.test_periods)
        payload = (
            f"SUI-QUANT-V1-E01-MODEL-BUNDLE|"
            f"model_id={manifest.model_id}|"
            f"version={manifest.version}|"
            f"sha256={manifest.sha256}|"
            f"features_schema_hash={manifest.features_schema_hash}|"
            f"train_end_utc={manifest.train_end_utc.isoformat()}|"
            f"test_periods={periods_str}|"
            f"risk_compatibility={manifest.risk_compatibility}|"
            f"rollback_ref={manifest.rollback_ref}|"
            f"exit_strategy_id={exit_strategy_id.value}|"
            f"exit_strategy_version={exit_strategy_version}|"
            f"signer_id={signer_id}|"
            f"signed_at_utc={signed_at_utc.isoformat()}|"
            f"cal={cal_str}|"
            f"costs={cost_str}"
        )
        return payload.encode("utf-8")


class E01ArtifactPackager:
    """Offline packager and Ed25519 signer for A-02 + C-01 model bundles (§11.2)."""

    @staticmethod
    def package_and_sign(
        model: A02BreakoutCalibratedModel,
        signing_private_key: Ed25519PrivateKey,
        *,
        exit_strategy_id: ExitStrategyId = ExitStrategyId.C1,
        exit_strategy_version: str = "C1-v1.0",
        signer_id: str = "win11-research-workstation",
        train_end_utc: datetime | None = None,
        signed_at_utc: datetime | None = None,
        artifact_output_dir: str | Path | None = None,
    ) -> SignedModelArtifactBundle:
        ref_signed_utc = ensure_utc(signed_at_utc or utc_now(), "signed_at_utc")
        out_path: Path | None = None
        if artifact_output_dir is not None:
            out_dir = Path(artifact_output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{model.model_id}_{model.version}.json"

        manifest = model.export_model_manifest(
            train_end_utc=train_end_utc,
            artifact_output_path=out_path,
        )
        artifact_bytes = model.serialize_artifact_bytes()

        signing_bytes = SignedModelArtifactBundle.canonical_signing_payload(
            manifest=manifest,
            exit_strategy_id=exit_strategy_id,
            exit_strategy_version=exit_strategy_version,
            signer_id=signer_id,
            signed_at_utc=ref_signed_utc,
        )
        sig_hex = signing_private_key.sign(signing_bytes).hex()

        return SignedModelArtifactBundle(
            manifest=manifest,
            artifact_bytes=artifact_bytes,
            exit_strategy_id=exit_strategy_id,
            exit_strategy_version=exit_strategy_version,
            signer_id=signer_id,
            signed_at_utc=ref_signed_utc,
            ed25519_signature_hex=sig_hex,
        )

    @staticmethod
    def deserialize_a02_model(
        artifact_bytes: bytes,
        manifest: ModelManifest,
    ) -> A02BreakoutCalibratedModel:
        """Deserialize and validate an `A02BreakoutCalibratedModel` from verified `artifact_bytes`."""
        try:
            doc = json.loads(artifact_bytes.decode("utf-8"))
        except Exception as exc:
            raise ModelRegistryVerificationError(
                f"ARTIFACT_CORRUPT_JSON: Failed to parse model artifact JSON: {exc}"
            ) from exc

        if not isinstance(doc, Mapping):
            raise ModelRegistryVerificationError("ARTIFACT_CORRUPT_JSON: Root must be a JSON object")

        symbol = str(doc.get("symbol", ""))
        if symbol != TRADING_SYMBOL:
            raise ModelRegistryVerificationError(
                f"SYMBOL_GOVERNANCE_VIOLATION: Model artifact targets '{symbol}', only '{TRADING_SYMBOL}' allowed"
            )

        if str(doc.get("execution_mode_override", "")).upper() == "LIVE" or doc.get("allow_live_trading") is True:
            raise ModelRegistryVerificationError(
                "LIVE_GATE_VIOLATION: Model artifact attempted to enable LIVE mode"
            )

        if "risk_override" in doc or "max_single_trade_risk_pct" in doc:
            raise ModelRegistryVerificationError(
                "RISK_GOVERNANCE_VIOLATION: Model artifact is forbidden from overriding R-01 risk parameters"
            )

        feature_names = tuple(str(x) for x in doc.get("feature_names", ()))
        if not feature_names:
            raise ModelRegistryVerificationError("FEATURES_SCHEMA_MISMATCH: Empty feature_names in artifact")

        raw_costs = doc.get("costs", {})
        costs = A02CostAssumptions(
            buy_fee_rate=Decimal(str(raw_costs.get("buy_fee_rate", "0.0010"))),
            sell_fee_rate=Decimal(str(raw_costs.get("sell_fee_rate", "0.0010"))),
            target_net_win_return=Decimal(str(raw_costs.get("target_net_win_return", "0.0150"))),
            stop_loss_trigger_pct=Decimal(str(raw_costs.get("stop_loss_trigger_pct", "0.0100"))),
            entry_slippage_rate=Decimal(str(raw_costs.get("entry_slippage_rate", "0.0003"))),
            stop_slippage_rate=Decimal(str(raw_costs.get("stop_slippage_rate", "0.0005"))),
            adverse_selection_penalty=Decimal(str(raw_costs.get("adverse_selection_penalty", "0.0004"))),
            expected_timeout_gross_return=Decimal(str(raw_costs.get("expected_timeout_gross_return", "0.0000"))),
            timeout_slippage_rate=Decimal(str(raw_costs.get("timeout_slippage_rate", "0.0002"))),
        )

        intercepts_raw = doc.get("intercepts", [0.15, -0.20, 0.05])
        biases_raw = doc.get("calibration_biases", [0.0, 0.0, 0.0])
        verdict_str = str(doc.get("statistical_verdict", StatisticalVerdict.NO_EDGE.value))

        model = A02BreakoutCalibratedModel(
            model_id=str(doc.get("model_id", manifest.model_id)),
            version=str(doc.get("version", manifest.version)),
            feature_names=feature_names,
            costs=costs,
            risk_compatibility=str(doc.get("risk_compatibility", manifest.risk_compatibility)),
            rollback_ref=str(doc.get("rollback_ref", manifest.rollback_ref)),
            feature_means={str(k): float(v) for k, v in doc.get("feature_means", {}).items()},
            feature_stds={str(k): float(v) for k, v in doc.get("feature_stds", {}).items()},
            weights_win={str(k): float(v) for k, v in doc.get("weights_win", {}).items()},
            weights_stop={str(k): float(v) for k, v in doc.get("weights_stop", {}).items()},
            weights_timeout={str(k): float(v) for k, v in doc.get("weights_timeout", {}).items()},
            intercepts=(float(intercepts_raw[0]), float(intercepts_raw[1]), float(intercepts_raw[2])),
            calibration_temperature=float(doc.get("calibration_temperature", 1.0)),
            calibration_biases=(float(biases_raw[0]), float(biases_raw[1]), float(biases_raw[2])),
            used_l2_fallback=bool(doc.get("used_l2_fallback", False)),
            calibration_metrics=dict(manifest.calibration_metrics),
            statistical_verdict=StatisticalVerdict(verdict_str),
            train_end_utc=manifest.train_end_utc,
            test_periods=manifest.test_periods,
        )
        return model


@dataclass
class InFlightPositionPinState:
    """Tracks the pinned exit strategy version and multi-tranche state for an open position (QA-12)."""

    position_id: str
    pinned_bundle_id: str
    pinned_exit_strategy_id: ExitStrategyId
    pinned_exit_strategy_version: str
    opened_at_utc: datetime
    entry_avg_price: Decimal
    initial_sellable_qty: Decimal
    tranche_plan: TranchePlanResult | None = None
    filled_tranche_indices: list[int] = field(default_factory=list)
    high_water_mark_price: Decimal = Decimal("0.0")
    bars_held: int = 0
    hot_swaps_survived: int = 0


class InFlightVersionPinRegistry:
    """Immutable version pool guaranteeing in-flight positions keep their opening exit strategy version (§11.2, QA-12)."""

    def __init__(
        self,
        *,
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
    ) -> None:
        self.filters = filters
        self._strategies_by_version: dict[str, ExitStrategyInstance] = {}
        self._pinned_positions: dict[str, InFlightPositionPinState] = {}
        self._version_crosstalk_violations = 0

        # Pre-register default v1.0 strategies
        self.register_exit_strategy("C1-v1.0", C1FixedNetTargetExit(filters=filters))
        self.register_exit_strategy("C2-v1.0", C2GridTrailingExit(filters=filters))
        self.register_exit_strategy("C3-v1.0", C3AdaptiveHybridExit(
            c1_delegate=C1FixedNetTargetExit(filters=filters),
            c2_delegate=C2GridTrailingExit(filters=filters),
        ))

    @property
    def version_crosstalk_violations(self) -> int:
        return self._version_crosstalk_violations

    def register_exit_strategy(self, version: str, strategy: ExitStrategyInstance) -> None:
        if not version.strip():
            raise ValueError("Exit strategy version cannot be empty")
        strategy.version = version
        self._strategies_by_version[version] = strategy

    def get_strategy_by_version(self, version: str) -> ExitStrategyInstance:
        if version not in self._strategies_by_version:
            raise KeyError(f"Exit strategy version '{version}' is not registered in InFlightVersionPinRegistry")
        return self._strategies_by_version[version]

    def pin_position(
        self,
        *,
        ledger: PositionLedger,
        bundle_id: str,
        now_utc: datetime | None = None,
    ) -> InFlightPositionPinState:
        """Pin `ledger.position_id` to `ledger.exit_strategy_version` at the moment the position opens."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        strat = self.get_strategy_by_version(ledger.exit_strategy_version)
        sellable_qty = self.filters.quantize_qty_down(ledger.actual_qty)

        plan: TranchePlanResult | None = None
        if isinstance(strat, C2GridTrailingExit) and sellable_qty >= self.filters.min_qty:
            plan = strat.plan_tranches(
                sellable_qty=sellable_qty,
                avg_buy_price=ledger.avg_fill_price,
            )
        elif isinstance(strat, C3AdaptiveHybridExit) and sellable_qty >= self.filters.min_qty:
            plan = strat.c2.plan_tranches(
                sellable_qty=sellable_qty,
                avg_buy_price=ledger.avg_fill_price,
            )

        state = InFlightPositionPinState(
            position_id=ledger.position_id,
            pinned_bundle_id=bundle_id,
            pinned_exit_strategy_id=strat.strategy_id,
            pinned_exit_strategy_version=ledger.exit_strategy_version,
            opened_at_utc=ref_now,
            entry_avg_price=ledger.avg_fill_price,
            initial_sellable_qty=sellable_qty,
            tranche_plan=plan,
            filled_tranche_indices=[],
            high_water_mark_price=ledger.avg_fill_price,
            bars_held=0,
            hot_swaps_survived=0,
        )
        self._pinned_positions[ledger.position_id] = state
        return state

    def notify_hot_swap_or_rollback(self, new_active_exit_version: str) -> None:
        """Record that a model hot-swap or rollback occurred while any position is open, without mutating its pin."""
        for pin in self._pinned_positions.values():
            if pin.pinned_exit_strategy_version != new_active_exit_version:
                pin.hot_swaps_survived += 1

    def resolve_pinned_exit_strategy(
        self,
        ledger: PositionLedger,
    ) -> tuple[ExitStrategyInstance, InFlightPositionPinState]:
        """Resolve the pinned exit strategy for `ledger` and enforce zero version cross-talk (QA-12)."""
        pin = self._pinned_positions.get(ledger.position_id)
        if pin is None:
            pin = self.pin_position(ledger=ledger, bundle_id="RECOVERED_IN_FLIGHT")

        if ledger.exit_strategy_version != pin.pinned_exit_strategy_version:
            self._version_crosstalk_violations += 1
            raise RuntimeError(
                f"QA-12 Version Cross-Talk Violation: Position {ledger.position_id} was pinned to "
                f"'{pin.pinned_exit_strategy_version}', but ledger has '{ledger.exit_strategy_version}'"
            )

        strat = self.get_strategy_by_version(pin.pinned_exit_strategy_version)
        return strat, pin

    def release_position(self, position_id: str) -> InFlightPositionPinState | None:
        """Release pin state only after the position has completely exited to `FLAT`."""
        return self._pinned_positions.pop(position_id, None)

    @property
    def active_pinned_count(self) -> int:
        return len(self._pinned_positions)


@dataclass(frozen=True)
class ShadowInferenceRecord:
    """Telemetry record from evaluating a candidate model in the `SHADOW` slot (§11.2)."""

    bundle_id: str
    feature_time_utc: datetime
    p_win: Decimal
    p_stop: Decimal
    p_timeout: Decimal
    ev_net: Decimal
    confidence: Decimal
    proposal_generated: bool
    latency_ns: int
    healthy: bool
    error_reason: str | None = None


@dataclass(frozen=True)
class RollbackAuditRecord:
    """Immutable audit log of an automatic or manual atomic model rollback (QA-12)."""

    rollback_id: str
    failed_bundle_id: str
    restored_bundle_id: str
    trigger_reason: str
    rollback_latency_ns: int
    occurred_at_utc: datetime
    alert_id: str | None


class E01ModelRegistry:
    """Cloud-side E-01 signed model registry, `SHADOW -> PAPER` promoter, and `AutoRollbackGuard` (§11.2, QA-12)."""

    def __init__(
        self,
        *,
        store: StateStore,
        alert_manager: AlertManager,
        risk_config: RiskConfig,
        pinned_signer_pubkey_hex: str,
        expected_feature_names: tuple[str, ...] = A02_FEATURE_NAMES,
        max_allowed_ece: Decimal = Decimal("0.080000"),
        max_inference_latency_ms: Decimal = Decimal("5.0000"),
        filters: SymbolFilters = P0_SNAPSHOT_SUIUSDT_FILTERS,
    ) -> None:
        if len(pinned_signer_pubkey_hex) != 64:
            raise ValueError("pinned_signer_pubkey_hex must be a 64-character hex Ed25519 public key")
        self.store = store
        self.alert_manager = alert_manager
        self.risk_config = risk_config
        self.pinned_signer_pubkey_hex = pinned_signer_pubkey_hex
        self.expected_feature_names = expected_feature_names
        self.expected_features_schema_hash = compute_features_schema_hash(expected_feature_names)
        self.max_allowed_ece = require_decimal(max_allowed_ece, "max_allowed_ece")
        self.max_inference_latency_ms = require_decimal(max_inference_latency_ms, "max_inference_latency_ms")
        self.pin_registry = InFlightVersionPinRegistry(filters=filters)

        self._init_p6_wal_tables()
        self._verified_bundles: dict[str, SignedModelArtifactBundle] = {}
        self._verified_models: dict[str, A02BreakoutCalibratedModel] = {}
        self._active_paper_bundle_id: str | None = None
        self._shadow_bundle_id: str | None = None
        self._shadow_records: dict[str, list[ShadowInferenceRecord]] = {}
        self._rollback_history: list[RollbackAuditRecord] = []
        self._tampered_models_rejected_count = 0
        self._shadow_orders_submitted_count = 0
        self._runtime_Hooks: dict[str, Callable[[MarketFeatures], None]] = {}

    def _init_p6_wal_tables(self) -> None:
        self.store._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS p6_model_bundles (
                bundle_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                version TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                features_schema_hash TEXT NOT NULL,
                risk_compatibility TEXT NOT NULL,
                rollback_ref TEXT NOT NULL,
                exit_strategy_id TEXT NOT NULL,
                exit_strategy_version TEXT NOT NULL,
                slot_role TEXT NOT NULL,
                signer_id TEXT NOT NULL,
                signature_hex TEXT NOT NULL,
                registered_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS p6_registry_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                bundle_id TEXT NOT NULL,
                target_bundle_id TEXT,
                reason TEXT NOT NULL,
                latency_ns INTEGER NOT NULL,
                occurred_at_utc TEXT NOT NULL
            );
            """
        )

    @property
    def active_paper_bundle(self) -> SignedModelArtifactBundle:
        if self._active_paper_bundle_id is None:
            raise RuntimeError("No ACTIVE_PAPER model bundle is currently mounted")
        return self._verified_bundles[self._active_paper_bundle_id]

    @property
    def active_paper_model(self) -> A02BreakoutCalibratedModel:
        if self._active_paper_bundle_id is None:
            raise RuntimeError("No ACTIVE_PAPER model is currently mounted")
        return self._verified_models[self._active_paper_bundle_id]

    @property
    def active_paper_exit_strategy_version(self) -> str:
        return self.active_paper_bundle.exit_strategy_version

    @property
    def shadow_bundle(self) -> SignedModelArtifactBundle | None:
        if self._shadow_bundle_id is None:
            return None
        return self._verified_bundles.get(self._shadow_bundle_id)

    @property
    def shadow_model(self) -> A02BreakoutCalibratedModel | None:
        if self._shadow_bundle_id is None:
            return None
        return self._verified_models.get(self._shadow_bundle_id)

    @property
    def tampered_models_rejected_count(self) -> int:
        return self._tampered_models_rejected_count

    @property
    def shadow_orders_submitted_count(self) -> int:
        """Strictly 0: SHADOW slot never submits orders (§11.2)."""
        return self._shadow_orders_submitted_count

    @property
    def rollback_history(self) -> tuple[RollbackAuditRecord, ...]:
        return tuple(self._rollback_history)

    def inject_runtime_fault_hook(
        self,
        bundle_id: str,
        hook: Callable[[MarketFeatures], None] | None,
    ) -> None:
        """Fault-injection hook for QA-12 testing: inject runtime exception or latency spike on `bundle_id`."""
        if hook is None:
            self._runtime_Hooks.pop(bundle_id, None)
        else:
            self._runtime_Hooks[bundle_id] = hook

    def verify_bundle(
        self,
        bundle: SignedModelArtifactBundle,
        *,
        require_existing_rollback_target: bool = True,
    ) -> A02BreakoutCalibratedModel:
        """Execute 6-layer cryptographic, schema, risk, calibration, and rollback verification (QA-12)."""
        try:
            # 1. Verify SHA-256 of artifact_bytes against manifest.sha256
            actual_sha256 = hashlib.sha256(bundle.artifact_bytes).hexdigest()
            if actual_sha256 != bundle.manifest.sha256:
                raise ModelRegistryVerificationError(
                    f"ARTIFACT_SHA256_TAMPERED: manifest.sha256 ({bundle.manifest.sha256}) != "
                    f"actual artifact_bytes sha256 ({actual_sha256})"
                )

            # 2. Verify Ed25519 signature against pinned research signer public key
            try:
                pub_bytes = bytes.fromhex(self.pinned_signer_pubkey_hex)
                sig_bytes = bytes.fromhex(bundle.ed25519_signature_hex)
                pubkey = Ed25519PublicKey.from_public_bytes(pub_bytes)
            except ValueError as exc:
                raise ModelRegistryVerificationError(f"SIGNATURE_HEX_MALFORMED: {exc}") from exc

            payload_bytes = SignedModelArtifactBundle.canonical_signing_payload(
                manifest=bundle.manifest,
                exit_strategy_id=bundle.exit_strategy_id,
                exit_strategy_version=bundle.exit_strategy_version,
                signer_id=bundle.signer_id,
                signed_at_utc=bundle.signed_at_utc,
            )
            try:
                pubkey.verify(sig_bytes, payload_bytes)
            except InvalidSignature as exc:
                raise ModelRegistryVerificationError(
                    "SIGNATURE_INVALID: Ed25519 signature verification failed for model bundle"
                ) from exc

            # 3. Verify features_schema_hash against active FeaturePipeline / A02 schema
            if bundle.manifest.features_schema_hash != self.expected_features_schema_hash:
                raise ModelRegistryVerificationError(
                    f"FEATURES_SCHEMA_MISMATCH: manifest schema hash '{bundle.manifest.features_schema_hash}' "
                    f"!= expected '{self.expected_features_schema_hash}'"
                )

            # 4. Verify risk_compatibility against active R-01 RiskConfig.version
            if bundle.manifest.risk_compatibility != self.risk_config.version:
                raise ModelRegistryVerificationError(
                    f"RISK_COMPATIBILITY_MISMATCH: bundle requires risk '{bundle.manifest.risk_compatibility}', "
                    f"active R-01 is '{self.risk_config.version}'"
                )

            # 5. Verify out-of-sample calibration metrics gates
            ece = bundle.manifest.calibration_metrics.get("ece", Decimal("1"))
            brier = bundle.manifest.calibration_metrics.get("brier_score", Decimal("1"))
            base_brier = bundle.manifest.calibration_metrics.get("baseline_brier_score", Decimal("0"))
            if ece > self.max_allowed_ece:
                raise ModelRegistryVerificationError(
                    f"CALIBRATION_GATE_FAILED: ECE ({ece}) > max_allowed_ece ({self.max_allowed_ece})"
                )
            if brier > base_brier:
                raise ModelRegistryVerificationError(
                    f"CALIBRATION_GATE_FAILED: Brier ({brier}) > baseline_brier ({base_brier})"
                )

            # 6. Verify rollback_ref existence if required
            if require_existing_rollback_target:
                if bundle.manifest.rollback_ref not in self._verified_bundles:
                    raise ModelRegistryVerificationError(
                        f"MISSING_ROLLBACK_TARGET: rollback_ref '{bundle.manifest.rollback_ref}' "
                        f"is not registered in E01ModelRegistry"
                    )

            # 7. Deserialize and verify embedded model parameters & governance constraints
            model = E01ArtifactPackager.deserialize_a02_model(
                artifact_bytes=bundle.artifact_bytes,
                manifest=bundle.manifest,
            )
            if compute_features_schema_hash(model.feature_names) != bundle.manifest.features_schema_hash:
                raise ModelRegistryVerificationError(
                    "FEATURES_SCHEMA_MISMATCH: Deserialized model feature_names do not match manifest hash"
                )
            if model.costs.stop_loss_trigger_pct != self.risk_config.stop_loss_trigger_pct:
                raise ModelRegistryVerificationError(
                    f"RISK_GOVERNANCE_VIOLATION: Model stop_loss_trigger_pct ({model.costs.stop_loss_trigger_pct}) "
                    f"!= R-01 ({self.risk_config.stop_loss_trigger_pct})"
                )
            return model
        except ModelRegistryVerificationError:
            self._tampered_models_rejected_count += 1
            raise

    def _persist_bundle_record(
        self,
        bundle: SignedModelArtifactBundle,
        role: ModelSlotRole,
        now_utc: datetime,
    ) -> None:
        self.store._conn.execute(
            """
            INSERT INTO p6_model_bundles (
                bundle_id, model_id, version, sha256, features_schema_hash,
                risk_compatibility, rollback_ref, exit_strategy_id, exit_strategy_version,
                slot_role, signer_id, signature_hex, registered_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bundle_id) DO UPDATE SET
                slot_role=excluded.slot_role
            """,
            (
                bundle.bundle_id,
                bundle.manifest.model_id,
                bundle.manifest.version,
                bundle.manifest.sha256,
                bundle.manifest.features_schema_hash,
                bundle.manifest.risk_compatibility,
                bundle.manifest.rollback_ref,
                bundle.exit_strategy_id.value,
                bundle.exit_strategy_version,
                role.value,
                bundle.signer_id,
                bundle.ed25519_signature_hex,
                now_utc.isoformat(),
            ),
        )

    def _record_registry_event(
        self,
        event_type: str,
        bundle_id: str,
        target_bundle_id: str | None,
        reason: str,
        latency_ns: int,
        now_utc: datetime,
    ) -> None:
        self.store._conn.execute(
            """
            INSERT INTO p6_registry_events (
                event_type, bundle_id, target_bundle_id, reason, latency_ns, occurred_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                bundle_id,
                target_bundle_id,
                reason,
                latency_ns,
                now_utc.isoformat(),
            ),
        )

    def bootstrap_initial_active_bundle(
        self,
        bundle: SignedModelArtifactBundle,
        *,
        exit_strategy: ExitStrategyInstance | None = None,
        now_utc: datetime | None = None,
    ) -> None:
        """Mount the genesis verified baseline model bundle into `ACTIVE_PAPER`."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        model = self.verify_bundle(bundle, require_existing_rollback_target=False)

        if exit_strategy is not None:
            self.pin_registry.register_exit_strategy(bundle.exit_strategy_version, exit_strategy)
        else:
            self._ensure_default_exit_strategy_registered(bundle)

        self._verified_bundles[bundle.bundle_id] = bundle
        self._verified_models[bundle.bundle_id] = model
        self._active_paper_bundle_id = bundle.bundle_id

        self._persist_bundle_record(bundle, ModelSlotRole.ACTIVE_PAPER, ref_now)
        self._record_registry_event(
            event_type="BOOTSTRAP_ACTIVE_PAPER",
            bundle_id=bundle.bundle_id,
            target_bundle_id=None,
            reason=f"Genesis bundle mounted with exit={bundle.exit_strategy_version}",
            latency_ns=monotonic_ns() - t0_ns,
            now_utc=ref_now,
        )

    def _ensure_default_exit_strategy_registered(self, bundle: SignedModelArtifactBundle) -> None:
        try:
            self.pin_registry.get_strategy_by_version(bundle.exit_strategy_version)
        except KeyError:
            if bundle.exit_strategy_id == ExitStrategyId.C1:
                strat: ExitStrategyInstance = C1FixedNetTargetExit(filters=self.pin_registry.filters)
            elif bundle.exit_strategy_id == ExitStrategyId.C2:
                strat = C2GridTrailingExit(filters=self.pin_registry.filters)
            else:
                strat = C3AdaptiveHybridExit(
                    c1_delegate=C1FixedNetTargetExit(filters=self.pin_registry.filters),
                    c2_delegate=C2GridTrailingExit(filters=self.pin_registry.filters),
                )
            self.pin_registry.register_exit_strategy(bundle.exit_strategy_version, strat)

    def stage_bundle_to_shadow(
        self,
        bundle: SignedModelArtifactBundle,
        *,
        exit_strategy: ExitStrategyInstance | None = None,
        now_utc: datetime | None = None,
    ) -> None:
        """Verify a candidate model bundle (including its `rollback_ref`) and mount it in the `SHADOW` slot."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        model = self.verify_bundle(bundle, require_existing_rollback_target=True)

        if exit_strategy is not None:
            self.pin_registry.register_exit_strategy(bundle.exit_strategy_version, exit_strategy)
        else:
            self._ensure_default_exit_strategy_registered(bundle)

        self._verified_bundles[bundle.bundle_id] = bundle
        self._verified_models[bundle.bundle_id] = model
        self._shadow_bundle_id = bundle.bundle_id
        self._shadow_records[bundle.bundle_id] = []

        self._persist_bundle_record(bundle, ModelSlotRole.SHADOW, ref_now)
        self._record_registry_event(
            event_type="STAGE_TO_SHADOW",
            bundle_id=bundle.bundle_id,
            target_bundle_id=bundle.manifest.rollback_ref,
            reason=f"Staged in SHADOW slot (rollback_ref={bundle.manifest.rollback_ref})",
            latency_ns=monotonic_ns() - t0_ns,
            now_utc=ref_now,
        )

    def evaluate_shadow_inference(
        self,
        sui_features: MarketFeatures,
        regime_decision: RegimeDecision,
    ) -> ShadowInferenceRecord | None:
        """Run parallel `SHADOW` inference on `sui_features` with zero order execution authority (§11.2)."""
        if self._shadow_bundle_id is None:
            return None
        bundle_id = self._shadow_bundle_id
        model = self._verified_models[bundle_id]
        t0_ns = monotonic_ns()
        try:
            hook = self._runtime_Hooks.get(bundle_id)
            if hook is not None:
                hook(sui_features)

            p_win, p_stop, p_timeout, conf = model.predict_probabilities(sui_features)
            ev_net = model.costs.compute_ev_net(p_win, p_stop, p_timeout)
            proposal = model.evaluate_breakout_proposal(
                sui_features=sui_features,
                regime_decision=regime_decision,
                require_candidate_filter=True,
                require_positive_ev=False,
            )
            lat_ns = monotonic_ns() - t0_ns
            max_lat_ns = int(self.max_inference_latency_ms * Decimal("1000000"))
            healthy = lat_ns <= max_lat_ns
            err = None if healthy else f"SHADOW_LATENCY_EXCEEDED({lat_ns / 1e6:.3f}ms)"
            rec = ShadowInferenceRecord(
                bundle_id=bundle_id,
                feature_time_utc=sui_features.feature_time_utc,
                p_win=p_win,
                p_stop=p_stop,
                p_timeout=p_timeout,
                ev_net=ev_net,
                confidence=conf,
                proposal_generated=(proposal is not None),
                latency_ns=lat_ns,
                healthy=healthy,
                error_reason=err,
            )
        except Exception as exc:
            lat_ns = monotonic_ns() - t0_ns
            rec = ShadowInferenceRecord(
                bundle_id=bundle_id,
                feature_time_utc=sui_features.feature_time_utc,
                p_win=Decimal("0"),
                p_stop=Decimal("0"),
                p_timeout=Decimal("1"),
                ev_net=Decimal("0"),
                confidence=Decimal("0"),
                proposal_generated=False,
                latency_ns=lat_ns,
                healthy=False,
                error_reason=f"SHADOW_INFERENCE_ERROR:{exc}",
            )

        self._shadow_records.setdefault(bundle_id, []).append(rec)
        return rec

    def promote_shadow_to_paper(
        self,
        *,
        min_shadow_evaluations: int = 1,
        now_utc: datetime | None = None,
    ) -> tuple[str, int]:
        """Atomically promote the verified `SHADOW` bundle to `ACTIVE_PAPER` after passing shadow gates (QA-12).

        In-flight positions already open in `InFlightVersionPinRegistry` remain strictly locked to their
        original `exit_strategy_version` until they close to `FLAT`.
        """
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if self._shadow_bundle_id is None:
            raise ModelRegistryVerificationError("PROMOTION_BLOCKED: No bundle currently staged in SHADOW slot")

        cand_id = self._shadow_bundle_id
        cand_bundle = self._verified_bundles[cand_id]
        records = self._shadow_records.get(cand_id, [])

        if len(records) < min_shadow_evaluations:
            raise ModelRegistryVerificationError(
                f"PROMOTION_BLOCKED: SHADOW bundle '{cand_id}' has only {len(records)} evaluations "
                f"(minimum {min_shadow_evaluations} required)"
            )

        unhealthy = [r for r in records if not r.healthy]
        if unhealthy:
            raise ModelRegistryVerificationError(
                f"PROMOTION_BLOCKED: SHADOW bundle '{cand_id}' had {len(unhealthy)} unhealthy evaluations: "
                f"{unhealthy[0].error_reason}"
            )

        if cand_bundle.manifest.rollback_ref not in self._verified_bundles:
            raise ModelRegistryVerificationError(
                f"PROMOTION_BLOCKED: rollback_ref '{cand_bundle.manifest.rollback_ref}' not in verified registry"
            )

        prev_active_id = self._active_paper_bundle_id
        if prev_active_id is not None:
            prev_bundle = self._verified_bundles[prev_active_id]
            self._persist_bundle_record(prev_bundle, ModelSlotRole.REGISTERED_STANDBY, ref_now)

        # Atomic pointer swap
        self._active_paper_bundle_id = cand_id
        self._shadow_bundle_id = None
        self.pin_registry.notify_hot_swap_or_rollback(cand_bundle.exit_strategy_version)

        self._persist_bundle_record(cand_bundle, ModelSlotRole.ACTIVE_PAPER, ref_now)
        lat_ns = monotonic_ns() - t0_ns
        self._record_registry_event(
            event_type="PROMOTE_SHADOW_TO_PAPER",
            bundle_id=cand_id,
            target_bundle_id=prev_active_id,
            reason=f"Promoted {cand_id} (exit={cand_bundle.exit_strategy_version}) after {len(records)} healthy shadow runs",
            latency_ns=lat_ns,
            now_utc=ref_now,
        )
        return cand_id, lat_ns

    def trigger_atomic_rollback(
        self,
        reason: str,
        *,
        now_utc: datetime | None = None,
    ) -> RollbackAuditRecord:
        """Atomically revert `ACTIVE_PAPER` to `active_paper_bundle.manifest.rollback_ref` in `< 5.0 ms` (QA-12)."""
        t0_ns = monotonic_ns()
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if self._active_paper_bundle_id is None:
            raise RuntimeError("Cannot rollback when no ACTIVE_PAPER bundle is mounted")

        failed_id = self._active_paper_bundle_id
        failed_bundle = self._verified_bundles[failed_id]
        target_id = failed_bundle.manifest.rollback_ref

        if target_id not in self._verified_bundles or target_id == failed_id:
            raise ModelRegistryVerificationError(
                f"ROLLBACK_TARGET_UNAVAILABLE: rollback_ref '{target_id}' is not available for atomic rollback"
            )

        restored_bundle = self._verified_bundles[target_id]

        # Atomic pointer switch back to rollback_ref
        self._active_paper_bundle_id = target_id
        self.pin_registry.notify_hot_swap_or_rollback(restored_bundle.exit_strategy_version)

        self._persist_bundle_record(failed_bundle, ModelSlotRole.ROLLED_BACK, ref_now)
        self._persist_bundle_record(restored_bundle, ModelSlotRole.ACTIVE_PAPER, ref_now)

        lat_ns = monotonic_ns() - t0_ns
        alert_res = self.alert_manager.emit_alert(
            severity=AlertSeverity.HIGH,
            source="E-01",
            dedup_key=f"E01-AUTO-ROLLBACK-{failed_id}",
            actions_taken=("ATOMIC_MODEL_ROLLBACK", f"RESTORED_{target_id}"),
            details={
                "failed_bundle_id": failed_id,
                "restored_bundle_id": target_id,
                "restored_exit_strategy_version": restored_bundle.exit_strategy_version,
                "trigger_reason": reason,
                "rollback_latency_ms": round(lat_ns / 1e6, 4),
            },
            now_utc=ref_now,
        )

        self._record_registry_event(
            event_type="ATOMIC_AUTO_ROLLBACK",
            bundle_id=failed_id,
            target_bundle_id=target_id,
            reason=reason,
            latency_ns=lat_ns,
            now_utc=ref_now,
        )

        rec = RollbackAuditRecord(
            rollback_id=f"RB-{uuid.uuid4().hex[:10]}",
            failed_bundle_id=failed_id,
            restored_bundle_id=target_id,
            trigger_reason=reason,
            rollback_latency_ns=lat_ns,
            occurred_at_utc=ref_now,
            alert_id=alert_res.alert_event.alert_id,
        )
        self._rollback_history.append(rec)
        return rec

    def evaluate_active_paper_proposal(
        self,
        sui_features: MarketFeatures,
        regime_decision: RegimeDecision,
        *,
        require_candidate_filter: bool = True,
        require_positive_ev: bool = False,
        now_utc: datetime | None = None,
    ) -> tuple[BreakoutProposal | None, int, RollbackAuditRecord | None]:
        """Evaluate the `ACTIVE_PAPER` model under `AutoRollbackGuard` protection (§11.2, QA-12).

        If the active model raises a runtime error, fails feature schema check, or exceeds
        `max_inference_latency_ms`, `AutoRollbackGuard` immediately triggers an atomic rollback to
        `rollback_ref`, emits a `HIGH` alert, and seamlessly evaluates using the restored stable model.
        """
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        active_id = self.active_paper_bundle.bundle_id
        model = self.active_paper_model
        t0_ns = monotonic_ns()

        try:
            hook = self._runtime_Hooks.get(active_id)
            if hook is not None:
                hook(sui_features)

            missing = [k for k in model.feature_names if k not in sui_features.features]
            if missing:
                raise ValueError(f"MISSING_REQUIRED_FEATURES:{','.join(missing[:3])}")

            proposal = model.evaluate_breakout_proposal(
                sui_features=sui_features,
                regime_decision=regime_decision,
                require_candidate_filter=require_candidate_filter,
                require_positive_ev=require_positive_ev,
            )
            lat_ns = monotonic_ns() - t0_ns
            max_lat_ns = int(self.max_inference_latency_ms * Decimal("1000000"))
            if lat_ns > max_lat_ns:
                raise TimeoutError(
                    f"INFERENCE_LATENCY_SLO_BREACH: {lat_ns / 1e6:.3f}ms > {self.max_inference_latency_ms}ms"
                )
            return proposal, lat_ns, None
        except Exception as exc:
            rb_rec = self.trigger_atomic_rollback(
                reason=f"AUTO_ROLLBACK_ON_ACTIVE_FAILURE:{type(exc).__name__}:{exc}",
                now_utc=ref_now,
            )
            # Re-evaluate immediately using the restored stable model
            t1_ns = monotonic_ns()
            fallback_model = self.active_paper_model
            fallback_proposal = fallback_model.evaluate_breakout_proposal(
                sui_features=sui_features,
                regime_decision=regime_decision,
                require_candidate_filter=require_candidate_filter,
                require_positive_ev=require_positive_ev,
            )
            return fallback_proposal, monotonic_ns() - t1_ns, rb_rec
