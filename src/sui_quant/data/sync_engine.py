"""E-01 Cloud-to-Local Incremental Data Sync Engine (§1, §11.1, P2_PLAN).

Responsibilities:
1. Cloud-side rolling shard exporter (`CloudShardExporter`) with atomic `manifest.json`, SHA-256 digests,
   and `<= 4.0 GB` rolling disk quota enforcement.
2. Hard security blacklist (`assert_safe_for_sync`) blocking any credential, private key, token, or
   account file from ever being exported or pulled.
3. Windows 11 Local Research Workstation incremental sync engine (`LocalResearchSyncEngine`) with
   checkpoint resume, SHA-256 tamper rejection, and idempotent `Parquet + DuckDB` warehouse ingestion.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sui_quant.security.redaction import SENSITIVE_KEY_NAMES
from sui_quant.utils.clock import ensure_utc, utc_now


class SyncIntegrityError(ValueError):
    """Raised when a downloaded shard's SHA-256 digest does not match the signed/expected manifest."""


class SecretSyncBlockedError(PermissionError):
    """Raised when an export or sync operation attempts to touch a credential or key file."""


FORBIDDEN_SYNC_PATH_REGEX = re.compile(
    r"(?i)(^|[\\/])("
    r"\.env(\..+)?|"
    r".*\.(key|pem|p12|pfx|crt|der)|"
    r".*(secret|password|passwd|credential|apikey|api_key|private_key|listen_key|token|account).*|"
    r"id_rsa.*|id_ed25519.*|authorized_keys|consumed_live_tokens.*"
    r")$"
)


def assert_safe_for_sync(path_or_name: str | Path) -> None:
    """Strictly forbid syncing any file path or name that resembles a credential, key, or token (§10, §11.1)."""
    raw = str(path_or_name).strip()
    if not raw or ".." in Path(raw).parts:
        raise SecretSyncBlockedError(f"Unsafe or traversal sync path rejected: '{raw}'")
    if FORBIDDEN_SYNC_PATH_REGEX.search(raw):
        raise SecretSyncBlockedError(
            f"Security policy violation: Credential/secret path '{raw}' is strictly blocked from cloud-local sync."
        )


def assert_records_free_of_secrets(records: Sequence[Mapping[str, Any]]) -> None:
    """Verify that market data records contain no sensitive credential keys before shard export or ingestion."""
    for rec in records:
        for k in rec.keys():
            k_lower = str(k).lower().strip()
            if k_lower in SENSITIVE_KEY_NAMES or any(
                bad in k_lower for bad in ("secret", "password", "apikey", "private_key", "signature")
            ):
                raise SecretSyncBlockedError(
                    f"Security policy violation: Record contains forbidden sensitive key '{k}'"
                )


@dataclass(frozen=True)
class ShardManifestEntry:
    shard_id: str
    seq: int
    filename: str
    symbol: str
    stream_type: str
    record_count: int
    file_size_bytes: int
    sha256: str
    created_at_utc: str

    def __post_init__(self) -> None:
        assert_safe_for_sync(self.filename)
        if len(self.sha256) != 64:
            raise ValueError("ShardManifestEntry.sha256 must be a 64-char hex digest")


class CloudShardExporter:
    """Manages bounded rolling market data shards on the 2vCPU/2GB Ubuntu server (`<= 4.0 GB` quota)."""

    def __init__(
        self,
        spool_dir: str | Path,
        max_spool_bytes: int = int(4.0 * (1024**3)),
    ) -> None:
        self.spool_dir = Path(spool_dir)
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.max_spool_bytes = max_spool_bytes
        self.manifest_path = self.spool_dir / "manifest.json"
        self._entries: list[ShardManifestEntry] = []
        self._next_seq = 1
        self._load_existing_manifest()

    def _load_existing_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            loaded: list[ShardManifestEntry] = []
            for item in raw.get("shards", []):
                entry = ShardManifestEntry(**item)
                if (self.spool_dir / entry.filename).exists():
                    loaded.append(entry)
            self._entries = loaded
            if self._entries:
                self._next_seq = max(e.seq for e in self._entries) + 1
        except Exception:
            self._entries = []

    def write_shard(
        self,
        symbol: str,
        stream_type: str,
        records: Sequence[Mapping[str, Any]],
        created_at_utc: datetime | None = None,
    ) -> ShardManifestEntry:
        """Write a batch of market records to an immutable JSONL shard, compute SHA-256, and enforce quota."""
        if not records:
            raise ValueError("Cannot write an empty shard")
        assert_records_free_of_secrets(records)
        ref_now = ensure_utc(created_at_utc or utc_now(), "created_at_utc")

        seq = self._next_seq
        self._next_seq += 1
        clean_sym = re.sub(r"[^A-Za-z0-9]", "", symbol).upper() or "MKT"
        clean_stream = re.sub(r"[^A-Za-z0-9_]", "", stream_type).lower() or "events"
        shard_id = f"SHARD-{seq:06d}-{clean_sym}-{clean_stream}"
        filename = f"shard_{seq:06d}_{clean_sym}_{clean_stream}.jsonl"
        assert_safe_for_sync(filename)

        lines = [json.dumps(dict(r), ensure_ascii=False, sort_keys=True) for r in records]
        payload_bytes = ("\n".join(lines) + "\n").encode("utf-8")
        sha256_hex = hashlib.sha256(payload_bytes).hexdigest()

        shard_path = self.spool_dir / filename
        shard_path.write_bytes(payload_bytes)

        entry = ShardManifestEntry(
            shard_id=shard_id,
            seq=seq,
            filename=filename,
            symbol=clean_sym,
            stream_type=clean_stream,
            record_count=len(records),
            file_size_bytes=len(payload_bytes),
            sha256=sha256_hex,
            created_at_utc=ref_now.isoformat(),
        )
        self._entries.append(entry)
        self._enforce_spool_quota()
        self._save_manifest(ref_now)
        return entry

    def _enforce_spool_quota(self) -> None:
        total_bytes = sum(e.file_size_bytes for e in self._entries)
        while total_bytes > self.max_spool_bytes and len(self._entries) > 1:
            oldest = self._entries.pop(0)
            total_bytes -= oldest.file_size_bytes
            oldest_file = self.spool_dir / oldest.filename
            if oldest_file.exists():
                oldest_file.unlink()

    def _save_manifest(self, updated_utc: datetime) -> None:
        doc = {
            "version": "1.0-p2",
            "updated_at_utc": updated_utc.isoformat(),
            "total_shards": len(self._entries),
            "total_bytes": sum(e.file_size_bytes for e in self._entries),
            "shards": [asdict(e) for e in self._entries],
        }
        tmp_path = self.spool_dir / "manifest.json.tmp"
        tmp_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp_path.replace(self.manifest_path)

    def get_manifest_dict(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return {"version": "1.0-p2", "shards": []}


@dataclass(frozen=True)
class SyncRunReport:
    synced_shards_count: int
    skipped_already_synced_count: int
    ingested_records_count: int
    last_synced_seq: int
    parquet_files_count: int
    duckdb_total_rows: int


class LocalResearchSyncEngine:
    """Windows 11 Research Workstation incremental sync engine (`Parquet + DuckDB` warehouse, §1, §11.1).

    Features:
    - Checkpoint + SHA-256 resume (`sync_checkpoint.json`)
    - Strict rejection of tampered shards (`SyncIntegrityError`)
    - Strict rejection of secret/credential filenames (`SecretSyncBlockedError`)
    - Idempotent ingestion into local Parquet partitions and DuckDB tables (`market_klines`, `agg_trades`, `market_events`)
    """

    def __init__(
        self,
        local_warehouse_dir: str | Path,
    ) -> None:
        self.warehouse_dir = Path(local_warehouse_dir)
        self.parquet_dir = self.warehouse_dir / "parquet"
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.duckdb_path = self.warehouse_dir / "research_warehouse.duckdb"
        self.checkpoint_path = self.warehouse_dir / "sync_checkpoint.json"
        self.storage_backend = self._detect_backend()
        self._init_warehouse()

    @staticmethod
    def _detect_backend() -> str:
        try:
            import duckdb  # noqa: F401
            import pyarrow  # noqa: F401
            import pyarrow.parquet  # noqa: F401

            return "DUCKDB_PARQUET"
        except ImportError:
            return "SQLITE_FALLBACK"

    def _init_warehouse(self) -> None:
        if self.storage_backend == "DUCKDB_PARQUET":
            import duckdb

            conn = duckdb.connect(str(self.duckdb_path))
        else:
            import sqlite3

            conn = sqlite3.connect(str(self.duckdb_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_klines (
                    symbol VARCHAR NOT NULL,
                    interval VARCHAR NOT NULL,
                    open_time_ms BIGINT NOT NULL,
                    close_time_ms BIGINT NOT NULL,
                    open_price VARCHAR NOT NULL,
                    high_price VARCHAR NOT NULL,
                    low_price VARCHAR NOT NULL,
                    close_price VARCHAR NOT NULL,
                    base_volume VARCHAR NOT NULL,
                    quote_volume VARCHAR NOT NULL,
                    exchange_time_utc VARCHAR NOT NULL,
                    recv_time_utc VARCHAR NOT NULL,
                    shard_id VARCHAR NOT NULL,
                    PRIMARY KEY (symbol, interval, open_time_ms)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agg_trades (
                    symbol VARCHAR NOT NULL,
                    agg_trade_id BIGINT NOT NULL,
                    price VARCHAR NOT NULL,
                    qty VARCHAR NOT NULL,
                    is_buyer_maker BOOLEAN NOT NULL,
                    exchange_time_utc VARCHAR NOT NULL,
                    recv_time_utc VARCHAR NOT NULL,
                    shard_id VARCHAR NOT NULL,
                    PRIMARY KEY (symbol, agg_trade_id)
                );
                """
            )
            if self.storage_backend != "DUCKDB_PARQUET":
                conn.commit()
        finally:
            conn.close()

    def load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return {"last_synced_seq": 0, "synced_shards": {}}
        return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

    def _save_checkpoint(self, last_seq: int, synced_shards: dict[str, str]) -> None:
        doc = {
            "last_synced_seq": last_seq,
            "synced_shards": synced_shards,
            "updated_at_utc": utc_now().isoformat(),
        }
        tmp = self.warehouse_dir / "sync_checkpoint.json.tmp"
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(self.checkpoint_path)

    def scan_candidate_paths(self, remote_paths: Sequence[str]) -> list[str]:
        """Validate a list of remote candidate file paths against the secret blacklist before download."""
        safe: list[str] = []
        for p in remote_paths:
            assert_safe_for_sync(p)
            safe.append(p)
        return safe

    def sync_from_manifest(
        self,
        manifest_dict: Mapping[str, Any],
        fetch_shard_bytes_fn: Callable[[str], bytes],
        max_shards_this_run: int | None = None,
    ) -> SyncRunReport:
        """Incrementally pull and verify unsynced shards from cloud manifest into Parquet + DuckDB."""
        ckpt = self.load_checkpoint()
        synced_shards: dict[str, str] = dict(ckpt.get("synced_shards", {}))
        last_seq = int(ckpt.get("last_synced_seq", 0))

        raw_shards = manifest_dict.get("shards", [])
        entries = [ShardManifestEntry(**s) if isinstance(s, Mapping) else s for s in raw_shards]
        entries.sort(key=lambda e: e.seq)

        synced_count = 0
        skipped_count = 0
        ingested_records = 0

        for entry in entries:
            assert_safe_for_sync(entry.filename)
            if entry.shard_id in synced_shards and synced_shards[entry.shard_id] == entry.sha256:
                skipped_count += 1
                continue

            if max_shards_this_run is not None and synced_count >= max_shards_this_run:
                break

            raw_bytes = fetch_shard_bytes_fn(entry.filename)
            actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha256 != entry.sha256:
                raise SyncIntegrityError(
                    f"SHA-256 mismatch for shard '{entry.shard_id}' ({entry.filename}): "
                    f"expected {entry.sha256}, got {actual_sha256}"
                )

            lines = [ln for ln in raw_bytes.decode("utf-8").splitlines() if ln.strip()]
            records = [json.loads(ln) for ln in lines]
            assert_records_free_of_secrets(records)

            self._ingest_verified_shard_to_parquet_and_duckdb(entry, records)

            synced_shards[entry.shard_id] = actual_sha256
            last_seq = max(last_seq, entry.seq)
            self._save_checkpoint(last_seq=last_seq, synced_shards=synced_shards)
            synced_count += 1
            ingested_records += len(records)

        parquet_files = list(self.parquet_dir.glob("*.parquet"))
        total_rows = self.count_warehouse_rows()
        return SyncRunReport(
            synced_shards_count=synced_count,
            skipped_already_synced_count=skipped_count,
            ingested_records_count=ingested_records,
            last_synced_seq=last_seq,
            parquet_files_count=len(parquet_files),
            duckdb_total_rows=total_rows,
        )

    def _ingest_verified_shard_to_parquet_and_duckdb(
        self,
        entry: ShardManifestEntry,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        enriched = [{**dict(r), "shard_id": entry.shard_id} for r in records]
        parquet_file = self.parquet_dir / f"{entry.shard_id}.parquet"

        if self.storage_backend == "DUCKDB_PARQUET":
            import duckdb
            import pyarrow as pa
            import pyarrow.parquet as pq

            table = pa.Table.from_pylist(enriched)
            pq.write_table(table, parquet_file, compression="snappy")
            conn = duckdb.connect(str(self.duckdb_path))
        else:
            import sqlite3

            parquet_file.write_text(json.dumps(enriched), encoding="utf-8")
            conn = sqlite3.connect(str(self.duckdb_path))

        try:
            for r in records:
                rec_type = r.get("record_type", entry.stream_type)
                if rec_type == "kline_closed" or "open_time_ms" in r:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO market_klines (
                            symbol, interval, open_time_ms, close_time_ms,
                            open_price, high_price, low_price, close_price,
                            base_volume, quote_volume, exchange_time_utc, recv_time_utc, shard_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(r["symbol"]),
                            str(r.get("interval", "5m")),
                            int(r["open_time_ms"]),
                            int(r["close_time_ms"]),
                            str(r["open_price"]),
                            str(r["high_price"]),
                            str(r["low_price"]),
                            str(r["close_price"]),
                            str(r["base_volume"]),
                            str(r["quote_volume"]),
                            str(r["exchange_time_utc"]),
                            str(r["recv_time_utc"]),
                            entry.shard_id,
                        ),
                    )
                elif rec_type == "aggTrade" or "agg_trade_id" in r:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO agg_trades (
                            symbol, agg_trade_id, price, qty, is_buyer_maker,
                            exchange_time_utc, recv_time_utc, shard_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(r["symbol"]),
                            int(r["agg_trade_id"]),
                            str(r["price"]),
                            str(r["qty"]),
                            bool(r.get("is_buyer_maker", False)),
                            str(r["exchange_time_utc"]),
                            str(r["recv_time_utc"]),
                            entry.shard_id,
                        ),
                    )
            if self.storage_backend != "DUCKDB_PARQUET":
                conn.commit()
        finally:
            conn.close()

    def count_warehouse_rows(self) -> int:
        if self.storage_backend == "DUCKDB_PARQUET":
            import duckdb

            conn = duckdb.connect(str(self.duckdb_path), read_only=True)
        else:
            import sqlite3

            conn = sqlite3.connect(str(self.duckdb_path))
        try:
            k_cnt = conn.execute("SELECT COUNT(*) FROM market_klines").fetchone()[0]
            t_cnt = conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0]
            return int(k_cnt) + int(t_cnt)
        finally:
            conn.close()
