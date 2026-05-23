"""Unit tests for ``item_canonicalization_v2``.

Run with::

    python -m unittest tests.test_item_canonicalization_v2
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from item_canonicalization_v2 import (  # noqa: E402
    canonicalize_item,
    classify_items_in_receipt,
    get_item_variations,
    list_canonical_items,
)


class CanonicalizeItemExactMatch(unittest.TestCase):
    def test_ayam_exact(self):
        self.assertEqual(canonicalize_item("AYAM")["canonical"], "ayam")

    def test_telur_not_in_v2(self):
        res = canonicalize_item("TELUR")
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])
        self.assertFalse(res["is_noise"])

    def test_kopi_exact(self):
        self.assertEqual(canonicalize_item("KOPI")["canonical"], "kopi")

    def test_sotong_exact(self):
        self.assertEqual(canonicalize_item("SOTONG")["canonical"], "sotong")

    def test_cuka_exact(self):
        self.assertEqual(canonicalize_item("CUKA")["canonical"], "cuka")

    def test_case_insensitive_lower(self):
        self.assertEqual(canonicalize_item("ayam")["canonical"], "ayam")

    def test_case_insensitive_mixed(self):
        self.assertEqual(canonicalize_item("Ayam")["canonical"], "ayam")

    def test_case_insensitive_upper(self):
        self.assertEqual(canonicalize_item("AYAM")["canonical"], "ayam")


class CanonicalizeItemVariations(unittest.TestCase):
    def test_whole_leg_to_ayam(self):
        self.assertEqual(canonicalize_item("WHOLE LEG")["canonical"], "ayam")

    def test_super_ch_to_ayam(self):
        self.assertEqual(canonicalize_item("SUPER CH")["canonical"], "ayam")

    def test_li_agam_ocr_to_ayam(self):
        self.assertEqual(canonicalize_item("LI AGAM")["canonical"], "ayam")

    def test_li_agam_4_kg_substring_to_ayam(self):
        self.assertEqual(canonicalize_item("LI AGAM 4 KG")["canonical"], "ayam")

    def test_beef_3_kg_to_daging(self):
        self.assertEqual(canonicalize_item("BEEF 3 KG")["canonical"], "daging")

    def test_mutton_mysure_5_kg_to_kambing(self):
        self.assertEqual(canonicalize_item("MUTTON MYSURE 5 KG")["canonical"], "kambing")

    def test_block_ice_ais_blok_to_ais_batu(self):
        self.assertEqual(canonicalize_item("BLOCK ICE AIS BLOK")["canonical"], "ais_batu")

    def test_tube_ice_ais_tiub_to_ais_batu(self):
        self.assertEqual(canonicalize_item("TUBE ICE AIS TIUB")["canonical"], "ais_batu")

    def test_santan_1_kg_to_santan(self):
        self.assertEqual(canonicalize_item("SANTAN 1 KG")["canonical"], "santan")

    def test_extra_joss_manga_to_extra_juss(self):
        self.assertEqual(canonicalize_item("EXTRA JOSS MANGA")["canonical"], "extra_juss")


class CanonicalizeItemNoiseFiltering(unittest.TestCase):
    def test_open_code_is_noise(self):
        res = canonicalize_item("OPEN CODE")
        self.assertTrue(res["is_noise"])
        self.assertIsNone(res["canonical"])
        self.assertFalse(res["matched"])

    def test_leave_pay_is_noise(self):
        self.assertTrue(canonicalize_item("LEAVE PAY")["is_noise"])

    def test_deposit_silinder_substring_is_noise(self):
        res = canonicalize_item("DEPOSIT SILINDER 50KG")
        self.assertTrue(res["is_noise"])
        self.assertIsNone(res["canonical"])

    def test_tomyam_gaji_is_noise(self):
        self.assertTrue(canonicalize_item("TOMYAM GAJI")["is_noise"])

    def test_question_mark_is_noise(self):
        res = canonicalize_item("?")
        self.assertTrue(res["is_noise"])
        self.assertIsNone(res["canonical"])

    def test_real_item_ayam_is_not_noise(self):
        res = canonicalize_item("AYAM")
        self.assertFalse(res["is_noise"])
        self.assertTrue(res["matched"])


class CanonicalizeItemEdgeCases(unittest.TestCase):
    def test_none_input(self):
        res = canonicalize_item(None)
        self.assertFalse(res["matched"])
        self.assertFalse(res["is_noise"])
        self.assertIsNone(res["canonical"])

    def test_empty_string(self):
        res = canonicalize_item("")
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])

    def test_whitespace_only(self):
        res = canonicalize_item("   ")
        self.assertFalse(res["matched"])
        self.assertIsNone(res["canonical"])

    def test_random_new_item_unmatched_not_noise(self):
        res = canonicalize_item("RANDOM NEW ITEM")
        self.assertFalse(res["matched"])
        self.assertFalse(res["is_noise"])
        self.assertIsNone(res["canonical"])

    def test_whitespace_trimmed(self):
        self.assertEqual(canonicalize_item("  AYAM  ")["canonical"], "ayam")

    def test_special_chars_ali_cafe_accent(self):
        self.assertEqual(canonicalize_item("ALI CAFÉ")["canonical"], "kopi")


class ClassifyItemsInReceipt(unittest.TestCase):
    def test_single_item(self):
        out = classify_items_in_receipt([{"name": "AYAM"}])
        self.assertEqual(out["canonical_counts"], {"ayam": 1})
        self.assertEqual(out["noise_count"], 0)
        self.assertEqual(out["unmatched"], [])

    def test_multiple_same_canonical(self):
        out = classify_items_in_receipt([{"name": "AYAM"}, {"name": "ayam"}])
        self.assertEqual(out["canonical_counts"], {"ayam": 2})

    def test_mixed_canonical_noise_unmatched(self):
        out = classify_items_in_receipt([
            {"name": "AYAM"},
            {"name": "KOPI"},
            {"name": "OPEN CODE"},
            {"name": "LEAVE PAY"},
            {"name": "WEIRD ITEM 1"},
            {"name": "ANOTHER WEIRD"},
        ])
        self.assertEqual(out["canonical_counts"], {"ayam": 1, "kopi": 1})
        self.assertEqual(out["noise_count"], 2)
        self.assertEqual(sorted(out["unmatched"]), sorted(["WEIRD ITEM 1", "ANOTHER WEIRD"]))

    def test_qty_field_summed(self):
        out = classify_items_in_receipt([
            {"name": "AYAM", "qty": 5},
            {"name": "AYAM", "qty": 2},
        ])
        self.assertEqual(out["canonical_counts"], {"ayam": 7})

    def test_empty_list(self):
        out = classify_items_in_receipt([])
        self.assertEqual(out["canonical_counts"], {})
        self.assertEqual(out["noise_count"], 0)
        self.assertEqual(out["unmatched"], [])

    def test_none_input(self):
        out = classify_items_in_receipt(None)
        self.assertEqual(out["canonical_counts"], {})
        self.assertEqual(out["noise_count"], 0)
        self.assertEqual(out["unmatched"], [])


class ItemCanonicalUtilities(unittest.TestCase):
    def test_list_canonical_items_count_and_sorted(self):
        items = list_canonical_items()
        self.assertEqual(len(items), 34)
        self.assertEqual(items, sorted(items))

    def test_get_item_variations_ayam(self):
        vars_ = get_item_variations("ayam")
        self.assertEqual(len(vars_), 11)
        self.assertIn("AYAM", vars_)
        self.assertIn("LI AGAM", vars_)

    def test_get_item_variations_nonexistent(self):
        self.assertEqual(get_item_variations("nonexistent"), [])

    def test_utilities_do_not_crash_on_edge(self):
        # list_canonical_items should never raise
        self.assertIsInstance(list_canonical_items(), list)
        # get_item_variations with weird inputs returns an empty list
        self.assertEqual(get_item_variations(""), [])
        self.assertEqual(get_item_variations("AYAM"), [])  # canonical keys are lowercase


if __name__ == "__main__":
    unittest.main()
