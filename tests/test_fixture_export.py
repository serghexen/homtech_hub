from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).parents[1] / "api" / "scripts" / "export_crm_interhub_fixtures.py"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "interhub"
SPEC = importlib.util.spec_from_file_location("export_crm_interhub_fixtures", SCRIPT_PATH)
assert SPEC and SPEC.loader
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)


class FixtureExporterTests(unittest.TestCase):
    def test_recursive_sanitizer_removes_identifiers_and_secrets(self):
        raw = {
            "success": True,
            "status": 0,
            "transaction_id": "real-transaction-123",
            "account": "customer@example.test",
            "amount": 9123.45,
            "message": "Customer customer@example.test received ABCD-EFGH",
            "params": {
                "gift_code": "ABCD-EFGH-IJKL",
                "pin": 1234,
                "region": "EU",
            },
            "items": [{"service_id": 98765, "name": "Real product name"}],
        }

        safe = EXPORTER.sanitize(raw)
        serialized = json.dumps(safe)

        for forbidden in ("real-transaction-123", "customer@example.test", "ABCD-EFGH", "98765", "Real product"):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(safe["success"])
        self.assertEqual(safe["status"], 0)
        self.assertEqual(safe["params"]["gift_code"], "[REDACTED]")
        EXPORTER.assert_sanitized(safe)

    def test_assert_sanitized_fails_closed(self):
        with self.assertRaises(RuntimeError):
            EXPORTER.assert_sanitized({"gift_code": "still-secret"})

    def test_writer_only_persists_sanitized_payload(self):
        safe = EXPORTER.sanitize({"success": True, "params": {"code": "raw-secret"}})
        with tempfile.TemporaryDirectory() as directory:
            written = EXPORTER.write_fixtures(Path(directory), {"paid": safe})
            self.assertEqual(written, ["paid.json"])
            content = (Path(directory) / "paid.json").read_text(encoding="utf-8")
            self.assertNotIn("raw-secret", content)
            self.assertIn("[REDACTED]", content)

    def test_committed_fixtures_pass_independent_secret_scan(self):
        patterns = (
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            r"https?://",
            r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b",
            r"\b[A-Za-z0-9_-]{40,}\b",
        )
        for fixture_path in FIXTURE_DIR.glob("*.json"):
            content = fixture_path.read_text(encoding="utf-8")
            EXPORTER.assert_sanitized(json.loads(content))
            for pattern in patterns:
                self.assertIsNone(re.search(pattern, content, re.IGNORECASE), fixture_path.name)


if __name__ == "__main__":
    unittest.main()
