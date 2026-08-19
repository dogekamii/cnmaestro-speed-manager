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


_ASCII_DNS_HOST = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)


def _redirect_rejected(host: str | None, reason: str) -> ValueError:
    safe_host = ascii(host if host is not None else "<missing>")
    return ValueError(f"token redirect host {safe_host} rejected: {reason}")


def _normalize_dns_host(host: str, *, policy_name: str) -> str:
    normalized = host.lower()
    if normalized.endswith("."):
        raise ValueError(f"{policy_name} host {host!a} has a trailing dot")
    if not normalized.isascii() or not _ASCII_DNS_HOST.fullmatch(normalized):
        raise ValueError(f"{policy_name} host {host!a} must use ASCII DNS labels")
    if any(label.startswith("xn--") for label in normalized.split(".")):
        raise ValueError(f"{policy_name} host {host!a} uses unsupported IDNA encoding")
    return normalized


def validate_redirect(
    redirect: str,
    *,
    auth_url: str,
    approved_hosts: set[str],
    approved_suffixes: set[str],
) -> str:
    try:
        parsed = urlparse(redirect)
        host = parsed.hostname
    except ValueError as exc:
        raise _redirect_rejected(None, "URL authority is malformed") from exc

    if parsed.username is not None or parsed.password is not None:
        raise _redirect_rejected(host, "embedded credentials are not allowed")
    if parsed.scheme.lower() != "https":
        raise _redirect_rejected(host, "redirect must use HTTPS")
    if host is None:
        raise _redirect_rejected(host, "a hostname is required")
    if host.endswith("."):
        raise _redirect_rejected(host, "trailing-dot hostnames are not allowed")
    if not host.isascii() or not _ASCII_DNS_HOST.fullmatch(host.lower()):
        raise _redirect_rejected(host, "hostname must use unambiguous ASCII DNS labels")
    normalized_host = host.lower()
    if any(label.startswith("xn--") for label in normalized_host.split(".")):
        raise _redirect_rejected(host, "IDNA-encoded hostnames are not supported")

    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.count(":") > 1 or authority.endswith(":"):
        raise _redirect_rejected(host, "redirect has a malformed port")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _redirect_rejected(host, "redirect has a malformed port") from exc
    if port not in {None, 443}:
        raise _redirect_rejected(host, "port must be omitted or 443")
    if "?" in redirect or "#" in redirect:
        raise _redirect_rejected(host, "redirect must not include a query or fragment")
    if parsed.path not in {"", "/"}:
        raise _redirect_rejected(host, "redirect must be a base URL with an empty path or '/'")

    auth_host = urlparse(validate_endpoint(auth_url)).hostname
    assert auth_host is not None
    normalized_auth_host = _normalize_dns_host(auth_host, policy_name="authentication endpoint")
    normalized_approved_hosts = {
        _normalize_dns_host(item, policy_name="approved redirect") for item in approved_hosts
    }
    normalized_suffixes = {
        _normalize_dns_host(item, policy_name="approved redirect suffix")
        for item in approved_suffixes
    }
    suffix_match = any(
        normalized_host == suffix or normalized_host.endswith(f".{suffix}")
        for suffix in normalized_suffixes
    )
    if (
        normalized_host != normalized_auth_host
        and normalized_host not in normalized_approved_hosts
        and not suffix_match
    ):
        raise _redirect_rejected(host, "hostname is outside the approved redirect policy")

    normalized_authority = normalized_host if port is None else f"{normalized_host}:443"
    return f"https://{normalized_authority}"
