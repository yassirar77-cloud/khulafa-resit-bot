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

    def test_overnight_shift_business_date_is_previous_day(self):
        # A shift that closes at 07:01 on the 27th belongs to the 26th's business.
        _, bdate = determine_shift_type_and_business_date(datetime(2026, 5, 27, 7, 1, 45))
        self.assertEqual(bdate, datetime(2026, 5, 26).date())

    def test_unusual_close_time_returns_unknown_type(self):
        # 14:00 is neither the ~19:00 nor the ~07:00 window.
        stype, bdate = determine_shift_type_and_business_date(datetime(2026, 5, 26, 14, 0, 0))
        self.assertEqual(stype, "unknown")
        self.assertEqual(bdate, datetime(2026, 5, 26).date())


if __name__ == "__main__":
    unittest.main()
