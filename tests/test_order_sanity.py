import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_sanity as osy

TODAY = date(2026, 9, 24)


def _rows(days, items=("ayam", "ikan", "sotong", "kambing", "daging",
                       "roti", "telur", "gula", "garam")):
    out = []
    for i in range(days):
        d = (TODAY - timedelta(days=i * 2)).isoformat()
        for it in items:
            out.append({"receipt_date": d, "canonical_item": it})
    return out


LINES = [{"item": it, "qty": 5, "pack": "kg"} for it in
         ("ayam", "ikan", "sotong", "kambing", "daging", "roti")]


class HistoryTests(unittest.TestCase):
    def test_counts_days_items_and_ignores_future_and_old(self):
        rows = _rows(25) + [
            {"receipt_date": "2028-08-07", "canonical_item": "gas"},   # OCR future date
            {"receipt_date": "2026-01-01", "canonical_item": "gas"},   # > 60 days
            {"receipt_date": "garbage", "canonical_item": "gas"},
        ]
        h = osy.history_from_rows(rows, TODAY)
        self.assertEqual(h["days_60"], 25)
        self.assertEqual(h["days_28"], 15)
        self.assertEqual(h["items_60"], 9)
        self.assertNotIn("gas", h["item_days"])


class AssessTests(unittest.TestCase):
    def test_healthy_outlet_keeps_reliable_lines(self):
        v = osy.assess(osy.history_from_rows(_rows(25), TODAY), LINES)
        self.assertTrue(v["ok"])
        self.assertEqual(len(v["lines"]), 6)

    def test_klang_style_one_off_item_dropped(self):
        rows = _rows(25) + [
            {"receipt_date": "2026-09-01", "canonical_item": "knorr_like"},
            {"receipt_date": "2026-09-05", "canonical_item": "knorr_like"},
        ]
        lines = LINES + [{"item": "knorr_like", "qty": 2, "pack": "kg"}]
        v = osy.assess(osy.history_from_rows(rows, TODAY), lines)
        self.assertTrue(v["ok"])
        self.assertNotIn("knorr_like", [ln["item"] for ln in v["lines"]])

    def test_needs_review_lines_dropped(self):
        lines = [dict(LINES[0], flags="PACK_UNKNOWN,NEEDS_REVIEW")] + LINES[1:]
        v = osy.assess(osy.history_from_rows(_rows(25), TODAY), lines)
        self.assertNotIn("ayam", [ln["item"] for ln in v["lines"]])
        lines = [dict(LINES[0], needs_review=True)] + LINES[1:]
        v = osy.assess(osy.history_from_rows(_rows(25), TODAY), lines)
        self.assertNotIn("ayam", [ln["item"] for ln in v["lines"]])

    def test_thin_outlets_rejected_with_reason(self):
        # Damansara: nothing since June.
        v = osy.assess(osy.history_from_rows([], TODAY), LINES[:2])
        self.assertFalse(v["ok"])
        self.assertIn("days in 60", v["reason"])
        # Sungai Besi: 18 days, 5 items.
        v = osy.assess(osy.history_from_rows(
            _rows(18, items=("ais_batu", "ayam", "ikan", "gula", "garam")), TODAY), LINES)
        self.assertFalse(v["ok"])
        # Enough days but too few reliable lines (fewer than 3).
        v = osy.assess(osy.history_from_rows(_rows(25), TODAY), LINES[:2])
        self.assertFalse(v["ok"])
        self.assertIn("reliable draft lines", v["reason"])

    def test_three_reliable_lines_are_enough(self):
        v = osy.assess(osy.history_from_rows(_rows(25), TODAY), LINES[:3])
        self.assertTrue(v["ok"], v["reason"])
        self.assertEqual(len(v["lines"]), 3)


if __name__ == "__main__":
    unittest.main()
