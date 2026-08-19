from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

import httpx

from .api import HttpTransport, parse_pull_config, validate_endpoint, validate_redirect
from .models import DeviceSnapshot, Rates


@dataclass(frozen=True, slots=True)
class Submission:
    job_id: str | None


@dataclass(frozen=True, slots=True)
class JobResult:
    state: str
    success: int
    failed: int
    remaining: int
    skipped: int
    intended_mac: str | None
    intended_template: str | None


@runtime_checkable
class CnMaestroAdapter(Protocol):
    """Parity contract implemented by live and deterministic demo adapters."""

    @property
    def connection_identity(self) -> str: ...
    async def inventory(self) -> tuple[DeviceSnapshot, ...]: ...
    async def pull_rates(self, mac: str) -> Rates: ...
    async def submit_template(self, mac: str, template: str) -> Submission: ...
    async def job_status(self, job_id: str) -> JobResult: ...
    async def close(self) -> None: ...


class DemoCnMaestroAdapter:
    """In-memory adapter. It imports no network client and cannot contact cnMaestro."""

    network_enabled = False
    connection_identity = "demo"

    def __init__(self) -> None:
        now = datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        self._devices = {
            "0A:00:3E:80:42:EC": DeviceSnapshot(
                "0A:00:3E:80:42:EC",
                "Demo Bakery",
                Rates(10752, 1075),
                "10 Mbps",
                "Demo Access",
                "North",
                "AA:BB:CC:00:00:01",
                True,
                now,
            ),
            "0A:00:3E:80:42:ED": DeviceSnapshot(
                "0A:00:3E:80:42:ED",
                "Demo Library",
                Rates(26880, 3225),
                "25 Mbps",
                "Demo Access",
                "South",
                "AA:BB:CC:00:00:02",
                True,
                now,
            ),
            "0A:00:3E:80:42:EE": DeviceSnapshot(
                "0A:00:3E:80:42:EE",
                "Demo Clinic",
                Rates(53760, 10750),
                "50 Mbps",
                "Demo Access",
                "East",
                "AA:BB:CC:00:00:03",
                False,
                now,
            ),
        }
        self._pending: dict[str, tuple[str, str]] = {}

    async def inventory(self) -> tuple[DeviceSnapshot, ...]:
        return tuple(self._devices.values())

    async def pull_rates(self, mac: str) -> Rates:
        return self._devices[mac].rates

    async def submit_template(self, mac: str, template: str) -> Submission:
        job_id = f"demo-job-{len(self._pending) + 1}"
        self._pending[job_id] = (mac, template)
        return Submission(job_id)

    async def job_status(self, job_id: str) -> JobResult:
        mac, template = self._pending[job_id]
        by_template = {
            "6mbps Package": ("6 Mbps", Rates(6451, 2150)),
            "10mbps Package": ("10 Mbps", Rates(10752, 1075)),
            "15mbps Package": ("15 Mbps", Rates(16128, 3225)),
            "20mbps Package": ("20 Mbps", Rates(21500, 10752)),
            "25mbps Package": ("25 Mbps", Rates(26880, 3225)),
            "50mbps Package": ("50 Mbps", Rates(53760, 10750)),
            "75mbps Package": ("75 Mbps", Rates(80640, 10750)),
            "100mbps Package": ("100 Mbps", Rates(107520, 21500)),
        }
        package, rates = by_template[template]
        old = self._devices[mac]
        self._devices[mac] = DeviceSnapshot(
            old.mac,
            old.name,
            rates,
            package,
            old.network,
            old.tower,
            old.ap,
            old.online,
            datetime.now(UTC),
        )
        return JobResult("completed", 1, 0, 0, 0, mac, template)

    async def close(self) -> None:
        self._pending.clear()


class LiveCnMaestroAdapter:
    network_enabled = True

    def __init__(
        self,
        auth_url: str,
        client_id: str,
        client_secret: str,
        *,
        approved_redirect_hosts: set[str],
        approved_redirect_suffixes: set[str],
        client: httpx.AsyncClient | None = None,
        job_poll_attempts: int = 20,
        job_poll_interval: float = 1.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._auth_url = validate_endpoint(auth_url)
        self.connection_identity = hashlib.sha256(
            f"{self._auth_url}\0{client_id}".encode()
        ).hexdigest()
        self._client_id = client_id
        self._client_secret = client_secret
        self._approved_hosts = approved_redirect_hosts
        self._approved_suffixes = approved_redirect_suffixes
        if job_poll_attempts < 1 or job_poll_interval < 0:
            raise ValueError("invalid job polling policy")
        self._job_poll_attempts = job_poll_attempts
        self._job_poll_interval = job_poll_interval
        self._sleep = sleep
        self._closed = False
        self._client = client or httpx.AsyncClient(timeout=45, follow_redirects=False)
        self._transport = HttpTransport(cast(Any, self._client.request), sleep=asyncio.sleep)
        self._token: str | None = None
        self._base_url: str | None = None

    async def connect(self) -> str:
        self._client.headers.pop("Authorization", None)
        self._token = None
        self._base_url = None
        response = await self._client.post(
            f"{self._auth_url}/api/v2/access/token",
            auth=(self._client_id, self._client_secret),
            data={"grant_type": "client_credentials"},
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload["access_token"])
        base_url = validate_redirect(
            str(payload.get("redirect_uri", self._auth_url)),
            auth_url=self._auth_url,
            approved_hosts=self._approved_hosts,
            approved_suffixes=self._approved_suffixes,
        )
        self._token = token
        self._base_url = base_url
        self.connection_identity = hashlib.sha256(
            f"{self._auth_url}\0{self._base_url}\0{self._client_id}".encode()
        ).hexdigest()
        self._client.headers["Authorization"] = f"Bearer {self._token}"
        self._client.headers["Accept"] = "application/json"
        return self._base_url

    def _base(self) -> str:
        if self._base_url is None:
            raise RuntimeError("adapter is not connected")
        return self._base_url

    async def inventory(self) -> tuple[DeviceSnapshot, ...]:
        response = await self._transport.get(f"{self._base()}/api/v2/devices")
        rows = response.json().get("data", [])
        now = datetime.now(UTC)
        snapshots = []
        for row in rows:
            if (
                str(row.get("type", "")).lower() == "pmp"
                and str(row.get("mode", "")).lower() == "sm"
            ):
                rates = await self.pull_rates(str(row["mac"]))
                snapshots.append(
                    DeviceSnapshot(
                        str(row["mac"]),
                        str(row.get("name", "")),
                        rates,
                        None,
                        str(row.get("network", "")),
                        str(row.get("tower", "")),
                        str(row.get("ap_mac", "")),
                        bool(row.get("online")),
                        now,
                    )
                )
        return tuple(snapshots)

    async def pull_rates(self, mac: str) -> Rates:
        url = f"{self._base()}/api/v2/devices/{mac}/pull_config"
        initiation = await self._client.post(url, json={})
        initiation.raise_for_status()
        for _ in range(8):
            response = await self._transport.get(url)
            try:
                return parse_pull_config(response.json())
            except ValueError:
                await asyncio.sleep(1)
        raise TimeoutError("pull_config did not return QoS rates")

    async def submit_template(self, mac: str, template: str) -> Submission:
        response = await self._transport.put_once(
            f"{self._base()}/api/v2/devices/{mac}", json={"template": template}
        )
        payload = response.json()
        return Submission(payload.get("job_id"))

    async def _wait(self, seconds: float) -> None:
        result = self._sleep(seconds)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _job_result(payload: dict[str, Any]) -> JobResult:
        devices = payload.get("devices", {})
        return JobResult(
            str(payload.get("state", "unknown")),
            int(devices.get("success", 0)),
            int(devices.get("failed", 0)),
            int(devices.get("remaining", 0)),
            int(devices.get("skipped", 0)),
            payload.get("mac"),
            payload.get("template"),
        )

    async def job_status(self, job_id: str) -> JobResult:
        last: JobResult | None = None
        terminal = {"completed", "failed", "cancelled", "timed_out", "timeout"}
        for attempt in range(self._job_poll_attempts):
            response = await self._transport.get(f"{self._base()}/api/v2/jobs/{job_id}")
            payload = response.json().get("data", [{}])[0]
            last = self._job_result(payload)
            if last.state.lower() in terminal:
                return last
            if attempt + 1 < self._job_poll_attempts:
                await self._wait(self._job_poll_interval)
        assert last is not None
        return JobResult(
            "timed_out",
            last.success,
            last.failed,
            last.remaining,
            last.skipped,
            last.intended_mac,
            last.intended_template,
        )

    async def close(self) -> None:
        self._client.headers.pop("Authorization", None)
        self._token = None
        self._client_id = ""
        self._client_secret = ""
        self._base_url = None
        if not self._closed:
            self._closed = True
            await self._client.aclose()
