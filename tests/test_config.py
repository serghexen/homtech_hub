from __future__ import annotations

import unittest

from hub.config import parse_clients
from tests.helpers import settings


class SettingsTests(unittest.TestCase):
    def test_live_pay_requires_both_kill_switches(self):
        self.assertFalse(settings(purchases_enabled=True, interhub_pay_enabled=False).live_pay_allowed)
        self.assertFalse(settings(purchases_enabled=False, interhub_pay_enabled=True).live_pay_allowed)
        self.assertTrue(settings(purchases_enabled=True, interhub_pay_enabled=True).live_pay_allowed)

    def test_live_pay_requires_provider_configuration(self):
        configured = settings(
            purchases_enabled=True,
            interhub_pay_enabled=True,
            interhub_token="",
        )
        self.assertIn("InterHub URL and token are required when live payments are enabled", configured.readiness_errors())

    def test_invalid_client_json_is_rejected_as_empty(self):
        self.assertEqual(parse_clients("not-json"), {})
        self.assertEqual(parse_clients("[]"), {})


if __name__ == "__main__":
    unittest.main()
