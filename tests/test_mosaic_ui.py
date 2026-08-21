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
            "select", "customer", "code", "subscriber", "device", "model", "match", "eligibility", "state", "download", "upload", "latency", "jitter"
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



    def test_repeated_run_clicks_start_only_one_worker(self):
        self.app.mosaic_api = object()
        self.app.mosaic_confirmation.set("RUN SPEED TESTS")
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
