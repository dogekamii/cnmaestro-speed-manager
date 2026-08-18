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
_NAMED_SECRET = re.compile(
    r"(?i)\b(password|secret|client_secret|access_token|refresh_token|token)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\r\n,;&]+)"
)


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group(3)
    quote = value[0] if value[:1] in {"'", '"'} and value[-1:] == value[:1] else ""
    return f"{match.group(1)}{match.group(2)}{quote}[REDACTED]{quote}"


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
        value = _AUTHORIZATION.sub(_redact_assignment, value)
        value = _BEARER.sub(r"\1[REDACTED]", value)
        return _NAMED_SECRET.sub(_redact_assignment, value)
    return value
