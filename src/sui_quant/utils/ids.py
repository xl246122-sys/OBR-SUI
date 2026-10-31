"""Idempotent and deterministic clientOrderId and event ID generators."""

from __future__ import annotations

import hashlib
import re

VALID_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]{1,36}$")


def generate_client_order_id(
    prefix: str,
    signal_id: str,
    leg: str = "ENTRY",
    attempt: int = 1,
) -> str:
    """Generate a deterministic, idempotent Binance-compliant clientOrderId (<= 36 chars).

    Using a deterministic digest of (prefix, signal_id, leg, attempt) guarantees that
    retrying an order check for the same signal and leg never creates a duplicate order ID.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    clean_prefix = re.sub(r"[^a-zA-Z0-9]", "", prefix)[:6] or "SUIQ"
    clean_leg = re.sub(r"[^a-zA-Z0-9]", "", leg)[:4] or "ENT"
    digest = hashlib.sha256(f"{clean_prefix}:{signal_id}:{clean_leg}:{attempt}".encode("utf-8")).hexdigest()[:16]
    candidate = f"{clean_prefix}-{clean_leg}-{attempt}-{digest}"
    if len(candidate) > 36 or not VALID_CLIENT_ORDER_ID_PATTERN.match(candidate):
        raise ValueError(f"Generated invalid clientOrderId: {candidate}")
    return candidate
