from pathlib import Path
import unittest
import cnmaestro_speed_manager as toolkit

ROOT = Path(__file__).resolve().parents[1]

class VersionCandidateTests(unittest.TestCase):
    def test_local_candidate_is_150_but_published_manifest_remains_140(self):
        self.assertEqual(toolkit.APP_VERSION, "1.5.0")
        self.assertIn("# Operations Toolkit 1.5.0", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("Operations-Toolkit-v1.5.0", (ROOT / "build_windows.bat").read_text(encoding="utf-8"))
        workflow = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        self.assertIn("Operations-Toolkit-v1.5.0", workflow)
        self.assertIn("tests/test_mosaic_service.py", workflow)
        self.assertIn("tests/test_mosaic_credentials.py", workflow)
        self.assertIn("tests/test_mosaic_ui.py", workflow)
        self.assertIn('"version": "1.4.0"', (ROOT / "latest.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
