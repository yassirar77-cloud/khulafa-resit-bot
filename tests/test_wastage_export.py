"""Unit tests for ``wastage_export``.

The exclusion rule is the load-bearing part: a line with real money but an
unknown quantity must never be folded into a cost-per-kg average, and must
never disappear silently either. Both halves are tested here.

Run with::

    python -m unittest tests.test_wastage_export
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402
from wastage_export import (  # noqa: E402
    CSV_COLUMNS,
    aggregate_purchases,
    build_csv,
    export_filename,
    fetch_receipt_item_rows,
    format_summary,
    parse_month,
)


def item_row(canonical="ayam", qty_base=1000.0, base_unit="g", line_total=10.0,
             needs_review=False, **extra):
    return dict(
        {"canonical_item": canonical, "qty_base": qty_base,
         "base_unit": base_unit, "line_total": line_total,
         "needs_review": needs_review, "qty": None, "unit_price": None},
        **extra,
    )


class ParseMonth(unittest.TestCase):

    def test_valid_month(self):
        self.assertEqual(parse_month("2026-08"), ("2026-08-01", "2026-08-31"))

    def test_short_and_leap_months(self):
        self.assertEqual(parse_month("2026-02"), ("2026-02-01", "2026-02-28"))
        self.assertEqual(parse_month("2028-02"), ("2028-02-01", "2028-02-29"))
        self.assertEqual(parse_month("2026-04"), ("2026-04-01", "2026-04-30"))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(parse_month(" 2026-08 "), ("2026-08-01", "2026-08-31"))

    def test_invalid_input_is_rejected(self):
        for bad in (None, "", "2026", "2026-8", "26-08", "2026-13", "2026-00",
                    "1999-05", "August 2026", 202608, "2026-08-01"):
            self.assertIsNone(parse_month(bad), bad)


class ExportFilename(unittest.TestCase):

    def test_mirrors_the_wastage_report_naming(self):
        self.assertEqual(
            export_filename("Klang B.Emas", "2026-08"),
            "Khulafa_KLANG_B_EMAS_AUGUST_2026_Cost_Calculator.csv",
        )

    def test_simple_outlet(self):
        self.assertEqual(
            export_filename("SEK-20", "2026-01"),
            "Khulafa_SEK_20_JANUARY_2026_Cost_Calculator.csv",
        )

    def test_missing_pieces_degrade_to_something_sendable(self):
        self.assertTrue(export_filename(None, "nonsense").endswith(".csv"))
        self.assertIn("UNKNOWN", export_filename(None, "nonsense"))


class AggregatePurchases(unittest.TestCase):

    def test_sums_quantity_and_cost_per_item(self):
        result = aggregate_purchases([
            item_row("ayam", 12500.0, "g", 122.5),
            item_row("ayam", 7500.0, "g", 73.5),
        ])
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["canonical_item"], "ayam")
        self.assertEqual(row["total_qty_base"], 20000.0)
        self.assertEqual(row["base_unit"], "g")
        self.assertEqual(row["total_cost"], 196.0)
        self.assertEqual(row["avg_cost_per_kg"], 9.8)  # 196 / 20kg
        self.assertEqual(result["included"], 2)

    def test_same_item_in_two_units_stays_two_rows(self):
        result = aggregate_purchases([
            item_row("ayam", 20000.0, "g", 196.0),
            item_row("ayam", 6.0, "pcs", 132.0),
        ])
        self.assertEqual(len(result["rows"]), 2)
        by_unit = {r["base_unit"]: r for r in result["rows"]}
        self.assertEqual(by_unit["g"]["total_qty_base"], 20000.0)
        self.assertEqual(by_unit["pcs"]["total_qty_base"], 6.0)

    def test_cost_per_kg_only_for_gram_items(self):
        result = aggregate_purchases([
            item_row("ayam", 6.0, "pcs", 132.0),
            item_row("drinks", 12000.0, "ml", 60.0),
        ])
        for row in result["rows"]:
            self.assertIsNone(row["avg_cost_per_kg"], row)

    def test_rows_are_sorted_by_item_then_unit(self):
        result = aggregate_purchases([
            item_row("santan", 1000.0, "g", 5.0),
            item_row("ayam", 1000.0, "g", 9.0),
            item_row("ayam", 2.0, "pcs", 4.0),
        ])
        self.assertEqual(
            [(r["canonical_item"], r["base_unit"]) for r in result["rows"]],
            [("ayam", "g"), ("ayam", "pcs"), ("santan", "g")],
        )

    def test_numeric_strings_from_postgrest_are_handled(self):
        # PostgREST returns `numeric` columns as strings.
        result = aggregate_purchases([item_row("ayam", "12500.0", "g", "122.50")])
        self.assertEqual(result["rows"][0]["total_qty_base"], 12500.0)
        self.assertEqual(result["rows"][0]["total_cost"], 122.5)

    def test_falls_back_to_qty_times_unit_price(self):
        row = item_row("ayam", 2000.0, "g", line_total=None, qty=2, unit_price=9.5)
        result = aggregate_purchases([row])
        self.assertEqual(result["rows"][0]["total_cost"], 19.0)

    def test_unknown_items_group_as_uncategorised(self):
        result = aggregate_purchases([
            item_row(None, 1000.0, "g", 5.0),
            item_row("", 1000.0, "g", 5.0),
        ])
        self.assertEqual(result["rows"][0]["canonical_item"], "UNCATEGORISED")
        self.assertEqual(result["rows"][0]["total_cost"], 10.0)
        self.assertEqual(result["uncategorised"], 2)

    def test_empty_and_junk_input(self):
        for rows in ([], None, ["x", None, 5]):
            result = aggregate_purchases(rows)
            self.assertEqual(result["rows"], [])
            self.assertEqual(result["included"], 0)


class AggregateExclusions(unittest.TestCase):

    def test_flagged_rows_are_excluded_but_their_cost_is_reported(self):
        result = aggregate_purchases([
            item_row("ayam", 10000.0, "g", 98.0),
            item_row("ayam", 5000.0, "g", 49.0, needs_review=True),
        ])
        self.assertEqual(result["rows"][0]["total_qty_base"], 10000.0)
        self.assertEqual(result["rows"][0]["total_cost"], 98.0)
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(result["excluded_cost"], 49.0)

    def test_rows_without_a_base_quantity_are_excluded(self):
        result = aggregate_purchases([
            item_row("beras-ish", None, None, 257.0),
            item_row("ayam", 1000.0, "g", 10.0),
        ])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(result["excluded_cost"], 257.0)

    def test_an_excluded_row_cannot_distort_cost_per_kg(self):
        # RM49 of ayam with an unknown quantity would push the per-kg figure
        # from 9.80 to 14.70 if its cost were counted against the known 10kg.
        result = aggregate_purchases([
            item_row("ayam", 10000.0, "g", 98.0),
            item_row("ayam", None, None, 49.0),
        ])
        self.assertEqual(result["rows"][0]["avg_cost_per_kg"], 9.8)

    def test_everything_excluded_yields_no_rows_but_a_visible_count(self):
        result = aggregate_purchases([item_row("ayam", None, None, 49.0)])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["excluded"], 1)


class BuildCsv(unittest.TestCase):

    def test_header_matches_the_cost_calculator_columns(self):
        header = build_csv([]).splitlines()[0]
        self.assertEqual(header, ",".join(CSV_COLUMNS))
        self.assertEqual(
            CSV_COLUMNS,
            ("canonical_item", "total_qty_base", "base_unit", "total_cost",
             "avg_cost_per_kg"),
        )

    def test_row_rendering(self):
        result = aggregate_purchases([item_row("ayam", 20000.0, "g", 196.0)])
        lines = build_csv(result["rows"]).splitlines()
        self.assertEqual(lines[1], "ayam,20000,g,196,9.8")

    def test_undefined_cost_per_kg_is_blank_not_zero(self):
        result = aggregate_purchases([item_row("ayam", 6.0, "pcs", 132.0)])
        line = build_csv(result["rows"]).splitlines()[1]
        self.assertTrue(line.endswith(","), line)
        self.assertNotIn("0.00", line)

    def test_commas_in_item_names_are_quoted(self):
        csv_text = build_csv([{
            "canonical_item": "ayam, segar", "total_qty_base": 1000,
            "base_unit": "g", "total_cost": 9.8, "avg_cost_per_kg": 9.8,
        }])
        self.assertIn('"ayam, segar"', csv_text)

    def test_empty_export_still_has_a_header(self):
        self.assertEqual(build_csv([]).strip(), ",".join(CSV_COLUMNS))
        self.assertEqual(build_csv(None).strip(), ",".join(CSV_COLUMNS))


class FetchReceiptItemRows(unittest.TestCase):

    def _client(self):
        client = FakeSupabase()
        client.table("receipt_items").insert([
            item_row("ayam", outlet="Klang B.Emas", invoice_date="2026-08-02"),
            item_row("ayam", outlet="Klang B.Emas", invoice_date="2026-08-31"),
            item_row("ayam", outlet="Klang B.Emas", invoice_date="2026-09-01"),
            item_row("ayam", outlet="Vista", invoice_date="2026-08-10"),
        ]).execute()
        return client

    def test_filters_by_outlet_and_month(self):
        rows = fetch_receipt_item_rows(
            self._client(), "Klang B.Emas", "2026-08-01", "2026-08-31"
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["outlet"] == "Klang B.Emas" for r in rows))
        self.assertTrue(all(r["invoice_date"].startswith("2026-08") for r in rows))

    def test_month_boundaries_are_inclusive(self):
        rows = fetch_receipt_item_rows(
            self._client(), "Klang B.Emas", "2026-08-31", "2026-08-31"
        )
        self.assertEqual(len(rows), 1)

    def test_unknown_outlet_returns_nothing(self):
        rows = fetch_receipt_item_rows(self._client(), "Nowhere", "2026-08-01", "2026-08-31")
        self.assertEqual(rows, [])


class FormatSummary(unittest.TestCase):

    def test_reports_totals_and_exclusions(self):
        aggregated = aggregate_purchases([
            item_row("ayam", 20000.0, "g", 196.0),
            item_row("santan", None, None, 49.0),
        ])
        text = format_summary("Klang B.Emas", "2026-08", aggregated, filename="x.csv")
        self.assertIn("Klang B.Emas", text)
        self.assertIn("2026-08", text)
        self.assertIn("RM196.00", text)
        self.assertIn("1 line(s) excluded", text)
        self.assertIn("RM49.00", text)
        self.assertIn("x.csv", text)

    def test_clean_month_has_no_exclusion_noise(self):
        aggregated = aggregate_purchases([item_row("ayam", 20000.0, "g", 196.0)])
        text = format_summary("Vista", "2026-08", aggregated)
        self.assertNotIn("excluded", text)

    def test_empty_month(self):
        text = format_summary("Vista", "2026-08", aggregate_purchases([]))
        self.assertIn("No purchase lines", text)


if __name__ == "__main__":
    unittest.main()
