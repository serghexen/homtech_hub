from __future__ import annotations

from pathlib import Path
import json
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
        self.assertIn("SUPPLIER_HUB_OPERATORS_JSON=", content)
        self.assertNotIn("replace-with", content)

    def test_existing_environment_gets_one_private_operator_credential(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / ".env.staging"
            output.write_text(
                "SUPPLIER_HUB_PURCHASES_ENABLED=false\nINTERHUB_PAY_ENABLED=false\n",
                encoding="utf-8",
            )
            output.chmod(0o600)
            command = [
                sys.executable,
                str(root / "api" / "scripts" / "ensure_operator_environment.py"),
                "--target",
                str(output),
            ]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            content_after_first = output.read_text(encoding="utf-8")
            second = subprocess.run(command, check=True, capture_output=True, text=True)
            content_after_second = output.read_text(encoding="utf-8")
            mode = stat.S_IMODE(output.stat().st_mode)

        line = next(
            item for item in content_after_first.splitlines()
            if item.startswith("SUPPLIER_HUB_OPERATORS_JSON=")
        )
        configured = json.loads(line.split("=", 1)[1])
        self.assertEqual(mode, 0o600)
        self.assertGreaterEqual(len(configured["operator"]), 32)
        self.assertEqual(content_after_first, content_after_second)
        self.assertNotIn(configured["operator"], first.stdout)
        self.assertNotIn(configured["operator"], second.stdout)


if __name__ == "__main__":
    unittest.main()
