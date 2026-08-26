from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).parents[1] / "api" / "scripts" / "import_interhub_environment.py"
SPEC = importlib.util.spec_from_file_location("import_interhub_environment", SCRIPT_PATH)
assert SPEC and SPEC.loader
IMPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORTER)


class InterHubEnvironmentImportTests(unittest.TestCase):
    def test_only_interhub_subset_is_copied_and_payment_stays_disabled(self):
        source = IMPORTER.parse_docker_environment(
            '["INTERHUB_API_URL=https://provider.test",'
            '"INTERHUB_TOKEN=provider-secret",'
            '"INTERHUB_SSL_VERIFY=false",'
            '"DATABASE_URL=must-not-be-copied"]'
        )
        content = (
            "DATABASE_URL=hub-database\n"
            "INTERHUB_TOKEN=old-token\n"
            "SUPPLIER_HUB_PURCHASES_ENABLED=true\n"
            "INTERHUB_PAY_ENABLED=true\n"
        )
        updated, keys = IMPORTER.update_environment(content, source)

        self.assertIn("DATABASE_URL=hub-database", updated)
        self.assertNotIn("must-not-be-copied", updated)
        self.assertIn("INTERHUB_TOKEN=provider-secret", updated)
        self.assertIn("INTERHUB_API_URL=https://provider.test", updated)
        self.assertIn("INTERHUB_SSL_VERIFY=false", updated)
        self.assertIn("SUPPLIER_HUB_PURCHASES_ENABLED=false", updated)
        self.assertIn("INTERHUB_PAY_ENABLED=false", updated)
        self.assertNotIn("DATABASE_URL", keys)

    def test_missing_token_fails_without_modifying_content(self):
        with self.assertRaises(RuntimeError):
            IMPORTER.update_environment("INTERHUB_TOKEN=old\n", {"INTERHUB_API_URL": "https://provider.test"})


if __name__ == "__main__":
    unittest.main()
