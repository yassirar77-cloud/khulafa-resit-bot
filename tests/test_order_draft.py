"""Quantity fork + draft formatting (order_draft)."""
import unittest
from datetime import date, timedelta

import order_cadence as oc
import order_draft


def _recs(start: date, step: int, qtys: list[float]) -> list[dict]:
    return [{"date": start + timedelta(days=step * i), "qty": q}
            for i, q in enumerate(qtys)]


class WeekendMultiplierTests(unittest.TestCase):
    def test_derives_multiplier_from_data(self):
        per_buy = []
        d = date(2026, 4, 6)  # Monday
        for _ in range(4):
            per_buy.append((d, 10.0))             # Mon weekday
            per_buy.append((d + timedelta(days=5), 20.0))  # Sat weekend
            d += timedelta(days=7)
        mult = order_draft.weekend_multiplier(per_buy)
        self.assertAlmostEqual(mult, 2.0, places=2)

    def test_clamped_and_safe_without_data(self):
        self.assertEqual(order_draft.weekend_multiplier([]), 1.0)
        # weekday only -> no weekend evidence -> 1.0
        weekday_only = [(date(2026, 4, 6), 10.0)]
        self.assertEqual(order_draft.weekend_multiplier(weekday_only), 1.0)


class ForecastQtyTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 7)

    def test_daily_uses_trailing_average(self):
        recs = _recs(self.today - timedelta(days=9), 1, [10] * 10)
        ci = {"cadence": oc.DAILY, "canonical_item": "ayam"}
        # target a weekday (Wednesday) so no weekend bump.
        fc = order_draft.forecast_qty(recs, ci, target_day=date(2026, 6, 10))
        self.assertEqual(fc["qty"], 10)
        self.assertFalse(fc["pack_known"])  # no known pack -> flag for manager

    def test_weekly_qty_covers_cycle(self):
        recs = _recs(self.today - timedelta(days=28), 7, [100, 100, 100, 100, 100])
        ci = {"cadence": oc.WEEKLY, "canonical_item": "beras"}
        fc = order_draft.forecast_qty(recs, ci, target_day=self.today + timedelta(days=1))
        self.assertEqual(fc["qty"], 100)  # whole-cycle qty, not 1/7th

    def test_rounds_up(self):
        recs = _recs(self.today - timedelta(days=5), 1, [3.2, 3.4, 3.1])
        ci = {"cadence": oc.DAILY, "canonical_item": "udang"}
        fc = order_draft.forecast_qty(recs, ci, target_day=date(2026, 6, 10))
        self.assertEqual(fc["qty"], 4)

    def test_no_history_returns_none(self):
        ci = {"cadence": oc.DAILY, "canonical_item": "ayam"}
        fc = order_draft.forecast_qty([], ci, target_day=self.today)
        self.assertIsNone(fc["qty"])


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 7)
        self.tomorrow = self.today + timedelta(days=1)

    def _line(self, **over):
        base = {
            "canonical_item": "ayam",
            "qty": 12, "pack": "kg", "pack_known": False,
            "cadence_info": {"cadence": oc.DAILY, "needs_review": False,
                             "last_purchase_date": self.today, "median_gap_days": 1.0,
                             "reason": "daily"},
            "due_info": {"due": True, "reason": "daily item"},
            "supplier": "BESTARI FARM",
            "alternate": None, "spike": None,
        }
        base.update(over)
        return base

    def test_line_shows_reasoning(self):
        txt = order_draft.format_item_line(self._line())
        self.assertIn("Ayam", txt)
        self.assertIn("12 kg", txt)
        self.assertIn("cadence: daily", txt)
        self.assertIn("last bought", txt)
        self.assertIn("BESTARI FARM", txt)

    def test_flags_render(self):
        line = self._line(
            pack_known=False,
            cadence_info={"cadence": oc.NEEDS_REVIEW, "needs_review": True,
                          "last_purchase_date": self.today, "median_gap_days": None,
                          "reason": "irregular gaps"},
            alternate={"alternate": "Shree Map Jaya", "note": "lagi murah"},
            spike="harga naik",
        )
        txt = order_draft.format_item_line(line)
        self.assertIn("❓", txt)
        self.assertIn("NEEDS REVIEW", txt)
        self.assertIn("💰", txt)
        self.assertIn("⚠️", txt)

    def test_outlet_message_groups_by_supplier(self):
        lines = [self._line(supplier="BESTARI FARM"),
                 self._line(canonical_item="udang", supplier="FOOK LEONG")]
        msg = order_draft.build_outlet_message("SEK-20", self.tomorrow, lines)
        self.assertIn("SEK-20", msg)
        self.assertIn("BESTARI FARM", msg)
        self.assertIn("FOOK LEONG", msg)

    def test_empty_message(self):
        msg = order_draft.build_outlet_message("SEK-20", self.tomorrow, [])
        self.assertIn("Tiada item due", msg)


if __name__ == "__main__":
    unittest.main()
