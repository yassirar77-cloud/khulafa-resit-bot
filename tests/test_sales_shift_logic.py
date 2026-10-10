"""Shift-classification tests for PR #35 (24/7 = 2 shifts/day).

Run with::

    python -m unittest tests.test_sales_shift_logic
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_parser import determine_shift_type_and_business_date  # noqa: E402


class ShiftLogicTests(unittest.TestCase):
    def test_7pm_shift_close_classified_as_day_shift(self):
        stype, bdate = determine_shift_type_and_business_date(datetime(2026, 5, 26, 19, 0, 0))
        self.assertEqual(stype, "day")
        self.assertEqual(bdate, datetime(2026, 5, 26).date())

    def test_7am_shift_close_classified_as_overnight_shift(self):
        stype, _ = determine_shift_type_and_business_date(datetime(2026, 5, 27, 7, 0, 0))
        self.assertEqual(stype, "overnight")

    def test_overnight_stamp_carries_the_business_date(self):
        # Live: "S-SEK14 SHIFTCLOSE (2097) ON 09/Oct/2026 07:00:03" arrived at
        # 07:00 on 10 Oct — the POS stamps the overnight close with the day the
        # shift opened, so it is NOT shifted back again.
        _, bdate = determine_shift_type_and_business_date(
            datetime(2026, 10, 9, 7, 0, 3), "2026-10-09T23:00:06+00:00")
        self.assertEqual(bdate, datetime(2026, 10, 9).date())
        # No arrival time -> the business-date convention is assumed.
        _, bdate = determine_shift_type_and_business_date(datetime(2026, 5, 27, 7, 1, 45))
        self.assertEqual(bdate, datetime(2026, 5, 27).date())

    def test_overnight_stamp_with_real_time_is_previous_day(self):
        # SEK15 stamps its 07:00 close with the real date (arrives seconds later).
        _, bdate = determine_shift_type_and_business_date(
            datetime(2026, 10, 10, 7, 0, 2), "2026-10-10T07:00:07+08:00")
        self.assertEqual(bdate, datetime(2026, 10, 9).date())

    def test_unusual_close_time_returns_unknown_type(self):
        # 14:00 is neither the ~19:00 nor the ~07:00 window.
        stype, bdate = determine_shift_type_and_business_date(datetime(2026, 5, 26, 14, 0, 0))
        self.assertEqual(stype, "unknown")
        self.assertEqual(bdate, datetime(2026, 5, 26).date())

    def test_just_after_midnight_close_is_an_overnight(self):
        # Live SEK15: "(3744) ON 09/Oct/2026 00:02:11" arrived 10 Oct 00:02 —
        # business day 9 Oct, same as its D-file.
        stype, bdate = determine_shift_type_and_business_date(
            datetime(2026, 10, 9, 0, 2, 11), "2026-10-09T16:02:47+00:00")
        self.assertEqual(stype, "overnight")
        self.assertEqual(bdate, datetime(2026, 10, 9).date())

    def test_late_evening_close_is_same_days_day_shift(self):
        stype, bdate = determine_shift_type_and_business_date(datetime(2026, 5, 26, 23, 30, 0))
        self.assertEqual(stype, "day")
        self.assertEqual(bdate, datetime(2026, 5, 26).date())


if __name__ == "__main__":
    unittest.main()
