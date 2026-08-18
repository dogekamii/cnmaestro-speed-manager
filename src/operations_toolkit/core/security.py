from __future__ import annotations

import re
from typing import Any

_SECRET_KEYS = {
    "password",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "authorization",
    "token",
}
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in _SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return _BEARER.sub(r"\1[REDACTED]", value)
    return value
