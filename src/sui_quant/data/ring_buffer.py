"""Fixed-capacity zero-leak memory ring buffer and bounded backpressure queue for F-01 (§2.1, §13, QA-02).

Guarantees strictly bounded memory on the 2vCPU / 2GB RAM cloud server even under extreme
`depth10@100ms` and `aggTrade` message bursts.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Generic, Iterable, TypeVar

from sui_quant.contracts import require_decimal

T = TypeVar("T")


@dataclass(frozen=True)
class BufferTelemetry:
    capacity: int
    current_size: int
    total_appended: int
    total_evicted: int
    utilization_pct: Decimal


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
    """Bounded ingestion queue enforcing backpressure watermarks and automatic stream degradation (QA-02).

    - Never exceeds `maxsize` items in memory.
    - When queue occupancy reaches `high_watermark_ratio` (default 80%), triggers `on_high_watermark`
      (e.g., degrading `suiusdt@depth10@100ms` to `1000ms` and emitting an audit event).
    - When full (`len == maxsize`), drops the oldest droppable item (or oldest item) so the producer
      never blocks the asyncio event loop and memory never grows unbounded.
    """

    def __init__(
        self,
        maxsize: int = 4096,
        high_watermark_ratio: Decimal = Decimal("0.80"),
        low_watermark_ratio: Decimal = Decimal("0.40"),
        is_critical_fn: Callable[[T], bool] | None = None,
        on_high_watermark: Callable[[int, int], None] | None = None,
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
        self._on_high_watermark = on_high_watermark
        self._deque: deque[T] = deque()
        self._not_empty = asyncio.Condition()
        self._peak_size = 0
        self._dropped_count = 0
        self._total_enqueued = 0
        self._degraded_mode = False
        self._high_watermark_triggers = 0

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
    def total_enqueued(self) -> int:
        return self._total_enqueued

    @property
    def degraded_mode(self) -> bool:
        return self._degraded_mode

    @property
    def high_watermark_triggers(self) -> int:
        return self._high_watermark_triggers

    def put_nowait(self, item: T) -> bool:
        """Enqueue an item without blocking or exceeding `maxsize`.

        Returns True if enqueued without dropping, False if an older item had to be shed.
        """
        shed = False
        if len(self._deque) >= self.maxsize:
            self._shed_one_item()
            shed = True

        self._deque.append(item)
        self._total_enqueued += 1
        cur_len = len(self._deque)
        if cur_len > self._peak_size:
            self._peak_size = cur_len

        if not self._degraded_mode and cur_len >= self.high_watermark_count:
            self._degraded_mode = True
            self._high_watermark_triggers += 1
            if self._on_high_watermark is not None:
                self._on_high_watermark(cur_len, self.maxsize)

        return not shed

    def get_nowait(self) -> T | None:
        """Pop the oldest item from the queue, updating watermark state."""
        if not self._deque:
            return None
        item = self._deque.popleft()
        if self._degraded_mode and len(self._deque) <= self.low_watermark_count:
            self._degraded_mode = False
        return item

    def drain_batch(self, max_items: int = 256) -> list[T]:
        """Drain up to `max_items` in FIFO order."""
        batch: list[T] = []
        while self._deque and len(batch) < max_items:
            batch.append(self._deque.popleft())
        if self._degraded_mode and len(self._deque) <= self.low_watermark_count:
            self._degraded_mode = False
        return batch

    def _shed_one_item(self) -> None:
        """Evict the oldest non-critical item (or oldest item if all are critical)."""
        if not self._deque:
            return
        if self._is_critical_fn is not None:
            for idx, existing in enumerate(self._deque):
                if not self._is_critical_fn(existing):
                    del self._deque[idx]
                    self._dropped_count += 1
                    return
        self._deque.popleft()
        self._dropped_count += 1
