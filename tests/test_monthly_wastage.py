"""Tests for ``monthly_wastage``.

Two properties matter most and are tested hardest:

* theoretical usage obeys the LOCKED v12 rules (portion sizes, Thai/staff
  exclusion, ayam cut rules) — not a re-derivation of them;
* a variance percentage is never printed when it would be computed over
  incomplete or unit-mismatched data.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitchen_usage import KG_PORTION_GRAMS  # noqa: E402
from monthly_wastage import (  # noqa: E402
    VERDICT_HEALTHY,
    VERDICT_HIGH,
    VERDICT_NOT_MODELLED,
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


class TheoreticalUsageUsesLockedPortions(unittest.TestCase):

    def test_kambing_uses_the_locked_180g_portion(self):
        self.assertEqual(KG_PORTION_GRAMS["kambing"], 180.0)
        usage = theoretical_usage([dish("Nasi Kambing", 10)])
        self.assertEqual(usage["kambing"]["qty"], 1800.0)
        self.assertEqual(usage["kambing"]["unit"], "g")

    def test_daging_uses_the_locked_60g_portion(self):
        self.assertEqual(KG_PORTION_GRAMS["daging"], 60.0)
        usage = theoretical_usage([dish("Nasi Daging", 10)])
        self.assertEqual(usage["daging"]["qty"], 600.0)

    def test_ayam_stays_in_pieces_because_no_portion_is_locked(self):
        usage = theoretical_usage([dish("Ayam Goreng", 13)])
        self.assertEqual(usage["ayam"]["qty"], 13.0)
        self.assertEqual(usage["ayam"]["unit"], "pcs")

    def test_all_ayam_cuts_sum_into_one_ingredient(self):
        usage = theoretical_usage([dish("Ayam Goreng", 5), dish("Ayam Bawang", 3),
                                   dish("Ayam Kicap", 2)])
        self.assertEqual(usage["ayam"]["qty"], 10.0)

    def test_a_dish_counts_once_even_if_two_rules_could_match(self):
        # "Ayam Goreng Bawang" must not add to both the goreng and bawang cut.
        usage = theoretical_usage([dish("Ayam Goreng Bawang", 4)])
        self.assertEqual(usage["ayam"]["qty"], 4.0)

    def test_contributing_dishes_are_recorded_for_traceability(self):
        usage = theoretical_usage([dish("Nasi Kambing", 2)])
        self.assertIn("Nasi Kambing", usage["kambing"]["contributing"])


class V12Exclusions(unittest.TestCase):

    def test_staff_meals_are_excluded(self):
        self.assertEqual(theoretical_usage([dish("Ayam Goreng Staff", 50)]), {})

    def test_thai_dishes_are_excluded(self):
        self.assertEqual(theoretical_usage([dish("Thai Ayam Paprik", 50)]), {})

    def test_ayam_isi_and_noodle_dishes_are_excluded(self):
        for name in ("Ayam Paprik", "Mee Goreng Ayam", "Kuey Teow Ayam",
                     "Ayam Rendang", "Ayam Kurma", "Maggi Ayam"):
            self.assertEqual(theoretical_usage([dish(name, 10)]), {}, name)

    def test_those_exclusions_do_not_apply_to_kambing(self):
        # v12 excludes isi/noodle dishes for ayam only.
        usage = theoretical_usage([dish("Mee Kambing", 10)])
        self.assertEqual(usage["kambing"]["qty"], 1800.0)

    def test_zero_and_negative_quantities_are_ignored(self):
        self.assertEqual(theoretical_usage([dish("Ayam Goreng", 0), dish("Ayam Goreng", -2)]), {})

    def test_junk_rows_are_ignored(self):
        self.assertEqual(theoretical_usage([None, "x", {"item_name": " "}, 5]), {})


class SummarisePurchases(unittest.TestCase):

    def test_sums_convertible_rows(self):
        summary = summarise_purchases([purchase("ayam", 1000), purchase("ayam", 500)])
        self.assertEqual(summary["ayam"]["qty_base"], 1500.0)
        self.assertEqual(summary["ayam"]["unit"], "g")

    def test_flagged_rows_are_counted_separately_not_summed(self):
        summary = summarise_purchases([
            purchase("ayam", 1000), purchase("ayam", None, None, cost=250.0, needs_review=True),
        ])
        self.assertEqual(summary["ayam"]["qty_base"], 1000.0)
        self.assertEqual(summary["ayam"]["flagged_rows"], 1)
        self.assertEqual(summary["ayam"]["flagged_cost"], 250.0)

    def test_needs_review_row_with_a_quantity_is_still_excluded(self):
        summary = summarise_purchases([purchase("ayam", 5000, needs_review=True)])
        self.assertEqual(summary["ayam"]["qty_base"], 0.0)
        self.assertEqual(summary["ayam"]["flagged_rows"], 1)

    def test_mixed_units_are_marked(self):
        summary = summarise_purchases([purchase("ayam", 1000, "g"), purchase("ayam", 4, "pcs")])
        self.assertEqual(summary["ayam"]["unit"], "MIXED")

    def test_numeric_strings_from_postgrest(self):
        summary = summarise_purchases([purchase("ayam", "1000.5")])
        self.assertEqual(summary["ayam"]["qty_base"], 1000.5)

    def test_null_canonical_groups_as_uncategorised(self):
        self.assertIn("UNCATEGORISED", summarise_purchases([purchase(None, 100)]))


class VarianceBands(unittest.TestCase):

    def test_bands(self):
        self.assertEqual(classify_variance(20.0), VERDICT_HIGH)
        self.assertEqual(classify_variance(15.1), VERDICT_HIGH)
        self.assertEqual(classify_variance(15.0), VERDICT_HEALTHY)
        self.assertEqual(classify_variance(0.0), VERDICT_HEALTHY)
        self.assertEqual(classify_variance(-5.0), VERDICT_HEALTHY)
        self.assertEqual(classify_variance(-5.1), VERDICT_OVER_USED)
        self.assertEqual(classify_variance(None), VERDICT_UNRELIABLE)

    def test_variance_formula(self):
        # theoretical 1800 g, purchased 2160 g -> +20 %
        result = build_wastage([dish("Nasi Kambing", 10)], [purchase("kambing", 2160)])
        row = next(r for r in result["rows"] if r["canonical_item"] == "kambing")
        self.assertEqual(row["variance_pct"], 20.0)
        self.assertEqual(row["verdict"], VERDICT_HIGH)

    def test_over_used(self):
        result = build_wastage([dish("Nasi Kambing", 10)], [purchase("kambing", 1000)])
        row = result["rows"][0]
        self.assertEqual(row["verdict"], VERDICT_OVER_USED)


class NoPercentageWithoutGoodData(unittest.TestCase):

    def test_flagged_purchases_make_it_unreliable_with_the_rm_shown(self):
        result = build_wastage(
            [dish("Nasi Kambing", 10)],
            [purchase("kambing", 1800), purchase("kambing", None, None, cost=90.0, needs_review=True)],
        )
        row = result["rows"][0]
        self.assertIsNone(row["variance_pct"])
        self.assertEqual(row["verdict"], VERDICT_UNRELIABLE)
        self.assertEqual(row["flagged_cost"], 90.0)
        self.assertIn("90.00", row["reason"])

    def test_unit_mismatch_is_unreliable_not_converted(self):
        # Theoretical kambing is grams; purchases in pieces have no locked rule.
        result = build_wastage([dish("Nasi Kambing", 10)], [purchase("kambing", 4, "pcs")])
        row = result["rows"][0]
        self.assertIsNone(row["variance_pct"])
        self.assertIn("no locked rule converts", row["reason"])

    def test_mixed_purchase_units_are_unreliable(self):
        result = build_wastage(
            [dish("Ayam Goreng", 10)],
            [purchase("ayam", 5, "pcs"), purchase("ayam", 1000, "g")],
        )
        self.assertIsNone(result["rows"][0]["variance_pct"])

    def test_no_purchases_at_all_is_unreliable(self):
        result = build_wastage([dish("Nasi Kambing", 10)], [])
        self.assertEqual(result["rows"][0]["verdict"], VERDICT_UNRELIABLE)

    def test_ingredient_with_no_v12_rule_is_not_modelled(self):
        result = build_wastage([], [purchase("santan", 12000, "ml")])
        row = next(r for r in result["rows"] if r["canonical_item"] == "santan")
        self.assertEqual(row["verdict"], VERDICT_NOT_MODELLED)
        self.assertIsNone(row["variance_pct"])
        self.assertIn("santan", result["not_modelled"])

    def test_purchases_are_still_reported_for_unmodelled_ingredients(self):
        result = build_wastage([], [purchase("santan", 12000, "ml", cost=333.0)])
        row = next(r for r in result["rows"] if r["canonical_item"] == "santan")
        self.assertEqual(row["purchased_qty"], 12000.0)
        self.assertEqual(row["purchase_cost"], 333.0)


class Aggregates(unittest.TestCase):

    def test_counts_and_flagged_cost_roll_up(self):
        result = build_wastage(
            [dish("Nasi Kambing", 10), dish("Ayam Goreng", 10)],
            [purchase("kambing", None, None, cost=50.0, needs_review=True),
             purchase("ayam", 10, "pcs")],
        )
        self.assertEqual(result["flagged_rows"], 1)
        self.assertEqual(result["flagged_cost"], 50.0)
        self.assertGreaterEqual(result["unreliable_count"], 1)

    def test_top_variances_excludes_rows_without_a_percentage(self):
        result = build_wastage(
            [dish("Nasi Kambing", 10), dish("Ayam Goreng", 10)],
            [purchase("kambing", 3600), purchase("ayam", None, None, needs_review=True)],
        )
        top = top_variances(result, 3)
        self.assertEqual([r["canonical_item"] for r in top], ["kambing"])

    def test_top_variances_ranks_by_magnitude(self):
        result = build_wastage(
            [dish("Nasi Kambing", 10), dish("Nasi Daging", 10)],
            [purchase("kambing", 1980), purchase("daging", 1200)],
        )
        self.assertEqual(top_variances(result, 1)[0]["canonical_item"], "daging")

    def test_empty_everything(self):
        result = build_wastage([], [])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["flagged_rows"], 0)


if __name__ == "__main__":
    unittest.main()
