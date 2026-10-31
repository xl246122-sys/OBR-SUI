"""F-01 Market Data Collector: Real-time Binance Spot WebSocket ingestion (`SUIUSDT` & `BTCUSDT`),
RFC 6455 protocol-hardened TLS WebSocket client, dual-tier scanning clock (1s fast scan + 5m closed-bar trigger),
23h proactive hot rotation with cross-connection deduplication and sequence-gap detection (no false zero-loss promise),
disconnect gap marking & REST K-line backfill, and bounded SQLite WAL / spool storage (§2, §13, §14).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import ssl
import struct
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sui_quant.contracts import (
    ALLOWED_SYMBOLS,
    REFERENCE_SYMBOL,
    TRADING_SYMBOL,
    AlertSeverity,
    DataQuality,
    MarketEvent,
    MarketFeatures,
)
from sui_quant.daemon.alert_manager import AlertManager
from sui_quant.daemon.storage import StateStore
from sui_quant.data.quality_guard import (
    CALIBRATION_UNRECOVERABLE_TAG,
    ClosedKlineBar,
    OrderbookGapMarker,
    PartialOrderbookSnapshot,
    StreamQualityGuard,
)
from sui_quant.data.ring_buffer import (
    BackpressureTelemetry,
    BoundedBackpressureQueue,
    FixedRingBuffer,
)
from sui_quant.state_machine import StateTransitionRecord, TradingState, assert_valid_transition
from sui_quant.utils.clock import ensure_utc, monotonic_ns, utc_now


HOT_ROTATION_THRESHOLD_SEC = 23 * 3600  # 23 hours (proactive rotation before 24h Binance WS limit)
MAX_SPOOL_QUOTA_BYTES = int(4.0 * (1024**3))  # <= 4.0 GB cloud rolling spool quota (§11.1)
RFC6455_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_COMBINED_STREAMS_100MS: tuple[str, ...] = (
    "suiusdt@aggTrade",
    "btcusdt@aggTrade",
    "suiusdt@bookTicker",
    "btcusdt@bookTicker",
    "suiusdt@depth10@100ms",
    "suiusdt@kline_1m",
    "suiusdt@kline_5m",
    "suiusdt@kline_15m",
    "suiusdt@kline_1h",
    "btcusdt@kline_1m",
    "btcusdt@kline_5m",
    "btcusdt@kline_15m",
    "btcusdt@kline_1h",
)

DEGRADED_COMBINED_STREAMS_1000MS: tuple[str, ...] = tuple(
    "suiusdt@depth10@1000ms" if s == "suiusdt@depth10@100ms" else s
    for s in DEFAULT_COMBINED_STREAMS_100MS
)


class WebSocketProtocolError(ConnectionError):
    """Raised when the peer violates RFC 6455 framing, handshake, or size invariants."""


class WebSocketHandshakeError(WebSocketProtocolError):
    """Raised when the HTTP/1.1 101 WebSocket Upgrade or Sec-WebSocket-Accept verification fails."""


class WebSocketClosedError(ConnectionError):
    """Raised when a RFC 6455 CLOSE frame (opcode 0x8) is received from the server."""

    def __init__(self, close_code: int, close_reason: str = "") -> None:
        self.close_code = close_code
        self.close_reason = close_reason
        super().__init__(f"WebSocket closed by peer: code={close_code}, reason='{close_reason}'")


def compute_rfc6455_accept_key(sec_websocket_key: str) -> str:
    """Compute the RFC 6455 §4.2.2 `Sec-WebSocket-Accept` value for a given `Sec-WebSocket-Key`."""
    digest = hashlib.sha1((sec_websocket_key.strip() + RFC6455_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


@dataclass(frozen=True)
class AggTradeRecord:
    symbol: str
    agg_trade_id: int
    price: Decimal
    qty: Decimal
    first_trade_id: int
    last_trade_id: int
    is_buyer_maker: bool
    exchange_time_utc: datetime
    recv_time_utc: datetime
    recv_monotonic_ns: int


@dataclass(frozen=True)
class BookTickerQuote:
    symbol: str
    update_id: int
    best_bid_price: Decimal
    best_bid_qty: Decimal
    best_ask_price: Decimal
    best_ask_qty: Decimal
    exchange_time_utc: datetime
    recv_time_utc: datetime
    recv_monotonic_ns: int

    @property
    def spread_bps(self) -> Decimal:
        mid = (self.best_bid_price + self.best_ask_price) / Decimal("2")
        if mid <= Decimal("0"):
            return Decimal("0")
        return ((self.best_ask_price - self.best_bid_price) / mid) * Decimal("10000")


@dataclass(frozen=True)
class FastScanSnapshot:
    """1-second fast candidate & risk scan output (§2.2).

    Strictly used for short-window anomaly detection, spread monitoring, and BTC crash warning;
    `can_directly_trigger_buy` is permanently False (formal entry requires a closed 5m K-line).
    """

    scanned_at_utc: datetime
    data_quality: DataQuality
    sui_best_bid: Decimal | None
    sui_best_ask: Decimal | None
    sui_spread_bps: Decimal | None
    sui_top10_imbalance: Decimal
    sui_recent_trade_count: int
    sui_taker_buy_ratio: Decimal
    btc_short_return_pct: Decimal
    btc_rapid_drop_warning: bool
    depth_sampling_ms: int
    can_directly_trigger_buy: bool = False

    def __post_init__(self) -> None:
        if self.can_directly_trigger_buy:
            raise ValueError("1s FastScanSnapshot must never directly trigger a BUY order (§2.2)")


@dataclass(frozen=True)
class LatencyPercentiles:
    sample_count: int
    p50_ms: Decimal
    p95_ms: Decimal
    p99_ms: Decimal
    max_ms: Decimal
    slo_p99_limit_ms: Decimal = Decimal("50.0")

    @property
    def within_slo(self) -> bool:
        return self.p99_ms <= self.slo_p99_limit_ms


class F01MarketCollector:
    """F-01 Market Collector managing WebSocket streams, quality guards, 23h hot rotation,
    REST K-line backfill, 1s/5m dual-tier scanning, and SQLite WAL persistence.
    """

    def __init__(
        self,
        store: StateStore,
        alert_manager: AlertManager,
        spool_dir: str | Path,
        max_staleness_ms: int = 2000,
        queue_maxsize: int = 4096,
        depth_ring_capacity: int = 600,
        trade_ring_capacity: int = 2000,
        on_5m_bar_closed: Callable[[ClosedKlineBar, MarketFeatures], None] | None = None,
        on_regime_bar_closed: Callable[[ClosedKlineBar, MarketFeatures], None] | None = None,
    ) -> None:
        self.store = store
        self.alert_manager = alert_manager
        self.spool_dir = Path(spool_dir)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

        self.quality_guard = StreamQualityGuard(max_staleness_ms=max_staleness_ms)
        self.on_5m_bar_closed = on_5m_bar_closed
        self.on_regime_bar_closed = on_regime_bar_closed

        # Ring buffers (strictly bounded memory on 2GB RAM cloud server)
        self.depth_buffers: dict[str, FixedRingBuffer[PartialOrderbookSnapshot]] = {
            TRADING_SYMBOL: FixedRingBuffer(depth_ring_capacity),
            REFERENCE_SYMBOL: FixedRingBuffer(depth_ring_capacity),
        }
        self.trade_buffers: dict[str, FixedRingBuffer[AggTradeRecord]] = {
            TRADING_SYMBOL: FixedRingBuffer(trade_ring_capacity),
            REFERENCE_SYMBOL: FixedRingBuffer(trade_ring_capacity),
        }
        self.latest_book_ticker: dict[str, BookTickerQuote] = {}
        self.latency_samples_ms: FixedRingBuffer[Decimal] = FixedRingBuffer(2000)

        # Backpressure queue with automatic 100ms -> 1000ms depth stream degradation
        # and guaranteed protection for closed K-lines + DataQuality events (Item 3)
        self._depth_sampling_ms = 100
        self.ingestion_queue: BoundedBackpressureQueue[tuple[Mapping[str, Any], datetime, int]] = (
            BoundedBackpressureQueue(
                maxsize=queue_maxsize,
                high_watermark_ratio=Decimal("0.80"),
                low_watermark_ratio=Decimal("0.40"),
                is_critical_fn=self._is_critical_raw_frame,
                category_fn=self._categorize_raw_frame,
                on_high_watermark=self._handle_backpressure_high_watermark,
                on_low_watermark_recovery=self._handle_backpressure_low_watermark_recovery,
            )
        )

        # Connection & 23h hot-rotation state
        self.active_conn_id: str = "WS-PRIMARY-001"
        self.standby_conn_id: str | None = None
        self.active_conn_opened_utc: datetime = utc_now()
        self.hot_rotation_count: int = 0
        self._disconnect_started_utc: datetime | None = None
        self._last_depth_update_id_before_disconnect: int | None = None

        # Counters for QA-01 / QA-02 / Item 3 verification
        self.five_min_signal_trigger_count: int = 0
        self.total_valid_events_processed: int = 0
        self.data_quality_events_processed: int = 0

        self._init_f01_tables()

    def _init_f01_tables(self) -> None:
        conn = sqlite3.connect(str(self.store.db_path), timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_klines (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    close_time_ms INTEGER NOT NULL,
                    open_price TEXT NOT NULL,
                    high_price TEXT NOT NULL,
                    low_price TEXT NOT NULL,
                    close_price TEXT NOT NULL,
                    base_volume TEXT NOT NULL,
                    quote_volume TEXT NOT NULL,
                    taker_buy_base_volume TEXT NOT NULL,
                    taker_buy_quote_volume TEXT NOT NULL,
                    trade_count INTEGER NOT NULL,
                    exchange_time_utc TEXT NOT NULL,
                    recv_time_utc TEXT NOT NULL,
                    backfilled_via_rest INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (symbol, interval, open_time_ms)
                );

                CREATE TABLE IF NOT EXISTS orderbook_gaps (
                    gap_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    gap_start_utc TEXT NOT NULL,
                    gap_end_utc TEXT NOT NULL,
                    last_update_id_before_gap INTEGER,
                    first_update_id_after_gap INTEGER,
                    recoverable INTEGER NOT NULL DEFAULT 0,
                    calibration_tag TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    @property
    def depth_sampling_ms(self) -> int:
        return self._depth_sampling_ms

    def _is_critical_raw_frame(self, item: tuple[Mapping[str, Any], datetime, int]) -> bool:
        """Identify frames that MUST NEVER be dropped under backpressure (Item 3):
        1. Closed K-line bars (`e == 'kline'` and `k.x == True`);
        2. Explicit DataQuality / control events (`e == 'data_quality_event'`, `is_data_quality_event == True`,
           or `data_quality` flag present and != OK);
        3. Stale frames (`recv_time_utc - exchange_time_utc > max_staleness_ms`) that must trigger `DataQuality.STALE`.
        """
        raw, recv_utc, _ = item
        if not isinstance(raw, Mapping):
            return False
        if raw.get("is_data_quality_event") is True or raw.get("data_quality") not in (None, "OK", DataQuality.OK):
            return True
        data = raw.get("data", raw)
        if not isinstance(data, Mapping):
            return False
        event_type = str(data.get("e", ""))
        if event_type == "data_quality_event":
            return True
        if event_type == "kline" or "k" in data:
            k = data.get("k", {})
            if isinstance(k, Mapping) and k.get("x") is True:
                return True
        # Check if frame is stale so staleness DataQuality events are never shed before freezing entry
        ex_ms = data.get("E")
        if ex_ms is not None:
            try:
                lag_ms = (recv_utc.timestamp() * 1000.0) - float(ex_ms)
                if lag_ms > self.quality_guard.max_staleness_ms:
                    return True
            except (TypeError, ValueError):
                pass
        return False

    @staticmethod
    def _categorize_raw_frame(item: tuple[Mapping[str, Any], datetime, int]) -> str:
        raw, _, _ = item
        if not isinstance(raw, Mapping):
            return "invalid"
        if raw.get("is_data_quality_event") is True or raw.get("data_quality") not in (None, "OK", DataQuality.OK):
            return "data_quality"
        stream_name = str(raw.get("stream", ""))
        data = raw.get("data", raw)
        if not isinstance(data, Mapping):
            return "invalid"
        event_type = str(data.get("e", ""))
        if event_type == "data_quality_event":
            return "data_quality"
        if event_type == "kline" or "k" in data:
            k = data.get("k", {})
            if isinstance(k, Mapping) and k.get("x") is True:
                return "kline_closed"
            return "kline_unclosed"
        if "lastUpdateId" in data or "depth10" in stream_name:
            return "depth10"
        if event_type == "aggTrade" or stream_name.endswith("@aggTrade"):
            return "aggTrade"
        if "u" in data or stream_name.endswith("@bookTicker"):
            return "bookTicker"
        return "other"

    def _handle_backpressure_high_watermark(self, current_qsize: int, maxsize: int) -> None:
        """Automatically degrade depth10@100ms to 1000ms when queue occupancy reaches >= 80% (QA-02, Item 3)."""
        self._depth_sampling_ms = 1000
        self.alert_manager.emit_alert(
            severity=AlertSeverity.HIGH,
            source="F-01",
            dedup_key="F01-BACKPRESSURE-DEPTH-DEGRADED",
            actions_taken=("DEGRADE_DEPTH10_TO_1000MS",),
            details={
                "queue_size": current_qsize,
                "queue_maxsize": maxsize,
                "new_depth_sampling_ms": 1000,
            },
        )

    def _handle_backpressure_low_watermark_recovery(self, current_qsize: int, maxsize: int) -> None:
        """Restore depth10@100ms when queue drains below low watermark (<= 40%)."""
        self._depth_sampling_ms = 100

    def get_backpressure_telemetry(self) -> BackpressureTelemetry:
        """Return complete snapshot degradation and drop statistics (Item 3)."""
        return self.ingestion_queue.telemetry()

    def enqueue_raw_message(
        self,
        raw_payload: Mapping[str, Any],
        recv_time_utc: datetime | None = None,
        recv_mono_ns: int | None = None,
    ) -> bool:
        """Enqueue a raw WebSocket payload into the bounded backpressure queue."""
        r_utc = ensure_utc(recv_time_utc or utc_now(), "recv_time_utc")
        r_mono = recv_mono_ns if recv_mono_ns is not None else monotonic_ns()
        return self.ingestion_queue.put_nowait((raw_payload, r_utc, r_mono))

    def enqueue_data_quality_event(
        self,
        quality: DataQuality,
        reason: str,
        symbol: str = TRADING_SYMBOL,
        recv_time_utc: datetime | None = None,
        recv_mono_ns: int | None = None,
    ) -> bool:
        """Enqueue an explicit critical DataQuality event that is guaranteed never to be shed under backpressure."""
        r_utc = ensure_utc(recv_time_utc or utc_now(), "recv_time_utc")
        r_mono = recv_mono_ns if recv_mono_ns is not None else monotonic_ns()
        payload = {
            "is_data_quality_event": True,
            "data_quality": quality.value,
            "data": {
                "e": "data_quality_event",
                "s": symbol,
                "quality": quality.value,
                "reason": reason,
                "E": int(r_utc.timestamp() * 1000),
            },
        }
        return self.ingestion_queue.put_nowait((payload, r_utc, r_mono))

    def drain_and_process_queue(self, max_items: int = 512) -> int:
        """Drain up to `max_items` from the bounded queue and process them."""
        batch = self.ingestion_queue.drain_batch(max_items=max_items)
        for raw_payload, r_utc, r_mono in batch:
            self.process_ws_message(raw_payload, recv_time_utc=r_utc, recv_mono_ns=r_mono)
        if not self.ingestion_queue.degraded_mode and self._depth_sampling_ms == 1000:
            self._depth_sampling_ms = 100
        return len(batch)

    def process_ws_message(
        self,
        raw_payload: Mapping[str, Any],
        recv_time_utc: datetime | None = None,
        recv_mono_ns: int | None = None,
        source_conn_id: str | None = None,
        strict_consecutive_seq: bool = False,
    ) -> DataQuality:
        """Parse, validate, deduplicate, and route a single Binance Spot WebSocket or DataQuality message."""
        r_utc = ensure_utc(recv_time_utc or utc_now(), "recv_time_utc")
        r_mono = recv_mono_ns if recv_mono_ns is not None else monotonic_ns()

        stream_name = str(raw_payload.get("stream", ""))
        data = raw_payload.get("data", raw_payload)
        if not isinstance(data, Mapping):
            return DataQuality.INVALID_PAYLOAD

        event_type = str(data.get("e", ""))
        symbol = str(data.get("s", "")).upper()
        if not symbol and "@" in stream_name:
            symbol = stream_name.split("@", 1)[0].upper()
        if symbol not in ALLOWED_SYMBOLS:
            return DataQuality.INVALID_PAYLOAD

        # Route 0: Explicit critical DataQuality event (Item 3)
        if event_type == "data_quality_event" or raw_payload.get("is_data_quality_event") is True:
            q_str = str(data.get("quality", raw_payload.get("data_quality", "GAP_DETECTED")))
            q_val = DataQuality(q_str)
            reason = str(data.get("reason", "EXPLICIT_DATA_QUALITY_EVENT"))
            self.quality_guard._current_quality = q_val
            self.data_quality_events_processed += 1
            if q_val != DataQuality.OK:
                self._freeze_new_entry_on_unhealthy_data(f"DATA_QUALITY_EVENT:{q_val.value}:{reason}", r_utc)
            self._record_processing_latency(r_mono)
            return q_val

        # Route 1: aggTrade (`@aggTrade`)
        if event_type == "aggTrade" or stream_name.endswith("@aggTrade"):
            agg_id = int(data["a"])
            ex_ms = int(data.get("E", data.get("T", int(r_utc.timestamp() * 1000))))
            ex_utc = datetime.fromtimestamp(ex_ms / 1000.0, tz=timezone.utc)
            mkt_event = MarketEvent(
                event_id=f"AGG-{symbol}-{agg_id}",
                symbol=symbol,
                source="aggTrade",
                exchange_time_utc=ex_utc,
                recv_time_utc=r_utc,
                recv_monotonic_ns=r_mono,
                schema_version="1.0",
                payload=data,
                seq_or_update_id=agg_id,
            )
            quality = self.quality_guard.evaluate_event(
                mkt_event,
                strict_consecutive_seq=strict_consecutive_seq,
                connection_context=source_conn_id or "SINGLE_STREAM",
            )
            if quality == DataQuality.STALE:
                self.data_quality_events_processed += 1
                self._freeze_new_entry_on_unhealthy_data("STALE_AGGTRADE_EVENT", r_utc)
                return quality
            if quality in {DataQuality.DUPLICATE, DataQuality.OUT_OF_ORDER}:
                return quality

            # Even if quality == GAP_DETECTED under strict_consecutive_seq, record the valid trade
            # that arrived after the gap AND freeze new entries due to the detected sequence gap!
            rec = AggTradeRecord(
                symbol=symbol,
                agg_trade_id=agg_id,
                price=Decimal(str(data["p"])),
                qty=Decimal(str(data["q"])),
                first_trade_id=int(data.get("f", agg_id)),
                last_trade_id=int(data.get("l", agg_id)),
                is_buyer_maker=bool(data["m"]),
                exchange_time_utc=ex_utc,
                recv_time_utc=r_utc,
                recv_monotonic_ns=r_mono,
            )
            self.trade_buffers[symbol].append(rec)
            self._record_processing_latency(r_mono)
            self.total_valid_events_processed += 1
            if quality == DataQuality.GAP_DETECTED:
                self.data_quality_events_processed += 1
                self._freeze_new_entry_on_unhealthy_data(f"AGGTRADE_SEQUENCE_GAP_AT_{agg_id}", r_utc)
            return quality

        # Route 2: Partial Book Depth (`@depth10@100ms` or `@depth10@1000ms`)
        if "lastUpdateId" in data and ("bids" in data or "b" in data):
            update_id = int(data["lastUpdateId"])
            ex_ms = int(data.get("E", int(r_utc.timestamp() * 1000)))
            ex_utc = datetime.fromtimestamp(ex_ms / 1000.0, tz=timezone.utc)
            mkt_event = MarketEvent(
                event_id=f"DEPTH10-{symbol}-{update_id}",
                symbol=symbol,
                source="depth10",
                exchange_time_utc=ex_utc,
                recv_time_utc=r_utc,
                recv_monotonic_ns=r_mono,
                schema_version="1.0",
                payload=data,
                seq_or_update_id=update_id,
            )
            quality = self.quality_guard.evaluate_event(mkt_event)
            if quality != DataQuality.OK:
                if quality == DataQuality.STALE:
                    self.data_quality_events_processed += 1
                    self._freeze_new_entry_on_unhealthy_data("STALE_DEPTH10_SNAPSHOT", r_utc)
                return quality

            raw_bids = data.get("bids", data.get("b", []))
            raw_asks = data.get("asks", data.get("a", []))
            bids = tuple((Decimal(str(p)), Decimal(str(q))) for p, q in raw_bids[:10])
            asks = tuple((Decimal(str(p)), Decimal(str(q))) for p, q in raw_asks[:10])
            snap = PartialOrderbookSnapshot(
                symbol=symbol,
                last_update_id=update_id,
                bids=bids,
                asks=asks,
                exchange_time_utc=ex_utc,
                recv_time_utc=r_utc,
                recv_monotonic_ns=r_mono,
                sampling_interval_ms=self._depth_sampling_ms,
            )
            self.depth_buffers[symbol].append(snap)
            self._record_processing_latency(r_mono)
            self.total_valid_events_processed += 1
            return DataQuality.OK

        # Route 3: bookTicker (`@bookTicker`)
        if ("u" in data and "b" in data and "B" in data and "a" in data and "A" in data) or stream_name.endswith("@bookTicker"):
            update_id = int(data["u"])
            ex_ms = int(data.get("E", int(r_utc.timestamp() * 1000)))
            ex_utc = datetime.fromtimestamp(ex_ms / 1000.0, tz=timezone.utc)
            mkt_event = MarketEvent(
                event_id=f"BT-{symbol}-{update_id}",
                symbol=symbol,
                source="bookTicker",
                exchange_time_utc=ex_utc,
                recv_time_utc=r_utc,
                recv_monotonic_ns=r_mono,
                schema_version="1.0",
                payload=data,
                seq_or_update_id=update_id,
            )
            quality = self.quality_guard.evaluate_event(mkt_event)
            if quality != DataQuality.OK:
                if quality == DataQuality.STALE:
                    self.data_quality_events_processed += 1
                    self._freeze_new_entry_on_unhealthy_data("STALE_BOOKTICKER_EVENT", r_utc)
                return quality

            quote = BookTickerQuote(
                symbol=symbol,
                update_id=update_id,
                best_bid_price=Decimal(str(data["b"])),
                best_bid_qty=Decimal(str(data["B"])),
                best_ask_price=Decimal(str(data["a"])),
                best_ask_qty=Decimal(str(data["A"])),
                exchange_time_utc=ex_utc,
                recv_time_utc=r_utc,
                recv_monotonic_ns=r_mono,
            )
            self.latest_book_ticker[symbol] = quote
            self._record_processing_latency(r_mono)
            self.total_valid_events_processed += 1
            return DataQuality.OK

        # Route 4: Kline (`@kline_1m`, `@kline_5m`, `@kline_15m`, `@kline_1h`)
        if event_type == "kline" or "k" in data:
            kline_dict = data["k"]
            if not isinstance(kline_dict, Mapping):
                return DataQuality.INVALID_PAYLOAD
            ex_ms = int(data.get("E", kline_dict.get("T", int(r_utc.timestamp() * 1000))))
            ex_utc = datetime.fromtimestamp(ex_ms / 1000.0, tz=timezone.utc)

            lag_ms = (r_utc - ex_utc).total_seconds() * 1000.0
            if lag_ms > self.quality_guard.max_staleness_ms:
                self.quality_guard._stale_count += 1
                self.quality_guard._current_quality = DataQuality.STALE
                self.data_quality_events_processed += 1
                self._freeze_new_entry_on_unhealthy_data("STALE_KLINE_EVENT", r_utc)
                return DataQuality.STALE

            quality, closed_bar, _ = self.quality_guard.ingest_kline_payload(
                symbol=symbol,
                kline_dict=kline_dict,
                exchange_time_utc=ex_utc,
                recv_time_utc=r_utc,
                backfilled_via_rest=False,
            )
            if quality != DataQuality.OK:
                return quality

            if closed_bar is not None:
                self._persist_closed_kline(closed_bar)
                self._dispatch_closed_bar_callbacks(closed_bar)

            self._record_processing_latency(r_mono)
            self.total_valid_events_processed += 1
            return DataQuality.OK

        return DataQuality.INVALID_PAYLOAD

    def _dispatch_closed_bar_callbacks(self, bar: ClosedKlineBar) -> None:
        """Trigger formal A-02 5m breakout evaluation or A-01 regime update strictly on closed bars."""
        extra: dict[str, Decimal] = {}
        if bar.symbol == TRADING_SYMBOL and bar.interval == "5m":
            history = self.quality_guard.get_closed_bars(TRADING_SYMBOL, "5m", limit=21)
            prev_bars = [b for b in history if b.open_time_ms < bar.open_time_ms][-20:]
            if prev_bars:
                extra["prev_20_high"] = max(b.high_price for b in prev_bars)
            features = bar.to_complete_bar_features(extra_features=extra)
            self.five_min_signal_trigger_count += 1
            if self.on_5m_bar_closed is not None:
                self.on_5m_bar_closed(bar, features)
        else:
            features = bar.to_complete_bar_features()
            if self.on_regime_bar_closed is not None:
                self.on_regime_bar_closed(bar, features)

    def _persist_closed_kline(self, bar: ClosedKlineBar) -> None:
        conn = sqlite3.connect(str(self.store.db_path), timeout=5.0, isolation_level=None)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO market_klines (
                    symbol, interval, open_time_ms, close_time_ms,
                    open_price, high_price, low_price, close_price,
                    base_volume, quote_volume, taker_buy_base_volume, taker_buy_quote_volume,
                    trade_count, exchange_time_utc, recv_time_utc, backfilled_via_rest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bar.symbol,
                    bar.interval,
                    bar.open_time_ms,
                    bar.close_time_ms,
                    str(bar.open_price),
                    str(bar.high_price),
                    str(bar.low_price),
                    str(bar.close_price),
                    str(bar.base_volume),
                    str(bar.quote_volume),
                    str(bar.taker_buy_base_volume),
                    str(bar.taker_buy_quote_volume),
                    bar.trade_count,
                    bar.exchange_time_utc.isoformat(),
                    bar.recv_time_utc.isoformat(),
                    1 if bar.backfilled_via_rest else 0,
                ),
            )
        finally:
            conn.close()

    def handle_unexpected_disconnect(self, now_utc: datetime | None = None, reason: str = "WS_EOF") -> None:
        """Immediately mark DataQuality.GAP_DETECTED and freeze new entries on unexpected WS disconnect (QA-01)."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        self.quality_guard.mark_ws_disconnected()
        self._disconnect_started_utc = ref_now
        latest_depth = self.depth_buffers[TRADING_SYMBOL].latest()
        self._last_depth_update_id_before_disconnect = (
            latest_depth.last_update_id if latest_depth is not None else None
        )
        self._freeze_new_entry_on_unhealthy_data(f"WS_DISCONNECT:{reason}", ref_now)

    def recover_from_disconnect(
        self,
        reconnected_at_utc: datetime,
        backfilled_klines_by_symbol: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
        first_depth_update_id_after_reconnect: int | None = None,
    ) -> OrderbookGapMarker:
        """Execute post-disconnect recovery (§2.1, QA-01):
        1. Backfill missed closed K-lines (`is_closed=True`) via REST;
        2. Explicitly mark the disconnect window's `depth10` orderbook as `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION`;
        3. Restore `DataQuality.OK` and allow transitioning back from `ENTRY_FROZEN` once consistent.
        """
        ensure_utc(reconnected_at_utc, "reconnected_at_utc")
        gap_start = self._disconnect_started_utc or reconnected_at_utc

        for (symbol, _interval), klines in backfilled_klines_by_symbol.items():
            for k_dict in klines:
                if not bool(k_dict.get("x", True)):
                    continue
                close_ms = int(k_dict["T"])
                ex_utc = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)
                q, closed_bar, _ = self.quality_guard.ingest_kline_payload(
                    symbol=symbol,
                    kline_dict=k_dict,
                    exchange_time_utc=ex_utc,
                    recv_time_utc=reconnected_at_utc,
                    backfilled_via_rest=True,
                )
                if q == DataQuality.OK and closed_bar is not None:
                    self._persist_closed_kline(closed_bar)

        gap_id = f"GAP-{reconnected_at_utc.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        marker = self.quality_guard.record_unrecoverable_orderbook_gap(
            gap_id=gap_id,
            symbol=TRADING_SYMBOL,
            stream=f"depth10@{self._depth_sampling_ms}ms",
            gap_start_utc=gap_start,
            gap_end_utc=reconnected_at_utc,
            last_update_id_before_gap=self._last_depth_update_id_before_disconnect,
            first_update_id_after_gap=first_depth_update_id_after_reconnect,
            reason="WS_DISCONNECT_ORDERBOOK_UNRECOVERABLE",
        )
        self._persist_orderbook_gap(marker)
        self._disconnect_started_utc = None
        self.quality_guard.mark_recovery_complete()
        return marker

    def _persist_orderbook_gap(self, marker: OrderbookGapMarker) -> None:
        conn = sqlite3.connect(str(self.store.db_path), timeout=5.0, isolation_level=None)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO orderbook_gaps (
                    gap_id, symbol, stream, gap_start_utc, gap_end_utc,
                    last_update_id_before_gap, first_update_id_after_gap,
                    recoverable, calibration_tag, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    marker.gap_id,
                    marker.symbol,
                    marker.stream,
                    marker.gap_start_utc.isoformat(),
                    marker.gap_end_utc.isoformat(),
                    marker.last_update_id_before_gap,
                    marker.first_update_id_after_gap,
                    0,
                    marker.calibration_tag,
                    marker.reason,
                ),
            )
        finally:
            conn.close()

    def should_rotate_connection_23h(self, now_utc: datetime | None = None) -> bool:
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        age_sec = (ref_now - self.active_conn_opened_utc).total_seconds()
        return age_sec >= HOT_ROTATION_THRESHOLD_SEC

    def execute_23h_hot_rotation(
        self,
        new_conn_id: str,
        overlapping_primary_frames: Sequence[tuple[Mapping[str, Any], datetime, int]],
        overlapping_standby_frames: Sequence[tuple[Mapping[str, Any], datetime, int]],
        rotated_at_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Perform 23-hour dual-connection hot rotation with cross-connection deduplication AND
        sequence-gap detection (QA-01, P2 Remediation Item 2).

        Governance & Engineering Reality (Item 2):
        - Dual-connection warm handoff reduces cutover disruption via cross-connection deduplication,
          but NEVER promises absolute zero packet loss (`zero_loss_guaranteed = False`).
        - If a sequence discontinuity occurs across `old_conn_id -> new_conn_id` (e.g., `standby` first
          sequence ID > `primary` last sequence ID + 1), the gap is explicitly detected (`gap_detected = True`),
          `DataQuality.GAP_DETECTED` is set, new entries are frozen (`ENTRY_FROZEN`), and an
          `OrderbookGapMarker(recoverable=False)` is persisted.
        """
        ref_now = ensure_utc(rotated_at_utc or utc_now(), "rotated_at_utc")
        old_conn_id = self.active_conn_id
        self.standby_conn_id = new_conn_id

        accepted_count = 0
        deduped_count = 0
        gaps_before = len(self.quality_guard.sequence_gaps)

        # Step 1: Drain active primary connection frames up to warm handoff point
        for raw_p, utc_p, mono_p in overlapping_primary_frames:
            q_p = self.process_ws_message(
                raw_p,
                recv_time_utc=utc_p,
                recv_mono_ns=mono_p,
                source_conn_id=old_conn_id,
                strict_consecutive_seq=True,
            )
            if q_p in {DataQuality.OK, DataQuality.GAP_DETECTED}:
                accepted_count += 1
            elif q_p == DataQuality.DUPLICATE:
                deduped_count += 1

        primary_last_seq = self.quality_guard.get_last_seq(TRADING_SYMBOL, "aggTrade")

        # Step 2: Switch to standby connection and drain warm-up buffer with strict gap detection
        for raw_s, utc_s, mono_s in overlapping_standby_frames:
            q_s = self.process_ws_message(
                raw_s,
                recv_time_utc=utc_s,
                recv_mono_ns=mono_s,
                source_conn_id=new_conn_id,
                strict_consecutive_seq=True,
            )
            if q_s in {DataQuality.OK, DataQuality.GAP_DETECTED}:
                accepted_count += 1
            elif q_s == DataQuality.DUPLICATE:
                deduped_count += 1

        new_gaps = self.quality_guard.sequence_gaps[gaps_before:]
        gap_detected = len(new_gaps) > 0
        missing_ranges = [(g.missing_start_seq, g.missing_end_seq) for g in new_gaps]

        if gap_detected:
            self.quality_guard.mark_ws_disconnected()
            self._freeze_new_entry_on_unhealthy_data(
                f"HOT_ROTATION_23H_GAP:{old_conn_id}->{new_conn_id}:missing={missing_ranges}",
                ref_now,
            )
            gap_id = f"GAP-ROT-{ref_now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
            marker = self.quality_guard.record_unrecoverable_orderbook_gap(
                gap_id=gap_id,
                symbol=TRADING_SYMBOL,
                stream=f"depth10@{self._depth_sampling_ms}ms",
                gap_start_utc=ref_now,
                gap_end_utc=ref_now,
                last_update_id_before_gap=primary_last_seq,
                first_update_id_after_gap=self.quality_guard.get_last_seq(TRADING_SYMBOL, "aggTrade"),
                reason=f"HOT_ROTATION_SEQUENCE_GAP:{missing_ranges}",
            )
            self._persist_orderbook_gap(marker)

        self.active_conn_id = new_conn_id
        self.standby_conn_id = None
        self.active_conn_opened_utc = ref_now
        self.hot_rotation_count += 1

        return {
            "old_conn_id": old_conn_id,
            "new_conn_id": self.active_conn_id,
            "rotated_at_utc": ref_now.isoformat(),
            "accepted_unique_events": accepted_count,
            "deduplicated_overlap_events": deduped_count,
            "gap_detected": gap_detected,
            "missing_seq_ranges": missing_ranges,
            "zero_loss_guaranteed": False,
            "hot_rotation_count": self.hot_rotation_count,
        }

    def run_1s_fast_scan(self, now_utc: datetime | None = None) -> FastScanSnapshot:
        """Execute 1-second fast candidate & risk scan from in-memory ring buffers (§2.2)."""
        ref_now = ensure_utc(now_utc or utc_now(), "now_utc")
        bt = self.latest_book_ticker.get(TRADING_SYMBOL)
        latest_depth = self.depth_buffers[TRADING_SYMBOL].latest()

        best_bid = bt.best_bid_price if bt is not None else (latest_depth.best_bid if latest_depth else None)
        best_ask = bt.best_ask_price if bt is not None else (latest_depth.best_ask if latest_depth else None)
        spread_bps = bt.spread_bps if bt is not None else (latest_depth.spread_bps if latest_depth else None)
        imbalance = latest_depth.top10_imbalance if latest_depth is not None else Decimal("0")

        sui_trades = self.trade_buffers[TRADING_SYMBOL].tail(100)
        total_vol = sum((t.qty for t in sui_trades), Decimal("0"))
        taker_buy_vol = sum((t.qty for t in sui_trades if not t.is_buyer_maker), Decimal("0"))
        taker_buy_ratio = (taker_buy_vol / total_vol) if total_vol > Decimal("0") else Decimal("0.5")

        btc_trades = self.trade_buffers[REFERENCE_SYMBOL].tail(100)
        if len(btc_trades) >= 2 and btc_trades[0].price > Decimal("0"):
            btc_ret_pct = ((btc_trades[-1].price - btc_trades[0].price) / btc_trades[0].price) * Decimal("100")
        else:
            btc_ret_pct = Decimal("0")

        btc_rapid_drop = btc_ret_pct <= Decimal("-1.5")
        if btc_rapid_drop:
            self._freeze_new_entry_on_unhealthy_data(f"BTC_RAPID_DROP_1S:{btc_ret_pct:.2f}%", ref_now)

        return FastScanSnapshot(
            scanned_at_utc=ref_now,
            data_quality=self.quality_guard.current_quality,
            sui_best_bid=best_bid,
            sui_best_ask=best_ask,
            sui_spread_bps=spread_bps,
            sui_top10_imbalance=imbalance,
            sui_recent_trade_count=len(sui_trades),
            sui_taker_buy_ratio=taker_buy_ratio,
            btc_short_return_pct=btc_ret_pct,
            btc_rapid_drop_warning=btc_rapid_drop,
            depth_sampling_ms=self._depth_sampling_ms,
            can_directly_trigger_buy=False,
        )

    def compute_latency_percentiles(self) -> LatencyPercentiles:
        samples = sorted(self.latency_samples_ms.items())
        if not samples:
            return LatencyPercentiles(
                sample_count=0,
                p50_ms=Decimal("0"),
                p95_ms=Decimal("0"),
                p99_ms=Decimal("0"),
                max_ms=Decimal("0"),
            )
        n = len(samples)
        p50 = samples[min(n - 1, int(n * 0.50))]
        p95 = samples[min(n - 1, int(n * 0.95))]
        p99 = samples[min(n - 1, int(n * 0.99))]
        return LatencyPercentiles(
            sample_count=n,
            p50_ms=p50.quantize(Decimal("0.001")),
            p95_ms=p95.quantize(Decimal("0.001")),
            p99_ms=p99.quantize(Decimal("0.001")),
            max_ms=samples[-1].quantize(Decimal("0.001")),
        )

    def _record_processing_latency(self, recv_mono_ns: int) -> None:
        done_ns = monotonic_ns()
        diff_ns = max(0, done_ns - recv_mono_ns)
        lat_ms = Decimal(diff_ns) / Decimal("1000000")
        self.latency_samples_ms.append(lat_ms)

    def _freeze_new_entry_on_unhealthy_data(self, reason: str, now_utc: datetime) -> None:
        current = self.store.get_trading_state()
        if current not in {TradingState.SECURITY_LOCKDOWN, TradingState.ENTRY_FROZEN}:
            assert_valid_transition(current, TradingState.ENTRY_FROZEN)
            rec = StateTransitionRecord(
                transition_id=f"TR-{uuid.uuid4().hex[:12]}",
                from_state=current,
                to_state=TradingState.ENTRY_FROZEN,
                reason=f"F01_DATA_GUARD:{reason}",
                occurred_at_utc=now_utc,
            )
            self.store.record_transition(rec)
        self.alert_manager.emit_alert(
            severity=AlertSeverity.HIGH,
            source="F-01",
            dedup_key=f"F01-FREEZE-{reason.split(':')[0]}",
            actions_taken=("ENTRY_FROZEN",),
            details={"reason": reason, "data_quality": self.quality_guard.current_quality.value},
            now_utc=now_utc,
        )


def fetch_binance_rest_klines(
    symbol: str,
    interval: str,
    limit: int = 50,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    timeout_sec: float = 5.0,
) -> list[dict[str, Any]]:
    """Fetch historical K-lines from Binance Spot public REST API (`/api/v3/klines`) and return
    only strictly closed bars (`close_time_ms < current_utc_ms`).
    """
    params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    if start_time_ms is not None:
        params["startTime"] = start_time_ms
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    qs = urllib.parse.urlencode(params)
    url = f"https://api.binance.com/api/v3/klines?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "SUI-Quant-F01/1.0"})
    now_ms = int(utc_now().timestamp() * 1000)
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw_list = json.loads(resp.read().decode("utf-8"))

    closed_klines: list[dict[str, Any]] = []
    for row in raw_list:
        open_ms = int(row[0])
        close_ms = int(row[6])
        is_closed = close_ms < now_ms
        if not is_closed:
            continue
        closed_klines.append(
            {
                "t": open_ms,
                "T": close_ms,
                "s": symbol.upper(),
                "i": interval,
                "o": str(row[1]),
                "h": str(row[2]),
                "l": str(row[3]),
                "c": str(row[4]),
                "v": str(row[5]),
                "q": str(row[7]),
                "n": int(row[8]),
                "V": str(row[9]),
                "Q": str(row[10]),
                "x": True,
            }
        )
    return closed_klines


class BinanceSpotWebSocketStream:
    """RFC 6455 Protocol-Hardened & Fail-Safe TLS WebSocket Client for `stream.binance.com:9443` (Item 1).

    Protocol Compatibility & Safety Guarantees:
    1. Handshake Verification (RFC 6455 §4.2.2): Validates HTTP 101 status, `Upgrade: websocket`,
       `Connection: Upgrade`, and exact SHA-1 `Sec-WebSocket-Accept` digest.
    2. Fragmented Frames & Interleaved Control Frames (RFC 6455 §5.4 & §5.5):
       - Reassembles multi-frame fragmented messages (`FIN=0, opcode=0x1` -> `FIN=0, opcode=0x0` -> `FIN=1, opcode=0x0`).
       - Allows `PING` (`0x9`) and `PONG` (`0xA`) control frames to arrive in the middle of a fragmented
         message without corrupting the in-flight fragment buffer.
       - Rejects protocol violations: non-zero `RSV1..3` bits, fragmented control frames (`opcode >= 0x8` with `FIN=0`),
         control frames with payload > 125 bytes, uninitiated continuation frames (`opcode=0x0` when not fragmenting),
         and overlapping new data frames (`opcode in {0x1, 0x2}` while a fragmented message is incomplete).
    3. Memory Fail-Safety Bounds: Enforces `max_frame_payload_bytes` (default 4 MiB) and `max_message_bytes`
       (default 8 MiB) prior to socket allocation to prevent OOM attacks on the 2GB cloud server.
    4. Close Frame Handshake (RFC 6455 §5.5.1): Parses 2-byte status code and UTF-8 reason on `0x8 CLOSE`,
       echoes a masked `CLOSE` frame back to peer, closes transport, and raises `WebSocketClosedError(code, reason)`.
    5. TLS Verification: Enforces `ssl.CERT_REQUIRED`, hostname verification, `TLSv1.2+` minimum, and
       records negotiated `tls_version` and `tls_cipher`.
    """

    def __init__(
        self,
        streams: Sequence[str] = DEFAULT_COMBINED_STREAMS_100MS,
        host: str = "stream.binance.com",
        port: int = 9443,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        max_frame_payload_bytes: int = 4 * 1024 * 1024,
        max_message_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.streams = tuple(streams)
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.server_hostname = server_hostname or host
        self.max_frame_payload_bytes = max_frame_payload_bytes
        self.max_message_bytes = max_message_bytes

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._close_sent: bool = False

        # Protocol & fail-safety telemetry (Item 1)
        self.tls_version: str | None = None
        self.tls_cipher: str | None = None
        self.pings_received_and_replied: int = 0
        self.last_ping_payload: bytes = b""
        self.pongs_received: int = 0
        self.last_pong_payload: bytes = b""
        self.fragmented_messages_reassembled: int = 0
        self.close_frames_received: int = 0
        self.last_close_code: int | None = None
        self.last_close_reason: str = ""
        self.reconnect_count: int = 0

    @staticmethod
    def build_default_tls_context() -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    async def connect(self, timeout_sec: float = 10.0) -> None:
        ctx = self.ssl_context if self.ssl_context is not None else self.build_default_tls_context()
        self._close_sent = False
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.host,
                self.port,
                ssl=ctx,
                server_hostname=self.server_hostname if ctx.check_hostname else None,
            ),
            timeout=timeout_sec,
        )
        ssl_obj = self._writer.get_extra_info("ssl_object")
        if ssl_obj is not None:
            self.tls_version = ssl_obj.version()
            cipher_info = ssl_obj.cipher()
            self.tls_cipher = cipher_info[0] if cipher_info else None

        stream_path = "/".join(self.streams)
        path = f"/stream?streams={stream_path}"
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = compute_rfc6455_accept_key(ws_key)

        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._writer.write(handshake.encode("ascii"))
        await self._writer.drain()

        status_line_bytes = await asyncio.wait_for(self._reader.readline(), timeout=timeout_sec)
        status_line = status_line_bytes.decode("latin1", errors="replace").strip()
        if not status_line.startswith("HTTP/1.1 101"):
            await self._abort_transport()
            raise WebSocketHandshakeError(f"WebSocket upgrade failed: {status_line}")

        headers: dict[str, str] = {}
        while True:
            hdr_bytes = await asyncio.wait_for(self._reader.readline(), timeout=timeout_sec)
            if hdr_bytes in (b"\r\n", b"\n", b""):
                break
            line = hdr_bytes.decode("latin1", errors="replace").strip()
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if headers.get("upgrade", "").lower() != "websocket":
            await self._abort_transport()
            raise WebSocketHandshakeError(f"Missing or invalid Upgrade header: {headers.get('upgrade')!r}")
        if "upgrade" not in headers.get("connection", "").lower():
            await self._abort_transport()
            raise WebSocketHandshakeError(f"Missing or invalid Connection header: {headers.get('connection')!r}")
        actual_accept = headers.get("sec-websocket-accept", "")
        if actual_accept != expected_accept:
            await self._abort_transport()
            raise WebSocketHandshakeError(
                f"Sec-WebSocket-Accept mismatch: expected {expected_accept!r}, got {actual_accept!r}"
            )

    async def reconnect(self, timeout_sec: float = 10.0) -> None:
        """Close any existing session and establish a fresh WebSocket connection."""
        await self.close()
        self.reconnect_count += 1
        await self.connect(timeout_sec=timeout_sec)

    async def send_ping(self, payload: bytes = b"SUIQ-PING") -> None:
        """Send a masked RFC 6455 PING control frame (opcode 0x9)."""
        if len(payload) > 125:
            raise WebSocketProtocolError("PING payload cannot exceed 125 bytes (RFC 6455 §5.5)")
        await self._send_masked_frame(0x9, payload)

    async def recv_json(self, timeout_sec: float = 30.0) -> tuple[dict[str, Any], datetime, int]:
        """Read next complete JSON message, handling fragmented frames and interleaved control frames."""
        if self._reader is None or self._writer is None:
            raise ConnectionError("WebSocket is not connected")

        fragments: list[bytes] = []
        accumulated_bytes = 0
        fragment_count = 0

        while True:
            hdr2 = await asyncio.wait_for(self._reader.readexactly(2), timeout=timeout_sec)
            b1, b2 = hdr2[0], hdr2[1]
            fin = bool(b1 & 0x80)
            rsv = b1 & 0x70
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            payload_len = b2 & 0x7F

            if rsv != 0:
                await self._abort_transport()
                raise WebSocketProtocolError(f"Non-zero RSV bits (0x{rsv:02x}) without negotiated extension")

            is_control = bool(opcode & 0x08)
            if is_control:
                if not fin:
                    await self._abort_transport()
                    raise WebSocketProtocolError(f"Fragmented control frame (opcode=0x{opcode:x}, FIN=0) forbidden by RFC 6455 §5.5")
                if payload_len > 125:
                    await self._abort_transport()
                    raise WebSocketProtocolError(f"Control frame payload length ({payload_len}) > 125 forbidden by RFC 6455 §5.5")

            if payload_len == 126:
                ext = await asyncio.wait_for(self._reader.readexactly(2), timeout=timeout_sec)
                payload_len = struct.unpack("!H", ext)[0]
            elif payload_len == 127:
                ext = await asyncio.wait_for(self._reader.readexactly(8), timeout=timeout_sec)
                payload_len = struct.unpack("!Q", ext)[0]

            if payload_len > self.max_frame_payload_bytes:
                await self._abort_transport()
                raise WebSocketProtocolError(
                    f"Frame payload ({payload_len} B) exceeds max_frame_payload_bytes ({self.max_frame_payload_bytes} B)"
                )

            mask_key = await asyncio.wait_for(self._reader.readexactly(4), timeout=timeout_sec) if masked else b""
            payload = await asyncio.wait_for(self._reader.readexactly(payload_len), timeout=timeout_sec)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            # Handle Control Frames (0x8 CLOSE, 0x9 PING, 0xA PONG) — may be interleaved inside fragmented data!
            if opcode == 0x8:  # CLOSE
                self.close_frames_received += 1
                if len(payload) == 1:
                    await self._abort_transport()
                    raise WebSocketProtocolError("CLOSE frame payload cannot be 1 byte (RFC 6455 §5.5.1)")
                elif len(payload) >= 2:
                    close_code = struct.unpack("!H", payload[:2])[0]
                    close_reason = payload[2:].decode("utf-8", errors="replace")
                    reply_payload = payload[:2]
                else:
                    close_code = 1005
                    close_reason = ""
                    reply_payload = struct.pack("!H", 1000)

                self.last_close_code = close_code
                self.last_close_reason = close_reason
                if not self._close_sent:
                    self._close_sent = True
                    try:
                        await self._send_masked_frame(0x8, reply_payload)
                    except Exception:
                        pass
                await self._abort_transport()
                raise WebSocketClosedError(close_code=close_code, close_reason=close_reason)

            if opcode == 0x9:  # PING -> reply with masked PONG (0xA) echoing exact payload
                self.pings_received_and_replied += 1
                self.last_ping_payload = payload
                await self._send_masked_frame(0xA, payload)
                continue

            if opcode == 0xA:  # PONG
                self.pongs_received += 1
                self.last_pong_payload = payload
                continue

            # Handle Data Frames (0x1 TEXT, 0x2 BINARY, 0x0 CONTINUATION)
            if opcode == 0x0:
                if not fragments:
                    await self._abort_transport()
                    raise WebSocketProtocolError("Received CONTINUATION frame (0x0) with no active fragmented message")
            elif opcode in (0x1, 0x2):
                if fragments:
                    await self._abort_transport()
                    raise WebSocketProtocolError(
                        f"Received new data frame (opcode=0x{opcode:x}) before previous fragmented message finished"
                    )
            else:
                await self._abort_transport()
                raise WebSocketProtocolError(f"Unsupported WebSocket opcode: 0x{opcode:x}")

            accumulated_bytes += len(payload)
            if accumulated_bytes > self.max_message_bytes:
                await self._abort_transport()
                raise WebSocketProtocolError(
                    f"Reassembled message ({accumulated_bytes} B) exceeds max_message_bytes ({self.max_message_bytes} B)"
                )

            fragments.append(payload)
            fragment_count += 1

            if fin:
                recv_mono = monotonic_ns()
                recv_utc = utc_now()
                if fragment_count > 1:
                    self.fragmented_messages_reassembled += 1
                full_bytes = b"".join(fragments)
                return json.loads(full_bytes.decode("utf-8")), recv_utc, recv_mono

    async def _send_masked_frame(self, opcode: int, payload: bytes) -> None:
        if self._writer is None:
            return
        b1 = 0x80 | (opcode & 0x0F)
        length = len(payload)
        mask_key = os.urandom(4)
        header = bytearray([b1])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self._writer.write(bytes(header) + masked_payload)
        await self._writer.drain()

    async def _abort_transport(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def close(self, code: int = 1000) -> None:
        if self._writer is not None:
            try:
                if not self._close_sent:
                    self._close_sent = True
                    await self._send_masked_frame(0x8, struct.pack("!H", code))
            except Exception:
                pass
            await self._abort_transport()


def _current_rss_bytes() -> int:
    statm = Path("/proc/self/statm")
    if statm.exists():
        parts = statm.read_text(encoding="utf-8").split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1]) * 4096
    return 18 * 1024 * 1024


async def run_live_f01_daemon(
    db_path: Path,
    spool_dir: Path,
    duration_sec: float = 0.0,
) -> dict[str, Any]:
    """Run the live F-01 collector against Binance Spot WebSocket + REST on the server."""
    from sui_quant.data.sync_engine import CloudShardExporter

    store = StateStore(db_path)
    alert_mgr = AlertManager(store)
    exporter = CloudShardExporter(spool_dir=spool_dir, max_spool_bytes=MAX_SPOOL_QUOTA_BYTES)
    collector = F01MarketCollector(
        store=store,
        alert_manager=alert_mgr,
        spool_dir=spool_dir,
    )

    boot_id = uuid.uuid4().hex[:12]
    pid = os.getpid()
    tick_seq = 0
    started_mono = monotonic_ns()
    last_hb_mono = 0
    last_scan_mono = 0
    last_shard_mono = monotonic_ns()
    pending_shard_records: list[dict[str, Any]] = []

    # Initial REST backfill of recent 5m & 1m closed bars
    for sym in (TRADING_SYMBOL, REFERENCE_SYMBOL):
        for interval in ("1m", "5m"):
            try:
                klines = await asyncio.to_thread(fetch_binance_rest_klines, sym, interval, 25)
                now_u = utc_now()
                for kd in klines:
                    ex_u = datetime.fromtimestamp(int(kd["T"]) / 1000.0, tz=timezone.utc)
                    q, bar, _ = collector.quality_guard.ingest_kline_payload(
                        symbol=sym,
                        kline_dict=kd,
                        exchange_time_utc=ex_u,
                        recv_time_utc=now_u,
                        backfilled_via_rest=True,
                    )
                    if q == DataQuality.OK and bar is not None:
                        collector._persist_closed_kline(bar)
                        pending_shard_records.append(
                            {
                                "record_type": "kline_closed",
                                "symbol": bar.symbol,
                                "interval": bar.interval,
                                "open_time_ms": bar.open_time_ms,
                                "close_time_ms": bar.close_time_ms,
                                "open_price": str(bar.open_price),
                                "high_price": str(bar.high_price),
                                "low_price": str(bar.low_price),
                                "close_price": str(bar.close_price),
                                "base_volume": str(bar.base_volume),
                                "quote_volume": str(bar.quote_volume),
                                "exchange_time_utc": bar.exchange_time_utc.isoformat(),
                                "recv_time_utc": bar.recv_time_utc.isoformat(),
                            }
                        )
            except Exception:
                pass

    ws = BinanceSpotWebSocketStream(DEFAULT_COMBINED_STREAMS_100MS)
    try:
        while True:
            try:
                await ws.connect(timeout_sec=10.0)
                collector.active_conn_opened_utc = utc_now()
                collector.quality_guard.mark_recovery_complete()

                while True:
                    now_m = monotonic_ns()
                    if duration_sec > 0 and (now_m - started_mono) / 1e9 >= duration_sec:
                        if pending_shard_records:
                            exporter.write_shard("MIXED", "market_events", pending_shard_records)
                            pending_shard_records.clear()
                        lat = collector.compute_latency_percentiles()
                        bp = collector.get_backpressure_telemetry()
                        return {
                            "total_valid_events": collector.total_valid_events_processed,
                            "latency_p50_ms": str(lat.p50_ms),
                            "latency_p95_ms": str(lat.p95_ms),
                            "latency_p99_ms": str(lat.p99_ms),
                            "latency_max_ms": str(lat.max_ms),
                            "within_slo": lat.within_slo,
                            "rss_mib": round(_current_rss_bytes() / (1024 * 1024), 2),
                            "data_quality": collector.quality_guard.current_quality.value,
                            "tls_version": ws.tls_version,
                            "tls_cipher": ws.tls_cipher,
                            "critical_dropped_count": bp.critical_dropped_count,
                        }

                    # 5s heartbeat update
                    if (now_m - last_hb_mono) / 1e9 >= 5.0:
                        tick_seq += 1
                        now_iso = utc_now().isoformat()
                        store.upsert_heartbeat(
                            service_name="f01",
                            pid=pid,
                            boot_id=boot_id,
                            monotonic_ns=now_m,
                            wall_time_utc_iso=now_iso,
                            loop_tick_seq=tick_seq,
                            last_work_time_utc_iso=now_iso,
                            rss_bytes=_current_rss_bytes(),
                            service_state="RUNNING",
                        )
                        last_hb_mono = now_m

                    # 1s fast scan
                    if (now_m - last_scan_mono) / 1e9 >= 1.0:
                        collector.run_1s_fast_scan()
                        last_scan_mono = now_m

                    # Flush spool shard every 30s or 200 records
                    if len(pending_shard_records) >= 200 or (
                        pending_shard_records and (now_m - last_shard_mono) / 1e9 >= 30.0
                    ):
                        exporter.write_shard("MIXED", "market_events", pending_shard_records)
                        pending_shard_records.clear()
                        last_shard_mono = now_m

                    raw_msg, r_utc, r_mono = await ws.recv_json(timeout_sec=15.0)
                    collector.enqueue_raw_message(raw_msg, recv_time_utc=r_utc, recv_mono_ns=r_mono)
                    collector.drain_and_process_queue(max_items=64)

                    stream_name = str(raw_msg.get("stream", ""))
                    if stream_name.endswith("@aggTrade") and len(pending_shard_records) < 250:
                        d = raw_msg.get("data", {})
                        pending_shard_records.append(
                            {
                                "record_type": "aggTrade",
                                "symbol": str(d.get("s", "SUIUSDT")),
                                "agg_trade_id": int(d.get("a", 0)),
                                "price": str(d.get("p", "0")),
                                "qty": str(d.get("q", "0")),
                                "is_buyer_maker": bool(d.get("m", False)),
                                "exchange_time_utc": datetime.fromtimestamp(
                                    int(d.get("E", int(r_utc.timestamp() * 1000))) / 1000.0, tz=timezone.utc
                                ).isoformat(),
                                "recv_time_utc": r_utc.isoformat(),
                            }
                        )

            except Exception as exc:
                collector.handle_unexpected_disconnect(reason=str(exc))
                await ws.close()
                if duration_sec > 0 and (monotonic_ns() - started_mono) / 1e9 >= duration_sec:
                    break
                await asyncio.sleep(2.0)
    finally:
        await ws.close()
        store.close()
    return {"total_valid_events": collector.total_valid_events_processed}


def main() -> None:
    parser = argparse.ArgumentParser(description="SUI-Quant F-01 Real-Time Market Collector Daemon")
    parser.add_argument("--db-path", default="/var/lib/sui-quant/state.db")
    parser.add_argument("--spool-dir", default="/var/lib/sui-quant/spool")
    parser.add_argument("--duration-sec", type=float, default=0.0)
    args = parser.parse_args()
    res = asyncio.run(
        run_live_f01_daemon(
            db_path=Path(args.db_path),
            spool_dir=Path(args.spool_dir),
            duration_sec=args.duration_sec,
        )
    )
    print(json.dumps(res))


if __name__ == "__main__":
    main()
