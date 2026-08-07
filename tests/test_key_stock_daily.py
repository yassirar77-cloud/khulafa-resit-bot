"""Unit tests for ``key_stock_daily``.

Hermetic — the fold/evaluation/formatting layers are fed plain dicts.
The 24h business-day correctness (two shift emails folding onto one day)
is the heart of the feature, so the fold tests model exactly the real
case: the 19:00 day-shift row and the next-morning 07:00 overnight row
both carrying the SAME shift_business_date.

Run with::

    python -m unittest tests.test_key_stock_daily
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from key_stock_daily import (  # noqa: E402
    evaluate_day,
    format_manager_key_stock,
    format_owner_summary,
    qty_by_day,
    sales_by_day,
    target_business_day,
)

D = "2026-08-06"


class TargetDay(unittest.TestCase):

    def test_judges_yesterday_never_today(self):
        # Today's overnight shift closes ~07:00 TOMORROW, so today can't be
        # judged — the target is always the just-closed day.
        self.assertEqual(target_business_day(date(2026, 8, 7)), date(2026, 8, 6))


class SalesFold(unittest.TestCase):

    def test_day_and_overnight_shift_sum_to_one_business_day(self):
        # The user's exact case: 6 Aug 19:00 close + 7 Aug 07:00 close were
        # both ingested with shift_business_date 2026-08-06. The whole-day
        # sales must be the SUM of both.
        rows = [
            {"outlet_code": "S-SBESI", "outlet_canonical": "Sungai Besi",
             "shift_business_date": D, "shift_type": "day",
             "total_sales": 2400.0},
            {"outlet_code": "S-SBESI", "outlet_canonical": "Sungai Besi",
             "shift_business_date": D, "shift_type": "overnight",
             "total_sales": 1500.0},
        ]
        out = sales_by_day(rows, "SBESI")
        self.assertAlmostEqual(out[D], 3900.0)

    def test_other_outlets_do_not_leak_in(self):
        rows = [
            {"outlet_code": "S-SBESI", "shift_business_date": D,
             "shift_type": "day", "total_sales": 2400.0},
            {"outlet_code": "S-SEK20", "shift_business_date": D,
             "shift_type": "day", "total_sales": 9999.0},
        ]
        self.assertAlmostEqual(sales_by_day(rows, "SBESI")[D], 2400.0)

    def test_garbage_never_raises(self):
        try:
            self.assertEqual(sales_by_day(None, "SBESI"), {})
            self.assertEqual(sales_by_day(["junk", {}], "SBESI"), {})
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"sales_by_day raised: {e}")


class QtyFold(unittest.TestCase):

    def test_kg_grouped_per_day_and_category(self):
        rows = [
            {"outlet_code": "SBESI", "canonical_item": "ayam",
             "raw_item_name": "AYAM BERSIH", "qty": 30.0,
             "receipt_date": D},
            {"outlet_code": "SBESI", "canonical_item": "ayam",
             "raw_item_name": "AYAM BERSIH", "qty": 25.0,
             "receipt_date": D},
            {"outlet_code": "SEK20", "canonical_item": "ayam",
             "raw_item_name": "AYAM BERSIH", "qty": 99.0,
             "receipt_date": D},
        ]
        out = qty_by_day(rows, "SBESI")
        self.assertAlmostEqual(out[D]["ayam"]["kg"], 55.0)


def _baseline(days=10, daily_kg=50.0, daily_sales=4000.0):
    """A steady baseline: same kg and sales every day -> median ratio
    daily_kg per (daily_sales/1000)."""
    base_sales, base_qty = {}, {}
    for i in range(1, days + 1):
        day = f"2026-07-{i:02d}"
        base_sales[day] = daily_sales
        base_qty[day] = {"ayam": {"kg": daily_kg, "units": 0.0}}
    return base_sales, base_qty


class EvaluateDay(unittest.TestCase):

    def test_overbought_day_is_flagged_with_expected_qty(self):
        base_sales, base_qty = _baseline()  # 50 kg per RM4000 -> 12.5 kg/RM1000
        # Sales RM4000 again but bought 82 kg (expected 50): ratio 1.64x, +32 kg.
        flags = evaluate_day(
            4000.0, {"ayam": {"kg": 82.0, "units": 0.0}}, base_sales, base_qty
        )
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["category"], "ayam")
        self.assertEqual(flags[0]["unit"], "kg")
        self.assertAlmostEqual(flags[0]["expected_qty"], 50.0)

    def test_busy_day_buying_more_is_fine(self):
        # Sales RM6000 (busy) -> expected 75 kg; buying 80 kg is proportional.
        base_sales, base_qty = _baseline()
        flags = evaluate_day(
            6000.0, {"ayam": {"kg": 80.0, "units": 0.0}}, base_sales, base_qty
        )
        self.assertEqual(flags, [])

    def test_small_excess_below_absolute_gate_is_tolerated(self):
        # 20% over expected but only +2 kg -> under both gates' spirit.
        base_sales, base_qty = _baseline(daily_kg=5.0)
        flags = evaluate_day(
            4000.0, {"ayam": {"kg": 7.0, "units": 0.0}}, base_sales, base_qty
        )
        self.assertEqual(flags, [])

    def test_thin_baseline_is_never_judged(self):
        base_sales, base_qty = _baseline(days=5)  # < 8 buying days
        flags = evaluate_day(
            4000.0, {"ayam": {"kg": 82.0, "units": 0.0}}, base_sales, base_qty
        )
        self.assertEqual(flags, [])

    def test_unit_counted_category_judged_in_ekor(self):
        base_sales, base_qty = {}, {}
        for i in range(1, 11):
            day = f"2026-07-{i:02d}"
            base_sales[day] = 4000.0
            base_qty[day] = {"ayam": {"kg": 0.0, "units": 30.0}}
        flags = evaluate_day(
            4000.0, {"ayam": {"kg": 0.0, "units": 50.0}}, base_sales, base_qty
        )
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["unit"], "ekor")
        self.assertAlmostEqual(flags[0]["expected_qty"], 30.0)

    def test_no_sales_or_garbage_never_raises(self):
        base_sales, base_qty = _baseline()
        try:
            self.assertEqual(evaluate_day(0, {"ayam": {"kg": 82.0}}, base_sales, base_qty), [])
            self.assertEqual(evaluate_day(None, None, None, None), [])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"evaluate_day raised: {e}")


class Formatting(unittest.TestCase):

    def _entry(self):
        return {
            "outlet_code": "SBESI",
            "display": "Sungai Besi",
            "business_date": D,
            "day_sales": 3900.0,
            "flags": [{
                "category": "ayam", "label": "Ayam", "emoji": "🐔",
                "unit": "kg", "day_qty": 82.0, "expected_qty": 55.0,
                "baseline_days": 12,
            }],
        }

    def test_manager_message_spells_out_the_24h_day(self):
        out = format_manager_key_stock(self._entry())
        self.assertIn("📦 Key stock அதிகம் — Sungai Besi • 2026-08-06", out)
        self.assertIn("day shift + night shift, 24 மணி நேரம் சேர்த்து", out)
        self.assertIn("Sales: RM3,900", out)
        self.assertIn("🐔 Ayam: 82 kg வாங்கியிருக்கீங்க", out)
        self.assertIn("~55 kg தான்", out)
        self.assertIn("ஏன் இவ்வளவு வாங்கினீங்கன்னு சொல்லுங்க", out)

    def test_owner_summary_lists_flags(self):
        out = format_owner_summary([self._entry()])
        self.assertIn("full 24h business day", out)
        self.assertIn("Sungai Besi", out)
        self.assertIn("Ayam 82kg vs ~55kg expected", out)

    def test_empty_or_garbage_never_raises(self):
        try:
            self.assertEqual(format_manager_key_stock(None), "")
            self.assertEqual(format_manager_key_stock({}), "")
            self.assertEqual(format_owner_summary([]), "")
            self.assertEqual(format_owner_summary(["junk"]), "")
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"formatters raised: {e}")


if __name__ == "__main__":
    unittest.main()
