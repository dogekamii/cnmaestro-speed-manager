import json
import tempfile
import unittest
from pathlib import Path

import cnmaestro_speed_manager as toolkit


DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/dogekamii/Operations-Toolkit/"
    "refs/heads/main/latest.json"
)


class ManifestUrlResolutionTests(unittest.TestCase):
    def test_missing_sidecar_uses_builtin_manifest_url(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_config = Path(directory) / "update_config.json"

            self.assertEqual(toolkit.resolve_manifest_url(missing_config), DEFAULT_MANIFEST_URL)

    def test_valid_sidecar_overrides_builtin_manifest_url(self):
        override_url = "https://updates.example.test/operations-toolkit/latest.json"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "update_config.json"
            config.write_text(json.dumps({"manifest_url": override_url}), encoding="utf-8")

            self.assertEqual(toolkit.resolve_manifest_url(config), override_url)


if __name__ == "__main__":
    unittest.main()
