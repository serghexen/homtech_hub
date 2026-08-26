from __future__ import annotations

import unittest

from hub.crypto import value_hash


class CryptoTests(unittest.TestCase):
    def test_result_hash_is_stable_and_not_plaintext(self):
        self.assertEqual(value_hash("A"), value_hash("A"))
        self.assertNotEqual(value_hash("A"), value_hash("B"))
        self.assertNotIn("GAME-CODE", value_hash("GAME-CODE"))


if __name__ == "__main__":
    unittest.main()
