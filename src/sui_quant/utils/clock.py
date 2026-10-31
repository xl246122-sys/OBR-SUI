"""Dual-clock utilities enforcing strict UTC timestamps and monotonic nanosecond timers."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return current wall-clock time with explicit UTC timezone."""
    return datetime.now(timezone.utc)


def monotonic_ns() -> int:
    """Return monotonic clock in nanoseconds for intra-process latency measurement."""
    return time.monotonic_ns()


def ensure_utc(dt: datetime, field_name: str = "timestamp") -> datetime:
    """Validate that a datetime object is timezone-aware and strictly in UTC."""
    if not isinstance(dt, datetime):
        raise TypeError(f"{field_name} must be a datetime instance, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC, got naive datetime")
    if dt.tzinfo != timezone.utc and dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError(f"{field_name} must be strictly UTC, got tzinfo={dt.tzinfo}")
    return dt
