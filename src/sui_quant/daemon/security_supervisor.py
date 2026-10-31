"""S-01 Host Security Supervisor with Offline Ed25519 Signed Release Manifest and 6-Dimension Audit (§10).

Covers:
1. SSH & Auth anomaly detection (unauthorized user, non-whitelisted IP login, sudo escalation, brute-force spikes)
2. Account, sudoers, authorized_keys, and secret file permission (`0600`) monitoring
3. Critical file & RiskConfig tampering detection backed by an offline Ed25519 signature (defeating local hash forgery)
4. Suspicious process / reverse-shell detection
5. Listening port whitelist enforcement
6. Disk free space & inode watermark enforcement (<20% HIGH + cleanup, <15% CRITICAL + ENTRY_FROZEN)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from sui_quant.config import RiskConfig
from sui_quant.contracts import AlertSeverity
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.security.redaction import redact_mapping, redact_text
from sui_quant.state_machine import StateTransitionRecord, TradingState
from sui_quant.utils.clock import ensure_utc, utc_now


DEFAULT_ALLOWED_PORTS = frozenset({22, 53, 323, 8443})
SUSPICIOUS_CMD_SUBSTRINGS = (
    "nc -e",
    "ncat -e",
    "bash -i",
    "/dev/tcp/",
    "socat ",
    "xmrig",
    "minerd",
    "cryptonight",
    "curl http",
    "wget http",
)


@dataclass(frozen=True)
class SignedReleaseManifest:
    """Out-of-host trusted file & config integrity baseline signed offline with Ed25519 private key."""

    release_version: str
    created_at_utc: datetime
    file_sha256_map: Mapping[str, str]
    risk_config_sha256: str
    manifest_root_sha256: str
    ed25519_signature_hex: str

    @staticmethod
    def compute_manifest_root_sha256(
        release_version: str,
        created_at_utc: datetime,
        file_sha256_map: Mapping[str, str],
        risk_config_sha256: str,
    ) -> str:
        ensure_utc(created_at_utc, "created_at_utc")
        sorted_items = "|".join(f"{k}:{file_sha256_map[k]}" for k in sorted(file_sha256_map.keys()))
        canonical = (
            f"SUI-QUANT-MANIFEST|v={release_version}|ts={created_at_utc.isoformat()}|"
            f"risk={risk_config_sha256}|files={sorted_items}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def build_and_sign_offline(
        cls,
        priv_key: Ed25519PrivateKey,
        base_dir: Path,
        relative_file_paths: Sequence[str],
        risk_config: RiskConfig,
        release_version: str = "1.0.0-p1",
        created_at_utc: datetime | None = None,
    ) -> SignedReleaseManifest:
        ref_now = ensure_utc(created_at_utc or utc_now(), "created_at_utc")
        file_map: dict[str, str] = {}
        for rel in sorted(relative_file_paths):
            full_path = base_dir / rel
            digest = hashlib.sha256(full_path.read_bytes()).hexdigest()
            file_map[rel] = digest
        risk_sha = risk_config.canonical_sha256()
        root_sha = cls.compute_manifest_root_sha256(
            release_version=release_version,
            created_at_utc=ref_now,
            file_sha256_map=file_map,
            risk_config_sha256=risk_sha,
        )
        sig_hex = priv_key.sign(root_sha.encode("utf-8")).hex()
        return cls(
            release_version=release_version,
            created_at_utc=ref_now,
            file_sha256_map=file_map,
            risk_config_sha256=risk_sha,
            manifest_root_sha256=root_sha,
            ed25519_signature_hex=sig_hex,
        )

    def verify_integrity(
        self,
        pinned_pubkey_hex: str,
        base_dir: Path,
        active_risk_config: RiskConfig,
    ) -> tuple[bool, list[str], str]:
        """Verify Ed25519 signature AND live on-disk file hashes AND active RiskConfig hash.

        Returns `(passed, violation_reasons, live_computed_root_sha256)`.
        Even if a local attacker modifies a file AND edits `file_sha256_map` or `manifest_root_sha256`
        on the server, the Ed25519 signature verification against `pinned_pubkey_hex` will fail!
        """
        violations: list[str] = []
        recomputed_manifest_root = self.compute_manifest_root_sha256(
            release_version=self.release_version,
            created_at_utc=self.created_at_utc,
            file_sha256_map=self.file_sha256_map,
            risk_config_sha256=self.risk_config_sha256,
        )
        if recomputed_manifest_root != self.manifest_root_sha256:
            violations.append("MANIFEST_ROOT_HASH_TAMPERED")

        try:
            pub_bytes = bytes.fromhex(pinned_pubkey_hex)
            sig_bytes = bytes.fromhex(self.ed25519_signature_hex)
            pubkey = Ed25519PublicKey.from_public_bytes(pub_bytes)
            pubkey.verify(sig_bytes, recomputed_manifest_root.encode("utf-8"))
        except (ValueError, InvalidSignature):
            violations.append("MANIFEST_ED25519_SIGNATURE_INVALID")

        live_risk_sha = active_risk_config.canonical_sha256()
        if live_risk_sha != self.risk_config_sha256:
            violations.append("ACTIVE_RISK_CONFIG_SHA256_MISMATCH")

        live_file_map: dict[str, str] = {}
        for rel, expected_sha in sorted(self.file_sha256_map.items()):
            target = base_dir / rel
            if not target.exists():
                violations.append(f"MISSING_PROTECTED_FILE:{rel}")
                live_file_map[rel] = "MISSING"
            else:
                actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                live_file_map[rel] = actual_sha
                if actual_sha != expected_sha:
                    violations.append(f"FILE_HASH_MISMATCH:{rel}")

        live_root_sha = self.compute_manifest_root_sha256(
            release_version=self.release_version,
            created_at_utc=self.created_at_utc,
            file_sha256_map=live_file_map,
            risk_config_sha256=live_risk_sha,
        )
        return (len(violations) == 0), violations, live_root_sha


class S01SecuritySupervisor:
    """S-01 Host Security Supervisor enforcing 6-dimension checks, forensic preservation, and SECURITY_LOCKDOWN."""

    def __init__(
        self,
        store: StateStore,
        alert_manager: AlertManager,
        evidence_dir: Path,
        pinned_release_pubkey_hex: str,
        allowed_ssh_users: frozenset[str] = frozenset({"ubuntu"}),
        allowed_ssh_ips: frozenset[str] | None = None,
        allowed_listening_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
        disk_cleanup_callback: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.alert_manager = alert_manager
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.pinned_release_pubkey_hex = pinned_release_pubkey_hex
        self.allowed_ssh_users = allowed_ssh_users
        self.allowed_ssh_ips = allowed_ssh_ips
        self.allowed_listening_ports = allowed_listening_ports
        self.disk_cleanup_callback = disk_cleanup_callback

    def inspect_file_and_config_integrity(
        self,
        manifest: SignedReleaseManifest,
        base_dir: Path,
        active_risk_config: RiskConfig,
        now_utc: datetime | None = None,
    ) -> tuple[bool, str]:
        """Dimension 3: Verify code & RiskConfig integrity against offline Ed25519-signed manifest."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        ok, violations, live_root_sha = manifest.verify_integrity(
            pinned_pubkey_hex=self.pinned_release_pubkey_hex,
            base_dir=base_dir,
            active_risk_config=active_risk_config,
        )
        if not ok:
            self._trigger_security_lockdown(
                dimension="FILE_AND_CONFIG_INTEGRITY",
                dedup_key="S01_INTEGRITY_VIOLATION",
                details={
                    "violations": violations,
                    "live_root_sha256": live_root_sha,
                    "expected_root_sha256": manifest.manifest_root_sha256,
                },
                now_utc=ref_now,
            )
        return ok, live_root_sha

    def inspect_ssh_auth_logs(
        self,
        log_lines: Sequence[str],
        now_utc: datetime | None = None,
    ) -> bool:
        """Dimension 1: Inspect SSH & sudo auth journal lines for unauthorized logins or brute-force spikes."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        failed_count = 0
        critical_violations: list[str] = []

        for line in log_lines:
            clean = redact_text(line)
            if "Failed password" in line or "Invalid user" in line:
                failed_count += 1
            if "Accepted password for " in line or "Accepted publickey for " in line:
                # Parse "Accepted <method> for <user> from <ip>"
                parts = line.split()
                try:
                    for_idx = parts.index("for")
                    from_idx = parts.index("from")
                    login_user = parts[for_idx + 1]
                    login_ip = parts[from_idx + 1]
                except (ValueError, IndexError):
                    login_user = "UNKNOWN"
                    login_ip = "UNKNOWN"

                if login_user not in self.allowed_ssh_users:
                    critical_violations.append(f"UNAUTHORIZED_SSH_USER_SUCCESS:{login_user}@{login_ip}")
                elif self.allowed_ssh_ips is not None and login_ip not in self.allowed_ssh_ips:
                    critical_violations.append(f"NON_WHITELISTED_SSH_IP_SUCCESS:{login_user}@{login_ip}")

            if "sudo:" in line and ("NOT in sudoers" in line or "authentication failure" in line):
                critical_violations.append(f"UNAUTHORIZED_SUDO_ATTEMPT:{clean}")

        if critical_violations:
            self._trigger_security_lockdown(
                dimension="SSH_AUTH_ANOMALY",
                dedup_key="S01_SSH_CRITICAL_INTRUSION",
                details={"violations": critical_violations, "failed_count": failed_count},
                now_utc=ref_now,
            )
            return False

        if failed_count >= 10:
            self.alert_manager.emit_alert(
                severity=AlertSeverity.INFO,
                source="S-01",
                dedup_key="S01_SSH_BRUTEFORCE_BLOCKED",
                actions_taken=("FAIL2BAN_MONITORED",),
                details={"failed_login_attempts": failed_count},
                now_utc=ref_now,
            )
        return True

    def inspect_permissions_and_accounts(
        self,
        uid_zero_accounts: Sequence[str],
        sudoers_modified: bool,
        secret_file_modes: Mapping[str, int],
        now_utc: datetime | None = None,
    ) -> bool:
        """Dimension 2: Check for rogue root accounts, sudoers tampering, or overly permissive secret files."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        violations: list[str] = []
        if set(uid_zero_accounts) != {"root"}:
            violations.append(f"ROGUE_UID_ZERO_ACCOUNTS:{list(uid_zero_accounts)}")
        if sudoers_modified:
            violations.append("SUDOERS_UNEXPECTED_MODIFICATION")
        for path_str, mode_octal in secret_file_modes.items():
            # mode_octal e.g. 0o600; any group/world permission bit (mode & 0o077 != 0) is a violation
            if (mode_octal & 0o077) != 0:
                violations.append(f"INSECURE_SECRET_FILE_MODE:{path_str}:{oct(mode_octal)}")

        if violations:
            self._trigger_security_lockdown(
                dimension="ACCOUNT_AND_PERMISSION_AUDIT",
                dedup_key="S01_PERMISSION_VIOLATION",
                details={"violations": violations},
                now_utc=ref_now,
            )
            return False
        return True

    def inspect_processes_and_ports(
        self,
        running_cmdlines: Sequence[str],
        listening_ports: Sequence[int],
        now_utc: datetime | None = None,
    ) -> bool:
        """Dimensions 4 & 5: Detect suspicious processes (reverse shells/miners) and unauthorized listening ports."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        violations: list[str] = []
        for cmd in running_cmdlines:
            cmd_lower = cmd.lower()
            for bad in SUSPICIOUS_CMD_SUBSTRINGS:
                if bad in cmd_lower:
                    violations.append(f"SUSPICIOUS_PROCESS:{redact_text(cmd)}")
                    break

        for port in listening_ports:
            if port not in self.allowed_listening_ports:
                violations.append(f"UNAUTHORIZED_LISTENING_PORT:{port}")

        if violations:
            self._trigger_security_lockdown(
                dimension="PROCESS_AND_PORT_AUDIT",
                dedup_key="S01_PROCESS_OR_PORT_INTRUSION",
                details={"violations": violations},
                now_utc=ref_now,
            )
            return False
        return True

    def inspect_disk_watermark(
        self,
        free_space_pct: Decimal,
        inode_used_pct: Decimal = Decimal("0.10"),
        now_utc: datetime | None = None,
    ) -> bool:
        """Dimension 6: Enforce disk & inode watermarks (<20% HIGH + cleanup; <15% CRITICAL + ENTRY_FROZEN)."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        if free_space_pct < Decimal("0.15") or inode_used_pct > Decimal("0.85"):
            current_state = self.store.get_trading_state()
            if current_state != TradingState.SECURITY_LOCKDOWN:
                rec = StateTransitionRecord(
                    transition_id=f"TR-{uuid.uuid4().hex[:12]}",
                    from_state=current_state,
                    to_state=TradingState.ENTRY_FROZEN,
                    reason=f"DISK_WATERMARK_CRITICAL:free={free_space_pct:.3f},inode={inode_used_pct:.3f}",
                    occurred_at_utc=ref_now,
                )
                self.store.record_transition(rec)
            self.alert_manager.emit_alert(
                severity=AlertSeverity.CRITICAL,
                source="S-01",
                dedup_key="DISK_WATERMARK_CRITICAL",
                actions_taken=("ENTRY_FROZEN",),
                details={"free_space_pct": str(free_space_pct), "inode_used_pct": str(inode_used_pct)},
                now_utc=ref_now,
            )
            return False

        if free_space_pct < Decimal("0.20"):
            reclaimed_bytes = self.disk_cleanup_callback() if self.disk_cleanup_callback else 0
            self.alert_manager.emit_alert(
                severity=AlertSeverity.HIGH,
                source="S-01",
                dedup_key="DISK_WATERMARK_HIGH",
                actions_taken=("LOG_AND_SPOOL_ROTATION_TRIGGERED",),
                details={"free_space_pct": str(free_space_pct), "reclaimed_bytes": reclaimed_bytes},
                now_utc=ref_now,
            )
        return True

    def _trigger_security_lockdown(
        self,
        dimension: str,
        dedup_key: str,
        details: dict[str, Any],
        now_utc: datetime,
    ) -> None:
        """Execute §10 CRITICAL security response:
        1. Transition to SECURITY_LOCKDOWN (freeze all new entries);
        2. Strictly preserve existing stop-loss protection orders (never blindly cancel);
        3. Write immutable forensic evidence snapshot (never delete evidence);
        4. Emit CRITICAL high-priority alert.
        """
        incident_id = f"INC-{now_utc.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        evidence_file = self.evidence_dir / f"evidence_{incident_id}.json"
        sanitized = redact_mapping(details)
        evidence_payload = {
            "incident_id": incident_id,
            "dimension": dimension,
            "detected_at_utc": now_utc.isoformat(),
            "previous_trading_state": self.store.get_trading_state().value,
            "enforced_trading_state": TradingState.SECURITY_LOCKDOWN.value,
            "preserve_existing_protection_orders": True,
            "details": sanitized,
        }
        evidence_file.write_text(json.dumps(evidence_payload, indent=2, ensure_ascii=False), encoding="utf-8")

        current_state = self.store.get_trading_state()
        if current_state != TradingState.SECURITY_LOCKDOWN:
            rec = StateTransitionRecord(
                transition_id=f"TR-{uuid.uuid4().hex[:12]}",
                from_state=current_state,
                to_state=TradingState.SECURITY_LOCKDOWN,
                reason=f"S01_CRITICAL:{dimension}:{incident_id}",
                occurred_at_utc=now_utc,
            )
            self.store.record_transition(rec)

        self.store.record_security_incident(
            incident_id=incident_id,
            dimension=dimension,
            severity=AlertSeverity.CRITICAL,
            detected_at_utc_iso=now_utc.isoformat(),
            evidence_path=str(evidence_file),
            details=sanitized,
        )

        self.alert_manager.emit_alert(
            severity=AlertSeverity.CRITICAL,
            source="S-01",
            dedup_key=dedup_key,
            actions_taken=(
                "SECURITY_LOCKDOWN",
                "FREEZE_NEW_ENTRIES",
                "PRESERVE_PROTECTION_ORDERS",
                "FORENSIC_EVIDENCE_SAVED",
            ),
            details={"incident_id": incident_id, "evidence_path": str(evidence_file), **sanitized},
            now_utc=ref_now if (ref_now := now_utc) else utc_now(),
        )
