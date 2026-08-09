"""Unit tests for ``item_sales_watch``.

Hermetic — fold/evaluation/formatting are fed plain dicts; the end-to-end
gather runs against the in-memory FakeSupabase. The 24h business-day
correctness (two shift emails folding onto one day) is the heart of the
feature, so the fold test models the REAL uploaded BISTRO7 pair: the 19:00
day-shift close and the next-morning 07:00 overnight close both carrying the
SAME business date, whose NAAN counts (36 + 91) must sum to one day's 127.

Run with::

    python -m unittest tests.test_item_sales_watch
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from item_sales_watch import (  # noqa: E402
    _STREAK_DAYS,
    evaluate_day,
    fold_item_days,
    format_manager_slow_items,
    format_owner_summary,
    gather_slow_item_flags,
    target_business_day,
)

D = "2026-08-08"


class TargetDay(unittest.TestCase):

    def test_judges_yesterday_never_today(self):
        self.assertEqual(target_business_day(date(2026, 8, 9)), date(2026, 8, 8))


class Fold(unittest.TestCase):

    def test_both_shift_emails_sum_to_one_business_day(self):
        # The uploaded BISTRO7 pair: shift 1492 (19:00 close) sold NAAN 36,
        # shift 1493 (07:00-next-morning close) sold NAAN 91 — one 24h day.
        shift_rows = [
            {"id": 1, "outlet_code": "S-BISTRO7", "outlet_canonical": "Bistro",
             "shift_business_date": D, "shift_type": "day"},
            {"id": 2, "outlet_code": "S-BISTRO7", "outlet_canonical": "Bistro",
             "shift_business_date": D, "shift_type": "overnight"},
        ]
        item_rows = [
            {"sales_daily_id": 1, "item_name": "NAAN", "qty": 36},
            {"sales_daily_id": 2, "item_name": "NAAN", "qty": 91},
            {"sales_daily_id": 1, "item_name": "AYAM", "qty": 233},
            {"sales_daily_id": 2, "item_name": "AYAM", "qty": 167},
        ]
        out = fold_item_days(shift_rows, item_rows, "BISTRO7")
        self.assertAlmostEqual(out[D]["NAAN"]["qty"], 127.0)
        self.assertAlmostEqual(out[D]["AYAM"]["qty"], 400.0)

    def test_other_outlets_do_not_leak_in(self):
        shift_rows = [
            {"id": 1, "outlet_code": "S-BISTRO7", "shift_business_date": D,
             "shift_type": "day"},
            {"id": 2, "outlet_code": "S-SEK20", "shift_business_date": D,
             "shift_type": "day"},
        ]
        item_rows = [
            {"sales_daily_id": 1, "item_name": "NAAN", "qty": 10},
            {"sales_daily_id": 2, "item_name": "NAAN", "qty": 99},
        ]
        out = fold_item_days(shift_rows, item_rows, "BISTRO7")
        self.assertAlmostEqual(out[D]["NAAN"]["qty"], 10.0)


def _day_map(baseline_qty_by_group, today_qty_by_group, *, days=10, target=D):
    """A fold-shaped map: ``days`` baseline days with fixed quantities, then
    the target day."""
    t = date.fromisoformat(target)
    out = {}
    for i in range(days, 0, -1):
        d = (t - timedelta(days=i)).isoformat()
        out[d] = {
            g: {"label": g, "qty": float(q)}
            for g, q in baseline_qty_by_group.items()
        }
    out[target] = {
        g: {"label": g, "qty": float(q)} for g, q in today_qty_by_group.items()
    }
    return out


class Evaluate(unittest.TestCase):

    def test_flags_item_selling_well_under_usual(self):
        day_map = _day_map({"NAAN": 120, "TOSAI": 40}, {"NAAN": 50, "TOSAI": 38})
        flags = evaluate_day(D, day_map)
        self.assertEqual([f["group"] for f in flags], ["NAAN"])
        f = flags[0]
        self.assertEqual(f["qty"], 50.0)
        self.assertEqual(f["median"], 120.0)
        self.assertEqual(f["drop_pct"], 58)
        self.assertEqual(f["streak"], 1)

    def test_item_missing_today_counts_as_zero(self):
        day_map = _day_map({"NAAN": 30}, {})
        flags = evaluate_day(D, day_map)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["qty"], 0.0)

    def test_small_items_and_small_dips_never_flag(self):
        day_map = _day_map(
            {"WESTERN": 5, "TEH": 300},      # WESTERN usual < min median
            {"WESTERN": 0, "TEH": 290},      # TEH dip inside tolerance
        )
        self.assertEqual(evaluate_day(D, day_map), [])

    def test_streak_counts_consecutive_slow_days(self):
        day_map = _day_map({"NAAN": 120}, {"NAAN": 50})
        t = date.fromisoformat(D)
        for i in (1, 2):  # yesterday and the day before were slow too
            day_map[(t - timedelta(days=i)).isoformat()]["NAAN"]["qty"] = 55.0
        flags = evaluate_day(D, day_map)
        self.assertEqual(flags[0]["streak"], 3)

    def test_needs_enough_baseline_days(self):
        day_map = _day_map({"NAAN": 120}, {"NAAN": 10}, days=3)
        self.assertEqual(evaluate_day(D, day_map), [])


class Messages(unittest.TestCase):

    def _entry(self, streak=1):
        return {
            "outlet_code": "BISTRO7", "display": "Bistro", "business_date": D,
            "flags": [{
                "group": "NAAN", "label": "NAAN", "qty": 50.0, "median": 120.0,
                "drop_pct": 58, "streak": streak,
            }],
        }

    def test_manager_message_names_item_and_push_suggestion(self):
        text = format_manager_slow_items(self._entry())
        self.assertIn("Bistro", text)
        self.assertIn("NAAN", text)
        self.assertIn("50", text)
        self.assertIn("120", text)
        self.assertIn("push", text)
        # 1-day dip: no quality escalation yet.
        self.assertNotIn("taste", text)

    def test_streak_adds_taste_quality_question(self):
        text = format_manager_slow_items(self._entry(streak=_STREAK_DAYS))
        self.assertIn("taste", text)
        self.assertIn(str(_STREAK_DAYS), text)

    def test_owner_summary_marks_slumps(self):
        summary = format_owner_summary([self._entry(streak=3)])
        self.assertIn("Bistro", summary)
        self.assertIn("3-day slump", summary)
        self.assertEqual(format_owner_summary([]), "")


class Gather(unittest.TestCase):

    def _seed(self, fake, *, overnight_on_target=True):
        t = date.fromisoformat(D)
        fake._store["outlet_canonical"] = [
            {"code": "S-BISTRO7", "canonical_name": "Bistro", "active": True},
        ]
        daily, items = [], []
        next_id = 1
        for i in range(8, 0, -1):  # 8 baseline days, one shift row each
            d = (t - timedelta(days=i)).isoformat()
            daily.append({"id": next_id, "outlet_code": "S-BISTRO7",
                          "outlet_canonical": "Bistro", "shift_no": str(1400 + i),
                          "shift_business_date": d, "shift_type": "day"})
            items.append({"sales_daily_id": next_id, "item_name": "NAAN", "qty": 120})
            next_id += 1
        # Target day: both shifts (the 24h fold), NAAN way under usual.
        daily.append({"id": next_id, "outlet_code": "S-BISTRO7",
                      "outlet_canonical": "Bistro", "shift_no": "1492",
                      "shift_business_date": D, "shift_type": "day"})
        items.append({"sales_daily_id": next_id, "item_name": "NAAN", "qty": 20})
        next_id += 1
        if overnight_on_target:
            daily.append({"id": next_id, "outlet_code": "S-BISTRO7",
                          "outlet_canonical": "Bistro", "shift_no": "1493",
                          "shift_business_date": D, "shift_type": "overnight"})
            items.append({"sales_daily_id": next_id, "item_name": "NAAN", "qty": 30})
        fake._store["sales_daily"] = daily
        fake._store["sales_items"] = items

    def test_flags_when_day_complete(self):
        from tests.fake_supabase import FakeSupabase

        fake = FakeSupabase()
        self._seed(fake)
        bundle = gather_slow_item_flags(fake, today=date(2026, 8, 9))
        self.assertEqual(bundle["business_date"], D)
        self.assertEqual(len(bundle["entries"]), 1)
        entry = bundle["entries"][0]
        self.assertEqual(entry["display"], "Bistro")
        self.assertEqual(entry["flags"][0]["group"], "NAAN")
        self.assertEqual(entry["flags"][0]["qty"], 50.0)  # 20 + 30, both shifts

    def test_incomplete_day_is_skipped_not_judged(self):
        from tests.fake_supabase import FakeSupabase

        fake = FakeSupabase()
        self._seed(fake, overnight_on_target=False)  # 7AM email not in yet
        bundle = gather_slow_item_flags(fake, today=date(2026, 8, 9))
        self.assertEqual(bundle["entries"], [])
        self.assertEqual(bundle["skipped_incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
