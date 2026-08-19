import contextlib
import io
import json
import os
import tempfile
import unittest
from keyring.errors import PasswordDeleteError
from pathlib import Path
from tkinter import ttk
from unittest import mock

import cnmaestro_speed_manager as toolkit


class FakeKeyring:
    def __init__(self, stored_secret=None):
        self.stored_secret = stored_secret
        self.get_calls = []
        self.set_calls = []
        self.delete_calls = []

    def get_password(self, service, username):
        self.get_calls.append((service, username))
        return self.stored_secret

    def set_password(self, service, username, password):
        self.set_calls.append((service, username, password))

    def delete_password(self, service, username):
        self.delete_calls.append((service, username))


class CredentialHelperTests(unittest.TestCase):
    def test_remembered_client_id_loads_and_secret_is_requested_from_keyring(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({
                "theme": "Dark",
                "remember_credentials": True,
                "client_id": "saved-client",
            }), encoding="utf-8")
            backend = FakeKeyring("saved-" + "secret")

            remembered, client_id, secret = toolkit.load_saved_credentials(settings, backend)

            self.assertTrue(remembered)
            self.assertEqual((client_id, secret), ("saved-client", backend.stored_secret))
            self.assertEqual(backend.get_calls, [
                (toolkit.CREDENTIAL_SERVICE, "saved-client"),
            ])

    def test_remember_disabled_does_not_load_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({
                "remember_credentials": False,
                "client_id": "ignored-client",
            }), encoding="utf-8")
            backend = FakeKeyring("must-not-" + "load")

            remembered, client_id, secret = toolkit.load_saved_credentials(settings, backend)

            self.assertFalse(remembered)
            self.assertEqual((client_id, secret), ("", ""))
            self.assertEqual(backend.get_calls, [])


    def test_successful_save_stores_client_id_and_secret_in_keyring_only(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            backend = FakeKeyring()
            secret = "fixture-" + "credential-value"

            saved = toolkit.save_credentials("client-123", secret, settings, backend)

            self.assertTrue(saved)
            self.assertEqual(backend.set_calls, [
                (toolkit.CREDENTIAL_SERVICE, "client-123", secret),
            ])
            on_disk = settings.read_text(encoding="utf-8")
            self.assertNotIn(secret, on_disk)
            self.assertEqual(json.loads(on_disk), {
                "remember_credentials": True,
                "client_id": "client-123",
            })

    def test_settings_writer_removes_unexpected_plaintext_secret_key(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            secret = "legacy-" + "plaintext-sentinel"
            settings.write_text(json.dumps({
                "theme": "Light",
                "client_secret": secret,
            }), encoding="utf-8")

            toolkit.merge_settings({"theme": "Dark"}, settings)

            self.assertNotIn(secret, settings.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {"theme": "Dark"})

    def test_keyring_save_failure_has_no_plaintext_fallback(self):
        class FailingKeyring(FakeKeyring):
            def set_password(self, service, username, password):
                raise RuntimeError("credential store unavailable")

        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            secret = "fallback-" + "must-never-happen"

            saved = toolkit.save_credentials("client-123", secret, settings, FailingKeyring())

            self.assertFalse(saved)
            self.assertFalse(settings.exists())

    def test_theme_setting_merge_preserves_remembered_credential_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({
                "theme": "Light",
                "remember_credentials": True,
                "client_id": "saved-client",
            }), encoding="utf-8")

            toolkit.merge_settings({"theme": "Dark"}, settings)

            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {
                "theme": "Dark",
                "remember_credentials": True,
                "client_id": "saved-client",
            })


class FakeVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ConnectPersistenceTests(unittest.TestCase):
    def make_app(self, settings, backend, remember=True):
        app = toolkit.App.__new__(toolkit.App)
        app.cid = FakeVar("connected-client")
        app.sec = FakeVar("runtime-" + "secret-value")
        app.remember_credentials = FakeVar(remember)
        app.status = FakeVar("Connecting...")
        app.settings_path = settings
        app.credential_backend = backend
        return app

    def test_successful_auth_with_remember_enabled_stores_id_and_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            backend = FakeKeyring()
            app = self.make_app(settings, backend)

            app.finish_connect("https://tenant.example.test", None)

            self.assertEqual(app.status.get(), "Connected: https://tenant.example.test")
            self.assertEqual(len(backend.set_calls), 1)
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["client_id"],
                             "connected-client")

    def test_failed_auth_stores_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            backend = FakeKeyring()
            app = self.make_app(settings, backend)

            app.finish_connect(None, RuntimeError("authentication failed"))

            self.assertEqual(backend.set_calls, [])
            self.assertFalse(settings.exists())
            self.assertTrue(app.status.get().startswith("Error:"))

    def test_keyring_failure_does_not_make_successful_connection_fail(self):
        class FailingKeyring(FakeKeyring):
            def set_password(self, service, username, password):
                raise RuntimeError("do not expose this backend detail")

        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            app = self.make_app(settings, FailingKeyring())

            app.finish_connect("https://tenant.example.test", None)

            self.assertEqual(app.status.get(), "Connected; credentials not saved")
            self.assertFalse(settings.exists())
            self.assertNotIn("backend detail", app.status.get())

    def test_remember_disabled_does_not_save_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            backend = FakeKeyring()
            app = self.make_app(settings, backend, remember=False)

            app.finish_connect("https://tenant.example.test", None)

            self.assertEqual(backend.set_calls, [])
            self.assertFalse(settings.exists())


class ForgetCredentialTests(unittest.TestCase):
    def test_forget_deletes_keyring_secret_and_clears_settings_and_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({
                "theme": "Dark",
                "remember_credentials": True,
                "client_id": "saved-client",
            }), encoding="utf-8")
            backend = FakeKeyring()
            app = toolkit.App.__new__(toolkit.App)
            app.cid = FakeVar("saved-client")
            app.sec = FakeVar("memory-only-" + "secret")
            app.remember_credentials = FakeVar(True)
            app.status = FakeVar("")
            app.settings_path = settings
            app.credential_backend = backend

            app.forget_saved_credentials()

            self.assertEqual(backend.delete_calls, [
                (toolkit.CREDENTIAL_SERVICE, "saved-client"),
            ])
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {"theme": "Dark"})
            self.assertEqual((app.cid.get(), app.sec.get()), ("", ""))
            self.assertFalse(app.remember_credentials.get())
            self.assertEqual(app.status.get(), "Saved credentials forgotten")

    def test_forget_missing_credential_is_idempotent(self):
        class MissingKeyring(FakeKeyring):
            def delete_password(self, service, username):
                self.delete_calls.append((service, username))
                raise PasswordDeleteError("already missing")

        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({
                "remember_credentials": True,
                "client_id": "saved-client",
            }), encoding="utf-8")
            backend = MissingKeyring()

            self.assertTrue(toolkit.forget_credentials(settings, backend))
            self.assertTrue(toolkit.forget_credentials(settings, backend))
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {})


class CredentialUiTests(unittest.TestCase):
    def all_descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.all_descendants(child)

    def test_startup_loads_remembered_fields_without_auto_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({
                "theme": "Dark",
                "remember_credentials": True,
                "client_id": "startup-client",
            }), encoding="utf-8")
            backend = FakeKeyring("startup-" + "secret")
            with mock.patch.object(toolkit, "SETTINGS", settings), \
                 mock.patch.object(toolkit, "keyring", backend), \
                 mock.patch.object(toolkit.App, "connect") as connect, \
                 mock.patch.object(toolkit.App, "check_updates"), \
                 mock.patch.dict(os.environ, {"OPERATIONS_TOOLKIT_PREVIEW": ""}):
                app = toolkit.App()
                app.withdraw()
                app.update()
                try:
                    self.assertTrue(app.remember_credentials.get())
                    self.assertEqual(app.cid.get(), "startup-client")
                    self.assertEqual(app.sec.get(), backend.stored_secret)
                    self.assertEqual(backend.get_calls, [
                        (toolkit.CREDENTIAL_SERVICE, "startup-client"),
                    ])
                    connect.assert_not_called()
                finally:
                    app.destroy()

    def test_compact_auth_area_has_remember_checkbox_and_masked_secret(self):
        with mock.patch.dict(os.environ, {"OPERATIONS_TOOLKIT_PREVIEW": "1"}):
            app = toolkit.App()
            app.withdraw()
            app.update()
            try:
                checkbuttons = [widget for widget in self.all_descendants(app.pages["speed_manager"])
                                if isinstance(widget, ttk.Checkbutton)]
                self.assertIn("Remember credentials", [widget.cget("text") for widget in checkbuttons])
                secret_entries = [widget for widget in self.all_descendants(app.pages["speed_manager"])
                                  if isinstance(widget, ttk.Entry)
                                  and str(widget.cget("textvariable")) == str(app.sec)]
                self.assertEqual(len(secret_entries), 1)
                self.assertEqual(secret_entries[0].cget("show"), "*")
            finally:
                app.destroy()

    def test_settings_page_has_forget_saved_credentials_action(self):
        with mock.patch.dict(os.environ, {"OPERATIONS_TOOLKIT_PREVIEW": "1"}):
            app = toolkit.App()
            app.withdraw()
            app.update()
            try:
                buttons = [widget.cget("text") for widget in self.all_descendants(app.pages["settings"])
                           if isinstance(widget, ttk.Button)]
                self.assertIn("Forget saved credentials", buttons)
            finally:
                app.destroy()


class SecretSurfaceTests(unittest.TestCase):
    def test_secret_does_not_appear_in_settings_database_output_status_or_stdio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            database = root / "speed_manager.db"
            secret = "surface-" + "leak-sentinel"
            backend = FakeKeyring()
            old_db = toolkit.DB
            toolkit.DB = database
            try:
                toolkit.initdb()
                app = toolkit.App.__new__(toolkit.App)
                app.cid = FakeVar("surface-client")
                app.sec = FakeVar(secret)
                app.remember_credentials = FakeVar(True)
                app.status = FakeVar("Connecting...")
                app.settings_path = settings
                app.credential_backend = backend
                stdout = io.StringIO()
                stderr = io.StringIO()

                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    app.finish_connect("https://tenant.example.test", None)

                surfaces = "\n".join([
                    settings.read_text(encoding="utf-8"),
                    database.read_bytes().decode("latin-1"),
                    app.status.get(),
                    stdout.getvalue(),
                    stderr.getvalue(),
                ])
                self.assertNotIn(secret, surfaces)
                self.assertEqual(backend.set_calls[0][2], secret)
            finally:
                toolkit.DB = old_db


if __name__ == "__main__":
    unittest.main()
