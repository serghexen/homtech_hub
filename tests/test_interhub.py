from __future__ import annotations

import unittest

from hub.providers.base import ProviderState
from hub.providers.interhub import InterHubProvider, provider_state, redact_payload
from tests.helpers import settings


class InterHubTests(unittest.TestCase):
    def test_provider_status_matches_working_crm_semantics(self):
        self.assertEqual(provider_state(True, 1), ProviderState.PROCESSING)
        self.assertEqual(provider_state(True, 0), ProviderState.SUCCEEDED)
        self.assertEqual(provider_state(False, 0), ProviderState.FAILED)

    def test_normalization_extracts_secret_but_redacts_public_payload(self):
        result = InterHubProvider._normalize(
            {
                "success": True,
                "status": 0,
                "transaction_id": "provider-1",
                "params": {"gift_code": "SECRET-CODE", "region": "TR"},
            }
        )
        self.assertEqual(result.secret_value, "SECRET-CODE")
        self.assertEqual(result.public_payload["params"]["gift_code"], "[REDACTED]")
        self.assertEqual(result.public_payload["params"]["region"], "TR")

    def test_redaction_is_recursive(self):
        redacted = redact_payload({"result": [{"pin": "1234"}], "code": "ABC"})
        self.assertEqual(redacted["code"], "[REDACTED]")
        self.assertEqual(redacted["result"][0]["pin"], "[REDACTED]")

    def test_catalog_normalizes_nested_services(self):
        provider = InterHubProvider(settings())
        provider._request = lambda *_args, **_kwargs: {
            "data": {
                "services": [
                    {
                        "id": "7",
                        "name": "Voucher",
                        "type": "voucher",
                        "fields": [{"name": "nominal", "required": True, "value_list": ["10"]}],
                    }
                ]
            }
        }
        items = provider.services()
        self.assertEqual(items[0]["service_id"], 7)
        self.assertEqual(items[0]["type"], "VOUCHER")
        self.assertEqual(items[0]["fields"][0]["name"], "nominal")


if __name__ == "__main__":
    unittest.main()
