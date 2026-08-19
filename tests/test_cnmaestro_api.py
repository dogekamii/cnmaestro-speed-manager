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
        approved_suffixes=set(),
    )
    with pytest.raises(ValueError, match="redirect host"):
        validate_redirect(
            "https://evil.example",
            auth_url="https://cloud.cambiumnetworks.com",
            approved_hosts=set(),
            approved_suffixes=set(),
        )


def test_regional_cnmaestro_redirect_under_approved_cloud_suffix_is_accepted() -> None:
    redirect = "https://us-e1-s2-jwwsc39qdd.cloud.cambiumnetworks.com:443"

    assert (
        validate_redirect(
            redirect,
            auth_url="https://cloud.cambiumnetworks.com",
            approved_hosts=set(),
            approved_suffixes={"cloud.cambiumnetworks.com"},
        )
        == redirect
    )


@pytest.mark.parametrize(
    ("redirect", "expected"),
    [
        ("https://CLOUD.CAMBIUMNETWORKS.COM", "https://cloud.cambiumnetworks.com"),
        ("https://API.CAMBIUMNETWORKS.COM:443/", "https://api.cambiumnetworks.com:443"),
        (
            "https://REGION-1.CLOUD.CAMBIUMNETWORKS.COM:443/",
            "https://region-1.cloud.cambiumnetworks.com:443",
        ),
    ],
)
def test_redirect_policy_accepts_and_normalizes_safe_base_urls(
    redirect: str, expected: str
) -> None:
    assert (
        validate_redirect(
            redirect,
            auth_url="https://cloud.cambiumnetworks.com",
            approved_hosts={"api.cambiumnetworks.com"},
            approved_suffixes={"cloud.cambiumnetworks.com"},
        )
        == expected
    )


@pytest.mark.parametrize(
    ("redirect", "reason"),
    [
        ("https://cambiumnetworks.com.evil.test", "outside the approved"),
        ("https://cloud.cambiumnetworks.com.evil.test", "outside the approved"),
        ("https://evilcloud.cambiumnetworks.com", "outside the approved"),
        ("https://evilcambiumnetworks.com", "outside the approved"),
        ("https://cloud.cambiumnetworks.com%2eevil.test", "ASCII DNS"),
        (
            "https://client:secret@region.cloud.cambiumnetworks.com",  # pragma: allowlist secret
            "embedded credentials",
        ),
        ("http://region.cloud.cambiumnetworks.com", "HTTPS"),
        ("https://region.cloud.cambiumnetworks.com:444", "omitted or 443"),
        ("https://region.cloud.cambiumnetworks.com:not-a-port", "malformed port"),
        ("https://region.cloud.cambiumnetworks.com:", "malformed port"),
        ("https://region.cloud.cambiumnetworks.com:65536", "malformed port"),
        ("https://region.cloud.cambiumnetworks.com/api/v2", "base URL"),
        ("https://region.cloud.cambiumnetworks.com/?access_token=sensitive", "query or fragment"),
        ("https://region.cloud.cambiumnetworks.com/#sensitive", "query or fragment"),
        ("https://region.cloud.cambiumnetworks.com?", "query or fragment"),
        ("https://region.cloud.cambiumnetworks.com#", "query or fragment"),
        ("https://region.cloud.cambiumnetworks.com.", "trailing-dot"),
        ("https://é.cloud.cambiumnetworks.com", "ASCII DNS"),
        ("https://xn--9ca.cloud.cambiumnetworks.com", "IDNA"),
    ],
)
def test_redirect_policy_rejects_unsafe_or_ambiguous_bases(redirect: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason) as caught:
        validate_redirect(
            redirect,
            auth_url="https://cloud.cambiumnetworks.com",
            approved_hosts={"api.cambiumnetworks.com"},
            approved_suffixes={"cloud.cambiumnetworks.com"},
        )

    message = str(caught.value)
    assert "access_token" not in message
    assert "sensitive" not in message
    assert "client" not in message
    assert "secret" not in message


def test_redirect_rejection_names_only_the_hostname_and_policy_reason() -> None:
    with pytest.raises(ValueError) as caught:
        validate_redirect(
            "https://user:password@outside.example/private-path?token=top-secret-query#private-fragment",  # pragma: allowlist secret
            auth_url="https://cloud.cambiumnetworks.com",
            approved_hosts=set(),
            approved_suffixes={"cloud.cambiumnetworks.com"},
        )

    message = str(caught.value)
    assert "outside.example" in message
    assert "embedded credentials" in message
    for secret_value in ("user", "password", "private-path", "top-secret-query", "private-fragment"):
        assert secret_value not in message


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
