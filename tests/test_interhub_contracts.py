from __future__ import annotations

import json
from pathlib import Path
import unittest

from hub.providers.base import ProviderState
from hub.providers.interhub import InterHubProvider
from tests.helpers import settings


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "interhub"


def fixture(name: str):
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


class InterHubContractTests(unittest.TestCase):
    def test_observed_checked_response_normalizes_as_success(self):
        result = InterHubProvider._normalize(fixture("checked"))
        self.assertEqual(result.state, ProviderState.SUCCEEDED)
        self.assertTrue(result.success)
        self.assertEqual(result.status, 0)

    def test_observed_paid_response_has_redacted_public_secret(self):
        result = InterHubProvider._normalize(fixture("paid"))
        self.assertEqual(result.state, ProviderState.SUCCEEDED)
        self.assertEqual(result.public_payload["params"]["gift_code"], "[REDACTED]")

    def test_observed_failed_response_normalizes_as_failure(self):
        result = InterHubProvider._normalize(fixture("failed"))
        self.assertEqual(result.state, ProviderState.FAILED)
        self.assertFalse(result.success)
        self.assertEqual(result.status, -136)

    def test_processing_contract_matches_crm_semantics(self):
        result = InterHubProvider._normalize(fixture("processing"))
        self.assertEqual(result.state, ProviderState.PROCESSING)
        self.assertTrue(result.success)
        self.assertEqual(result.status, 1)

    def test_observed_catalog_shape_is_accepted(self):
        provider = InterHubProvider(settings())
        provider._request = lambda *_args, **_kwargs: fixture("services")
        items = provider.services()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["service_id"], 1001)
        self.assertEqual(len(items[0]["fields"]), 2)
        self.assertTrue(items[0]["fields"][0]["required"])
        self.assertFalse(items[0]["fields"][1]["required"])

    def test_observed_balance_shape_is_accepted(self):
        provider = InterHubProvider(settings())
        provider._request = lambda *_args, **_kwargs: fixture("balance")
        self.assertEqual(
            provider.balance(),
            {"balance": "100", "currency": "RUB", "over_balance": "100", "over_limit": "100"},
        )


if __name__ == "__main__":
    unittest.main()
