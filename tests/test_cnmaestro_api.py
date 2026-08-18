import json

import httpx
import pytest

from operations_toolkit.modules.cnmaestro.api import (
    AmbiguousWrite,
    HttpTransport,
    RetryPolicy,
    parse_pull_config,
    validate_endpoint,
    validate_redirect,
)
from operations_toolkit.modules.cnmaestro.models import Rates


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"sustainedDownlinkDataRate": 53760, "sustainedUplinkDataRate": 10750}]},
        {
            "message": {
                "data": json.dumps(
                    {
                        "qos": {
                            "sustainedDownlinkDataRate": "53760",
                            "sustainedUplinkDataRate": "10750",
                        }
                    }
                )
            }
        },
        {
            "message": json.dumps(
                {
                    "data": json.dumps(
                        {"sustainedDownlinkDataRate": 53760.0, "sustainedUplinkDataRate": 10750.0}
                    )
                }
            )
        },
    ],
)
def test_pull_config_contract_shapes_return_exact_rates(payload: object) -> None:
    assert parse_pull_config(payload) == Rates(53760, 10750)


def test_pull_config_rejects_non_numeric_or_fractional_api_values() -> None:
    with pytest.raises(ValueError, match="numeric"):
        parse_pull_config({"sustainedDownlinkDataRate": "fast", "sustainedUplinkDataRate": 1})
    with pytest.raises(ValueError, match="whole"):
        parse_pull_config({"sustainedDownlinkDataRate": 1.5, "sustainedUplinkDataRate": 1})


def test_legacy_string_fallback_is_narrow() -> None:
    legacy = r"{\"sustainedDownlinkDataRate\":6451,\"sustainedUplinkDataRate\":2150}"
    assert parse_pull_config(legacy) == Rates(6451, 2150)
    with pytest.raises(ValueError):
        parse_pull_config("downlink=6451 uplink=2150")


class FakeResponse:
    def __init__(
        self, status_code: int, payload: dict | None = None, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(self.status_code),
            )


@pytest.mark.asyncio
async def test_429_get_retry_uses_retry_after_and_is_bounded() -> None:
    calls = 0
    waits: list[float] = []

    async def request(method: str, url: str, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return (
            FakeResponse(429, headers={"Retry-After": "0.25"})
            if calls == 1
            else FakeResponse(200, {"ok": True})
        )

    transport = HttpTransport(
        request,
        policy=RetryPolicy(max_get_attempts=3, base_delay=0.1, max_delay=1, jitter=0),
        sleep=waits.append,
    )
    response = await transport.get("https://api.example.test/devices")
    assert response.json() == {"ok": True}
    assert calls == 2
    assert waits == [0.25]


@pytest.mark.asyncio
async def test_ambiguous_put_timeout_is_never_retried() -> None:
    calls = 0

    async def request(method: str, url: str, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("lost after send")

    transport = HttpTransport(request, policy=RetryPolicy(max_get_attempts=6), sleep=lambda _: None)
    with pytest.raises(AmbiguousWrite, match="reconciliation"):
        await transport.put_once(
            "https://api.example.test/device", json={"template": "50mbps Package"}
        )
    assert calls == 1


def test_https_and_redirect_host_policy() -> None:
    assert (
        validate_endpoint("https://cloud.cambiumnetworks.com")
        == "https://cloud.cambiumnetworks.com"
    )
    assert (
        validate_endpoint("http://localhost:8080", allow_localhost=True) == "http://localhost:8080"
    )
    with pytest.raises(ValueError, match="HTTPS"):
        validate_endpoint("http://cloud.cambiumnetworks.com")
    validate_redirect(
        "https://api.cambiumnetworks.com",
        auth_url="https://cloud.cambiumnetworks.com",
        approved_hosts={"api.cambiumnetworks.com"},
    )
    with pytest.raises(ValueError, match="redirect host"):
        validate_redirect(
            "https://evil.example",
            auth_url="https://cloud.cambiumnetworks.com",
            approved_hosts=set(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["999999999", "not-a-delay", "Wed, 31 Dec 2099 23:59:59 GMT"])
async def test_retry_after_values_are_validated_and_capped(retry_after: str) -> None:
    calls = 0
    waits: list[float] = []

    async def request(method: str, url: str, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(429, headers={"Retry-After": retry_after}) if calls == 1 else FakeResponse(200)

    transport = HttpTransport(
        request,
        policy=RetryPolicy(max_get_attempts=2, base_delay=0.25, max_delay=1, jitter=0),
        sleep=waits.append,
    )
    await transport.get("https://api.example.test/devices")
    assert len(waits) == 1
    assert 0 <= waits[0] <= 1
