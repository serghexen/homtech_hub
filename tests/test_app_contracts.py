from __future__ import annotations

import unittest
from uuid import UUID

from hub.app import normalize_request_id


class AppContractTests(unittest.TestCase):
    def test_request_id_accepts_safe_correlation_value(self):
        self.assertEqual(normalize_request_id("seller:order-1/item-2"), "seller:order-1/item-2")

    def test_request_id_is_generated_when_header_is_absent(self):
        self.assertIsInstance(UUID(normalize_request_id("")), UUID)

    def test_request_id_rejects_log_injection_and_oversized_values(self):
        for value in ("unsafe\nvalue", "a" * 129, " space"):
            with self.subTest(value=value[:20]):
                with self.assertRaises(ValueError):
                    normalize_request_id(value)


if __name__ == "__main__":
    unittest.main()
