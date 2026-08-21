import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

_TEST_ROOT = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = _TEST_ROOT
os.environ["OPERATIONS_TOOLKIT_PREVIEW"] = "1"

import cnmaestro_speed_manager as toolkit


class MosaicUiTests(unittest.TestCase):
    def setUp(self):
        self.app = toolkit.App()
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.destroy()

    def test_nested_mosaic_speed_test_page_uses_in_app_navigation(self):
        hierarchy = [(service["label"], [(tool["key"], tool["label"]) for tool in service["tools"]]) for service in self.app.service_navigation]
        self.assertEqual(hierarchy, [
            ("cnMaestro", [("speed_manager", "Speed Manager")]),
            ("Mosaic", [("mosaic_speed_test", "Speed Test")]),
        ])
        self.assertIn("mosaic_speed_test", self.app.pages)
        self.app.deiconify()
        self.app.show_page("mosaic_speed_test")
        self.app.update()
        self.assertEqual(self.app.active_page, "mosaic_speed_test")
        self.assertTrue(self.app.pages["mosaic_speed_test"].winfo_ismapped())
        self.assertEqual([child for child in self.app.winfo_children() if child.winfo_class() == "Toplevel"], [])

    def test_mosaic_page_has_masked_credentials_candidate_table_and_actions(self):
        self.assertEqual(self.app.mosaic_password_entry.cget("show"), "*")
        self.assertEqual(tuple(self.app.mosaic_tree["columns"]), (
            "select", "customer", "code", "subscriber", "device", "model", "match", "eligibility", "state", "test_time", "download", "upload", "latency", "jitter"
        ))
        labels = {widget.cget("text") for widget in self.app.pages["mosaic_speed_test"].winfo_children() if hasattr(widget, "cget") and "text" in widget.keys()}
        self.assertIn("Mosaic Speed Test", labels)
        self.assertTrue(hasattr(self.app, "mosaic_find_button"))
        self.assertTrue(hasattr(self.app, "mosaic_run_button"))
        self.assertTrue(hasattr(self.app, "mosaic_reconcile_button"))

    def test_batch_completion_handoff_contains_only_successes_and_does_not_auto_run(self):
        rows = [
            {"name": "10014 Johnson Harriet", "mac": "AA", "success": True, "target_package": "50 Mbps"},
            {"name": "10029 Flores Lorenta", "mac": "BB", "success": False, "target_package": "50 Mbps"},
        ]
        self.app.deiconify()
        self.app.show_page("speed_manager")
        self.app.update()
        self.app.out.delete("1.0", "end")
        self.app.out.insert("1.0", json.dumps(rows))
        with mock.patch.object(self.app, "run_selected_mosaic_tests") as run:
            self.app.progtext.set("Completed | Success 1 | Failed 1")
            self.app.update()
        self.assertEqual([row["name"] for row in self.app.pending_speedtest_customers], ["10014 Johnson Harriet"])
        self.assertTrue(self.app.mosaic_handoff_outer.winfo_ismapped())
        run.assert_not_called()

    def test_handoff_review_opens_mosaic_page_without_starting_network_actions(self):
        self.app.pending_speedtest_customers = [{"name": "10014 Johnson Harriet", "mac": "AA", "success": True}]
        with mock.patch.object(self.app, "find_mosaic_matches") as find, mock.patch.object(self.app, "run_selected_mosaic_tests") as run:
            self.app.review_speedtests()
            self.app.update()
        self.assertEqual(self.app.active_page, "mosaic_speed_test")
        find.assert_not_called()
        run.assert_not_called()





    def test_clear_selected_and_clear_all_only_change_visible_candidates(self):
        self.app.pending_speedtest_customers = [{"name": "10014 Test"}]
        self.app.mosaic_candidates = [
            {"key": "2", "eligible": True, "customer": "10014 A"},
            {"key": "3", "eligible": True, "customer": "10015 B"},
        ]
        self.app.mosaic_checked = {"2"}
        self.app.clear_selected_mosaic_candidates()
        self.assertEqual([row["key"] for row in self.app.mosaic_candidates], ["3"])
        self.assertEqual(self.app.mosaic_checked, set())
        self.app.clear_all_mosaic_candidates()
        self.assertEqual(self.app.mosaic_candidates, [])
        self.assertEqual(self.app.pending_speedtest_customers, [])

    def test_csv_export_is_excel_compatible_and_formula_safe(self):
        import csv
        from pathlib import Path
        output = Path(_TEST_ROOT) / "speed-tests.csv"
        self.app.mosaic_candidates = [{
            "customer": "=danger", "subscriber_code": "10014", "subscriber_id": "1", "device_id": "2",
            "model": "ADTRAN", "match": "code_and_name", "eligibility": "Ready", "state": "verified",
            "test_time_utc": "2026-08-21 01:02:03 UTC", "download_mbps": 50.0, "upload_mbps": 10.0,
            "latency_ms": 12.0, "jitter_ms": 3.0,
        }]
        with mock.patch.object(toolkit.filedialog, "asksaveasfilename", return_value=str(output)), mock.patch.object(toolkit.messagebox, "showinfo"):
            self.app.export_mosaic_csv()
        with output.open(encoding="utf-8-sig", newline="") as handle: rows=list(csv.DictReader(handle))
        self.assertEqual(rows[0]["Customer"], "'=danger")
        self.assertEqual(rows[0]["Test time (UTC)"], "2026-08-21 01:02:03 UTC")
        self.assertEqual(rows[0]["Download Mbps"], "50.0")

    def test_result_management_controls_and_test_time_column_exist(self):
        self.assertEqual(self.app.mosaic_tree.heading("test_time")["text"], "Test time (UTC)")
        self.assertEqual(self.app.mosaic_remove_button.cget("text"), "Remove selected")
        self.assertEqual(self.app.mosaic_clear_button.cget("text"), "Clear all")
        self.assertEqual(self.app.mosaic_export_button.cget("text"), "Export CSV")


    def test_mosaic_actions_without_connection_notify_user(self):
        self.app.mosaic_api = None
        self.app.mosaic_search_code.set("10014")
        with mock.patch.object(toolkit.messagebox, "showerror") as error:
            self.app.search_mosaic_subscriber()
            self.app.run_selected_mosaic_tests()
            self.app.reconcile_mosaic_unknown()
        self.assertEqual(error.call_count, 3)
        self.assertTrue(all("Mosaic" in call.args[0] for call in error.call_args_list))

    def test_independent_subscriber_code_search_builds_candidate(self):
        class FakeApi:
            async def search_subscriber(self, code):
                return [{"fields": {"subscriberCode": code, "subscriberId": "1", "deviceId": "2", "model": "ADTRAN", "fullName": "Test Customer", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}}]
            async def read_device(self, device_id):
                return {"support": {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}, "data": {}}
        self.app.mosaic_api = FakeApi()
        self.app.mosaic_search_code.set("10014")
        def immediate(coroutine, callback):
            try: callback(asyncio.run(coroutine), None)
            except Exception as exc: callback(None, exc)
        with mock.patch.object(self.app, "bg", side_effect=immediate):
            self.app.search_mosaic_subscriber()
        self.assertEqual(len(self.app.mosaic_candidates), 1)
        self.assertEqual(self.app.mosaic_candidates[0]["subscriber_code"], "10014")
        self.assertEqual(self.app.mosaic_candidates[0]["device_id"], "2")

    def test_multiple_devices_expand_into_reviewable_device_rows(self):
        class FakeApi:
            async def read_device(self, device_id):
                return {"support": {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}, "data": {}}
        self.app.mosaic_api = FakeApi()
        records = [
            {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "2", "model": "Router A", "fullName": "Test", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}},
            {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "3", "model": "Router B", "fullName": "Test", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}},
        ]
        rows = asyncio.run(self.app.mosaic_candidates_for_records({"name": "10014 Test"}, 0, records))
        self.assertEqual({row["device_id"] for row in rows}, {"2", "3"})
        self.assertTrue(all(row["match"] == "multiple_devices" for row in rows))
        self.assertTrue(all(row["eligible"] for row in rows))

    def test_speed_test_controls_use_clear_labels_without_confirmation_box(self):
        self.assertTrue(hasattr(self.app, "mosaic_search_entry"))
        self.assertEqual(self.app.mosaic_search_button.cget("text"), "Search Mosaic")
        self.assertEqual(self.app.mosaic_reconcile_button.cget("text"), "Check uncertain tests")
        self.assertEqual(self.app.mosaic_tree.heading("state")["text"], "Test status")
        self.assertFalse(hasattr(self.app, "mosaic_confirmation"))
        import inspect
        self.assertNotIn("RUN SPEED TESTS", inspect.getsource(toolkit.App.run_selected_mosaic_tests))

    def test_repeated_run_clicks_start_only_one_worker(self):
        self.app.mosaic_api = object()
        self.app.mosaic_candidates = [{"key": "2", "device_id": "2", "subscriber_code": "10014", "model": "SDG", "eligible": True, "record": {"fields": {}}}]
        self.app.mosaic_checked = {"2"}
        def hold(coroutine, callback): coroutine.close()
        with mock.patch.object(self.app, "bg", side_effect=hold) as background:
            self.app.run_selected_mosaic_tests()
            self.app.run_selected_mosaic_tests()
        self.assertEqual(background.call_count, 1)
        self.assertEqual(str(self.app.mosaic_run_button.cget("state")), "disabled")

    def test_ui_passes_match_record_to_fresh_preflight(self):
        import inspect
        source = inspect.getsource(toolkit.App.run_selected_mosaic_tests)
        self.assertIn("record=c['record']", source)

    def test_mosaic_worker_does_not_call_tk_after_from_background_coroutine(self):
        import inspect
        source = inspect.getsource(toolkit.App.run_selected_mosaic_tests)
        self.assertNotIn("self.after(", source)

    def test_close_clears_mosaic_session_and_password(self):
        class FakeClient:
            def __init__(self): self.closed = False
            def clear_session(self): self.closed = True
        fake = FakeClient()
        self.app.mosaic_api = fake
        self.app.mosaic_password.set("sensitive")
        with mock.patch.object(self.app, "destroy") as destroy:
            self.app.close_app()
        self.assertTrue(fake.closed)
        self.assertEqual(self.app.mosaic_password.get(), "")
        destroy.assert_called_once()


    def test_minimum_size_keeps_back_navigation_visible(self):
        self.app.deiconify()
        self.app.geometry("1120x700+0+0")
        self.app.show_page("mosaic_speed_test")
        self.app.update_idletasks()
        self.app.update()
        self.assertLessEqual(
            self.app.mosaic_back_button.winfo_rootx() + self.app.mosaic_back_button.winfo_width(),
            self.app.winfo_rootx() + self.app.winfo_width(),
        )

    def test_unsupported_candidate_displays_reason_and_cannot_be_selected(self):
        self.app.mosaic_candidates = [{
            "key": "8588", "selected": False, "customer": "999999999 Test", "subscriber_code": "999999999",
            "subscriber_id": "6230", "device_id": "8588", "model": "C0 - SR905acv", "match": "Exact code",
            "eligible": False, "eligibility": "SR905 routers do not support Mosaic speed tests", "state": "unsupported",
            "download_mbps": None, "upload_mbps": None, "latency_ms": None, "jitter_ms": None,
        }]
        self.app.render_mosaic_candidates()
        item = self.app.mosaic_tree.item("8588", "values")
        self.assertIn("SR905", item[7])
        self.app.toggle_mosaic_candidate("8588")
        self.assertFalse(self.app.mosaic_candidates[0]["selected"])


if __name__ == "__main__":
    unittest.main()
