import json
import tempfile
import unittest
from pathlib import Path

from keyring.errors import PasswordDeleteError

from mosaic_service import (
    DEFAULT_MOSAIC_URL,
    MOSAIC_CREDENTIAL_SERVICE,
    forget_mosaic_credentials,
    load_mosaic_credentials,
    save_mosaic_credentials,
)


class FakeKeyring:
    def __init__(self):
        self.values = {}
        self.set_calls = []
        self.delete_calls = []
    def get_password(self, service, username):
        return self.values.get((service, username))
    def set_password(self, service, username, password):
        self.set_calls.append((service, username, password))
        self.values[(service, username)] = password
    def delete_password(self, service, username):
        self.delete_calls.append((service, username))
        if (service, username) not in self.values:
            raise PasswordDeleteError("missing")
        del self.values[(service, username)]


class MosaicCredentialTests(unittest.TestCase):
    def test_remembered_credentials_load_from_keyring_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"theme": "Dark", "mosaic_remember_credentials": True, "mosaic_username": "test-user", "mosaic_base_url": "https://mosaic.example"}), encoding="utf-8")
            backend = FakeKeyring()
            backend.values[(MOSAIC_CREDENTIAL_SERVICE, "test-user")] = "stored-password"
            remembered, base_url, username, password = load_mosaic_credentials(path, backend)
            self.assertTrue(remembered)
            self.assertEqual((base_url, username, password), ("https://mosaic.example", "test-user", "stored-password"))

    def test_disabled_remember_does_not_read_secret_or_auto_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"mosaic_remember_credentials": False, "mosaic_username": "ignored"}), encoding="utf-8")
            backend = FakeKeyring()
            remembered, base_url, username, password = load_mosaic_credentials(path, backend)
            self.assertFalse(remembered)
            self.assertEqual((base_url, username, password), (DEFAULT_MOSAIC_URL, "", ""))

    def test_save_keeps_password_out_of_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"theme": "Dark", "client_id": "cn-id"}), encoding="utf-8")
            backend = FakeKeyring()
            password = "memory-only-sentinel"
            self.assertTrue(save_mosaic_credentials("https://mosaic.example", "user", password, path, backend))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["theme"], "Dark")
            self.assertEqual(data["client_id"], "cn-id")
            self.assertEqual(data["mosaic_username"], "user")
            self.assertNotIn(password, path.read_text(encoding="utf-8"))
            self.assertEqual(backend.set_calls, [(MOSAIC_CREDENTIAL_SERVICE, "user", password)])

    def test_forget_is_idempotent_and_clears_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            backend = FakeKeyring()
            save_mosaic_credentials(DEFAULT_MOSAIC_URL, "user", "secret", path, backend)
            self.assertTrue(forget_mosaic_credentials(path, backend))
            self.assertTrue(forget_mosaic_credentials(path, backend))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("mosaic_username", data)
            self.assertNotIn("mosaic_remember_credentials", data)
            self.assertNotIn((MOSAIC_CREDENTIAL_SERVICE, "user"), backend.values)


if __name__ == "__main__":
    unittest.main()
