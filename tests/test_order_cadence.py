"""Cadence detection + due logic (order_cadence)."""
import unittest
from datetime import date, timedelta

import order_cadence as oc


def _series(start: date, step: int, n: int) -> list[date]:
    return [start + timedelta(days=step * i) for i in range(n)]


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 7)

    def test_daily(self):
        dates = _series(self.today - timedelta(days=20), 1, 21)
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.DAILY)
        self.assertAlmostEqual(info["median_gap_days"], 1.0)
        self.assertFalse(info["needs_review"])

    def test_twice_weekly(self):
        # Every ~3-4 days (Mon/Thu rhythm).
        start = date(2026, 4, 6)  # a Monday
        dates = []
        d = start
        for _ in range(12):
            dates.append(d)
            dates.append(d + timedelta(days=3))
            d += timedelta(days=7)
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.TWICE_WEEKLY)

    def test_weekly_with_dow(self):
        # Every Monday for 10 weeks.
        start = date(2026, 4, 6)
        dates = _series(start, 7, 10)
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.WEEKLY)
        self.assertEqual(info["dow_pattern"], ["Mon"])

    def test_monthly(self):
        # ~28-day cadence yields 4 buys inside a 90-day window (3 gaps).
        dates = [self.today - timedelta(days=d) for d in (84, 56, 28, 0)]
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.MONTHLY)
        self.assertFalse(info["needs_review"])

    def test_erratic_flagged_not_dropped(self):
        dates = [self.today - timedelta(days=d) for d in (0, 1, 17, 18, 55, 80)]
        info = oc.detect_cadence(dates, today=self.today)
        # Whatever the band, the noisy gaps must trip the review flag.
        self.assertTrue(info["needs_review"])


class StalenessTests(unittest.TestCase):
    """A tidy historical rhythm must NOT assert a cadence once it goes silent."""

    def setUp(self):
        self.today = date(2026, 6, 7)

    def test_daily_gone_silent_is_downgraded(self):
        # 15 daily buys, then nothing for 20 days — rhythm broken.
        dates = _series(self.today - timedelta(days=34), 1, 15)  # last = today-20
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.NEEDS_REVIEW)
        self.assertTrue(info["needs_review"])
        self.assertIn("rhythm broken", info["reason"])

    def test_recent_daily_stays_daily(self):
        dates = _series(self.today - timedelta(days=14), 1, 15)  # last = today
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.DAILY)
        self.assertFalse(info["needs_review"])

    def test_weekly_silent_two_months_downgraded(self):
        # Weekly for 8 weeks, but last buy was ~8 weeks ago (> 3 × 7 days).
        dates = _series(self.today - timedelta(days=112), 7, 8)  # last = today-63
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.NEEDS_REVIEW)
        self.assertTrue(info["needs_review"])

    def test_single_purchase_needs_review(self):
        info = oc.detect_cadence([self.today - timedelta(days=3)], today=self.today)
        self.assertEqual(info["cadence"], oc.NEEDS_REVIEW)
        self.assertTrue(info["needs_review"])
        self.assertTrue(info["verify_only"])
        self.assertIsNone(info["median_gap_days"])  # no invented cycle
        self.assertEqual(info["sample_count"], 1)

    def test_two_purchases_do_not_invent_a_cycle(self):
        # Two buys 2 days apart must NOT become a "2-day cadence": no median gap,
        # quiet verify_only, no "rhythm broken".
        info = oc.detect_cadence([self.today - timedelta(days=2), self.today],
                                 today=self.today)
        self.assertEqual(info["cadence"], oc.NEEDS_REVIEW)
        self.assertTrue(info["verify_only"])
        self.assertIsNone(info["median_gap_days"])
        self.assertNotIn("rhythm broken", info["reason"])
        self.assertIn("verify", info["reason"])

    def test_three_purchases_can_classify(self):
        # Three buys (2 gaps) clears the gate and is allowed to classify.
        info = oc.detect_cadence(_series(self.today - timedelta(days=2), 1, 3),
                                 today=self.today)
        self.assertFalse(info["verify_only"])
        self.assertEqual(info["cadence"], oc.DAILY)

    def test_no_purchases(self):
        info = oc.detect_cadence([], today=self.today)
        self.assertEqual(info["cadence"], oc.NEEDS_REVIEW)
        self.assertIsNone(info["last_purchase_date"])
        self.assertEqual(info["confidence"], 0)

    def test_lookback_window_excludes_old(self):
        old = _series(date(2025, 1, 1), 1, 10)  # well outside 90 days
        info = oc.detect_cadence(old, today=self.today, lookback_days=90)
        self.assertEqual(info["sample_count"], 0)

    def test_iso_strings_accepted(self):
        dates = [(self.today - timedelta(days=i)).isoformat() for i in range(10)]
        info = oc.detect_cadence(dates, today=self.today)
        self.assertEqual(info["cadence"], oc.DAILY)

    def test_confidence_rises_with_clean_history(self):
        clean = oc.detect_cadence(_series(self.today - timedelta(days=30), 1, 31),
                                  today=self.today)
        sparse = oc.detect_cadence(_series(self.today - timedelta(days=21), 7, 4),
                                   today=self.today)
        self.assertGreater(clean["confidence"], sparse["confidence"])


class DueTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 7)
        self.tomorrow = self.today + timedelta(days=1)

    def test_daily_always_due(self):
        info = {"cadence": oc.DAILY}
        self.assertTrue(oc.is_due(info, today=self.today)["due"])

    def test_weekly_due_on_predicted_day(self):
        info = {"cadence": oc.WEEKLY,
                "last_purchase_date": self.tomorrow - timedelta(days=7),
                "median_gap_days": 7.0, "needs_review": False}
        self.assertTrue(oc.is_due(info, today=self.today)["due"])

    def test_weekly_not_due_midcycle(self):
        info = {"cadence": oc.WEEKLY,
                "last_purchase_date": self.today - timedelta(days=1),
                "median_gap_days": 7.0, "needs_review": False}
        self.assertFalse(oc.is_due(info, today=self.today)["due"])

    def test_overdue_is_due(self):
        info = {"cadence": oc.MONTHLY,
                "last_purchase_date": self.today - timedelta(days=60),
                "median_gap_days": 30.0, "needs_review": False}
        res = oc.is_due(info, today=self.today)
        self.assertTrue(res["due"])
        self.assertIn("overdue", res["reason"])

    def test_dow_pattern_makes_due(self):
        # tomorrow is a buying day even if a day early.
        info = {"cadence": oc.WEEKLY,
                "last_purchase_date": self.tomorrow - timedelta(days=6),
                "median_gap_days": 7.0, "needs_review": False,
                "dow_pattern": [oc._WEEKDAY_NAMES[self.tomorrow.weekday()]]}
        self.assertTrue(oc.is_due(info, today=self.today)["due"])


if __name__ == "__main__":
    unittest.main()
