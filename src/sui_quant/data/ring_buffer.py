"""Fixed-capacity zero-leak memory ring buffer and bounded backpressure queue for F-01 (§2.1, §13, QA-02).

P2 Remediation (Item 3):
- Strictly guarantees that critical closed K-lines (`k.x == True`) and DataQuality / control events
  (`STALE`, `GAP_DETECTED`, `OUT_OF_ORDER`, `WS_DISCONNECT`) are NEVER dropped under any backpressure scenario
  (`critical_dropped_count == 0` invariant).
- Records granular snapshot degradation and drop statistics (`dropped_by_category`, `degradation_events`,
  `high_watermark_triggers`, `low_watermark_recoveries`, `critical_enqueued_count`, `transient_dropped_count`).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Generic, TypeVar

from sui_quant.contracts import require_decimal

T = TypeVar("T")


@dataclass(frozen=True)
class BufferTelemetry:
    capacity: int
    current_size: int
    total_appended: int
    total_evicted: int
    utilization_pct: Decimal


@dataclass(frozen=True)
class BackpressureTelemetry:
    """Comprehensive telemetry for F-01 backpressure queue, snapshot degradation, and drop accounting."""

    maxsize: int
    current_qsize: int
    peak_qsize: int
    total_enqueued: int
    critical_enqueued_count: int
    critical_dropped_count: int
    transient_enqueued_count: int
    transient_dropped_count: int
    dropped_by_category: dict[str, int]
    degraded_mode: bool
    high_watermark_triggers: int
    low_watermark_recoveries: int
    degradation_events: tuple[dict[str, Any], ...]


class FixedRingBuffer(Generic[T]):
    """Pre-allocated fixed-capacity circular buffer with O(1) append and zero unbounded growth."""

    def __init__(self, capacity: int) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError(f"capacity must be a positive integer, got {capacity}")
        self._capacity = capacity
        self._slots: list[T | None] = [None] * capacity
        self._head = 0  # Next write index
        self._size = 0
        self._total_appended = 0
        self._total_evicted = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def size(self) -> int:
        return self._size

    @property
    def total_appended(self) -> int:
        return self._total_appended

    @property
    def total_evicted(self) -> int:
        return self._total_evicted

    def __len__(self) -> int:
        return self._size

    def append(self, item: T) -> None:
        """Append an item in O(1), overwriting the oldest slot when at capacity."""
        if self._size == self._capacity:
            self._total_evicted += 1
        else:
            self._size += 1
        self._slots[self._head] = item
        self._head = (self._head + 1) % self._capacity
        self._total_appended += 1

    def latest(self) -> T | None:
        """Return the most recently appended item, or None if empty."""
        if self._size == 0:
            return None
        idx = (self._head - 1) % self._capacity
        return self._slots[idx]

    def items(self) -> list[T]:
        """Return all buffered items in chronological order (oldest -> newest)."""
        if self._size == 0:
            return []
        start = (self._head - self._size) % self._capacity
        result: list[T] = []
        for i in range(self._size):
            val = self._slots[(start + i) % self._capacity]
            if val is not None:
                result.append(val)
        return result

    def tail(self, n: int) -> list[T]:
        """Return up to `n` most recent items in chronological order (oldest -> newest)."""
        if n <= 0 or self._size == 0:
            return []
        count = min(n, self._size)
        start = (self._head - count) % self._capacity
        result: list[T] = []
        for i in range(count):
            val = self._slots[(start + i) % self._capacity]
            if val is not None:
                result.append(val)
        return result

    def clear(self) -> None:
        for i in range(self._capacity):
            self._slots[i] = None
        self._head = 0
        self._size = 0

    def telemetry(self) -> BufferTelemetry:
        util = (Decimal(self._size) / Decimal(self._capacity)) * Decimal("100")
        return BufferTelemetry(
            capacity=self._capacity,
            current_size=self._size,
            total_appended=self._total_appended,
            total_evicted=self._total_evicted,
            utilization_pct=util.quantize(Decimal("0.01")),
        )


class BoundedBackpressureQueue(Generic[T]):
    """Bounded ingestion queue enforcing backpressure watermarks, automatic stream degradation,
    and zero-loss protection for critical closed K-lines and DataQuality events (QA-02, Item 3).

    Invariants:
    1. Critical items (`is_critical_fn(item) == True`: closed K-lines `k.x=True` and DataQuality events)
       are NEVER dropped (`critical_dropped_count == 0`).
    2. When occupancy reaches `high_watermark_ratio` (default 80%), triggers `on_high_watermark`
       (degrading `suiusdt@depth10@100ms` to `1000ms` and recording a degradation event).
    3. When full (`len >= maxsize`), only non-critical transient snapshots (`depth10`, `bookTicker`,
       `kline_unclosed`, `aggTrade`) are shed, and every drop is recorded by category in
       `dropped_by_category`.
    """

    def __init__(
        self,
        maxsize: int = 4096,
        high_watermark_ratio: Decimal = Decimal("0.80"),
        low_watermark_ratio: Decimal = Decimal("0.40"),
        is_critical_fn: Callable[[T], bool] | None = None,
        category_fn: Callable[[T], str] | None = None,
        on_high_watermark: Callable[[int, int], None] | None = None,
        on_low_watermark_recovery: Callable[[int, int], None] | None = None,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be > 0")
        hw = require_decimal(high_watermark_ratio, "high_watermark_ratio")
        lw = require_decimal(low_watermark_ratio, "low_watermark_ratio")
        if lw <= Decimal("0") or hw > Decimal("1") or lw >= hw:
            raise ValueError("Invalid watermark ratios: require 0 < low < high <= 1")
        self.maxsize = maxsize
        self.high_watermark_count = max(1, int(Decimal(maxsize) * hw))
        self.low_watermark_count = max(0, int(Decimal(maxsize) * lw))
        self._is_critical_fn = is_critical_fn
        self._category_fn = category_fn
        self._on_high_watermark = on_high_watermark
        self._on_low_watermark_recovery = on_low_watermark_recovery

        self._deque: deque[T] = deque()
        self._peak_size = 0
        self._dropped_count = 0
        self._critical_dropped_count = 0
        self._total_enqueued = 0
        self._critical_enqueued_count = 0
        self._transient_enqueued_count = 0
        self._dropped_by_category: dict[str, int] = {}
        self._degraded_mode = False
        self._high_watermark_triggers = 0
        self._low_watermark_recoveries = 0
        self._degradation_events: list[dict[str, Any]] = []

    @property
    def qsize(self) -> int:
        return len(self._deque)

    @property
    def peak_size(self) -> int:
        return self._peak_size

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def critical_dropped_count(self) -> int:
        return self._critical_dropped_count

    @property
    def critical_enqueued_count(self) -> int:
        return self._critical_enqueued_count

    @property
    def transient_enqueued_count(self) -> int:
        return self._transient_enqueued_count

    @property
    def dropped_by_category(self) -> dict[str, int]:
        return dict(self._dropped_by_category)

    @property
    def total_enqueued(self) -> int:
        return self._total_enqueued

    @property
    def degraded_mode(self) -> bool:
        return self._degraded_mode

    @property
    def high_watermark_triggers(self) -> int:
        return self._high_watermark_triggers

    @property
    def low_watermark_recoveries(self) -> int:
        return self._low_watermark_recoveries

    @property
    def degradation_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._degradation_events)

    def _is_critical(self, item: T) -> bool:
        if self._is_critical_fn is None:
            return False
        return bool(self._is_critical_fn(item))

    def _category(self, item: T) -> str:
        if self._category_fn is None:
            return "transient_default"
        return str(self._category_fn(item))

    def _record_drop(self, category: str) -> None:
        self._dropped_count += 1
        self._dropped_by_category[category] = self._dropped_by_category.get(category, 0) + 1

    def put_nowait(self, item: T) -> bool:
        """Enqueue an item while strictly guaranteeing critical items are never dropped.

        Returns True if enqueued without shedding any item, False if a transient item was shed.
        """
        incoming_critical = self._is_critical(item)
        incoming_cat = self._category(item)
        shed = False

        if len(self._deque) >= self.maxsize:
            evicted_transient = self._evict_oldest_transient()
            if evicted_transient:
                shed = True
            elif not incoming_critical:
                # Queue consists 100% of critical items and incoming item is transient:
                # drop incoming transient item rather than ever evicting a critical item!
                self._record_drop(incoming_cat)
                self._check_high_watermark()
                return False
            # If incoming_critical is True and queue is 100% critical items, we preserve
            # the critical item without dropping anything (zero critical loss invariant).

        self._deque.append(item)
        self._total_enqueued += 1
        if incoming_critical:
            self._critical_enqueued_count += 1
        else:
            self._transient_enqueued_count += 1

        cur_len = len(self._deque)
        if cur_len > self._peak_size:
            self._peak_size = cur_len

        self._check_high_watermark()
        return not shed

    def _check_high_watermark(self) -> None:
        cur_len = len(self._deque)
        if not self._degraded_mode and cur_len >= self.high_watermark_count:
            self._degraded_mode = True
            self._high_watermark_triggers += 1
            self._degradation_events.append(
                {
                    "transition": "DEGRADED_100MS_TO_1000MS",
                    "trigger_seq": self._high_watermark_triggers,
                    "qsize": cur_len,
                    "maxsize": self.maxsize,
                }
            )
            if self._on_high_watermark is not None:
                self._on_high_watermark(cur_len, self.maxsize)

    def _check_low_watermark(self) -> None:
        cur_len = len(self._deque)
        if self._degraded_mode and cur_len <= self.low_watermark_count:
            self._degraded_mode = False
            self._low_watermark_recoveries += 1
            self._degradation_events.append(
                {
                    "transition": "RECOVERED_1000MS_TO_100MS",
                    "recovery_seq": self._low_watermark_recoveries,
                    "qsize": cur_len,
                    "maxsize": self.maxsize,
                }
            )
            if self._on_low_watermark_recovery is not None:
                self._on_low_watermark_recovery(cur_len, self.maxsize)

    def get_nowait(self) -> T | None:
        """Pop the oldest item from the queue, updating watermark state."""
        if not self._deque:
            return None
        item = self._deque.popleft()
        self._check_low_watermark()
        return item

    def drain_batch(self, max_items: int = 256) -> list[T]:
        """Drain up to `max_items` in FIFO order."""
        batch: list[T] = []
        while self._deque and len(batch) < max_items:
            batch.append(self._deque.popleft())
        self._check_low_watermark()
        return batch

    def _evict_oldest_transient(self) -> bool:
        """Find and evict the oldest non-critical item in `_deque`.

        Never evicts an item where `_is_critical(item) == True`.
        Returns True if a transient item was found and evicted, False if all items in `_deque` are critical.
        """
        for idx, existing in enumerate(self._deque):
            if not self._is_critical(existing):
                cat = self._category(existing)
                del self._deque[idx]
                self._record_drop(cat)
                return True
        return False

    def telemetry(self) -> BackpressureTelemetry:
        return BackpressureTelemetry(
            maxsize=self.maxsize,
            current_qsize=len(self._deque),
            peak_qsize=self._peak_size,
            total_enqueued=self._total_enqueued,
            critical_enqueued_count=self._critical_enqueued_count,
            critical_dropped_count=self._critical_dropped_count,
            transient_enqueued_count=self._transient_enqueued_count,
            transient_dropped_count=self._dropped_count,
            dropped_by_category=dict(self._dropped_by_category),
            degraded_mode=self._degraded_mode,
            high_watermark_triggers=self._high_watermark_triggers,
            low_watermark_recoveries=self._low_watermark_recoveries,
            degradation_events=tuple(self._degradation_events),
        )
