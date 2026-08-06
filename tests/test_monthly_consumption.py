"""Unit tests for ``monthly_consumption``.

Hermetic — uses the shared in-memory ``FakeSupabase`` double. Covers the
three kg-estimation paths (pack weight in the name, per-unit markers,
qty-is-kg), the month rollup, the purchase-only filtering (internal
transfers and own-outlet lines must not count), and the formatted report.

Run with::

    python -m unittest tests.test_monthly_consumption
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402

from monthly_consumption import (  # noqa: E402
    aggregate_rows,
    build_monthly_report,
    estimate_line_kg,
    format_monthly_report,
    load_month_rows,
    month_bounds,
    month_label,
    parse_month_arg,
)

TODAY = date(2026, 8, 6)


def _row(canonical, name, qty, line_total=100.0, receipt_id=1,
         receipt_date="2026-08-02", merchant="PASAR BORONG"):
    return {
        "canonical_item": canonical,
        "raw_item_name": name,
        "qty": qty,
        "line_total": line_total,
        "receipt_id": receipt_id,
        "receipt_date": receipt_date,
        "merchant": merchant,
    }


class EstimateLineKg(unittest.TestCase):
    def test_pack_weight_times_qty(self):
        self.assertEqual(estimate_line_kg("MUTTON MYSURE 5 KG", 2), (10.0, None))
        self.assertEqual(estimate_line_kg("AYAM 2.5KG", 4), (10.0, None))

    def test_grams_convert_to_kg(self):
        kg, units = estimate_line_kg("BAWANG GORENG 900 G", 3)
        self.assertAlmostEqual(kg, 2.7)
        self.assertIsNone(units)

    def test_pack_weight_without_qty_counts_once(self):
        self.assertEqual(estimate_line_kg("BEEF 3 KG", None), (3.0, None))

    def test_unit_marker_counts_units_not_kg(self):
        self.assertEqual(estimate_line_kg("WHOLE CHICKEN", 30), (None, 30.0))
        self.assertEqual(estimate_line_kg("AYAM 2 EKOR", 2), (None, 2.0))

    def test_weight_wins_over_unit_marker(self):
        # A weighed pack that also names a container is still a weight.
        self.assertEqual(estimate_line_kg("SOTONG 1KG PKT", 5), (5.0, None))

    def test_bare_qty_is_kg(self):
        self.assertEqual(estimate_line_kg("KAMBING", 7.2), (7.2, None))

    def test_zero_or_missing_qty(self):
        self.assertEqual(estimate_line_kg("KAMBING", 0), (None, None))
        self.assertEqual(estimate_line_kg("KAMBING", None), (None, None))


class MonthHelpers(unittest.TestCase):
    def test_month_bounds(self):
        self.assertEqual(month_bounds(2026, 8), ("2026-08-01", "2026-08-31"))
        self.assertEqual(month_bounds(2026, 12), ("2026-12-01", "2026-12-31"))
        self.assertEqual(month_bounds(2024, 2), ("2024-02-01", "2024-02-29"))

    def test_month_label_is_malay(self):
        self.assertEqual(month_label(2026, 8), "Ogos 2026")
        self.assertEqual(month_label(2026, 12), "Disember 2026")

    def test_parse_month_arg(self):
        self.assertEqual(parse_month_arg("", TODAY), (2026, 8))
        self.assertEqual(parse_month_arg("last", TODAY), (2026, 7))
        self.assertEqual(parse_month_arg("last", date(2026, 1, 5)), (2025, 12))
        self.assertEqual(parse_month_arg("2026-07", TODAY), (2026, 7))
        self.assertIsNone(parse_month_arg("2026-13", TODAY))
        self.assertIsNone(parse_month_arg("julai", TODAY))


class Aggregation(unittest.TestCase):
    def test_categories_roll_up_separately(self):
        totals = aggregate_rows([
            _row("ayam", "AYAM BERSIH", 10, 120.0),
            _row("ayam", "AYAM BERSIH", 5, 60.0),
            _row("kambing", "MUTTON MYSURE 5 KG", 2, 190.0),
        ])
        self.assertEqual(totals["ayam"]["kg"], 15.0)
        self.assertEqual(totals["ayam"]["rm"], 180.0)
        self.assertEqual(totals["ayam"]["lines"], 2)
        self.assertEqual(totals["kambing"]["kg"], 10.0)

    def test_untracked_categories_ignored(self):
        totals = aggregate_rows([_row("kicap", "KICAP CAIR 2.4 KG", 1)])
        self.assertEqual(totals, {})

    def test_unit_lines_kept_apart_from_kg(self):
        totals = aggregate_rows([
            _row("ayam", "WHOLE CHICKEN", 30, 570.0),
            _row("ayam", "AYAM", 12, 130.0),
        ])
        self.assertEqual(totals["ayam"]["kg"], 12.0)
        self.assertEqual(totals["ayam"]["units"], 30.0)


class LoadMonthRows(unittest.TestCase):
    def _client(self, price_rows, receipts=()):
        client = FakeSupabase()
        for row in price_rows:
            client.table("item_prices").insert(row).execute()
        for receipt in receipts:
            client.table("receipts").insert(receipt).execute()
        return client

    def test_date_window_and_category_filter(self):
        client = self._client([
            _row("ayam", "AYAM", 5, receipt_date="2026-08-02"),
            _row("ayam", "AYAM", 9, receipt_date="2026-07-30"),   # out of month
            _row("kicap", "KICAP", 2, receipt_date="2026-08-02"),  # untracked
        ])
        rows = load_month_rows(client, "2026-08-01", "2026-08-31")
        self.assertEqual([r["qty"] for r in rows], [5])

    def test_internal_transfer_receipts_dropped(self):
        client = self._client(
            [
                _row("ayam", "AYAM", 5, receipt_id=101),
                _row("ayam", "AYAM", 9, receipt_id=102),
            ],
            receipts=[
                {"id": 101, "receipt_type": "SUPPLIER_PURCHASE"},
                {"id": 102, "receipt_type": "INTERNAL_TRANSFER"},
            ],
        )
        rows = load_month_rows(client, "2026-08-01", "2026-08-31")
        self.assertEqual([r["receipt_id"] for r in rows], [101])

    def test_unclassified_receipts_kept(self):
        # receipt_type defaults to UNKNOWN / row missing — must NOT drop.
        client = self._client([_row("ayam", "AYAM", 5, receipt_id=7)])
        rows = load_month_rows(client, "2026-08-01", "2026-08-31")
        self.assertEqual(len(rows), 1)

    def test_own_outlet_lines_dropped(self):
        client = self._client([
            _row("ayam", "AYAM", 5, merchant="RESTORAN KHULAFA"),
            _row("ayam", "AYAM", 3, merchant="BESTARI FARM"),
        ])
        rows = load_month_rows(client, "2026-08-01", "2026-08-31")
        self.assertEqual([r["merchant"] for r in rows], ["BESTARI FARM"])


class Formatting(unittest.TestCase):
    def test_report_lists_kg_units_and_rm(self):
        totals = aggregate_rows([
            _row("ayam", "AYAM", 12, 130.0),
            _row("ayam", "WHOLE CHICKEN", 30, 570.0),
            _row("kambing", "MUTTON MYSURE 5 KG", 2, 190.0),
        ])
        text = format_monthly_report(2026, 7, totals)
        self.assertIn("Ogos", month_label(2026, 8))  # sanity on helper
        self.assertIn("Julai 2026", text)
        self.assertIn("🐔 Ayam: 12 kg + 30 ekor — RM700", text)
        self.assertIn("🐐 Kambing: 10 kg — RM190", text)
        self.assertIn("💰 Jumlah: RM890", text)

    def test_full_month_has_no_clutter(self):
        # The business report is numbers only: no receipt counts, no
        # methodology footnote, no scope line on a finished month.
        totals = aggregate_rows([_row("ayam", "AYAM", 5, 55.0)])
        text = format_monthly_report(2026, 7, totals)
        self.assertNotIn("resit", text)
        self.assertNotIn("Nota", text)
        self.assertNotIn("Setakat", text)
        self.assertEqual(len([l for l in text.splitlines() if l.strip()]), 3)

    def test_month_to_date_scope_line(self):
        totals = aggregate_rows([_row("ayam", "AYAM", 5, 55.0)])
        text = format_monthly_report(
            2026, 8, totals, partial_through=date(2026, 8, 6)
        )
        self.assertIn("Setakat 6 Ogos", text)

    def test_empty_month_message(self):
        text = format_monthly_report(2026, 8, {})
        self.assertIn("Tiada rekod belian", text)

    def test_build_monthly_report_end_to_end(self):
        client = FakeSupabase()
        client.table("item_prices").insert(
            _row("ayam", "AYAM BERSIH", 20, 240.0, receipt_id=1)
        ).execute()
        client.table("receipts").insert(
            {"id": 1, "receipt_type": "SUPPLIER_PURCHASE"}
        ).execute()
        text = build_monthly_report(client, 2026, 8, today=TODAY)
        self.assertIn("Ogos 2026", text)
        self.assertIn("20 kg", text)

    def test_build_never_raises(self):
        class Exploding:
            def table(self, _name):
                raise RuntimeError("boom")

        text = build_monthly_report(Exploding(), 2026, 8, today=TODAY)
        self.assertIn("Tiada rekod belian", text)


if __name__ == "__main__":
    unittest.main()
