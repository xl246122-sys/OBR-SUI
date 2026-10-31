"""Log and payload redaction to prevent leakage of API keys, secrets, signatures, or passwords."""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "secret",
        "secret_key",
        "password",
        "passwd",
        "pwd",
        "signature",
        "token",
        "access_token",
        "refresh_token",
        "listenkey",
        "listen_key",
        "private_key",
        "x-mbx-apikey",
        "authorization",
    }
)

_SENSITIVE_KV_REGEX = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret[_-]?key|password|passwd|signature|listen[_-]?key|x-mbx-apikey|authorization|bearer)\s*([=:]\s*['\"]?)([^'\"\s,&]+)(['\"]?)"
)

_CHINESE_PWD_REGEX = re.compile(r"(密码\s*[：:]\s*)(\S+)")


def redact_text(text: str) -> str:
    """Mask sensitive key-value patterns in free-form strings."""
    if not text:
        return text
    masked = _SENSITIVE_KV_REGEX.sub(r"\1\2[REDACTED]\4", text)
    masked = _CHINESE_PWD_REGEX.sub(r"\1[REDACTED]", masked)
    return masked


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys in dictionaries before logging or serialization."""
    sanitized: dict[str, Any] = {}
    for k, v in data.items():
        key_lower = str(k).lower().strip()
        if key_lower in SENSITIVE_KEY_NAMES or any(s in key_lower for s in ("secret", "password", "apikey", "signature", "token")):
            sanitized[k] = "[REDACTED]"
        elif isinstance(v, Mapping):
            sanitized[k] = redact_mapping(v)
        elif isinstance(v, str):
            sanitized[k] = redact_text(v)
        else:
            sanitized[k] = v
    return sanitized


class RedactingLogFilter(logging.Filter):
    """Logging filter that strips sensitive secrets from log messages and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = redact_mapping(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_text(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        return True
