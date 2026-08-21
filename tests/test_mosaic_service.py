import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from mosaic_service import (
    AmbiguousSubmissionError,
    MosaicJournal,
    MosaicPortalClient,
    evaluate_eligibility,
    execute_journaled_ookla,
    reconcile_journal_entry,
    latest_speed_result,
    match_subscriber,
    parse_customer_identity,
)


class IdentityAndMatchingTests(unittest.TestCase):
    def test_parses_only_leading_numeric_subscriber_code(self):
        identity = parse_customer_identity("  10014 Johnson Harriet ")
        self.assertEqual(identity.subscriber_code, "10014")
        self.assertEqual(identity.customer_name, "Johnson Harriet")
        self.assertIsNone(parse_customer_identity("Johnson 10014 Harriet"))
        self.assertIsNone(parse_customer_identity("Johnson Harriet"))

    def test_exact_code_is_authoritative_and_name_is_only_confidence_check(self):
        record = {"fields": {"subscriberCode": "10014", "fullName": "Harriet Johnson", "subscriberId": "1", "deviceId": "2", "disposition": "MANAGED_DEVICE"}}
        result = match_subscriber("10014 Johnson Harriet", [record])
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.subscriber_code, "10014")
        self.assertEqual(result.confidence, "code_only")
        self.assertIs(result.record, record)

        exact_name = {"fields": {**record["fields"], "fullName": "Johnson Harriet"}}
        result = match_subscriber("10014 Johnson Harriet", [exact_name])
        self.assertEqual(result.confidence, "code_and_name")

    def test_missing_duplicate_and_multi_device_matches_require_review(self):
        self.assertEqual(match_subscriber("No numeric code", []).status, "missing_code")
        self.assertEqual(match_subscriber("10014 Customer", []).status, "not_found")
        duplicate = [
            {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "2"}},
            {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "3"}},
        ]
        self.assertEqual(match_subscriber("10014 Customer", duplicate).status, "multiple_devices")


class EligibilityTests(unittest.TestCase):
    def eligible_inputs(self):
        now = datetime.now(timezone.utc)
        record = {"fields": {"subscriberCode": "10014", "deviceId": "2", "subscriberId": "1", "model": "SDG-8734v", "disposition": "MANAGED_DEVICE", "lastInform": now.isoformat()}}
        support = {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}}
        status = {"applications": {"OoklaSpeedTest": {"state": "OK", "messages": []}}}
        actions = {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}
        return record, support, status, actions, now

    def test_eligible_requires_full_preflight_not_support_flag_alone(self):
        record, support, status, actions, now = self.eligible_inputs()
        result = evaluate_eligibility(record, support, status, actions, now=now)
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason, "Ready")

        support["applications"]["OoklaSpeedTest"]["driver"] = None
        self.assertEqual(evaluate_eligibility(record, support, status, actions, now=now).reason, "Ookla driver unavailable")


    def test_terminal_ookla_pending_is_classified_as_stale_not_active(self):
        record, support, status, actions, now = self.eligible_inputs()
        actions["applications"]["OoklaSpeedTest"]["pendingSync"] = True
        data = {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"State": "Complete", "ExpectingResults": "false", "Results": {"1": {"Status": "Complete", "StartTimeStamp": "200"}}}}}}}}
        result = evaluate_eligibility(record, support, status, actions, data=data, now=now)
        self.assertFalse(result.eligible)
        self.assertTrue(result.stale_pending)
        self.assertEqual(result.reason, "Stale Ookla request — select this row and clear it")

    def test_pending_ookla_expecting_results_remains_active_and_not_clearable(self):
        record, support, status, actions, now = self.eligible_inputs()
        actions["applications"]["OoklaSpeedTest"]["pendingSync"] = True
        data = {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"State": "In Progress", "ExpectingResults": "true", "Results": {}}}}}}}
        result = evaluate_eligibility(record, support, status, actions, data=data, now=now)
        self.assertFalse(result.eligible)
        self.assertFalse(result.stale_pending)
        self.assertEqual(result.reason, "Another Mosaic action is pending")

    def test_unsupported_nodriver_pending_stale_and_sr905_are_ineligible(self):
        record, support, status, actions, now = self.eligible_inputs()
        support["applications"]["OoklaSpeedTest"]["supported"] = False
        self.assertEqual(evaluate_eligibility(record, support, status, actions, now=now).reason, "Ookla not supported")
        support["applications"]["OoklaSpeedTest"]["supported"] = True
        missing_status = {"applications": {}}
        self.assertEqual(evaluate_eligibility(record, support, missing_status, actions, now=now).reason, "Ookla application status unavailable")
        missing_action = {"applications": {}}
        self.assertEqual(evaluate_eligibility(record, support, status, missing_action, now=now).reason, "Ookla action unavailable")
        status["applications"]["OoklaSpeedTest"]["state"] = "NODRIVER"
        self.assertIn("NODRIVER", evaluate_eligibility(record, support, status, actions, now=now).reason)
        status["applications"]["OoklaSpeedTest"]["state"] = "OK"
        actions["applications"]["Other"] = {"pendingSync": True}
        self.assertEqual(evaluate_eligibility(record, support, status, actions, now=now).reason, "Another Mosaic action is pending")
        actions["applications"].pop("Other")
        record["fields"]["lastInform"] = (now - timedelta(days=2)).isoformat()
        self.assertEqual(evaluate_eligibility(record, support, status, actions, now=now).reason, "Device has not informed recently")
        record["fields"]["lastInform"] = now.isoformat()
        record["fields"]["model"] = "C0 - SR905acv"
        self.assertIn("SR905", evaluate_eligibility(record, support, status, actions, now=now).reason)


class ResultTests(unittest.TestCase):
    def test_latest_result_uses_timestamp_and_redacts_host_and_client_ip(self):
        data = {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"Results": {
            "z": {"StartTimeStamp": "100", "DownloadSpeed": "1000000", "UploadSpeed": "500000", "Host": "private", "ClientIP": "192.0.2.1"},
            "a": {"StartTimeStamp": "200", "DownloadSpeed": "50000000", "UploadSpeed": "10000000", "PingLatency": "12", "PingJitter": "3", "ISP": "Example", "Host": "private2", "ClientIP": "192.0.2.2"},
        }}}}}}}
        result = latest_speed_result(data)
        self.assertEqual(result["start_timestamp"], "200")
        self.assertEqual(result["download_mbps"], 50.0)
        self.assertEqual(result["upload_mbps"], 10.0)
        self.assertEqual(result["latency_ms"], 12.0)
        self.assertEqual(result["jitter_ms"], 3.0)
        self.assertEqual(result["test_time_utc"], "1970-01-01 00:03:20 UTC")
        self.assertNotIn("Host", json.dumps(result))
        self.assertNotIn("ClientIP", json.dumps(result))
        self.assertNotIn("192.0.2", json.dumps(result))


class JournalTests(unittest.TestCase):
    def test_unknown_outcome_blocks_retry_until_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            entry = journal.plan("10014", "2", "SDG-8734v", previous_timestamp="100")
            self.assertEqual(journal.get(entry)["previous_timestamp"], "100")
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                journal.assert_can_start("2")
            journal.transition(entry, "submitting")
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                journal.assert_can_start("2")
            journal.transition(entry, "submitted")
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                journal.assert_can_start("2")
            journal.transition(entry, "unknown", detail="timeout")
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                journal.assert_can_start("2")
            journal.transition(entry, "verified")
            journal.assert_can_start("2")
            row = journal.get(entry)
            self.assertNotIn("token", json.dumps(row).lower())
            self.assertNotIn("password", json.dumps(row).lower())





    def test_unresolved_returns_every_restart_blocking_state(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            states = ("planned", "submitting", "submitted", "unknown")
            for index, state in enumerate(states, 1):
                entry = journal.plan(str(index), str(index), "model")
                if state != "planned": journal.transition(entry, state)
            self.assertEqual({row["state"] for row in journal.unresolved()}, set(states))

    def test_unresolved_device_reservation_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mosaic.db"
            journals = [MosaicJournal(path), MosaicJournal(path)]
            barrier = threading.Barrier(2)
            results = []
            for journal in journals:
                journal.assert_can_start = lambda device_id, barrier=barrier: barrier.wait()
            def reserve(journal):
                try: results.append(("ok", journal.plan("10014", "2", "model")))
                except Exception as exc: results.append((type(exc).__name__, str(exc)))
            threads = [threading.Thread(target=reserve, args=(journal,)) for journal in journals]
            [thread.start() for thread in threads]; [thread.join() for thread in threads]
            self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
            self.assertEqual(sum(kind == "RuntimeError" for kind, _ in results), 1)


class JournaledExecutionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def managed_record():
        return {"fields": {"subscriberCode": "10014", "deviceId": "2", "subscriberId": "1", "model": "SDG", "disposition": "MANAGED_DEVICE", "lastInform": datetime.now(timezone.utc).isoformat()}}

    class FakeClient:
        def __init__(self, failure=None):
            self.failure = failure
            self.puts = 0
            self.cleanup_calls = 0
        async def search_subscriber(self, code):
            return [{"fields": {"subscriberCode": str(code), "deviceId": "2", "subscriberId": "1", "model": "SDG", "disposition": "MANAGED_DEVICE", "lastInform": datetime.now(timezone.utc).isoformat(), "fullName": ""}}]
        async def read_device(self, device_id):
            return {
                "support": {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}},
                "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}},
                "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}},
                "data": {},
            }
        async def latest_result(self, device_id): return None
        async def clear_terminal_ookla_pending(self, device_id, *, required=False):
            self.cleanup_calls += 1
            return {"cleared": True}
        async def start_ookla(self, device_id):
            self.puts += 1
            if self.failure == "before": raise ValueError("definite rejection")
            return "https://mosaic.example/prime-home/api/v1/action-status/1"
        async def poll_action(self, status_url):
            if self.failure == "after": raise TimeoutError("ambiguous poll")
            return {"completed": True, "solicitStatus": {"status": "SUCCESS"}, "syncApplications": [{"appCode": "OoklaSpeedTest", "complete": True}]}
        async def wait_for_speed_result(self, device_id, previous_timestamp, **kwargs):
            return {"start_timestamp": "200", "download_mbps": 50.0, "upload_mbps": 10.0, "latency_ms": 12.0, "jitter_ms": 3.0}



    async def test_explicit_device_choice_allows_multi_device_subscriber(self):
        class MultiDevice(self.FakeClient):
            async def search_subscriber(self, code):
                now = datetime.now(timezone.utc).isoformat()
                return [
                    {"fields": {"subscriberCode": str(code), "deviceId": "2", "subscriberId": "1", "model": "Chosen", "disposition": "MANAGED_DEVICE", "lastInform": now, "fullName": "Test"}},
                    {"fields": {"subscriberCode": str(code), "deviceId": "3", "subscriberId": "1", "model": "Other", "disposition": "MANAGED_DEVICE", "lastInform": now, "fullName": "Test"}},
                ]
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            client = MultiDevice()
            record = self.managed_record()
            outcome = await execute_journaled_ookla(client, journal, "10014", "2", "Chosen", record=record)
            self.assertEqual(outcome["state"], "verified")
            self.assertEqual(client.puts, 1)
            self.assertEqual(client.cleanup_calls, 1)
            self.assertTrue(outcome["cleanup"]["cleared"])

    async def test_capability_is_rechecked_immediately_before_submission(self):
        class BecameUnsupported(self.FakeClient):
            async def read_device(self, device_id):
                bundle = await super().read_device(device_id)
                bundle["support"]["applications"]["OoklaSpeedTest"]["supported"] = False
                return bundle
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            client = BecameUnsupported()
            outcome = await execute_journaled_ookla(client, journal, "10014", "2", "SDG", record=self.managed_record())
            self.assertEqual(outcome["state"], "ineligible")
            self.assertEqual(client.puts, 0)



    async def test_old_stale_status_url_without_baseline_can_be_release_candidate(self):
        class StaleStatusNoBaseline(self.FakeClient):
            async def poll_action(self, status_url, **kwargs): raise TimeoutError("old status unavailable")
            async def wait_for_speed_result(self, device_id, previous_timestamp, **kwargs): raise TimeoutError("no baseline result match")
            async def read_device(self, device_id):
                return {"support": {}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}, "data": {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"State": "Complete", "ExpectingResults": "false", "Results": {"1": {"Status": "Complete", "StartTimeStamp": "100"}}}}}}}}}
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            entry = journal.plan("10014", "2", "SDG", previous_timestamp=None);journal.transition(entry, "submitting");journal.transition(entry, "submitted", status_url="https://mosaic.example/prime-home/api/v1/action-status/old")
            row=journal.get(entry);created=datetime.fromisoformat(row["created_at"])
            outcome=await reconcile_journal_entry(StaleStatusNoBaseline(),journal,row,now=created+timedelta(minutes=11))
            self.assertEqual(outcome["state"],"release_candidate")
            self.assertEqual(journal.get(entry)["state"],"unknown")

    async def test_old_statusless_clear_remote_state_becomes_release_candidate(self):
        class NoRemoteEvidence(self.FakeClient):
            async def wait_for_speed_result(self, device_id, previous_timestamp, **kwargs): raise TimeoutError("no newer result")
            async def read_device(self, device_id):
                return {"support": {}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}, "data": {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"State": "Complete", "ExpectingResults": "false", "Results": {"1": {"Status": "Complete", "StartTimeStamp": "100"}}}}}}}}}
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            entry = journal.plan("10014", "2", "SDG", previous_timestamp="100");journal.transition(entry, "submitting");journal.transition(entry, "unknown", detail="timeout")
            row = journal.get(entry);created = datetime.fromisoformat(row["created_at"])
            outcome = await reconcile_journal_entry(NoRemoteEvidence(), journal, row, now=created + timedelta(minutes=11))
            self.assertEqual(outcome["state"], "release_candidate")
            self.assertEqual(journal.get(entry)["state"], "unknown")
            journal.release_retry_lock(entry)
            self.assertEqual(journal.get(entry)["state"], "failed")
            journal.assert_can_start("2")

    async def test_recent_or_remote_pending_statusless_entry_stays_unknown(self):
        class RemotePending(self.FakeClient):
            async def wait_for_speed_result(self, device_id, previous_timestamp, **kwargs): raise TimeoutError("no newer result")
            async def read_device(self, device_id):
                return {"support": {}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": True}}}, "data": {}}
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            entry = journal.plan("10014", "2", "SDG", previous_timestamp="100");journal.transition(entry, "submitting");journal.transition(entry, "unknown", detail="timeout")
            row = journal.get(entry);created = datetime.fromisoformat(row["created_at"])
            recent = await reconcile_journal_entry(RemotePending(), journal, row, now=created + timedelta(minutes=1))
            self.assertEqual(recent["state"], "unknown")
            old = await reconcile_journal_entry(RemotePending(), journal, journal.get(entry), now=created + timedelta(minutes=11))
            self.assertEqual(old["state"], "unknown")

    async def test_reconcile_planned_and_statusless_unknown_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            planned = journal.plan("10014", "2", "SDG", previous_timestamp="100")
            planned_outcome = await reconcile_journal_entry(self.FakeClient(), journal, journal.get(planned))
            self.assertEqual(planned_outcome["state"], "failed")
            self.assertEqual(journal.get(planned)["state"], "failed")
            unknown = journal.plan("10014", "3", "SDG", previous_timestamp="100")
            journal.transition(unknown, "submitting")
            journal.transition(unknown, "unknown", detail="timeout")
            client = self.FakeClient()
            outcome = await reconcile_journal_entry(client, journal, journal.get(unknown))
            self.assertEqual(outcome["state"], "verified")
            self.assertEqual(journal.get(unknown)["state"], "verified")

    async def test_success_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            client = self.FakeClient()
            outcome = await execute_journaled_ookla(client, journal, "10014", "2", "SDG", record=self.managed_record())
            self.assertEqual(outcome["state"], "verified")
            self.assertEqual(journal.get(outcome["entry_id"])["state"], "verified")
            self.assertEqual(client.puts, 1)

    async def test_definite_pre_submit_failure_is_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            outcome = await execute_journaled_ookla(self.FakeClient("before"), journal, "10014", "2", "SDG", record=self.managed_record())
            self.assertEqual(outcome["state"], "failed")


    async def test_completed_status_requires_ookla_application_complete(self):
        class IncompleteClient(self.FakeClient):
            async def poll_action(self, status_url):
                return {"completed": True, "solicitStatus": {"status": "SUCCESS"}, "syncApplications": [{"appCode": "OoklaSpeedTest", "complete": False}]}
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            outcome = await execute_journaled_ookla(IncompleteClient(), journal, "10014", "2", "SDG", record=self.managed_record())
            self.assertEqual(outcome["state"], "unknown")
            self.assertIn("did not confirm completion", outcome["detail"])

    async def test_post_submit_timeout_is_unknown_and_blocks_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = MosaicJournal(Path(directory) / "mosaic.db")
            client = self.FakeClient("after")
            outcome = await execute_journaled_ookla(client, journal, "10014", "2", "SDG", record=self.managed_record())
            self.assertEqual(outcome["state"], "unknown")
            self.assertEqual(client.puts, 1)
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                journal.assert_can_start("2")


class PortalClientTests(unittest.IsolatedAsyncioTestCase):

    def test_clear_session_removes_tokens_and_in_memory_password(self):
        client = MosaicPortalClient("https://mosaic.example", "user", "memory-password", transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        client.set_session_for_test("session", "xsrf")
        client.clear_session()
        self.assertIsNone(client.session_id)
        self.assertIsNone(client.xsrf_token)
        self.assertEqual(client.password, "")

    async def test_login_follows_same_origin_login_page_redirect(self):
        seen = []
        def handler(request):
            seen.append((request.method, request.url.path))
            if request.url.path == "/prime-home/":
                return httpx.Response(302, headers={"Location": "https://mosaic.example/prime-home/login/;jsessionid=test"})
            if request.url.path.startswith("/prime-home/login/"):
                return httpx.Response(200, text='<input name="loginPanel:ipAddress" value="10.10.19.115"/>')
            if request.url.path == "/prime-home/api/v1/sessions/portal":
                return httpx.Response(200, json={"sessionId": "session", "xsrfToken": "xsrf", "passphraseExpired": False})
            raise AssertionError(str(request.url))
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler))
        result = await client.login()
        self.assertTrue(result["authenticated"])
        self.assertEqual(seen, [("GET", "/prime-home/"), ("GET", "/prime-home/login/;jsessionid=test"), ("POST", "/prime-home/api/v1/sessions/portal")])

    async def test_login_search_and_device_read_contract(self):
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path == "/prime-home/":
                return httpx.Response(200, text='<input name="loginPanel:ipAddress" value="10.10.19.115"/>')
            if request.url.path == "/prime-home/api/v1/sessions/portal":
                body = json.loads(request.content)
                self.assertEqual(body, {"username": "user", "password": "pass", "lastIpAddress": "10.10.19.115"})
                return httpx.Response(200, json={"sessionId": "session", "xsrfToken": "xsrf", "passphraseExpired": False})
            self.assertEqual(request.headers["X-XsrfSessionHeader"], "xsrf")
            self.assertIn("CASESSIONID=session", request.headers["Cookie"])
            if request.url.path == "/prime-home/portal/query/execute":
                self.assertEqual(request.content.decode(), 'subscription with "10014" sort disposition desc lastInform desc')
                return httpx.Response(200, json=[{"fields": {"subscriberCode": "10014", "deviceId": "2"}}])
            if request.url.path.endswith("/support"):
                return httpx.Response(200, json={"applications": {}})
            if request.url.path.endswith("/actions"):
                return httpx.Response(200, json={"applications": {}})
            if request.url.path.endswith("/applicationStatus"):
                return httpx.Response(200, json={"applications": {}})
            if request.url.path.endswith("/data"):
                return httpx.Response(200, json={"deviceId": 2})
            raise AssertionError(str(request.url))
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler))
        await client.login()
        records = await client.search_subscriber("10014")
        bundle = await client.read_device("2")
        self.assertEqual(records[0]["fields"]["deviceId"], "2")
        self.assertEqual(bundle["data"]["deviceId"], 2)
        self.assertGreaterEqual(len(seen), 6)

    async def test_save_contract_puts_full_actions_once_and_returns_safe_result(self):
        puts = []
        data_reads = 0
        actions = {"revision": 7, "applications": {"OoklaSpeedTest": {"pendingSync": False, "dataOwner": "SERVER"}, "Other": {"pendingSync": False}}}
        def handler(request):
            nonlocal data_reads
            if request.url.path.endswith("/actions") and request.method == "GET":
                return httpx.Response(200, json=actions)
            if request.url.path.endswith("/actions") and request.method == "PUT":
                puts.append(json.loads(request.content))
                return httpx.Response(200, headers={"Action-Status": "/prime-home/api/v1/action-status/abc"}, json={})
            if request.url.path.endswith("/action-status/abc"):
                return httpx.Response(200, json={"completed": True, "solicitStatus": {"status": "SUCCESS"}, "syncApplications": [{"appCode": "OoklaSpeedTest", "complete": True}]})
            if request.url.path.endswith("/data"):
                data_reads += 1
                if data_reads == 1:
                    return httpx.Response(200, json={"applications": {}})
                return httpx.Response(200, json={"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"Results": {"1": {"StartTimeStamp": "200", "DownloadSpeed": "25000000", "UploadSpeed": "5000000", "Host": "redact", "ClientIP": "192.0.2.3"}}}}}}}})
            raise AssertionError(str(request.url))
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler))
        client.set_session_for_test("session", "xsrf")
        result = await client.run_ookla("2", poll_interval=0, max_polls=2)
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["revision"], 7)
        self.assertTrue(puts[0]["applications"]["OoklaSpeedTest"]["pendingSync"])
        self.assertTrue(puts[0]["solicit"])
        self.assertFalse(puts[0]["applications"]["Other"]["pendingSync"])
        self.assertEqual(result["download_mbps"], 25.0)
        self.assertNotIn("redact", json.dumps(result))


    async def test_wait_for_result_rejects_stale_result_until_timestamp_changes(self):
        reads = 0
        def handler(request):
            nonlocal reads
            if request.url.path.endswith("/data"):
                reads += 1
                stamp = "100" if reads == 1 else ("200" if reads == 2 else "300")
                return httpx.Response(200, json={"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"Results": {"1": {"StartTimeStamp": stamp, "DownloadSpeed": "1000000"}}}}}}}})
            raise AssertionError(str(request.url))
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler))
        client.set_session_for_test("session", "xsrf")
        result = await client.wait_for_speed_result("2", previous_timestamp="200", poll_interval=0, max_polls=3)
        self.assertEqual(result["start_timestamp"], "300")
        self.assertEqual(reads, 3)



    async def test_guarded_stale_clear_changes_only_ookla_and_verifies_readback(self):
        action_reads = 0
        puts = []
        before = {"revision": 7, "applications": {"OoklaSpeedTest": {"pendingSync": True, "dataOwner": "SERVER"}, "Other": {"pendingSync": False}}}
        data = {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"State": "Complete", "ExpectingResults": "false", "Results": {"1": {"Status": "Complete", "StartTimeStamp": "200"}}}}}}}}
        status = {"applications": {"OoklaSpeedTest": {"state": "OK"}}}
        def handler(request):
            nonlocal action_reads
            if request.url.path.endswith("/actions") and request.method == "GET":
                action_reads += 1
                return httpx.Response(200, json=before if action_reads == 1 else {"revision": 7, "applications": {"OoklaSpeedTest": {"pendingSync": False, "dataOwner": "SERVER"}, "Other": {"pendingSync": False}}})
            if request.url.path.endswith("/support"): return httpx.Response(200, json={"applications": {}})
            if request.url.path.endswith("/data"): return httpx.Response(200, json=data)
            if request.url.path.endswith("/applicationStatus"): return httpx.Response(200, json=status)
            if request.url.path.endswith("/actions") and request.method == "PUT": puts.append(json.loads(request.content)); return httpx.Response(200, json={})
            raise AssertionError(str(request.url))
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler));client.set_session_for_test("session", "xsrf")
        result = await client.clear_terminal_ookla_pending("2", required=True)
        self.assertTrue(result["cleared"])
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["revision"], 7)
        self.assertFalse(puts[0]["applications"]["OoklaSpeedTest"]["pendingSync"])
        self.assertFalse(puts[0]["applications"]["Other"]["pendingSync"])

    async def test_active_pending_request_is_never_cleared(self):
        puts = 0
        before = {"applications": {"OoklaSpeedTest": {"pendingSync": True}}}
        data = {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"State": "In Progress", "ExpectingResults": "true", "Results": {}}}}}}}
        def handler(request):
            nonlocal puts
            if request.url.path.endswith("/actions") and request.method == "GET": return httpx.Response(200, json=before)
            if request.url.path.endswith("/data"): return httpx.Response(200, json=data)
            if request.url.path.endswith("/applicationStatus"): return httpx.Response(200, json={"applications": {"OoklaSpeedTest": {"state": "OK"}}})
            if request.method == "PUT": puts += 1
            return httpx.Response(200, json={})
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler));client.set_session_for_test("session", "xsrf")
        with self.assertRaisesRegex(RuntimeError, "not stale"): await client.clear_terminal_ookla_pending("2", required=True)
        self.assertEqual(puts, 0)

    async def test_http_5xx_after_put_is_ambiguous_and_not_retried(self):
        puts = 0
        def handler(request):
            nonlocal puts
            if request.method == "GET":
                return httpx.Response(200, json={"applications": {"OoklaSpeedTest": {"pendingSync": False}}})
            puts += 1
            return httpx.Response(503, json={"error": "unavailable"})
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler))
        client.set_session_for_test("session", "xsrf")
        with self.assertRaises(AmbiguousSubmissionError):
            await client.start_ookla("2")
        self.assertEqual(puts, 1)

    async def test_ambiguous_put_is_not_retried(self):
        count = 0
        def handler(request):
            nonlocal count
            if request.method == "GET":
                return httpx.Response(200, json={"applications": {"OoklaSpeedTest": {"pendingSync": False}}})
            count += 1
            raise httpx.ReadTimeout("ambiguous", request=request)
        client = MosaicPortalClient("https://mosaic.example", "user", "pass", transport=httpx.MockTransport(handler))
        client.set_session_for_test("session", "xsrf")
        with self.assertRaises(AmbiguousSubmissionError):
            await client.start_ookla("2")
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
