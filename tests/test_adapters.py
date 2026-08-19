from typing import Any

import pytest

from operations_toolkit.modules.cnmaestro.adapters import LiveCnMaestroAdapter


class Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class JobClient:
    def __init__(self, states: list[str]) -> None:
        self.headers: dict[str, str] = {}
        self.states = states
        self.closed = False
        self.requests: list[tuple[str, str]] = []

    async def post(self, url: str, **kwargs: object) -> Response:
        return Response({"access_token": "secret-token", "redirect_uri": "https://api.example.test"})

    async def request(self, method: str, url: str, **kwargs: object) -> Response:
        self.requests.append((method, url))
        state = self.states.pop(0)
        remaining = 0 if state == "completed" else 1
        return Response(
            {
                "data": [
                    {
                        "state": state,
                        "devices": {"success": 1 if state == "completed" else 0, "failed": 0, "remaining": remaining, "skipped": 0},
                        "mac": "0A:00:3E:80:42:EC",
                        "template": "50mbps Package",
                    }
                ]
            }
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_live_job_status_polls_to_terminal_state_with_bounded_waits() -> None:
    client = JobClient(["running", "completed"])
    waits: list[float] = []
    adapter = LiveCnMaestroAdapter(
        "https://cloud.example.test",
        "client-id",
        "client-secret",
        approved_redirect_hosts={"api.example.test"},
        approved_redirect_suffixes=set(),
        client=client,  # type: ignore[arg-type]
        job_poll_attempts=3,
        job_poll_interval=0.25,
        sleep=waits.append,
    )
    await adapter.connect()

    result = await adapter.job_status("job-1")

    assert result.state == "completed"
    assert waits == [0.25]
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_live_job_status_returns_timed_out_after_bounded_poll_budget() -> None:
    client = JobClient(["running", "running"])
    adapter = LiveCnMaestroAdapter(
        "https://cloud.example.test",
        "client-id",
        "client-secret",
        approved_redirect_hosts={"api.example.test"},
        approved_redirect_suffixes=set(),
        client=client,  # type: ignore[arg-type]
        job_poll_attempts=2,
        job_poll_interval=0,
        sleep=lambda _seconds: None,
    )
    await adapter.connect()

    result = await adapter.job_status("job-2")

    assert result.state == "timed_out"
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_live_close_clears_all_credential_and_session_state_idempotently() -> None:
    client = JobClient(["completed"])
    adapter = LiveCnMaestroAdapter(
        "https://cloud.example.test",
        "client-id",
        "client-secret",
        approved_redirect_hosts={"api.example.test"},
        approved_redirect_suffixes=set(),
        client=client,  # type: ignore[arg-type]
    )
    await adapter.connect()
    await adapter.close()
    await adapter.close()

    assert "Authorization" not in client.headers
    assert adapter._token is None
    assert adapter._client_id == ""
    assert adapter._client_secret == ""
    assert adapter._base_url is None
    assert client.closed is True


class RoutingClient:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: object) -> Response:
        self.calls.append(("POST", url))
        if url.endswith("/access/token"):
            return Response({"access_token": "secret-token", "redirect_uri": "https://api.example.test"})
        return Response({"accepted": True}, 202)

    async def request(self, method: str, url: str, **kwargs: object) -> Response:
        self.calls.append((method, url))
        if method == "GET" and url.endswith("/api/v2/devices"):
            return Response(
                {
                    "data": [
                        {
                            "type": "PMP",
                            "mode": "SM",
                            "mac": "0A:00:3E:80:42:EC",
                            "name": "Customer",
                            "network": "Access",
                            "tower": "North",
                            "ap_mac": "AA:BB:CC:DD:EE:FF",
                            "online": True,
                        }
                    ]
                }
            )
        if method == "GET" and url.endswith("/pull_config"):
            return Response(
                {"data": [{"sustainedDownlinkDataRate": 10752, "sustainedUplinkDataRate": 1075}]}
            )
        if method == "PUT":
            return Response({"job_id": "job-7"})
        raise AssertionError((method, url))

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_live_adapter_connect_inventory_pull_submit_and_close_contract() -> None:
    client = RoutingClient()
    adapter = LiveCnMaestroAdapter(
        "https://cloud.example.test",
        "client-id",
        "client-secret",
        approved_redirect_hosts={"api.example.test"},
        approved_redirect_suffixes=set(),
        client=client,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
    )

    base = await adapter.connect()
    inventory = await adapter.inventory()
    submission = await adapter.submit_template(inventory[0].mac, "50mbps Package")
    await adapter.close()

    assert base == "https://api.example.test"
    assert inventory[0].rates.downlink == 10752
    assert inventory[0].online is True
    assert submission.job_id == "job-7"
    assert [method for method, _url in client.calls].count("PUT") == 1
    assert client.closed is True


class RedirectClient:
    def __init__(self, redirect: str) -> None:
        self.redirect = redirect
        self.headers: dict[str, str] = {}
        self.closed = False

    async def post(self, url: str, **kwargs: object) -> Response:
        assert url.endswith("/api/v2/access/token")
        return Response({"access_token": "secret-token", "redirect_uri": self.redirect})

    async def request(self, method: str, url: str, **kwargs: object) -> Response:
        raise AssertionError("no API request expected")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_live_adapter_accepts_recovered_regional_redirect_via_explicit_suffix_policy() -> None:
    redirect = "https://us-e1-s2-jwwsc39qdd.cloud.cambiumnetworks.com:443"
    client = RedirectClient(redirect)
    adapter = LiveCnMaestroAdapter(
        "https://cloud.cambiumnetworks.com",
        "client-id",
        "client-secret",
        approved_redirect_hosts={"api.cambiumnetworks.com"},
        approved_redirect_suffixes={"cloud.cambiumnetworks.com"},
        client=client,  # type: ignore[arg-type]
    )

    assert await adapter.connect() == redirect
    assert client.headers["Authorization"] == "Bearer secret-token"
    await adapter.close()


@pytest.mark.asyncio
async def test_rejected_redirect_never_retains_token_and_close_clears_session_state() -> None:
    client = RedirectClient("https://cloud.cambiumnetworks.com.evil.test")
    adapter = LiveCnMaestroAdapter(
        "https://cloud.cambiumnetworks.com",
        "client-id",
        "client-secret",
        approved_redirect_hosts={"api.cambiumnetworks.com"},
        approved_redirect_suffixes={"cloud.cambiumnetworks.com"},
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match=r"cloud\.cambiumnetworks\.com\.evil\.test"):
        await adapter.connect()

    assert adapter._token is None
    assert adapter._base_url is None
    assert "Authorization" not in client.headers

    await adapter.close()
    assert adapter._client_id == ""
    assert adapter._client_secret == ""
    assert client.closed is True
