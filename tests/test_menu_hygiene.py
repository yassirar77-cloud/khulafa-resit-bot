"""Tests for ``menu_hygiene``.

The duplicate-SKU rules are validated against genuine POS output: the real
Damansara fixture contains ``" Tambah_nasi"`` alongside ``"Tambah Nasi"``, which
is precisely the leading-space + underscore collision the sheet exists to find.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from menu_hygiene import (  # noqa: E402
    build_menu_hygiene,
    find_dead_skus,
    find_duplicate_skus,
    find_leakage_skus,
    normalize_sku,
)
from monthly_report_parser import parse_monthly_report  # noqa: E402
from sales_parser import read_shift_close_file  # noqa: E402

REAL_POS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "sales",
    "S-Damansara 25May2026.TXT",
)


def item(name, qty=1.0, amount=1.0):
    return {"item_name": name, "qty": qty, "amount": amount}


class NormalizeSku(unittest.TestCase):

    def test_folds_the_three_collision_shapes(self):
        for name in (" Tambah_nasi", "Tambah Nasi", "Tambah  Nasi ", "TAMBAH NASI",
                     "tambah_nasi"):
            self.assertEqual(normalize_sku(name), "TAMBAH NASI", name)

    def test_does_not_fold_genuinely_different_names(self):
        self.assertNotEqual(normalize_sku("Teh Ais"), normalize_sku("Teh Ais Special"))

    def test_non_string(self):
        self.assertEqual(normalize_sku(None), "")
        self.assertEqual(normalize_sku(7), "")


class DuplicateSkus(unittest.TestCase):

    def test_leading_space_collision(self):
        groups = find_duplicate_skus([item(" Nasi Goreng"), item("Nasi Goreng")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["variant_count"], 2)

    def test_underscore_collision(self):
        self.assertEqual(len(find_duplicate_skus([item(" Tambah_nasi"), item("Tambah Nasi")])), 1)

    def test_double_space_collision(self):
        self.assertEqual(len(find_duplicate_skus([item("Barli  Panas"), item("Barli Panas")])), 1)

    def test_a_single_spelling_is_not_a_duplicate(self):
        self.assertEqual(find_duplicate_skus([item("Nasi Goreng", qty=99)]), [])

    def test_invisible_differences_are_made_visible(self):
        groups = find_duplicate_skus([item(" Tambah_nasi"), item("Tambah Nasi")])
        displays = [v["display"] for v in groups[0]["variants"]]
        self.assertIn("« Tambah_nasi»", displays)

    def test_ranked_by_money_at_stake(self):
        groups = find_duplicate_skus([
            item("A", amount=5.0), item(" A", amount=5.0),
            item("B", amount=500.0), item(" B", amount=500.0),
        ])
        self.assertEqual(groups[0]["key"], "B")
        self.assertEqual(groups[0]["total_amount"], 1000.0)

    def test_quantities_and_amounts_are_summed_per_variant(self):
        groups = find_duplicate_skus([
            item("A", qty=2, amount=10.0), item("A", qty=3, amount=15.0), item(" A", qty=1, amount=5.0),
        ])
        by_name = {v["item_name"]: v for v in groups[0]["variants"]}
        self.assertEqual(by_name["A"]["qty"], 5.0)
        self.assertEqual(by_name["A"]["amount"], 25.0)

    def test_junk_rows_are_ignored(self):
        self.assertEqual(find_duplicate_skus([None, "x", {"item_name": "  "}, 5]), [])


class DeadSkus(unittest.TestCase):

    def test_threshold_is_five_or_fewer(self):
        dead = {e["item_name"] for e in find_dead_skus([
            item("Sold5", qty=5), item("Sold6", qty=6), item("Sold0", qty=0),
        ])}
        self.assertEqual(dead, {"Sold5", "Sold0"})

    def test_quantities_are_summed_before_the_test(self):
        # 3 + 3 = 6 is alive, even though each line is below the threshold.
        self.assertEqual(find_dead_skus([item("A", qty=3), item("A", qty=3)]), [])

    def test_sorted_by_quantity_ascending(self):
        dead = find_dead_skus([item("A", qty=4), item("B", qty=1)])
        self.assertEqual([e["item_name"] for e in dead], ["B", "A"])

    def test_custom_threshold(self):
        self.assertEqual(len(find_dead_skus([item("A", qty=9)], max_qty=10)), 1)


class LeakageSkus(unittest.TestCase):

    def test_matches_the_four_keywords_case_insensitively(self):
        rows = [item("Open Price"), item("FOC Teh O"), item("Jumbo Set"),
                item("Discount 10"), item("open item"), item("Nasi Goreng")]
        names = {e["item_name"] for e in find_leakage_skus(rows)}
        self.assertEqual(len(names), 5)
        self.assertNotIn("Nasi Goreng", names)

    def test_reports_txn_count_and_money(self):
        rows = find_leakage_skus([item("Open Price", qty=12, amount=340.0)])
        self.assertEqual(rows[0]["qty"], 12.0)
        self.assertEqual(rows[0]["amount"], 340.0)

    def test_ranked_by_money(self):
        rows = find_leakage_skus([item("Foc A", amount=5.0), item("Open B", amount=900.0)])
        self.assertEqual(rows[0]["item_name"], "Open B")


class AgainstRealPosOutput(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        body = read_shift_close_file(REAL_POS_FILE)
        parsed = parse_monthly_report("MONTHLY REPORT-Damansara ON Jul 2026\n" + body)
        cls.hygiene = build_menu_hygiene(parsed["itemwise"])

    def test_finds_the_real_tambah_nasi_collision(self):
        keys = {g["key"] for g in self.hygiene["duplicates"]}
        self.assertIn("TAMBAH NASI", keys)

    def test_the_collision_is_a_leading_space_plus_underscore(self):
        group = next(g for g in self.hygiene["duplicates"] if g["key"] == "TAMBAH NASI")
        names = {v["item_name"] for v in group["variants"]}
        self.assertEqual(names, {" Tambah_nasi", "Tambah Nasi"})

    def test_finds_the_real_foc_button(self):
        self.assertTrue(any("Foc" in e["item_name"] for e in self.hygiene["leakage"]))

    def test_counts_are_self_consistent(self):
        self.assertEqual(self.hygiene["duplicate_count"], len(self.hygiene["duplicates"]))
        self.assertEqual(self.hygiene["dead_count"], len(self.hygiene["dead"]))
        self.assertEqual(self.hygiene["distinct_skus"], 143)


class EmptyInput(unittest.TestCase):

    def test_no_rows(self):
        report = build_menu_hygiene([])
        self.assertEqual(report["duplicate_count"], 0)
        self.assertEqual(report["dead_count"], 0)
        self.assertEqual(report["distinct_skus"], 0)

    def test_none(self):
        self.assertEqual(build_menu_hygiene(None)["duplicate_count"], 0)


if __name__ == "__main__":
    unittest.main()
