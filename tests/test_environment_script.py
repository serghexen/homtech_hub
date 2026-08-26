from __future__ import annotations

from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


class EnvironmentScriptTests(unittest.TestCase):
    def test_environment_is_private_and_payments_are_disabled(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / ".env.staging"
            subprocess.run(
                [sys.executable, str(root / "api" / "scripts" / "create_environment.py"), "--output", str(output)],
                check=True,
                capture_output=True,
                text=True,
            )
            content = output.read_text(encoding="utf-8")
            mode = stat.S_IMODE(output.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertIn("SUPPLIER_HUB_PURCHASES_ENABLED=false", content)
        self.assertIn("INTERHUB_PAY_ENABLED=false", content)
        self.assertNotIn("replace-with", content)


if __name__ == "__main__":
    unittest.main()
