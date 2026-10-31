"""Risk boundary configuration and RESEARCH/SHADOW/PAPER/LIVE environment isolation gates (§0.1, §8.1, §16).

Task A.5 Trust-Boundary Hardening:
1. Trusted Approver Public Key Isolation: `from_untrusted_mapping` strictly forbids `raw_config` or
   `env_vars` from supplying or overriding `pinned_approver_pubkey_hex`, `consumed_token_ids`, or
   `code_tree_sha256`. The trust-root public key must come only from the immutable code constant
   `IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX` (default `None` = LIVE locked).
2. Persistent On-Disk Replay Protection (`PersistentNonceRegistry`): Consumed `token_id` nonces are
   atomically persisted to a local SQLite WAL table (`consumed_live_tokens`) on disk during
   `verify_live_gate()`. Process restarts or config reloads cannot replay a previously used token.
3. Runtime Trusted Build Binding (`compute_runtime_code_tree_sha256`): Instead of trusting a build hash
   string inside a config file, `verify_live_gate()` hashes the actual `src/sui_quant/**/*.py` source tree
   on disk at verification time and matches it against the Ed25519-signed `code_tree_sha256` in the token.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sui_quant.contracts import REFERENCE_SYMBOL, TRADING_SYMBOL, require_decimal
from sui_quant.utils.clock import ensure_utc, utc_now


class RiskBoundaryViolationError(ValueError):
    """Raised when a configuration attempts to relax hard risk or asset boundaries."""


class LiveAuthorizationError(PermissionError):
    """Raised when attempting to initialize or switch to LIVE mode without full cryptographic authorization."""


class ExecutionMode(str, Enum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


# Hard ceilings that cannot be relaxed without explicit user code-level re-authorization (§8.1)
HARD_STOP_LOSS_TRIGGER_PCT = Decimal("0.01")          # -1% price trigger relative to weighted entry price
HARD_MAX_SINGLE_TRADE_RISK_PCT = Decimal("0.0025")    # <= 0.25% account equity risk per trade
HARD_MAX_CAPITAL_ALLOCATION_PCT = Decimal("0.10")     # <= 10% account equity per position
HARD_MAX_ROLLING_24H_LOSS_PCT = Decimal("0.01")       # 1.0% rolling 24h equity loss freeze threshold
HARD_C1_NET_PROFIT_TARGET_PCT = Decimal("0.015")      # +1.5% net profit target for C1 baseline
MAX_LIVE_TOKEN_TTL = timedelta(days=7)                # Maximum validity window for a single LIVE token

# Immutable code-level trust root (Default None = LIVE permanently locked until investor pins public key in code)
IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX: str | None = None

# Keys that an untrusted configuration file/dict is NEVER allowed to supply or override
FORBIDDEN_UNTRUSTED_CONFIG_KEYS = frozenset(
    {
        "pinned_approver_pubkey_hex",
        "trusted_approver_pubkey_hex",
        "consumed_token_ids",
        "audited_git_commit_sha",
        "runtime_code_tree_sha256",
        "nonce_registry",
    }
)


def compute_runtime_code_tree_sha256(package_dir: Path | None = None) -> str:
    """Compute deterministic SHA-256 over all `.py` files in `src/sui_quant/` on disk at runtime.

    Guarantees that if any Python source file is modified after token signing, the runtime code hash
    will diverge from the signed `code_tree_sha256` inside `LiveAuthorizationToken`.
    """
    root = package_dir if package_dir is not None else Path(__file__).resolve().parent
    hasher = hashlib.sha256()
    py_files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    if not py_files:
        raise LiveAuthorizationError(f"No Python source files found under {root} for build verification")
    for file_path in py_files:
        rel_posix = file_path.relative_to(root).as_posix()
        file_digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        hasher.update(f"{rel_posix}:{file_digest}\n".encode("utf-8"))
    return hasher.hexdigest()


class PersistentNonceRegistry:
    """SQLite WAL-backed persistent anti-replay store for consumed `LiveAuthorizationToken` IDs.

    Survives process restarts and prevents a captured or reloaded `LiveAuthorizationToken` from
    being activated more than once.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consumed_live_tokens (
                    token_id TEXT PRIMARY KEY,
                    approver_id TEXT NOT NULL,
                    code_tree_sha256 TEXT NOT NULL,
                    consumed_at_utc TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    def is_consumed(self, token_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM consumed_live_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def consume_atomically(
        self,
        token_id: str,
        approver_id: str,
        code_tree_sha256: str,
        consumed_at_utc: datetime,
    ) -> None:
        """Atomically record `token_id` as consumed; raises `LiveAuthorizationError` on replay."""
        ensure_utc(consumed_at_utc, "consumed_at_utc")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT consumed_at_utc FROM consumed_live_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                raise LiveAuthorizationError(
                    f"Persistent anti-replay block: token_id '{token_id}' was already consumed at {row[0]}"
                )
            conn.execute(
                """
                INSERT INTO consumed_live_tokens (token_id, approver_id, code_tree_sha256, consumed_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (token_id, approver_id, code_tree_sha256, consumed_at_utc.isoformat()),
            )
            conn.execute("COMMIT")
        except LiveAuthorizationError:
            raise
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise LiveAuthorizationError(f"Failed to persist nonce to registry: {exc}") from exc
        finally:
            conn.close()


@dataclass(frozen=True)
class RiskConfig:
    """Immutable R-01 risk configuration enforcing non-relaxable governance boundaries."""

    version: str = "1.0.0-candidate"
    trading_symbol: str = TRADING_SYMBOL
    reference_symbol: str = REFERENCE_SYMBOL
    spot_only: bool = True
    allow_short: bool = False
    allow_leverage: bool = False
    allow_margin_or_loan: bool = False
    allow_averaging_down: bool = False
    stop_loss_trigger_pct: Decimal = HARD_STOP_LOSS_TRIGGER_PCT
    max_single_trade_risk_pct: Decimal = HARD_MAX_SINGLE_TRADE_RISK_PCT
    max_capital_allocation_pct: Decimal = HARD_MAX_CAPITAL_ALLOCATION_PCT
    max_rolling_24h_loss_pct: Decimal = HARD_MAX_ROLLING_24H_LOSS_PCT
    c1_net_profit_target_pct: Decimal = HARD_C1_NET_PROFIT_TARGET_PCT
    max_orders_per_10s: int = 10
    max_daily_entries: int = 12

    def __post_init__(self) -> None:
        if self.trading_symbol != TRADING_SYMBOL:
            raise RiskBoundaryViolationError(
                f"Only '{TRADING_SYMBOL}' is permitted as trading_symbol, got '{self.trading_symbol}'"
            )
        if self.reference_symbol != REFERENCE_SYMBOL:
            raise RiskBoundaryViolationError(
                f"Only '{REFERENCE_SYMBOL}' is permitted as reference_symbol, got '{self.reference_symbol}'"
            )
        if (
            not isinstance(self.spot_only, bool)
            or not isinstance(self.allow_short, bool)
            or not isinstance(self.allow_leverage, bool)
            or not isinstance(self.allow_margin_or_loan, bool)
            or not isinstance(self.allow_averaging_down, bool)
        ):
            raise RiskBoundaryViolationError("Boolean risk flags must be strict bool instances")
        if (
            not self.spot_only
            or self.allow_short
            or self.allow_leverage
            or self.allow_margin_or_loan
            or self.allow_averaging_down
        ):
            raise RiskBoundaryViolationError(
                "Spot-only upward breakout invariant violated: shorting, leverage, margin, loans, and averaging down are strictly forbidden."
            )

        sl = require_decimal(self.stop_loss_trigger_pct, "stop_loss_trigger_pct")
        sr = require_decimal(self.max_single_trade_risk_pct, "max_single_trade_risk_pct")
        ca = require_decimal(self.max_capital_allocation_pct, "max_capital_allocation_pct")
        dl = require_decimal(self.max_rolling_24h_loss_pct, "max_rolling_24h_loss_pct")
        c1 = require_decimal(self.c1_net_profit_target_pct, "c1_net_profit_target_pct")

        if sl <= Decimal("0") or sl > HARD_STOP_LOSS_TRIGGER_PCT:
            raise RiskBoundaryViolationError(
                f"stop_loss_trigger_pct ({sl}) cannot exceed hard ceiling {HARD_STOP_LOSS_TRIGGER_PCT} (1%)"
            )
        if sr <= Decimal("0") or sr > HARD_MAX_SINGLE_TRADE_RISK_PCT:
            raise RiskBoundaryViolationError(
                f"max_single_trade_risk_pct ({sr}) cannot exceed hard ceiling {HARD_MAX_SINGLE_TRADE_RISK_PCT} (0.25%)"
            )
        if ca <= Decimal("0") or ca > HARD_MAX_CAPITAL_ALLOCATION_PCT:
            raise RiskBoundaryViolationError(
                f"max_capital_allocation_pct ({ca}) cannot exceed hard ceiling {HARD_MAX_CAPITAL_ALLOCATION_PCT} (10%)"
            )
        if dl <= Decimal("0") or dl > HARD_MAX_ROLLING_24H_LOSS_PCT:
            raise RiskBoundaryViolationError(
                f"max_rolling_24h_loss_pct ({dl}) cannot exceed hard ceiling {HARD_MAX_ROLLING_24H_LOSS_PCT} (1%)"
            )
        if c1 <= Decimal("0"):
            raise RiskBoundaryViolationError("c1_net_profit_target_pct must be > 0")
        if not isinstance(self.max_orders_per_10s, int) or isinstance(self.max_orders_per_10s, bool):
            raise RiskBoundaryViolationError("max_orders_per_10s must be an integer")
        if self.max_orders_per_10s <= 0 or self.max_orders_per_10s > 50:
            raise RiskBoundaryViolationError("max_orders_per_10s must be in [1, 50]")
        if not isinstance(self.max_daily_entries, int) or isinstance(self.max_daily_entries, bool):
            raise RiskBoundaryViolationError("max_daily_entries must be an integer")
        if self.max_daily_entries <= 0 or self.max_daily_entries > 100:
            raise RiskBoundaryViolationError("max_daily_entries must be in [1, 100]")

    def canonical_sha256(self) -> str:
        """Compute deterministic SHA-256 fingerprint of all risk boundary parameters."""
        canonical = (
            f"RiskConfig|v={self.version}|sym={self.trading_symbol}|ref={self.reference_symbol}|"
            f"spot={self.spot_only}|short={self.allow_short}|lev={self.allow_leverage}|"
            f"margin={self.allow_margin_or_loan}|avgdown={self.allow_averaging_down}|"
            f"sl={self.stop_loss_trigger_pct.normalize()}|"
            f"sr={self.max_single_trade_risk_pct.normalize()}|"
            f"ca={self.max_capital_allocation_pct.normalize()}|"
            f"dl={self.max_rolling_24h_loss_pct.normalize()}|"
            f"c1={self.c1_net_profit_target_pct.normalize()}|"
            f"ord10s={self.max_orders_per_10s}|daily={self.max_daily_entries}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GoNoGoChecklist:
    """Section 16 non-bypassable 7-point GO/NO-GO gate before any real funds can be used."""

    account_spot_suiusdt_verified: bool = False
    protection_orders_fault_proven: bool = False
    user_risk_limits_explicitly_approved: bool = False
    anomaly_freeze_and_reconciliation_proven: bool = False
    out_of_sample_net_positive_proven: bool = False
    security_and_external_heartbeat_proven: bool = False
    independent_audit_record_signed: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "account_spot_suiusdt_verified",
            "protection_orders_fault_proven",
            "user_risk_limits_explicitly_approved",
            "anomaly_freeze_and_reconciliation_proven",
            "out_of_sample_net_positive_proven",
            "security_and_external_heartbeat_proven",
            "independent_audit_record_signed",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"GoNoGoChecklist.{field_name} must be a strict bool")

    @property
    def all_passed(self) -> bool:
        return (
            self.account_spot_suiusdt_verified
            and self.protection_orders_fault_proven
            and self.user_risk_limits_explicitly_approved
            and self.anomaly_freeze_and_reconciliation_proven
            and self.out_of_sample_net_positive_proven
            and self.security_and_external_heartbeat_proven
            and self.independent_audit_record_signed
        )


@dataclass(frozen=True)
class LiveAuthorizationToken:
    """Cryptographically signed user authorization artifact required to unlock LIVE mode (§16)."""

    token_id: str
    approver_id: str
    authorized_symbol: str
    max_capital_usdt: Decimal
    max_single_trade_risk_pct: Decimal
    max_rolling_24h_loss_pct: Decimal
    risk_config_version: str
    risk_config_sha256: str
    git_commit_sha: str
    issued_at_utc: datetime
    expires_at_utc: datetime
    ed25519_signature_hex: str

    @staticmethod
    def canonical_Signing_payload(
        token_id: str,
        approver_id: str,
        authorized_symbol: str,
        max_capital_usdt: Decimal,
        max_single_trade_risk_pct: Decimal,
        max_rolling_24h_loss_pct: Decimal,
        risk_config_version: str,
        risk_config_sha256: str,
        git_commit_sha: str,
        issued_at_utc: datetime,
        expires_at_utc: datetime,
    ) -> bytes:
        ensure_utc(issued_at_utc, "issued_at_utc")
        ensure_utc(expires_at_utc, "expires_at_utc")
        cap = require_decimal(max_capital_usdt, "max_capital_usdt")
        sr = require_decimal(max_single_trade_risk_pct, "max_single_trade_risk_pct")
        dl = require_decimal(max_rolling_24h_loss_pct, "max_rolling_24h_loss_pct")
        msg = (
            f"SUI-QUANT-V1-LIVE-AUTH|"
            f"token_id={token_id}|"
            f"approver_id={approver_id}|"
            f"symbol={authorized_symbol}|"
            f"max_capital_usdt={cap.normalize()}|"
            f"max_single_trade_risk_pct={sr.normalize()}|"
            f"max_rolling_24h_loss_pct={dl.normalize()}|"
            f"risk_version={risk_config_version}|"
            f"risk_sha256={risk_config_sha256}|"
            f"git_sha={git_commit_sha}|"
            f"issued={issued_at_utc.isoformat()}|"
            f"expires={expires_at_utc.isoformat()}"
        )
        return msg.encode("utf-8")

    def validate(
        self,
        expected_risk: RiskConfig,
        pinned_approver_pubkey_hex: str | None,
        consumed_token_ids: Sequence[str] = (),
        nonce_registry: PersistentNonceRegistry | None = None,
        expected_git_commit_sha: str | None = None,
        verify_runtime_code_tree: bool = False,
        code_package_dir: Path | None = None,
        now_utc: datetime | None = None,
    ) -> None:
        if not pinned_approver_pubkey_hex or len(pinned_approver_pubkey_hex) != 64:
            raise LiveAuthorizationError(
                "LIVE mode blocked: No valid 32-byte Ed25519 approver public key is pinned in trust root."
            )
        if not self.token_id.strip() or len(self.token_id.strip()) < 8:
            raise LiveAuthorizationError("token_id (nonce) must be at least 8 characters")
        if self.token_id in consumed_token_ids:
            raise LiveAuthorizationError(
                f"Replay attack or duplicate token activation blocked: token_id '{self.token_id}' was already consumed."
            )
        if nonce_registry is not None and nonce_registry.is_consumed(self.token_id):
            raise LiveAuthorizationError(
                f"Persistent anti-replay block: token_id '{self.token_id}' is already recorded in on-disk registry."
            )
        if not self.approver_id.strip():
            raise LiveAuthorizationError("approver_id cannot be empty")
        if self.authorized_symbol != TRADING_SYMBOL:
            raise LiveAuthorizationError(
                f"LiveAuthorizationToken only valid for {TRADING_SYMBOL}, got {self.authorized_symbol}"
            )

        if self.risk_config_version != expected_risk.version:
            raise LiveAuthorizationError(
                f"Token risk_config_version '{self.risk_config_version}' does not match '{expected_risk.version}'"
            )
        actual_risk_sha = expected_risk.canonical_sha256()
        if self.risk_config_sha256 != actual_risk_sha:
            raise LiveAuthorizationError(
                "RiskConfig parameter tampering detected: risk_config_sha256 does not match active RiskConfig!"
            )

        cap = require_decimal(self.max_capital_usdt, "max_capital_usdt")
        sr = require_decimal(self.max_single_trade_risk_pct, "max_single_trade_risk_pct")
        dl = require_decimal(self.max_rolling_24h_loss_pct, "max_rolling_24h_loss_pct")
        if cap <= Decimal("0"):
            raise LiveAuthorizationError("max_capital_usdt must be > 0")
        if sr <= Decimal("0") or sr > expected_risk.max_single_trade_risk_pct:
            raise LiveAuthorizationError(
                f"Token max_single_trade_risk_pct ({sr}) exceeds RiskConfig ({expected_risk.max_single_trade_risk_pct})"
            )
        if dl <= Decimal("0") or dl > expected_risk.max_rolling_24h_loss_pct:
            raise LiveAuthorizationError(
                f"Token max_rolling_24h_loss_pct ({dl}) exceeds RiskConfig ({expected_risk.max_rolling_24h_loss_pct})"
            )

        if not self.git_commit_sha.strip() or len(self.git_commit_sha.strip()) < 7:
            raise LiveAuthorizationError("git_commit_sha must be a valid commit/tree hash")

        if verify_runtime_code_tree:
            live_tree_sha = compute_runtime_code_tree_sha256(code_package_dir)
            if self.git_commit_sha != live_tree_sha:
                raise LiveAuthorizationError(
                    f"Runtime build binding failed: token build hash '{self.git_commit_sha}' "
                    f"does not match on-disk code tree SHA-256 '{live_tree_sha}'"
                )
        elif expected_git_commit_sha is not None and self.git_commit_sha != expected_git_commit_sha:
            raise LiveAuthorizationError(
                f"Token git_commit_sha '{self.git_commit_sha}' does not match audited build '{expected_git_commit_sha}'"
            )

        ensure_utc(self.issued_at_utc, "issued_at_utc")
        ensure_utc(self.expires_at_utc, "expires_at_utc")
        ttl = self.expires_at_utc - self.issued_at_utc
        if ttl <= timedelta(seconds=0) or ttl > MAX_LIVE_TOKEN_TTL:
            raise LiveAuthorizationError(
                f"Token TTL ({ttl}) must be > 0 and <= {MAX_LIVE_TOKEN_TTL}"
            )

        ref_now = now_utc or utc_now()
        ensure_utc(ref_now, "now_utc")
        if ref_now >= self.expires_at_utc or ref_now < self.issued_at_utc:
            raise LiveAuthorizationError("LiveAuthorizationToken is expired or not yet valid")

        try:
            pub_bytes = bytes.fromhex(pinned_approver_pubkey_hex)
            sig_bytes = bytes.fromhex(self.ed25519_signature_hex)
            pubkey = Ed25519PublicKey.from_public_bytes(pub_bytes)
        except ValueError as exc:
            raise LiveAuthorizationError(f"Malformed hex in public key or signature: {exc}") from exc

        payload_bytes = self.canonical_Signing_payload(
            token_id=self.token_id,
            approver_id=self.approver_id,
            authorized_symbol=self.authorized_symbol,
            max_capital_usdt=cap,
            max_single_trade_risk_pct=sr,
            max_rolling_24h_loss_pct=dl,
            risk_config_version=self.risk_config_version,
            risk_config_sha256=self.risk_config_sha256,
            git_commit_sha=self.git_commit_sha,
            issued_at_utc=self.issued_at_utc,
            expires_at_utc=self.expires_at_utc,
        )
        try:
            pubkey.verify(sig_bytes, payload_bytes)
        except InvalidSignature as exc:
            raise LiveAuthorizationError(
                "Ed25519 signature verification failed for LiveAuthorizationToken"
            ) from exc

        if nonce_registry is not None:
            nonce_registry.consume_atomically(
                token_id=self.token_id,
                approver_id=self.approver_id,
                code_tree_sha256=self.git_commit_sha,
                consumed_at_utc=ref_now,
            )


@dataclass(frozen=True)
class SystemConfig:
    """Top-level system configuration defaulting strictly to PAPER mode (QA-14)."""

    mode: ExecutionMode = ExecutionMode.PAPER
    risk: RiskConfig = field(default_factory=RiskConfig)
    allow_live_trading: bool = False
    go_no_go: GoNoGoChecklist = field(default_factory=GoNoGoChecklist)
    live_auth_token: LiveAuthorizationToken | None = None
    pinned_approver_pubkey_hex: str | None = IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX
    consumed_token_ids: tuple[str, ...] = field(default_factory=tuple)
    nonce_registry: PersistentNonceRegistry | None = None
    audited_git_commit_sha: str | None = None
    verify_runtime_code_tree: bool = False
    code_package_dir: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ExecutionMode):
            raise TypeError(f"mode must be ExecutionMode enum, got {type(self.mode).__name__}")
        if not isinstance(self.allow_live_trading, bool):
            raise TypeError("allow_live_trading must be a strict bool")
        if not isinstance(self.risk, RiskConfig):
            raise TypeError("risk must be a RiskConfig instance")
        if not isinstance(self.go_no_go, GoNoGoChecklist):
            raise TypeError("go_no_go must be a GoNoGoChecklist instance")
        if self.mode == ExecutionMode.LIVE:
            self.verify_live_gate()

    def verify_live_gate(self, now_utc: datetime | None = None) -> None:
        """Enforce non-bypassable multi-gate checks before permitting LIVE mode."""
        if not self.allow_live_trading:
            raise LiveAuthorizationError(
                "LIVE mode is strictly disabled by default (allow_live_trading=False)."
            )
        if not self.go_no_go.all_passed:
            raise LiveAuthorizationError(
                "LIVE mode blocked: Section 16 GO/NO-GO checklist is incomplete (NO-GO)."
            )
        if self.live_auth_token is None:
            raise LiveAuthorizationError(
                "LIVE mode blocked: Explicit user LiveAuthorizationToken is missing."
            )
        self.live_auth_token.validate(
            expected_risk=self.risk,
            pinned_approver_pubkey_hex=self.pinned_approver_pubkey_hex,
            consumed_token_ids=self.consumed_token_ids,
            nonce_registry=self.nonce_registry,
            expected_git_commit_sha=self.audited_git_commit_sha,
            verify_runtime_code_tree=self.verify_runtime_code_tree,
            code_package_dir=self.code_package_dir,
            now_utc=now_utc,
        )

    @classmethod
    def from_untrusted_mapping(
        cls,
        raw_config: Mapping[str, Any] | None = None,
        env_vars: Mapping[str, str] | None = None,
        trusted_nonce_registry: PersistentNonceRegistry | None = None,
        _trusted_pubkey_hex_override_for_test: str | None = None,
    ) -> SystemConfig:
        """Safely parse configuration while guaranteeing untrusted dicts or env vars cannot replace
        the trust-root public key, wipe the persistent nonce store, or spoof the runtime code hash.
        """
        data = dict(raw_config or {})

        # 1. Block any attempt by untrusted config to supply or replace trust-root parameters (Task A.5)
        forbidden_present = FORBIDDEN_UNTRUSTED_CONFIG_KEYS.intersection(data.keys())
        if forbidden_present:
            raise LiveAuthorizationError(
                f"Security violation: Untrusted config cannot supply or override trust-root keys: {sorted(forbidden_present)}"
            )

        # 2. Block any attempt by environment variables to enable LIVE or replace trust-root keys
        if env_vars:
            for env_k in env_vars:
                if "PUBKEY" in env_k.upper() or "TRUST_ROOT" in env_k.upper() or "COMMIT_SHA" in env_k.upper():
                    raise LiveAuthorizationError(
                        f"Security violation: Environment variable '{env_k}' cannot override trust root."
                    )
            env_mode = env_vars.get("SUI_QUANT_MODE", "").strip().upper()
            if env_mode == "LIVE" or env_vars.get("ALLOW_LIVE", "").strip() in {"1", "true", "TRUE", "yes"}:
                raise LiveAuthorizationError(
                    "Security violation: Environment variables are strictly forbidden from enabling LIVE mode."
                )
            if env_mode and "mode" not in data:
                data["mode"] = env_mode

        raw_mode = data.get("mode", ExecutionMode.PAPER)
        if isinstance(raw_mode, str):
            try:
                mode = ExecutionMode(raw_mode.upper())
            except ValueError as exc:
                raise ValueError(f"Invalid ExecutionMode '{raw_mode}'") from exc
        elif isinstance(raw_mode, ExecutionMode):
            mode = raw_mode
        else:
            raise TypeError(f"Invalid mode type: {type(raw_mode).__name__}")

        risk_data = data.get("risk")
        if isinstance(risk_data, RiskConfig):
            risk = risk_data
        elif isinstance(risk_data, Mapping):
            risk = RiskConfig(**risk_data)
        elif risk_data is None:
            risk = RiskConfig()
        else:
            raise TypeError("Invalid risk configuration format")

        allow_live = data.get("allow_live_trading", False)
        if not isinstance(allow_live, bool):
            raise LiveAuthorizationError("allow_live_trading must be a strict boolean")

        if mode == ExecutionMode.LIVE and trusted_nonce_registry is None:
            raise LiveAuthorizationError(
                "LIVE mode via config loader requires a trusted on-disk PersistentNonceRegistry."
            )

        trusted_pubkey = (
            _trusted_pubkey_hex_override_for_test
            if _trusted_pubkey_hex_override_for_test is not None
            else IMMUTABLE_APPROVER_ED25519_PUBKEY_HEX
        )

        return cls(
            mode=mode,
            risk=risk,
            allow_live_trading=allow_live,
            go_no_go=data.get("go_no_go", GoNoGoChecklist()),
            live_auth_token=data.get("live_auth_token"),
            pinned_approver_pubkey_hex=trusted_pubkey,
            nonce_registry=trusted_nonce_registry,
            verify_runtime_code_tree=True,
        )
