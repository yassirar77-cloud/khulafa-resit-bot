"""Unit tests for ``item_canonicalization``.

Run with::

    python -m unittest tests.test_item_canonicalization
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from item_canonicalization import (  # noqa: E402
    canonicalize_supplier,
    get_variations,
    list_canonical_categories,
)


class NormalizeExactMatch(unittest.TestCase):
    def test_exact_match_gas(self):
        self.assertEqual(canonicalize_supplier("GAS")["canonical"], "gas")

    def test_case_insensitive_lower(self):
        self.assertEqual(canonicalize_supplier("gas")["canonical"], "gas")

    def test_case_insensitive_mixed(self):
        self.assertEqual(canonicalize_supplier("Gas")["canonical"], "gas")

    def test_case_insensitive_upper(self):
        self.assertEqual(canonicalize_supplier("GAS")["canonical"], "gas")

    def test_whitespace_trimmed(self):
        self.assertEqual(canonicalize_supplier("  GAS  ")["canonical"], "gas")

    def test_multi_word_exact_bomba_gas(self):
        self.assertEqual(canonicalize_supplier("BOMBA GAS")["canonical"], "gas")

    def test_tepung_roti(self):
        self.assertEqual(canonicalize_supplier("TEPUNG ROTI")["canonical"], "tepung_roti")

    def test_reza_plastic(self):
        self.assertEqual(canonicalize_supplier("REZA PLASTIC")["canonical"], "plastic")

    def test_extra_juss(self):
        self.assertEqual(canonicalize_supplier("EXTRA JUSS")["canonical"], "extra_juss")

    def test_extra_juss_hyphen(self):
        self.assertEqual(canonicalize_supplier("EXTRA-JUSS")["canonical"], "extra_juss")

    def test_pasarmini_to_minimart(self):
        self.assertEqual(canonicalize_supplier("PASARMINI")["canonical"], "minimart")

    def test_mynews_to_minimart(self):
        self.assertEqual(canonicalize_supplier("MYNEWS")["canonical"], "minimart")

    def test_fookleong_ikan(self):
        self.assertEqual(canonicalize_supplier("FOOKLEONG IKAN")["canonical"], "ikan")

    def test_camellia_tea_canonical(self):
        self.assertEqual(canonicalize_supplier("CAMELLIA TEA")["canonical"], "tea_camellia")

    def test_diesel_variant(self):
        self.assertEqual(canonicalize_supplier("DIESAL 1604")["canonical"], "diesel")

    def test_kelapa_synonym_coconut(self):
        self.assertEqual(canonicalize_supplier("COCONUT")["canonical"], "kelapa")

    def test_lalamove_variants(self):
        self.assertEqual(canonicalize_supplier("LALAMOVE")["canonical"], "lalamove")
        self.assertEqual(canonicalize_supplier("LALA MOVE")["canonical"], "lalamove")


class NormalizeVariations(unittest.TestCase):
    def test_variation_teh_masala(self):
        self.assertEqual(canonicalize_supplier("TEH MASALA")["canonical"], "tea_masala")

    def test_variation_tea_masala(self):
        self.assertEqual(canonicalize_supplier("TEA MASALA")["canonical"], "tea_masala")

    def test_variation_the_masala(self):
        self.assertEqual(canonicalize_supplier("THE MASALA")["canonical"], "tea_masala")

    def test_babas_masala_distinct_from_saida(self):
        babas = canonicalize_supplier("BABAS MASALA")["canonical"]
        saida = canonicalize_supplier("SAIDA MASALA")["canonical"]
        self.assertEqual(babas, "babas_masala")
        self.assertEqual(saida, "saida_masala")
        self.assertNotEqual(babas, saida)

    def test_assorted_cheese_slice(self):
        self.assertEqual(canonicalize_supplier("CHEESE SLICE")["canonical"], "cheese")

    def test_assorted_appalam_box(self):
        self.assertEqual(canonicalize_supplier("APPALAM BOX")["canonical"], "papadam")

    def test_assorted_kailan(self):
        self.assertEqual(canonicalize_supplier("KAILAN")["canonical"], "sayur")

    def test_assorted_banana(self):
        self.assertEqual(canonicalize_supplier("BANANA")["canonical"], "pisang")

    def test_assorted_nasilemak(self):
        self.assertEqual(canonicalize_supplier("NASILEMAK")["canonical"], "nasi_lemak")

    def test_assorted_panadol(self):
        self.assertEqual(canonicalize_supplier("PANADOL")["canonical"], "medicine")

    def test_assorted_blender(self):
        self.assertEqual(canonicalize_supplier("BLENDER")["canonical"], "hardware")


class NormalizeSubstring(unittest.TestCase):
    def test_substring_ayam_with_suffix(self):
        res = canonicalize_supplier("AYAM BESTARI SDN BHD")
        self.assertEqual(res["canonical"], "ayam")
        self.assertTrue(res["matched"])

    def test_reverse_substring_ayam_bare(self):
        self.assertEqual(canonicalize_supplier("AYAM")["canonical"], "ayam")


class NormalizeEdgeCases(unittest.TestCase):
    def test_no_match_random_returns_unmatched(self):
        res = canonicalize_supplier("RANDOM XYZ")
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])

    def test_none_input_unmatched(self):
        res = canonicalize_supplier(None)
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])
        self.assertIsNone(res["raw"])

    def test_empty_string_unmatched(self):
        res = canonicalize_supplier("")
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])

    def test_whitespace_only_unmatched(self):
        res = canonicalize_supplier("    ")
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])

    def test_non_string_input_unmatched(self):
        res = canonicalize_supplier(12345)
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])

    def test_raw_field_preserved(self):
        raw = "  Ayam Bestari Sdn Bhd  "
        res = canonicalize_supplier(raw)
        self.assertEqual(res["raw"], raw)
        self.assertEqual(res["canonical"], "ayam")


class NormalizeFunctions(unittest.TestCase):
    def test_list_canonical_categories_count_and_sorted(self):
        cats = list_canonical_categories()
        self.assertEqual(len(cats), 54)
        self.assertEqual(cats, sorted(cats))

    def test_get_variations_gas(self):
        self.assertEqual(get_variations("gas"), ["GAS", "BOMBA GAS"])

    def test_get_variations_unknown_returns_empty(self):
        self.assertEqual(get_variations("not_a_real_category"), [])


if __name__ == "__main__":
    unittest.main()
