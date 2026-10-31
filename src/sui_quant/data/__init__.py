"""F-01 real-time market data collection, quality guards, ring buffers, and E-01 cloud-local incremental sync."""

from __future__ import annotations

from typing import Any

from sui_quant.data.quality_guard import (
    CALIBRATION_UNRECOVERABLE_TAG,
    PARTIAL_BOOK_SCOPE_DECLARATION,
    ClosedKlineBar,
    OrderbookGapMarker,
    PartialOrderbookSnapshot,
    SequenceGapRecord,
    StreamQualityGuard,
    UnclosedKlineTransientView,
)
from sui_quant.data.ring_buffer import (
    BackpressureTelemetry,
    BoundedBackpressureQueue,
    BufferTelemetry,
    FixedRingBuffer,
)
from sui_quant.data.sync_engine import (
    CloudShardExporter,
    LocalResearchSyncEngine,
    SecretSyncBlockedError,
    ShardManifestEntry,
    SyncIntegrityError,
    SyncRunReport,
    assert_safe_for_sync,
)

__all__ = [
    "AggTradeRecord",
    "BackpressureTelemetry",
    "BinanceSpotWebSocketStream",
    "BookTickerQuote",
    "BoundedBackpressureQueue",
    "BufferTelemetry",
    "CALIBRATION_UNRECOVERABLE_TAG",
    "ClosedKlineBar",
    "CloudShardExporter",
    "DEFAULT_COMBINED_STREAMS_100MS",
    "DEGRADED_COMBINED_STREAMS_1000MS",
    "F01MarketCollector",
    "FastScanSnapshot",
    "FixedRingBuffer",
    "LatencyPercentiles",
    "LocalResearchSyncEngine",
    "OrderbookGapMarker",
    "PARTIAL_BOOK_SCOPE_DECLARATION",
    "PartialOrderbookSnapshot",
    "SecretSyncBlockedError",
    "SequenceGapRecord",
    "ShardManifestEntry",
    "StreamQualityGuard",
    "SyncIntegrityError",
    "SyncRunReport",
    "UnclosedKlineTransientView",
    "WebSocketClosedError",
    "WebSocketHandshakeError",
    "WebSocketProtocolError",
    "assert_safe_for_sync",
    "compute_rfc6455_accept_key",
    "fetch_binance_rest_klines",
]


def __getattr__(name: str) -> Any:
    if name in {
        "AggTradeRecord",
        "BinanceSpotWebSocketStream",
        "BookTickerQuote",
        "DEFAULT_COMBINED_STREAMS_100MS",
        "DEGRADED_COMBINED_STREAMS_1000MS",
        "F01MarketCollector",
        "FastScanSnapshot",
        "LatencyPercentiles",
        "WebSocketClosedError",
        "WebSocketHandshakeError",
        "WebSocketProtocolError",
        "compute_rfc6455_accept_key",
        "fetch_binance_rest_klines",
    }:
        from sui_quant.data import collector

        return getattr(collector, name)
    raise AttributeError(f"module 'sui_quant.data' has no attribute {name!r}")
