"""Tests for ``monthly_wastage`` — theoretical usage vs purchases.

The rule set itself is tested in ``test_wastage_rules_v12``; these cover the
comparison layer: that ayam is compared per UNIT rather than collapsed, and that
a percentage is never printed over data that cannot support one.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monthly_wastage import (  # noqa: E402
    VERDICT_HEALTHY,
    VERDICT_HIGH,
    VERDICT_NOT_MODELLED,
    VERDICT_NO_PORTION_RULE,
    VERDICT_NO_PURCHASE_CATEGORY,
    VERDICT_OVER_USED,
    VERDICT_UNRELIABLE,
    build_wastage,
    classify_variance,
    summarise_purchases,
    theoretical_usage,
    top_variances,
)


def dish(name, qty):
    return {"item_name": name, "qty": qty, "amount": qty * 10.0}


def purchase(canonical, qty_base, base_unit="g", cost=100.0, needs_review=False):
    return {"canonical_item": canonical, "qty_base": qty_base, "base_unit": base_unit,
            "line_total": cost, "needs_review": needs_review}


def row_for(result, item, unit=None):
    for row in result["rows"]:
        if row["canonical_item"] == item and (unit is None or
                                              row["theoretical_unit"] == unit or
                                              row["purchased_unit"] == unit):
            return row
    raise AssertionError(f"no row for {item}/{unit}: "
                         f"{[(r['canonical_item'], r['theoretical_unit']) for r in result['rows']]}")


class TheoreticalUsage(unittest.TestCase):

    def test_ayam_is_no_longer_unmodelled(self):
        # The whole point of encoding the rules: ayam is ~1/3 of food cost and
        # used to come out NOT MODELLED.
        usage = theoretical_usage([dish("Ayam Goreng", 100)])
        self.assertEqual(usage[("ayam", "pcs")]["qty"], 100.0)

    def test_whole_cut_and_fillet_are_separate_keys(self):
        usage = theoretical_usage([dish("Ayam Bawang", 10), dish("Ayam Paprik", 10)])
        self.assertEqual(usage[("ayam", "pcs")]["qty"], 10.0)
        self.assertEqual(usage[("ayam", "g")]["qty"], 500.0)

    def test_both_beef_stocks_land_in_one_daging_bucket(self):
        # MYSOOR and MD HANI are indistinguishable on an invoice, so they are
        # compared as one — with the split kept as detail.
        usage = theoretical_usage([dish("Nasi Goreng Daging", 10), dish("Tomyam Daging", 5)])
        entry = usage[("daging", "g")]
        self.assertEqual(entry["qty"], 900.0)
        self.assertEqual(entry["ingredients"],
                         {"daging_mysoor": 600.0, "daging_md_hani": 300.0})

    def test_rice_variants_never_merge(self):
        usage = theoretical_usage([dish("Nasi Kandar", 10), dish("Briyani Ayam", 10)])
        self.assertEqual(usage[("beras_biasa", "g")]["qty"], 1200.0)
        self.assertEqual(usage[("beras_basmati", "g")]["qty"], 1500.0)

    def test_contributing_dishes_are_recorded(self):
        usage = theoretical_usage([dish("Nasi Goreng Pattaya", 3)])
        self.assertIn("Nasi Goreng Pattaya", usage[("ayam", "g")]["contributing"])

    def test_staff_meals_and_junk_are_ignored(self):
        self.assertEqual(theoretical_usage([dish("Ayam Goreng Staff", 50)]), {})
        self.assertEqual(theoretical_usage([None, "x", {"item_name": " "}, 5]), {})

    def test_zero_quantity_rows_are_ignored(self):
        self.assertEqual(theoretical_usage([dish("Ayam Goreng", 0)]), {})


class SummarisePurchases(unittest.TestCase):

    def test_grouped_by_item_and_unit(self):
        summary = summarise_purchases([purchase("ayam", 4, "pcs"),
                                       purchase("ayam", 1000, "g")])
        self.assertEqual(summary["buckets"][("ayam", "pcs")]["qty_base"], 4.0)
        self.assertEqual(summary["buckets"][("ayam", "g")]["qty_base"], 1000.0)

    def test_same_unit_rows_are_summed(self):
        summary = summarise_purchases([purchase("ayam", 1000), purchase("ayam", 500)])
        self.assertEqual(summary["buckets"][("ayam", "g")]["qty_base"], 1500.0)

    def test_flagged_rows_are_tracked_per_item_not_summed_in(self):
        summary = summarise_purchases([
            purchase("ayam", 1000),
            purchase("ayam", None, None, cost=250.0, needs_review=True),
        ])
        self.assertEqual(summary["buckets"][("ayam", "g")]["qty_base"], 1000.0)
        self.assertEqual(summary["flagged"]["ayam"], {"rows": 1, "cost": 250.0})

    def test_needs_review_row_with_a_quantity_is_still_excluded(self):
        summary = summarise_purchases([purchase("ayam", 5000, needs_review=True)])
        self.assertEqual(summary["buckets"], {})
        self.assertEqual(summary["flagged"]["ayam"]["rows"], 1)

    def test_numeric_strings_from_postgrest(self):
        summary = summarise_purchases([purchase("ayam", "1000.5")])
        self.assertEqual(summary["buckets"][("ayam", "g")]["qty_base"], 1000.5)

    def test_null_canonical_groups_as_uncategorised(self):
        summary = summarise_purchases([purchase(None, 100)])
        self.assertIn(("UNCATEGORISED", "g"), summary["buckets"])


class VarianceBands(unittest.TestCase):

    def test_thresholds(self):
        self.assertEqual(classify_variance(20.0), VERDICT_HIGH)
        self.assertEqual(classify_variance(15.1), VERDICT_HIGH)
        self.assertEqual(classify_variance(15.0), VERDICT_HEALTHY)
        self.assertEqual(classify_variance(-5.0), VERDICT_HEALTHY)
        self.assertEqual(classify_variance(-5.1), VERDICT_OVER_USED)
        self.assertEqual(classify_variance(None), VERDICT_UNRELIABLE)

    def test_ayam_whole_cut_variance(self):
        # 100 dishes -> 100 pcs theoretical; 130 pcs bought -> +30 %.
        result = build_wastage([dish("Ayam Goreng", 100)],
                               [purchase("ayam", 130, "pcs")])
        row = row_for(result, "ayam", "pcs")
        self.assertEqual(row["variance_pct"], 30.0)
        self.assertEqual(row["verdict"], VERDICT_HIGH)

    def test_isi_ayam_variance_in_grams(self):
        # 100 Thai dishes -> 5000 g; 4500 g bought -> −10 %.
        result = build_wastage([dish("Nasi Goreng Pattaya", 100)],
                               [purchase("ayam", 4500, "g")])
        row = row_for(result, "ayam", "g")
        self.assertEqual(row["variance_pct"], -10.0)
        self.assertEqual(row["verdict"], VERDICT_OVER_USED)

    def test_whole_cut_and_fillet_are_scored_independently(self):
        result = build_wastage(
            [dish("Ayam Bawang", 100), dish("Ayam Paprik", 100)],
            [purchase("ayam", 100, "pcs"), purchase("ayam", 10000, "g")],
        )
        self.assertEqual(row_for(result, "ayam", "pcs")["variance_pct"], 0.0)
        self.assertEqual(row_for(result, "ayam", "g")["variance_pct"], 100.0)


class NoPercentageWithoutGoodData(unittest.TestCase):

    def test_flagged_purchases_make_it_unreliable(self):
        result = build_wastage(
            [dish("Ayam Goreng", 10)],
            [purchase("ayam", 10, "pcs"),
             purchase("ayam", None, None, cost=90.0, needs_review=True)],
        )
        row = row_for(result, "ayam", "pcs")
        self.assertIsNone(row["variance_pct"])
        self.assertEqual(row["verdict"], VERDICT_UNRELIABLE)
        self.assertIn("90.00", row["reason"])

    def test_purchases_in_the_wrong_unit_say_so(self):
        result = build_wastage([dish("Ayam Goreng", 10)],
                               [purchase("ayam", 5000, "g")])
        row = row_for(result, "ayam", "pcs")
        self.assertIsNone(row["variance_pct"])
        self.assertIn("purchases arrived only in g", row["reason"])

    def test_no_purchases_at_all(self):
        result = build_wastage([dish("Ayam Goreng", 10)], [])
        self.assertEqual(row_for(result, "ayam", "pcs")["verdict"], VERDICT_UNRELIABLE)

    def test_unportioned_ingredients_are_reported_in_dishes(self):
        result = build_wastage([dish("Nasi Goreng Udang", 12)], [])
        row = row_for(result, "udang")
        self.assertEqual(row["verdict"], VERDICT_NO_PORTION_RULE)
        self.assertEqual(row["theoretical_qty"], 12.0)
        self.assertEqual(row["theoretical_unit"], "dishes")
        self.assertIsNone(row["variance_pct"])
        self.assertIn("no portion size is locked", row["reason"])

    def test_ingredients_with_no_purchase_category_say_which_fix_is_needed(self):
        result = build_wastage([dish("Nasi Goreng", 10)], [])
        for item in ("telur", "beras_biasa", "minyak_masak"):
            row = row_for(result, item)
            self.assertEqual(row["verdict"], VERDICT_NO_PURCHASE_CATEGORY, item)
            self.assertIn("canonical_items_v2.json", row["reason"], item)
        self.assertIn("telur", result["no_purchase_category"])

    def test_purchases_the_rules_do_not_model(self):
        result = build_wastage([], [purchase("santan", 12000, "ml")])
        row = row_for(result, "santan")
        self.assertEqual(row["verdict"], VERDICT_NOT_MODELLED)
        self.assertEqual(row["purchased_qty"], 12000.0)
        self.assertIn("santan", result["not_modelled"])

    def test_flagged_only_items_still_appear(self):
        result = build_wastage([], [purchase("kacang", None, None, cost=45.0,
                                             needs_review=True)])
        row = row_for(result, "kacang")
        self.assertEqual(row["verdict"], VERDICT_UNRELIABLE)
        self.assertEqual(row["flagged_cost"], 45.0)


class Aggregates(unittest.TestCase):

    def test_counts_roll_up(self):
        result = build_wastage(
            [dish("Ayam Goreng", 10), dish("Nasi Goreng Udang", 5)],
            [purchase("ayam", None, None, cost=50.0, needs_review=True)],
        )
        self.assertEqual(result["flagged_rows"], 1)
        self.assertEqual(result["flagged_cost"], 50.0)
        self.assertIn("udang", result["no_portion_rule"])

    def test_top_variances_only_ranks_rows_with_a_percentage(self):
        result = build_wastage(
            [dish("Ayam Goreng", 10), dish("Nasi Goreng Udang", 5)],
            [purchase("ayam", 20, "pcs")],
        )
        top = top_variances(result, 3)
        self.assertEqual([r["canonical_item"] for r in top], ["ayam"])

    def test_top_variances_ranks_by_magnitude(self):
        result = build_wastage(
            [dish("Ayam Goreng", 10), dish("Nasi Kambing", 10)],
            [purchase("ayam", 11, "pcs"), purchase("kambing", 3600, "g")],
        )
        self.assertEqual(top_variances(result, 1)[0]["canonical_item"], "kambing")

    def test_empty_everything(self):
        result = build_wastage([], [])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["flagged_rows"], 0)


class AgainstRealPosItemwise(unittest.TestCase):
    """The rules run over the genuine 143-item Damansara itemwise section."""

    @classmethod
    def setUpClass(cls):
        from monthly_report_parser import parse_monthly_report
        from sales_parser import read_shift_close_file

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                            "sales", "S-Damansara 25May2026.TXT")
        parsed = parse_monthly_report(read_shift_close_file(path))
        cls.usage = theoretical_usage(parsed["itemwise"])

    def test_ayam_now_produces_a_number(self):
        self.assertGreater(self.usage[("ayam", "pcs")]["qty"], 0)

    def test_the_staples_are_all_modelled(self):
        for key in (("beras_biasa", "g"), ("minyak_masak", "ml"),
                    ("telur", "pcs"), ("susu_pekat", "tin")):
            self.assertIn(key, self.usage, key)
            self.assertGreater(self.usage[key]["qty"], 0, key)

    def test_drinks_powder_is_modelled(self):
        self.assertGreater(self.usage[("tea_masala", "g")]["qty"], 0)


if __name__ == "__main__":
    unittest.main()
