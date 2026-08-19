from __future__ import annotations

import inspect
import json
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from .models import Rates


class ResponseLike(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...
    def raise_for_status(self) -> None: ...


class AmbiguousWrite(RuntimeError):
    """The server may have accepted a write; resubmission is unsafe."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_get_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.max_get_attempts < 1 or min(self.base_delay, self.max_delay, self.jitter) < 0:
            raise ValueError("invalid retry policy")


def _retry_after_delay(value: object, *, fallback: float, maximum: float) -> float:
    delay = fallback
    if isinstance(value, str):
        try:
            delay = float(value)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                delay = (parsed - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = fallback
    if not math.isfinite(delay):
        delay = fallback
    return min(max(delay, 0.0), maximum)


Request = Callable[..., Awaitable[ResponseLike]]
Sleep = Callable[[float], Any]


class HttpTransport:
    """GETs retry safely; writes are attempted exactly once."""

    def __init__(
        self, request: Request, *, policy: RetryPolicy | None = None, sleep: Sleep
    ) -> None:
        self._request = request
        self._policy = policy or RetryPolicy()
        self._sleep = sleep

    async def _wait(self, seconds: float) -> None:
        result = self._sleep(seconds)
        if inspect.isawaitable(result):
            await result

    async def get(self, url: str, **kwargs: Any) -> ResponseLike:
        last_error: Exception | None = None
        for attempt in range(self._policy.max_get_attempts):
            try:
                response = await self._request("GET", url, **kwargs)
                if response.status_code != 429:
                    response.raise_for_status()
                    return response
                last_error = RuntimeError("rate limited")
                fallback = min(
                    self._policy.base_delay * (2**attempt), self._policy.max_delay
                )
                delay = _retry_after_delay(
                    response.headers.get("Retry-After"),
                    fallback=fallback,
                    maximum=self._policy.max_delay,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                delay = min(self._policy.base_delay * (2**attempt), self._policy.max_delay)
            if attempt + 1 < self._policy.max_get_attempts:
                await self._wait(delay + random.uniform(0, self._policy.jitter))
        raise RuntimeError("safe GET retry budget exhausted") from last_error

    async def put_once(self, url: str, **kwargs: Any) -> ResponseLike:
        try:
            response = await self._request("PUT", url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AmbiguousWrite(
                "PUT outcome is unknown; reconciliation is required and automatic resubmission is blocked"
            ) from exc
        response.raise_for_status()
        return response


def _whole_number(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("API rate must be numeric")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("API rate must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError("API rate must be finite numeric data")
    if not number.is_integer():
        raise ValueError("API rate must be a whole number")
    return int(number)


def _find_rates(value: object) -> Rates | None:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return _find_rates(decoded)
    if isinstance(value, dict):
        if "sustainedDownlinkDataRate" in value or "sustainedUplinkDataRate" in value:
            if "sustainedDownlinkDataRate" not in value or "sustainedUplinkDataRate" not in value:
                raise ValueError("both QoS rates are required")
            return Rates(
                _whole_number(value["sustainedDownlinkDataRate"]),
                _whole_number(value["sustainedUplinkDataRate"]),
            )
        for nested in value.values():
            found = _find_rates(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_rates(nested)
            if found is not None:
                return found
    return None


_LEGACY = re.compile(
    r'\\?"sustainedDownlinkDataRate\\?"\s*:\s*(\d+)\s*,\s*\\?"sustainedUplinkDataRate\\?"\s*:\s*(\d+)'
)


def parse_pull_config(payload: object) -> Rates:
    """Parse documented JSON and observed nested message.data JSON strings."""
    found = _find_rates(payload)
    if found is not None:
        return found
    if isinstance(payload, str):
        match = _LEGACY.search(payload)
        if match:
            return Rates(int(match.group(1)), int(match.group(2)))
    raise ValueError("QoS rates not found in pull_config JSON")


def validate_endpoint(value: str, *, allow_localhost: bool = False) -> str:
    parsed = urlparse(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (allow_localhost and local and parsed.scheme == "http"):
        raise ValueError("endpoint must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("endpoint must have a valid host and no embedded credentials")
    return value.rstrip("/")


def validate_redirect(redirect: str, *, auth_url: str, approved_hosts: set[str]) -> str:
    clean = validate_endpoint(redirect)
    host = urlparse(clean).hostname
    auth_host = urlparse(validate_endpoint(auth_url)).hostname
    if host != auth_host and host not in approved_hosts:
        raise ValueError("token redirect host is not approved")
    return clean
