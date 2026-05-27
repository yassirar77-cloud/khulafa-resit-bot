"""Tests for receipts.outlet -> canonical outlet normalisation (PR #37 bug 3)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from outlet_resolver import canonical_outlet


class OutletResolverTests(unittest.TestCase):
    def test_already_canonical_passes_through(self):
        self.assertEqual(canonical_outlet("SEK-20"), "SEK-20")
        self.assertEqual(canonical_outlet("Klang B.Emas"), "Klang B.Emas")
        self.assertEqual(canonical_outlet("D.U"), "D.U")

    def test_spacing_and_case_variants_collapse(self):
        self.assertEqual(canonical_outlet("SEK 20"), "SEK-20")
        self.assertEqual(canonical_outlet("Sek 6"), "SEK-6")
        self.assertEqual(canonical_outlet("vista"), "Vista")
        self.assertEqual(canonical_outlet("KLANG"), "Klang B.Emas")

    def test_khulafa_prefix_stripped(self):
        self.assertEqual(canonical_outlet("KHULAFA SEK-20"), "SEK-20")
        self.assertEqual(canonical_outlet("Khulafa Vista"), "Vista")
        self.assertEqual(canonical_outlet("RESTORAN KHULAFA SEK 6"), "SEK-6")
        self.assertEqual(canonical_outlet("NASI KANDAR KHULAFA DAMANSARA"), "D.U")

    def test_receipt_code_short_forms(self):
        self.assertEqual(canonical_outlet("D"), "D.U")
        self.assertEqual(canonical_outlet("DAMANSARA"), "D.U")
        self.assertEqual(canonical_outlet("BISTRO7"), "Bistro")
        self.assertEqual(canonical_outlet("SEK14"), "Signature")
        self.assertEqual(canonical_outlet("SEK15"), "One Bistro")

    def test_unknown_and_empty_return_none(self):
        self.assertIsNone(canonical_outlet("UNKNOWN"))
        self.assertIsNone(canonical_outlet(""))
        self.assertIsNone(canonical_outlet("   "))
        self.assertIsNone(canonical_outlet(None))
        self.assertIsNone(canonical_outlet("Totally Random Cafe"))

    def test_bare_khulafa_does_not_vanish(self):
        # Stripping the prefix would empty the string; must not crash / map.
        self.assertIsNone(canonical_outlet("KHULAFA"))


if __name__ == "__main__":
    unittest.main()
