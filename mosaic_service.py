"""Mosaic matching, portal transport, and speed-test safety primitives."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import ssl
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import truststore


@dataclass(frozen=True)
class CustomerIdentity:
    subscriber_code: str
    customer_name: str


@dataclass(frozen=True)
class MatchResult:
    status: str
    reason: str
    subscriber_code: str | None = None
    customer_name: str = ""
    confidence: str | None = None
    record: dict | None = None
    records: tuple[dict, ...] = ()


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: str
    stale_pending: bool = False


class AmbiguousSubmissionError(RuntimeError):
    """A non-idempotent Mosaic write may have reached the server."""


def parse_customer_identity(value: str) -> CustomerIdentity | None:
    match = re.match(r"^\s*(\d{4,12})(?:\s+|[-_:]+)(.*?)\s*$", str(value or ""))
    if not match:
        return None
    return CustomerIdentity(match.group(1), match.group(2).strip())


def _normalized_name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def match_subscriber(customer_name: str, records: list[dict]) -> MatchResult:
    identity = parse_customer_identity(customer_name)
    if not identity:
        return MatchResult("missing_code", "Customer name has no leading subscriber code")
    exact = [record for record in records if str(record.get("fields", {}).get("subscriberCode", "")) == identity.subscriber_code]
    if not exact:
        return MatchResult("not_found", "No Mosaic subscriber found", identity.subscriber_code, identity.customer_name)
    device_ids = {str(record.get("fields", {}).get("deviceId") or "") for record in exact}
    if len(exact) > 1 or len(device_ids - {""}) > 1:
        return MatchResult("multiple_devices", "Multiple Mosaic devices require review", identity.subscriber_code, identity.customer_name, records=tuple(exact))
    record = exact[0]
    fields = record.get("fields", {})
    if not fields.get("deviceId"):
        return MatchResult("no_device", "Mosaic subscriber has no device", identity.subscriber_code, identity.customer_name, record=record)
    same_name = bool(identity.customer_name) and _normalized_name(identity.customer_name) == _normalized_name(fields.get("fullName"))
    return MatchResult(
        "matched",
        "Exact subscriber code and customer name" if same_name else "Exact subscriber code; customer name differs",
        identity.subscriber_code,
        identity.customer_name,
        "code_and_name" if same_name else "code_only",
        record,
        (record,),
    )


def _bool(value) -> bool:
    return value is True or str(value).casefold() == "true"


def _parse_timestamp(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        result = datetime.fromisoformat(text)
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def terminal_stale_ookla(actions: dict, data: dict, application_status: dict) -> bool:
    applications = actions.get("applications", {}) if isinstance(actions, dict) else {}
    pending = [name for name, action in applications.items() if isinstance(action, dict) and _bool(action.get("pendingSync"))]
    if pending != ["OoklaSpeedTest"]:
        return False
    try:
        value = data["applications"]["OoklaSpeedTest"]["dto"]["Settings"]["OoklaSpeedTest"]
    except (KeyError, TypeError):
        return False
    state = str(value.get("State") or "").casefold()
    expecting = _bool(value.get("ExpectingResults"))
    app_state = str(application_status.get("applications", {}).get("OoklaSpeedTest", {}).get("state") or "").casefold()
    latest = latest_speed_result(data)
    result_complete = bool(latest and str(latest.get("status") or "").casefold() == "complete")
    return state == "complete" and not expecting and app_state in {"ok", "complete", "completed"} and result_complete


def evaluate_eligibility(record: dict, support: dict, application_status: dict, actions: dict, *, data: dict | None = None, now: datetime | None = None) -> EligibilityResult:
    fields = record.get("fields", {}) if isinstance(record, dict) else {}
    model = str(fields.get("model") or "")
    if "sr905" in model.casefold():
        return EligibilityResult(False, "SR905 routers do not support Mosaic speed tests")
    if str(fields.get("disposition") or "") != "MANAGED_DEVICE":
        return EligibilityResult(False, "Device is not managed")
    now = now or datetime.now(timezone.utc)
    informed = _parse_timestamp(fields.get("lastInform"))
    if not informed or now - informed.astimezone(timezone.utc) > timedelta(hours=24):
        return EligibilityResult(False, "Device has not informed recently")
    app = support.get("applications", {}).get("OoklaSpeedTest", {}) if isinstance(support, dict) else {}
    if not _bool(app.get("supported")):
        return EligibilityResult(False, "Ookla not supported")
    if not isinstance(app.get("driver"), dict) or not app["driver"].get("ref"):
        return EligibilityResult(False, "Ookla driver unavailable")
    state = application_status.get("applications", {}).get("OoklaSpeedTest", {}) if isinstance(application_status, dict) else {}
    app_state = str(state.get("state") or "")
    if not app_state:
        return EligibilityResult(False, "Ookla application status unavailable")
    if "OoklaSpeedTest" not in (actions.get("applications", {}) if isinstance(actions, dict) else {}):
        return EligibilityResult(False, "Ookla action unavailable")
    if app_state.casefold() == "nodriver":
        return EligibilityResult(False, "Ookla application status is NODRIVER")
    if app_state and app_state.casefold() not in {"ok", "complete", "completed"}:
        return EligibilityResult(False, f"Ookla application status is {app_state}")
    if data is not None and terminal_stale_ookla(actions, data, application_status):
        return EligibilityResult(False, "Stale Ookla request — select this row and clear it", True)
    for action in (actions.get("applications", {}) if isinstance(actions, dict) else {}).values():
        if isinstance(action, dict) and _bool(action.get("pendingSync")):
            return EligibilityResult(False, "Another Mosaic action is pending")
    return EligibilityResult(True, "Ready")


def _result_container(data: dict) -> dict:
    try:
        value = data["applications"]["OoklaSpeedTest"]["dto"]["Settings"]["OoklaSpeedTest"]["Results"]
        return value if isinstance(value, dict) else {}
    except (KeyError, TypeError):
        return {}


def _timestamp_key(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        parsed = _parse_timestamp(value)
        return parsed.timestamp() if parsed else float("-inf")


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _test_time_utc(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        parsed = datetime.fromtimestamp(number, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        parsed = _parse_timestamp(value)
        if not parsed:
            return str(value)
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def latest_speed_result(data: dict) -> dict | None:
    results = [value for value in _result_container(data).values() if isinstance(value, dict)]
    if not results:
        return None
    latest = max(results, key=lambda item: _timestamp_key(item.get("StartTimeStamp")))
    download = _number(latest.get("DownloadSpeed"))
    upload = _number(latest.get("UploadSpeed"))
    return {
        "status": latest.get("Status"),
        "start_timestamp": str(latest.get("StartTimeStamp")) if latest.get("StartTimeStamp") is not None else None,
        "test_time_utc": _test_time_utc(latest.get("StartTimeStamp")),
        "download_mbps": download / 1_000_000 if download is not None else None,
        "upload_mbps": upload / 1_000_000 if upload is not None else None,
        "latency_ms": _number(latest.get("PingLatency")),
        "jitter_ms": _number(latest.get("PingJitter")),
        "isp": latest.get("ISP"),
    }


class MosaicJournal:
    STATES = {"planned", "submitting", "submitted", "verified", "failed", "unknown"}

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS mosaic_speed_tests("
                "id INTEGER PRIMARY KEY,created_at TEXT,updated_at TEXT,subscriber_code TEXT,device_id TEXT,model TEXT,"
                "state TEXT,status_url TEXT,download_mbps REAL,upload_mbps REAL,latency_ms REAL,jitter_ms REAL,detail TEXT,previous_timestamp TEXT)"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(mosaic_speed_tests)")}
            if "previous_timestamp" not in columns:
                connection.execute("ALTER TABLE mosaic_speed_tests ADD COLUMN previous_timestamp TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_mosaic_unresolved_device "
                "ON mosaic_speed_tests(device_id) "
                "WHERE state IN ('planned','submitting','submitted','unknown')"
            )

    def plan(self, subscriber_code: str, device_id: str, model: str, *, previous_timestamp: str | None = None) -> int:
        self.assert_can_start(device_id)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO mosaic_speed_tests(created_at,updated_at,subscriber_code,device_id,model,state,detail,previous_timestamp) VALUES(?,?,?,?,?,?,?,?)",
                    (now, now, subscriber_code, device_id, model, "planned", "", previous_timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("An unresolved Mosaic outcome must be reconciled before retry") from exc
            return int(cursor.lastrowid)

    def transition(self, entry_id: int, state: str, *, status_url: str | None = None, metrics: dict | None = None, detail: str = "") -> None:
        if state not in self.STATES:
            raise ValueError("invalid Mosaic journal state")
        metrics = metrics or {}
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE mosaic_speed_tests SET updated_at=?,state=?,status_url=COALESCE(?,status_url),"
                "download_mbps=COALESCE(?,download_mbps),upload_mbps=COALESCE(?,upload_mbps),"
                "latency_ms=COALESCE(?,latency_ms),jitter_ms=COALESCE(?,jitter_ms),detail=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), state, status_url, metrics.get("download_mbps"), metrics.get("upload_mbps"), metrics.get("latency_ms"), metrics.get("jitter_ms"), str(detail)[:500], entry_id),
            )

    def get(self, entry_id: int) -> dict:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM mosaic_speed_tests WHERE id=?", (entry_id,)).fetchone()
            if not row:
                raise KeyError(entry_id)
            return dict(row)

    def unresolved(self) -> list[dict]:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT * FROM mosaic_speed_tests WHERE state IN ('planned','submitting','submitted','unknown') ORDER BY id"
            )]

    def unknown(self) -> list[dict]:
        return [row for row in self.unresolved() if row["state"] == "unknown"]

    def assert_can_start(self, device_id: str) -> None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT id,state FROM mosaic_speed_tests WHERE device_id=? AND state IN ('planned','submitting','submitted','unknown') LIMIT 1", (str(device_id),)).fetchone()
            if row:
                raise RuntimeError("An unresolved Mosaic outcome must be reconciled before retry")


class _LoginIPParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.value = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.casefold() == "input" and values.get("name") == "loginPanel:ipAddress":
            self.value = values.get("value")


def ookla_action_complete(status: dict) -> bool:
    applications = status.get("syncApplications", []) if isinstance(status, dict) else []
    return any(
        isinstance(item, dict)
        and item.get("appCode") == "OoklaSpeedTest"
        and _bool(item.get("complete"))
        for item in applications
    )


class MosaicPortalClient:
    """Portal client that creates short-lived HTTP clients for event-loop safety."""

    def __init__(self, base_url: str, username: str, password: str, *, transport=None):
        parsed = urlparse(str(base_url).rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Mosaic base URL must be HTTPS")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.username = username
        self.password = password
        self.transport = transport
        self.session_id = None
        self.xsrf_token = None
        self.ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def _client_kwargs(self):
        values = {"timeout": 45.0, "transport": self.transport}
        if self.transport is None:
            values["verify"] = self.ssl_context
        return values

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _same_origin_url(self, value: str) -> str:
        url = self._url(value)
        if urlparse(url).scheme + "://" + urlparse(url).netloc != self.base_url:
            raise ValueError("Mosaic action status URL changed origin")
        if not urlparse(url).path.startswith("/prime-home/"):
            raise ValueError("Mosaic path is not allowlisted")
        return url

    def set_session_for_test(self, session_id: str, xsrf_token: str) -> None:
        self.session_id, self.xsrf_token = session_id, xsrf_token

    def clear_session(self) -> None:
        self.session_id = None
        self.xsrf_token = None
        self.password = ""

    async def login(self) -> dict:
        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            page = await client.get(
                self._url("/prime-home/"),
                headers={"Accept": "text/html"},
                follow_redirects=True,
            )
            page.raise_for_status()
            self._same_origin_url(str(page.url))
            parser = _LoginIPParser()
            parser.feed(page.text)
            if not parser.value:
                raise RuntimeError("Mosaic login page did not provide client IP")
            response = await client.post(
                self._url("/prime-home/api/v1/sessions/portal"),
                json={"username": self.username, "password": self.password, "lastIpAddress": parser.value},
            )
            response.raise_for_status()
            data = response.json()
        if not data.get("sessionId") or not data.get("xsrfToken"):
            raise RuntimeError("Mosaic login did not return session state")
        if data.get("passphraseExpired"):
            raise RuntimeError("Mosaic passphrase is expired")
        self.session_id, self.xsrf_token = data["sessionId"], data["xsrfToken"]
        return {"authenticated": True, "passphrase_expired": False}

    def _auth(self):
        if not self.session_id or not self.xsrf_token:
            raise RuntimeError("Mosaic is not connected")
        return {
            "headers": {"Accept": "application/json", "X-XsrfSessionHeader": self.xsrf_token, "Cookie": f"CASESSIONID={self.session_id}; XsrfSessionToken={self.xsrf_token}"},
        }

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        auth = self._auth()
        headers = dict(auth["headers"])
        headers.update(kwargs.pop("headers", {}))
        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            response = await client.request(method, self._same_origin_url(path), headers=headers, **kwargs)
            response.raise_for_status()
            return response

    async def search_subscriber(self, code: str) -> list[dict]:
        if not re.fullmatch(r"\d{4,12}", str(code)):
            raise ValueError("invalid subscriber code")
        response = await self._request(
            "POST",
            "/prime-home/portal/query/execute?first=1&count=10",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            content=f'subscription with "{code}" sort disposition desc lastInform desc',
        )
        data = response.json()
        return data if isinstance(data, list) else []

    async def read_device(self, device_id: str) -> dict:
        if not re.fullmatch(r"\d+", str(device_id)):
            raise ValueError("invalid Mosaic device ID")
        prefix = f"/prime-home/api/v1/devices/{device_id}"
        support = (await self._request("GET", prefix + "/support")).json()
        actions = (await self._request("GET", prefix + "/actions")).json()
        status = (await self._request("GET", prefix + "/applicationStatus")).json()
        data = (await self._request("GET", prefix + "/data")).json()
        return {"support": support, "actions": actions, "application_status": status, "data": data}

    async def clear_terminal_ookla_pending(self, device_id: str, *, required: bool = False) -> dict:
        bundle = await self.read_device(str(device_id))
        if not terminal_stale_ookla(bundle["actions"], bundle["data"], bundle["application_status"]):
            if required:
                raise RuntimeError("The selected Ookla request is not stale")
            return {"cleared": False, "reason": "not-stale"}
        payload = deepcopy(bundle["actions"])
        payload["applications"]["OoklaSpeedTest"]["pendingSync"] = False
        try:
            await self._request("PUT", f"/prime-home/api/v1/devices/{device_id}/actions", headers={"Content-Type": "application/json"}, json=payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise AmbiguousSubmissionError("Mosaic stale-request cleanup outcome is unknown") from exc
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise AmbiguousSubmissionError("Mosaic stale-request cleanup outcome is unknown") from exc
        after = (await self._request("GET", f"/prime-home/api/v1/devices/{device_id}/actions")).json()
        if _bool(after.get("applications", {}).get("OoklaSpeedTest", {}).get("pendingSync")):
            raise AmbiguousSubmissionError("Mosaic did not verify stale-request cleanup")
        return {"cleared": True}

    async def start_ookla(self, device_id: str) -> str:
        prefix = f"/prime-home/api/v1/devices/{device_id}"
        actions = (await self._request("GET", prefix + "/actions")).json()
        applications = actions.get("applications", {})
        pending = [name for name, action in applications.items() if isinstance(action, dict) and _bool(action.get("pendingSync"))]
        if pending:
            raise RuntimeError("Another Mosaic action is pending")
        if "OoklaSpeedTest" not in applications:
            raise RuntimeError("Ookla action is unavailable")
        payload = deepcopy(actions)
        payload["applications"]["OoklaSpeedTest"]["pendingSync"] = True
        payload["solicit"] = True
        try:
            response = await self._request("PUT", prefix + "/actions", headers={"Content-Type": "application/json"}, json=payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise AmbiguousSubmissionError("Mosaic action submission outcome is unknown") from exc
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise AmbiguousSubmissionError("Mosaic action submission outcome is unknown") from exc
        status_url = response.headers.get("Action-Status")
        if not status_url:
            raise AmbiguousSubmissionError("Mosaic did not return Action-Status")
        return self._same_origin_url(status_url)

    async def poll_action(self, status_url: str, *, poll_interval: float = 10.0, max_polls: int = 18) -> dict:
        url = self._same_origin_url(status_url)
        latest = None
        for attempt in range(max_polls):
            if attempt and poll_interval:
                await asyncio.sleep(poll_interval)
            latest = (await self._request("GET", url)).json()
            if "solicitStatus" not in latest:
                raise RuntimeError("Mosaic action status is missing solicitStatus")
            if latest.get("completed") is True:
                return latest
        raise TimeoutError("Mosaic action did not complete")

    async def latest_result(self, device_id: str) -> dict | None:
        data = (await self._request("GET", f"/prime-home/api/v1/devices/{device_id}/data")).json()
        return latest_speed_result(data)

    async def wait_for_speed_result(self, device_id: str, *, previous_timestamp: str | None, poll_interval: float = 3.0, max_polls: int = 60) -> dict:
        for attempt in range(max_polls):
            if attempt and poll_interval:
                await asyncio.sleep(poll_interval)
            result = await self.latest_result(device_id)
            if result and (
                previous_timestamp is None
                or _timestamp_key(result.get("start_timestamp")) > _timestamp_key(previous_timestamp)
            ):
                return result
        raise TimeoutError("Mosaic speed-test result did not arrive")

    async def read_speed_result(self, device_id: str) -> dict:
        data = (await self._request("GET", f"/prime-home/api/v1/devices/{device_id}/data")).json()
        result = latest_speed_result(data)
        if not result:
            raise RuntimeError("Mosaic completed without a speed-test result")
        return result

    async def run_ookla(self, device_id: str, *, poll_interval: float = 10.0, max_polls: int = 18) -> dict:
        previous = await self.latest_result(device_id)
        status_url = await self.start_ookla(device_id)
        status = await self.poll_action(status_url, poll_interval=poll_interval, max_polls=max_polls)
        if status.get("solicitStatus", {}).get("status") != "SUCCESS":
            raise RuntimeError("Mosaic could not contact the router")
        if not ookla_action_complete(status):
            raise RuntimeError("Mosaic did not confirm completion of OoklaSpeedTest")
        return await self.wait_for_speed_result(device_id, previous_timestamp=previous.get("start_timestamp") if previous else None, poll_interval=0 if poll_interval == 0 else 3.0)

async def execute_journaled_ookla(client: MosaicPortalClient, journal: MosaicJournal, subscriber_code: str, device_id: str, model: str, *, record: dict) -> dict:
    """Run one Ookla action with fresh capability checks and durable outcome states."""
    try:
        records = await client.search_subscriber(subscriber_code)
        exact = {
            (str(item.get("fields", {}).get("subscriberId") or ""), str(item.get("fields", {}).get("deviceId") or "")): item
            for item in records
            if str(item.get("fields", {}).get("subscriberCode") or "") == str(subscriber_code)
        }
        chosen = [item for (_, candidate_device), item in exact.items() if candidate_device == str(device_id)]
        if len(chosen) != 1:
            return {"entry_id": None, "state": "ineligible", "detail": "Selected Mosaic device is no longer an exact subscriber match"}
        current_record = chosen[0]
        bundle = await client.read_device(device_id)
        eligibility = evaluate_eligibility(
            current_record,
            bundle["support"],
            bundle["application_status"],
            bundle["actions"],
            data=bundle["data"],
        )
        if not eligibility.eligible:
            return {"entry_id": None, "state": "ineligible", "detail": eligibility.reason}
        previous = latest_speed_result(bundle["data"])
    except Exception as exc:
        return {"entry_id": None, "state": "failed", "detail": f"Mosaic preflight failed: {exc}"}
    entry_id = journal.plan(
        subscriber_code,
        device_id,
        model,
        previous_timestamp=previous.get("start_timestamp") if previous else None,
    )
    journal.transition(entry_id, "submitting")
    try:
        status_url = await client.start_ookla(device_id)
    except AmbiguousSubmissionError as exc:
        journal.transition(entry_id, "unknown", detail=str(exc))
        return {"entry_id": entry_id, "state": "unknown", "detail": str(exc)}
    except Exception as exc:
        journal.transition(entry_id, "failed", detail=str(exc))
        return {"entry_id": entry_id, "state": "failed", "detail": str(exc)}
    journal.transition(entry_id, "submitted", status_url=status_url)
    try:
        status = await client.poll_action(status_url)
        if status.get("solicitStatus", {}).get("status") != "SUCCESS":
            journal.transition(entry_id, "failed", detail="Mosaic could not contact the router")
            return {"entry_id": entry_id, "state": "failed", "detail": "Mosaic could not contact the router"}
        if not ookla_action_complete(status):
            raise RuntimeError("Mosaic did not confirm completion of OoklaSpeedTest")
        metrics = await client.wait_for_speed_result(
            device_id,
            previous_timestamp=previous.get("start_timestamp") if previous else None,
        )
    except Exception as exc:
        journal.transition(entry_id, "unknown", detail=str(exc))
        return {"entry_id": entry_id, "state": "unknown", "detail": str(exc)}
    cleanup = {"cleared": False}
    cleanup_detail = ""
    try:
        cleanup = await client.clear_terminal_ookla_pending(device_id, required=False)
    except Exception as exc:
        cleanup = {"cleared": False, "unknown": True}
        cleanup_detail = f"Result verified; stale-request cleanup needs review: {exc}"
    journal.transition(entry_id, "verified", metrics=metrics, detail=cleanup_detail)
    return {"entry_id": entry_id, "state": "verified", "metrics": metrics, "cleanup": cleanup}


async def reconcile_journal_entry(client: MosaicPortalClient, journal: MosaicJournal, entry: dict) -> dict:
    """Reconcile one interrupted/ambiguous operation without resubmitting it."""
    entry_id = int(entry["id"])
    if entry.get("state") == "planned":
        detail = "Interrupted before Mosaic submission"
        journal.transition(entry_id, "failed", detail=detail)
        return {"entry_id": entry_id, "state": "failed", "detail": detail}
    try:
        if entry.get("status_url"):
            status = await client.poll_action(entry["status_url"], poll_interval=0, max_polls=1)
            if status.get("solicitStatus", {}).get("status") != "SUCCESS":
                raise RuntimeError("Mosaic could not contact the router")
            if not ookla_action_complete(status):
                raise RuntimeError("Mosaic did not confirm completion of OoklaSpeedTest")
        metrics = await client.wait_for_speed_result(
            str(entry["device_id"]),
            previous_timestamp=entry.get("previous_timestamp"),
            poll_interval=0,
            max_polls=1,
        )
    except Exception as exc:
        journal.transition(entry_id, "unknown", detail=str(exc))
        return {"entry_id": entry_id, "state": "unknown", "detail": str(exc)}
    cleanup = {"cleared": False}
    cleanup_detail = ""
    try:
        cleanup = await client.clear_terminal_ookla_pending(device_id, required=False)
    except Exception as exc:
        cleanup = {"cleared": False, "unknown": True}
        cleanup_detail = f"Result verified; stale-request cleanup needs review: {exc}"
    journal.transition(entry_id, "verified", metrics=metrics, detail=cleanup_detail)
    return {"entry_id": entry_id, "state": "verified", "metrics": metrics, "cleanup": cleanup}


# Mosaic credential persistence -------------------------------------------------
import keyring as _keyring
from keyring.errors import PasswordDeleteError as _PasswordDeleteError

DEFAULT_MOSAIC_URL = "https://sacredwind.smartrg.com"
MOSAIC_CREDENTIAL_SERVICE = "Operations Toolkit/Mosaic"


def _load_settings(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_settings(path: Path, settings: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = dict(settings)
    for key in ("mosaic_password", "mosaic_session_id", "mosaic_xsrf_token"):
        clean.pop(key, None)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_mosaic_credentials(path: Path, keyring_backend=_keyring):
    settings = _load_settings(path)
    if not settings.get("mosaic_remember_credentials"):
        return False, DEFAULT_MOSAIC_URL, "", ""
    username = str(settings.get("mosaic_username") or "")
    if not username:
        return False, DEFAULT_MOSAIC_URL, "", ""
    base_url = str(settings.get("mosaic_base_url") or DEFAULT_MOSAIC_URL)
    return True, base_url, username, keyring_backend.get_password(MOSAIC_CREDENTIAL_SERVICE, username) or ""


def save_mosaic_credentials(base_url: str, username: str, password: str, path: Path, keyring_backend=_keyring) -> bool:
    settings = _load_settings(path)
    previous = str(settings.get("mosaic_username") or "")
    try:
        keyring_backend.set_password(MOSAIC_CREDENTIAL_SERVICE, username, password)
    except Exception:
        return False
    if previous and previous != username:
        try:
            keyring_backend.delete_password(MOSAIC_CREDENTIAL_SERVICE, previous)
        except _PasswordDeleteError:
            pass
        except Exception:
            pass
    settings.update({"mosaic_remember_credentials": True, "mosaic_username": username, "mosaic_base_url": base_url})
    try:
        _write_settings(path, settings)
    except OSError:
        try:
            keyring_backend.delete_password(MOSAIC_CREDENTIAL_SERVICE, username)
        except Exception:
            pass
        return False
    return True


def forget_mosaic_credentials(path: Path, keyring_backend=_keyring) -> bool:
    settings = _load_settings(path)
    username = str(settings.get("mosaic_username") or "")
    cleanup_ok = True
    if username:
        try:
            keyring_backend.delete_password(MOSAIC_CREDENTIAL_SERVICE, username)
        except _PasswordDeleteError:
            pass
        except Exception:
            cleanup_ok = False
    settings.pop("mosaic_remember_credentials", None)
    settings.pop("mosaic_username", None)
    settings.pop("mosaic_base_url", None)
    try:
        _write_settings(path, settings)
    except OSError:
        return False
    return cleanup_ok
