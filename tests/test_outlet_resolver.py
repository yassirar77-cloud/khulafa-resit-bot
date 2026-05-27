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

    def test_production_variants_25_26_may(self):
        # Real receipts.outlet values that previously failed to normalise and
        # caused reconciliation to produce 0 matches (hotfix).
        cases = {
            "HJ SHARFUDDIN SEK 6": "SEK-6",
            "SEK 20": "SEK-20",
            "SEK 15": "One Bistro",
            "Hj Sharfuddin Klang Bayumas": "Klang B.Emas",
            "Kl Sg Besi": "SBESI",
            "Vista": "Vista",
            "Damansara": "D.U",
            "Bistro": "Bistro",
            "Jakel": "Jakel",
            "Signature": "Signature",
        }
        for raw, expected in cases.items():
            self.assertEqual(canonical_outlet(raw), expected, f"{raw!r} -> {expected!r}")

    def test_sharfuddin_prefix_case_insensitive(self):
        self.assertEqual(canonical_outlet("hj sharfuddin sek 6"), "SEK-6")
        self.assertEqual(canonical_outlet("HAJI SHARFUDDIN SEK 6"), "SEK-6")

    def test_klang_bayu_emas_spellings(self):
        for raw in ("Klang Bayumas", "Bayu Emas", "B.EMAS", "Klang B.Emas",
                    "KLANG BAYU EMAS"):
            self.assertEqual(canonical_outlet(raw), "Klang B.Emas", raw)

    def test_sbesi_spellings(self):
        for raw in ("Kl Sg Besi", "KL SG BESI", "SG BESI", "SBESI"):
            self.assertEqual(canonical_outlet(raw), "SBESI", raw)

    def test_damansara_uptown(self):
        self.assertEqual(canonical_outlet("DAMANSARA UPTOWN"), "D.U")
        self.assertEqual(canonical_outlet("D.U"), "D.U")

    def test_null_is_unattributed(self):
        self.assertIsNone(canonical_outlet(None))


if __name__ == "__main__":
    unittest.main()
