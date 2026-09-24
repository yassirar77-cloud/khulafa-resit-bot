"""Unit tests for ``overbuy_watch``.

Hermetic — the trend/flagging/formatting layers are fed plain dicts; the
DB reads are exercised via a tiny fake client.

Run with::

    python -m unittest tests.test_overbuy_watch
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from overbuy_watch import (  # noqa: E402
    category_totals,
    find_overbuying,
    format_manager_overbuy,
    format_owner_summary,
    outlet_trends,
    sticky_items,
    watch_windows,
)

TODAY = date(2026, 8, 6)


def recon_rows(outlet, start, days, daily_sales, daily_purchases):
    """One reconciliation row per day for ``days`` days from ``start``."""
    return [
        {
            "outlet_canonical": outlet,
            "business_date": (start + timedelta(days=i)).isoformat(),
            "sales_total": daily_sales,
            "total_food_purchases": daily_purchases,
        }
        for i in range(days)
    ]


def windows():
    w = watch_windows(TODAY)
    return (
        date.fromisoformat(w["week_start"]),
        date.fromisoformat(w["base_start"]),
    )


class WatchWindows(unittest.TestCase):

    def test_week_is_the_last_seven_full_days(self):
        w = watch_windows(TODAY)
        self.assertEqual(w["week_start"], "2026-07-30")
        self.assertEqual(w["week_end"], "2026-08-05")   # yesterday, never today
        self.assertEqual(w["base_start"], "2026-07-09")
        self.assertEqual(w["base_end"], "2026-07-29")


class TrendsAndFlagging(unittest.TestCase):

    def _rows(self, week_sales, week_purch, base_sales=2000.0, base_purch=600.0):
        week_start, base_start = windows()
        return (
            recon_rows("SEK-20", week_start, 7, week_sales, week_purch),
            recon_rows("SEK-20", base_start, 21, base_sales, base_purch),
        )

    def test_sales_down_purchases_flat_is_flagged(self):
        # Sales −20% (1600 vs 2000/day), purchases −3% — not adjusted.
        week, base = self._rows(1600.0, 580.0)
        flagged = find_overbuying(outlet_trends(week, base))
        self.assertEqual(len(flagged), 1)
        t = flagged[0]
        self.assertEqual(t["outlet"], "SEK-20")
        self.assertAlmostEqual(t["sales_drop_pct"], 20.0)
        self.assertAlmostEqual(t["purchase_drop_pct"], 10.0 / 3, places=1)

    def test_purchases_that_followed_sales_down_are_fine(self):
        # Sales −20%, purchases −15% (>= half the sales drop) — adjusted.
        week, base = self._rows(1600.0, 510.0)
        self.assertEqual(find_overbuying(outlet_trends(week, base)), [])

    def test_small_sales_dip_is_ignored(self):
        # Sales −5% is normal week-to-week noise.
        week, base = self._rows(1900.0, 600.0)
        self.assertEqual(find_overbuying(outlet_trends(week, base)), [])

    def test_buying_more_while_sales_drop_is_flagged(self):
        week, base = self._rows(1600.0, 700.0)  # purchases UP 17%
        flagged = find_overbuying(outlet_trends(week, base))
        self.assertEqual(len(flagged), 1)
        self.assertLess(flagged[0]["purchase_drop_pct"], 0)

    def test_missing_data_days_never_masquerade_as_a_crash(self):
        # Only 4 of 7 week days have POS rows -> looks like −43% but is a
        # data gap, not a sales drop. Must be skipped.
        week_start, base_start = windows()
        week = recon_rows("SEK-20", week_start, 4, 2000.0, 600.0)
        base = recon_rows("SEK-20", base_start, 21, 2000.0, 600.0)
        self.assertEqual(find_overbuying(outlet_trends(week, base)), [])

    def test_thin_baseline_is_skipped(self):
        week_start, base_start = windows()
        week = recon_rows("SEK-20", week_start, 7, 1600.0, 600.0)
        base = recon_rows("SEK-20", base_start, 10, 2000.0, 600.0)  # < 18 days
        self.assertEqual(find_overbuying(outlet_trends(week, base)), [])

    def test_worst_drop_sorts_first_and_garbage_never_raises(self):
        week_start, base_start = windows()
        week = (
            recon_rows("SEK-20", week_start, 7, 1600.0, 600.0)
            + recon_rows("VISTA", week_start, 7, 1000.0, 600.0)
        )
        base = (
            recon_rows("SEK-20", base_start, 21, 2000.0, 600.0)
            + recon_rows("VISTA", base_start, 21, 2000.0, 600.0)
        )
        flagged = find_overbuying(outlet_trends(week, base))
        self.assertEqual([t["outlet"] for t in flagged], ["VISTA", "SEK-20"])
        try:
            self.assertEqual(find_overbuying(None), [])
            self.assertEqual(outlet_trends(None, ["junk"]), [])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"raised: {e}")


class StickyItems(unittest.TestCase):

    def test_unadjusted_category_is_named_adjusted_one_is_not(self):
        # Baseline 3 weeks: ayam 645 kg (215/wk), ikan 180 kg (60/wk).
        base = {"ayam": {"kg": 645.0, "units": 0.0}, "ikan": {"kg": 180.0, "units": 0.0}}
        # Week: ayam 208 kg (−3% — sticky), ikan 40 kg (−33% — adjusted).
        week = {"ayam": {"kg": 208.0, "units": 0.0}, "ikan": {"kg": 40.0, "units": 0.0}}
        out = sticky_items(week, base, sales_drop_pct=20.0)
        self.assertEqual([i["category"] for i in out], ["ayam"])
        self.assertEqual(out[0]["unit"], "kg")
        self.assertAlmostEqual(out[0]["base_qty"], 215.0)
        self.assertAlmostEqual(out[0]["week_qty"], 208.0)

    def test_tiny_categories_are_ignored(self):
        base = {"sotong": {"kg": 6.0, "units": 0.0}}  # 2 kg/week < floor
        week = {"sotong": {"kg": 2.0, "units": 0.0}}
        self.assertEqual(sticky_items(week, base, 20.0), [])

    def test_unit_counted_lines_use_ekor(self):
        base = {"ayam": {"kg": 0.0, "units": 90.0}}  # 30 ekor / week
        week = {"ayam": {"kg": 0.0, "units": 29.0}}
        out = sticky_items(week, base, 20.0)
        self.assertEqual(out[0]["unit"], "ekor")

    def test_category_matching_bridges_outlet_codes(self):
        rows = [
            {"outlet_code": "SEK20", "canonical_item": "ayam",
             "raw_item_name": "AYAM BERSIH", "qty": 30.0, "line_total": 250},
            {"outlet_code": "VISTA", "canonical_item": "ayam",
             "raw_item_name": "AYAM BERSIH", "qty": 99.0, "line_total": 800},
        ]
        totals = category_totals(rows, "SEK-20")
        self.assertAlmostEqual(totals["ayam"]["kg"], 30.0)


class Formatting(unittest.TestCase):

    def _entry(self):
        return {
            "outlet": "SEK-20",
            "week_sales": 10200.0,
            "base_weekly_sales": 13100.0,
            "week_purchases": 4900.0,
            "base_weekly_purchases": 5100.0,
            "sales_drop_pct": 22.1,
            "purchase_drop_pct": 3.9,
            "items": [{
                "category": "ayam", "label": "Ayam", "emoji": "🐔",
                "unit": "kg", "week_qty": 208.0, "base_qty": 215.0,
            }],
        }

    def test_manager_message_tamil_content(self):
        out = format_manager_overbuy(self._entry())
        self.assertIn("📉 Sales இறங்குது, order அப்படியே — SEK-20", out)
        self.assertIn("RM10,200", out)
        self.assertIn("22% கம்மி", out)
        self.assertIn("4% தான் குறைஞ்சிருக்கு", out)
        self.assertIn("🐔 Ayam: 208 kg (வழக்கமா 215 kg)", out)
        self.assertIn("ஏன் இந்த items குறைக்கலன்னு சொல்லுங்க", out)

    def test_bought_more_reads_kuraiyave_illa(self):
        entry = self._entry()
        entry["purchase_drop_pct"] = -8.0
        entry["week_purchases"] = 5500.0
        out = format_manager_overbuy(entry)
        self.assertIn("குறையவே இல்ல", out)

    def test_no_item_detail_still_sends_the_rm_comparison(self):
        entry = self._entry()
        entry["items"] = []
        out = format_manager_overbuy(entry)
        self.assertIn("RM10,200", out)
        self.assertNotIn("இதெல்லாம்", out)

    def test_owner_summary_lists_flagged_outlets(self):
        out = format_owner_summary([self._entry()])
        self.assertIn("SEK-20", out)
        self.assertIn("−22%", out)
        self.assertIn("Ayam 208kg vs 215kg", out)
        self.assertIn("Managers asked in Tamil", out)

    def test_empty_or_garbage_never_raises(self):
        try:
            self.assertEqual(format_manager_overbuy(None), "")
            self.assertEqual(format_manager_overbuy({}), "")
            self.assertEqual(format_owner_summary([]), "")
            self.assertEqual(format_owner_summary(["junk"]), "")
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"formatters raised: {e}")


if __name__ == "__main__":
    unittest.main()
