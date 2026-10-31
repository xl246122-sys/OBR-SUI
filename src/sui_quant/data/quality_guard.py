"""Data quality guard for F-01: deduplication, out-of-order rejection, staleness check,
closed vs. unclosed K-line physical isolation, and unrecoverable orderbook gap marking (§2.1, §14.1, QA-01, QA-02).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from sui_quant.contracts import ALLOWED_SYMBOLS, DataQuality, MarketEvent, MarketFeatures, require_decimal
from sui_quant.data.ring_buffer import FixedRingBuffer
from sui_quant.utils.clock import ensure_utc


CALIBRATION_UNRECOVERABLE_TAG = "GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION"
PARTIAL_BOOK_SCOPE_DECLARATION = "TOP10_AGGREGATED_SNAPSHOT_NO_L3_QUEUE_POSITION"


@dataclass(frozen=True)
class ClosedKlineBar:
    """Immutable representation of a strictly closed K-line bar (`k.x == True`)."""

    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    trade_count: int
    exchange_time_utc: datetime
    recv_time_utc: datetime
    is_closed: bool = True
    backfilled_via_rest: bool = False

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Unsupported symbol '{self.symbol}'")
        if not self.is_closed:
            raise ValueError("ClosedKlineBar requires is_closed=True; unclosed bars are physically forbidden here.")
        ensure_utc(self.exchange_time_utc, "exchange_time_utc")
        ensure_utc(self.recv_time_utc, "recv_time_utc")
        for name, val in (
            ("open_price", self.open_price),
            ("high_price", self.high_price),
            ("low_price", self.low_price),
            ("close_price", self.close_price),
            ("base_volume", self.base_volume),
            ("quote_volume", self.quote_volume),
            ("taker_buy_base_volume", self.taker_buy_base_volume),
            ("taker_buy_quote_volume", self.taker_buy_quote_volume),
        ):
            require_decimal(val, name)

    def to_complete_bar_features(self, extra_features: Mapping[str, Decimal] | None = None) -> MarketFeatures:
        """Convert this verified closed bar into a `MarketFeatures(complete_bar_only=True)` contract."""
        feat: dict[str, Decimal] = {
            "open": self.open_price,
            "high": self.high_price,
            "low": self.low_price,
            "close": self.close_price,
            "volume": self.base_volume,
            "quote_volume": self.quote_volume,
            "taker_buy_volume": self.taker_buy_base_volume,
            "taker_buy_quote_volume": self.taker_buy_quote_volume,
            "trade_count": Decimal(self.trade_count),
        }
        if extra_features:
            for k, v in extra_features.items():
                feat[k] = require_decimal(v, f"extra_features[{k}]")
        return MarketFeatures(
            feature_time_utc=self.exchange_time_utc,
            horizon=self.interval,
            symbol=self.symbol,
            complete_bar_only=True,
            features=feat,
            data_version="v1.0-p2-closed-bar",
        )


@dataclass(frozen=True)
class UnclosedKlineTransientView:
    """Transient in-memory view of an in-progress K-line (`k.x == False`).

    Physically isolated from `ClosedKlineBar`; cannot be converted into `complete_bar_only=True`
    features and cannot trigger A-02 5m breakout evaluations.
    """

    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    current_price: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    exchange_time_utc: datetime
    recv_time_utc: datetime
    is_closed: bool = False

    def __post_init__(self) -> None:
        if self.is_closed:
            raise ValueError("UnclosedKlineTransientView is strictly for unclosed (is_closed=False) bars.")
        ensure_utc(self.exchange_time_utc, "exchange_time_utc")
        ensure_utc(self.recv_time_utc, "recv_time_utc")
        for name, val in (
            ("open_price", self.open_price),
            ("high_price", self.high_price),
            ("low_price", self.low_price),
            ("current_price", self.current_price),
            ("base_volume", self.base_volume),
            ("quote_volume", self.quote_volume),
        ):
            require_decimal(val, name)


@dataclass(frozen=True)
class PartialOrderbookSnapshot:
    """Top-10 aggregated orderbook snapshot (`@depth10@100ms` or `@depth10@1000ms`).

    Explicitly declares `scope = TOP10_AGGREGATED_SNAPSHOT_NO_L3_QUEUE_POSITION` (§2.1, §14.1).
    """

    symbol: str
    last_update_id: int
    bids: tuple[tuple[Decimal, Decimal], ...]  # ((price, qty), ...) top 10 descending
    asks: tuple[tuple[Decimal, Decimal], ...]  # ((price, qty), ...) top 10 ascending
    exchange_time_utc: datetime
    recv_time_utc: datetime
    recv_monotonic_ns: int
    sampling_interval_ms: int = 100
    scope: str = PARTIAL_BOOK_SCOPE_DECLARATION

    def __post_init__(self) -> None:
        if self.symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Unsupported symbol '{self.symbol}'")
        ensure_utc(self.exchange_time_utc, "exchange_time_utc")
        ensure_utc(self.recv_time_utc, "recv_time_utc")
        if len(self.bids) > 10 or len(self.asks) > 10:
            raise ValueError("depth10 snapshot cannot exceed 10 bid or ask levels")
        for side_name, levels in (("bids", self.bids), ("asks", self.asks)):
            for p, q in levels:
                require_decimal(p, f"{side_name}.price")
                require_decimal(q, f"{side_name}.qty")

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None

    @property
    def spread_bps(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        bid = self.bids[0][0]
        ask = self.asks[0][0]
        mid = (bid + ask) / Decimal("2")
        if mid <= Decimal("0"):
            return None
        return ((ask - bid) / mid) * Decimal("10000")

    @property
    def top10_imbalance(self) -> Decimal:
        """Compute orderbook volume imbalance `(sum_bid - sum_ask) / (sum_bid + sum_ask)` in [-1, 1]."""
        bid_vol = sum((q for _, q in self.bids), Decimal("0"))
        ask_vol = sum((q for _, q in self.asks), Decimal("0"))
        total = bid_vol + ask_vol
        if total <= Decimal("0"):
            return Decimal("0")
        return (bid_vol - ask_vol) / total


@dataclass(frozen=True)
class OrderbookGapMarker:
    """Explicit unrecoverable gap marker for partial orderbook streams during WS disconnects (§2.1, §14.1, QA-01).

    Since historical `@depth10@100ms` states cannot be reconstructed via REST after a disconnect,
    any gap window is permanently tagged `GAP_UNRECOVERABLE_FOR_FILL_CALIBRATION` and blocked from
    being used in B-01 limit-order fill probability calibration.
    """

    gap_id: str
    symbol: str
    stream: str
    gap_start_utc: datetime
    gap_end_utc: datetime
    last_update_id_before_gap: int | None
    first_update_id_after_gap: int | None
    reason: str
    recoverable: bool = False
    calibration_tag: str = CALIBRATION_UNRECOVERABLE_TAG
    allow_b01_fill_calibration: bool = False

    def __post_init__(self) -> None:
        ensure_utc(self.gap_start_utc, "gap_start_utc")
        ensure_utc(self.gap_end_utc, "gap_end_utc")
        if self.gap_end_utc < self.gap_start_utc:
            raise ValueError("gap_end_utc cannot be earlier than gap_start_utc")
        if self.recoverable:
            raise ValueError("Historical depth10 orderbook gaps are strictly non-recoverable via REST (recoverable must be False)")
        if self.calibration_tag != CALIBRATION_UNRECOVERABLE_TAG:
            raise ValueError(f"calibration_tag must be '{CALIBRATION_UNRECOVERABLE_TAG}'")
        if self.allow_b01_fill_calibration:
            raise ValueError("allow_b01_fill_calibration must be False for unrecoverable orderbook gaps")


class StreamQualityGuard:
    """Stateful quality guard enforcing deduplication, sequence monotonicity, staleness bounds,
    closed/unclosed K-line isolation, and unrecoverable orderbook gap tracking.
    """

    def __init__(
        self,
        max_staleness_ms: int = 2000,
        dedup_window_size: int = 8192,
        closed_kline_buffer_capacity: int = 500,
    ) -> None:
        if max_staleness_ms <= 0:
            raise ValueError("max_staleness_ms must be > 0")
        self.max_staleness_ms = max_staleness_ms
        self.dedup_window_size = dedup_window_size
        self.closed_kline_buffer_capacity = closed_kline_buffer_capacity

        # Bounded deduplication set for (symbol, source, event_id)
        self._dedup_queue: deque[tuple[str, str, str]] = deque()
        self._dedup_set: set[tuple[str, str, str]] = set()

        # Monotonic sequence tracking per (symbol, stream_category)
        self._last_seq: dict[tuple[str, str], int] = {}

        # Closed vs Unclosed K-line physical isolation
        self._closed_bar_dedup_keys: set[tuple[str, str, int]] = set()
        self._closed_bar_dedup_queue: deque[tuple[str, str, int]] = deque()
        self._closed_bars: dict[tuple[str, str], FixedRingBuffer[ClosedKlineBar]] = {}
        self._unclosed_views: dict[tuple[str, str], UnclosedKlineTransientView] = {}

        # Unrecoverable orderbook gap markers
        self._orderbook_gaps: list[OrderbookGapMarker] = []

        # Active health flag
        self._current_quality: DataQuality = DataQuality.OK
        self._duplicate_count = 0
        self._out_of_order_count = 0
        self._stale_count = 0
        self._gap_count = 0

    @property
    def current_quality(self) -> DataQuality:
        return self._current_quality

    @property
    def duplicate_count(self) -> int:
        return self._duplicate_count

    @property
    def out_of_order_count(self) -> int:
        return self._out_of_order_count

    @property
    def stale_count(self) -> int:
        return self._stale_count

    @property
    def gap_count(self) -> int:
        return self._gap_count

    @property
    def orderbook_gaps(self) -> tuple[OrderbookGapMarker, ...]:
        return tuple(self._orderbook_gaps)

    def evaluate_event(self, event: MarketEvent) -> DataQuality:
        """Validate a `MarketEvent` for deduplication, sequence order, and staleness (QA-01, QA-02)."""
        dedup_key = (event.symbol, event.source, event.event_id)
        if dedup_key in self._dedup_set:
            self._duplicate_count += 1
            return DataQuality.DUPLICATE

        stream_key = (event.symbol, event.source)
        if event.seq_or_update_id is not None:
            prev_seq = self._last_seq.get(stream_key)
            if prev_seq is not None:
                if event.seq_or_update_id == prev_seq:
                    self._duplicate_count += 1
                    return DataQuality.DUPLICATE
                if event.seq_or_update_id < prev_seq:
                    self._out_of_order_count += 1
                    return DataQuality.OUT_OF_ORDER
            self._last_seq[stream_key] = event.seq_or_update_id

        # Record in bounded deduplication window
        if len(self._dedup_queue) >= self.dedup_window_size:
            oldest = self._dedup_queue.popleft()
            self._dedup_set.discard(oldest)
        self._dedup_queue.append(dedup_key)
        self._dedup_set.add(dedup_key)

        # Check exchange-to-receive staleness
        lag_ms = (event.recv_time_utc - event.exchange_time_utc).total_seconds() * 1000.0
        if lag_ms > self.max_staleness_ms:
            self._stale_count += 1
            self._current_quality = DataQuality.STALE
            return DataQuality.STALE

        return DataQuality.OK

    def ingest_kline_payload(
        self,
        symbol: str,
        kline_dict: Mapping[str, Any],
        exchange_time_utc: datetime,
        recv_time_utc: datetime,
        backfilled_via_rest: bool = False,
    ) -> tuple[DataQuality, ClosedKlineBar | None, UnclosedKlineTransientView | None]:
        """Process a K-line event while strictly isolating unclosed (`x=False`) vs closed (`x=True`) bars.

        Returns `(quality, closed_bar_if_newly_closed, unclosed_transient_view_if_unclosed)`.
        Guarantees that a closed bar `(symbol, interval, open_time_ms)` is emitted as `ClosedKlineBar`
        AT MOST ONCE even if duplicate WebSocket messages or REST backfills arrive (QA-01).
        """
        ensure_utc(exchange_time_utc, "exchange_time_utc")
        ensure_utc(recv_time_utc, "recv_time_utc")

        interval = str(kline_dict["i"])
        open_time_ms = int(kline_dict["t"])
        close_time_ms = int(kline_dict["T"])
        is_closed = bool(kline_dict["x"])

        open_p = Decimal(str(kline_dict["o"]))
        high_p = Decimal(str(kline_dict["h"]))
        low_p = Decimal(str(kline_dict["l"]))
        close_p = Decimal(str(kline_dict["c"]))
        base_vol = Decimal(str(kline_dict["v"]))
        quote_vol = Decimal(str(kline_dict["q"]))
        taker_base = Decimal(str(kline_dict.get("V", "0")))
        taker_quote = Decimal(str(kline_dict.get("Q", "0")))
        trades_cnt = int(kline_dict.get("n", 0))

        bar_key = (symbol, interval, open_time_ms)
        series_key = (symbol, interval)

        if not is_closed:
            # If bar is already closed, ignore late unclosed updates for the same open_time_ms
            if bar_key in self._closed_bar_dedup_keys:
                self._duplicate_count += 1
                return DataQuality.DUPLICATE, None, None

            transient = UnclosedKlineTransientView(
                symbol=symbol,
                interval=interval,
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
                open_price=open_p,
                high_price=high_p,
                low_price=low_p,
                current_price=close_p,
                base_volume=base_vol,
                quote_volume=quote_vol,
                exchange_time_utc=exchange_time_utc,
                recv_time_utc=recv_time_utc,
                is_closed=False,
            )
            self._unclosed_views[series_key] = transient
            return DataQuality.OK, None, transient

        # Closed bar path (k.x == True): enforce strict once-only confirmation
        if bar_key in self._closed_bar_dedup_keys:
            self._duplicate_count += 1
            return DataQuality.DUPLICATE, None, None

        # Check chronological ordering against latest closed bar for this (symbol, interval)
        buf = self._closed_bars.get(series_key)
        if buf is None:
            buf = FixedRingBuffer(self.closed_kline_buffer_capacity)
            self._closed_bars[series_key] = buf
        else:
            latest_closed = buf.latest()
            if latest_closed is not None and not backfilled_via_rest:
                if open_time_ms < latest_closed.open_time_ms:
                    self._out_of_order_count += 1
                    return DataQuality.OUT_OF_ORDER, None, None

        if len(self._closed_bar_dedup_queue) >= self.dedup_window_size:
            old_bar_key = self._closed_bar_dedup_queue.popleft()
            self._closed_bar_dedup_keys.discard(old_bar_key)
        self._closed_bar_dedup_queue.append(bar_key)
        self._closed_bar_dedup_keys.add(bar_key)

        closed_bar = ClosedKlineBar(
            symbol=symbol,
            interval=interval,
            open_time_ms=open_time_ms,
            close_time_ms=close_time_ms,
            open_price=open_p,
            high_price=high_p,
            low_price=low_p,
            close_price=close_p,
            base_volume=base_vol,
            quote_volume=quote_vol,
            taker_buy_base_volume=taker_base,
            taker_buy_quote_volume=taker_quote,
            trade_count=trades_cnt,
            exchange_time_utc=exchange_time_utc,
            recv_time_utc=recv_time_utc,
            is_closed=True,
            backfilled_via_rest=backfilled_via_rest,
        )
        buf.append(closed_bar)

        # Clear transient view if it belonged to this now-closed bar
        existing_transient = self._unclosed_views.get(series_key)
        if existing_transient is not None and existing_transient.open_time_ms <= open_time_ms:
            self._unclosed_views.pop(series_key, None)

        return DataQuality.OK, closed_bar, None

    def get_closed_bars(self, symbol: str, interval: str, limit: int | None = None) -> list[ClosedKlineBar]:
        buf = self._closed_bars.get((symbol, interval))
        if buf is None:
            return []
        if limit is not None:
            return buf.tail(limit)
        return buf.items()

    def get_unclosed_transient_view(self, symbol: str, interval: str) -> UnclosedKlineTransientView | None:
        return self._unclosed_views.get((symbol, interval))

    def mark_ws_disconnected(self) -> DataQuality:
        """Mark active data quality as GAP_DETECTED immediately on WebSocket loss."""
        self._gap_count += 1
        self._current_quality = DataQuality.GAP_DETECTED
        return self._current_quality

    def record_unrecoverable_orderbook_gap(
        self,
        gap_id: str,
        symbol: str,
        stream: str,
        gap_start_utc: datetime,
        gap_end_utc: datetime,
        last_update_id_before_gap: int | None,
        first_update_id_after_gap: int | None,
        reason: str = "WS_DISCONNECT_ORDERBOOK_UNRECOVERABLE",
    ) -> OrderbookGapMarker:
        """Record an explicit unrecoverable orderbook gap marker during disconnect recovery."""
        marker = OrderbookGapMarker(
            gap_id=gap_id,
            symbol=symbol,
            stream=stream,
            gap_start_utc=gap_start_utc,
            gap_end_utc=gap_end_utc,
            last_update_id_before_gap=last_update_id_before_gap,
            first_update_id_after_gap=first_update_id_after_gap,
            reason=reason,
            recoverable=False,
            calibration_tag=CALIBRATION_UNRECOVERABLE_TAG,
            allow_b01_fill_calibration=False,
        )
        self._orderbook_gaps.append(marker)
        return marker

    def mark_recovery_complete(self) -> DataQuality:
        """Restore DataQuality.OK only after closed K-line REST backfill and gap marking are complete."""
        self._current_quality = DataQuality.OK
        return self._current_quality

    def is_window_valid_for_b01_calibration(
        self,
        symbol: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> bool:
        """Return False if any unrecoverable orderbook gap for `symbol` overlaps `[window_start_utc, window_end_utc]`."""
        ensure_utc(window_start_utc, "window_start_utc")
        ensure_utc(window_end_utc, "window_end_utc")
        for gap in self._orderbook_gaps:
            if gap.symbol != symbol:
                continue
            if gap.gap_start_utc <= window_end_utc and gap.gap_end_utc >= window_start_utc:
                return False
        return True
