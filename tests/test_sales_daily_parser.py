"""D-file (daily summary) parser tests for PR #60 against the 7 real fixtures.

Run with::

    python -m unittest tests.test_sales_daily_parser
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime  # noqa: E402

from sales_daily_parser import business_date_for_printed, parse_daily_summary  # noqa: E402
from sales_parser import read_shift_close_file  # noqa: E402
from tests.sales_daily_fixtures import EXPECTED, path_for_code  # noqa: E402


def _parsed(code):
    return parse_daily_summary(read_shift_close_file(path_for_code(code)))


class BusinessDateTests(unittest.TestCase):
    """PR #61: a D-file's business day is the print date only for an evening
    (>=17:00) close; post-midnight overnight prints belong to the previous day."""

    def test_business_date_morning_returns_previous_day(self):
        # Header date 26 May, printed 00:09 / 07:00 -> business day is 25 May.
        self.assertEqual(business_date_for_printed(datetime(2026, 5, 26, 0, 9, 19)),
                         datetime(2026, 5, 25).date())
        self.assertEqual(business_date_for_printed(datetime(2026, 5, 26, 7, 0, 2)),
                         datetime(2026, 5, 25).date())
        self.assertEqual(business_date_for_printed(datetime(2026, 5, 26, 16, 59)),
                         datetime(2026, 5, 25).date())
        # Real D-SEK20 (printed 00:09) -> 2026-05-25.
        self.assertEqual(str(_parsed("D-SEK20")["header"]["business_date"]), "2026-05-25")

    def test_business_date_evening_returns_same_day(self):
        # 17:00 cutoff inclusive; 19:00 close prints same-day.
        self.assertEqual(business_date_for_printed(datetime(2026, 5, 26, 17, 0)),
                         datetime(2026, 5, 26).date())
        self.assertEqual(business_date_for_printed(datetime(2026, 5, 26, 19, 0, 4)),
                         datetime(2026, 5, 26).date())
        # Real D-Damansara (printed 19:00) -> 2026-05-26.
        self.assertEqual(str(_parsed("D-DAMANSARA")["header"]["business_date"]), "2026-05-26")


class DailyParserTests(unittest.TestCase):
    def test_parses_all_7_without_error(self):
        for e in EXPECTED:
            p = _parsed(e["code"])
            self.assertAlmostEqual(p["daily_aggregate"]["day_sales"], e["sales"], places=2, msg=e["code"])
            self.assertEqual(p["daily_aggregate"]["customers"], e["customers"], e["code"])
            self.assertEqual(len(p["shifts"]), e["shifts"], e["code"])

    def test_parses_d_sek20_total_sales_8246_10(self):
        self.assertAlmostEqual(_parsed("D-SEK20")["daily_aggregate"]["day_sales"], 8246.10, places=2)

    def test_parses_d_sek6_handles_3_shifts(self):
        p = _parsed("D-SEK6")
        self.assertEqual(p["header"]["total_shifts"], 3)
        self.assertEqual(len(p["shifts"]), 3)
        self.assertEqual([s["shift_index"] for s in p["shifts"]], [1, 2, 3])

    def test_parses_d_sek14_customers_550_avg_15_97(self):
        d = _parsed("D-SEK14")["daily_aggregate"]
        self.assertEqual(d["customers"], 550)
        self.assertAlmostEqual(d["average_spent"], 15.97, places=2)

    def test_parses_d_damansara_low_avg_7_72(self):
        d = _parsed("D-DAMANSARA")["daily_aggregate"]
        self.assertAlmostEqual(d["average_spent"], 7.72, places=2)

    def test_parses_takeaway_dine_in_split(self):
        d = _parsed("D-SEK20")["daily_aggregate"]
        self.assertAlmostEqual(d["take_away"], 1510.00, places=2)
        self.assertAlmostEqual(d["dine_in"], 6736.10, places=2)

    def test_parses_payout_with_vendor_name(self):
        payouts = _parsed("D-SEK20")["payouts"]
        kachang = next((p for p in payouts if p["vendor_name"] == "KACHANG"), None)
        self.assertIsNotNone(kachang)
        self.assertEqual(kachang["description"], "PAY TO KACHANG")
        self.assertAlmostEqual(kachang["amount"], 264.00, places=2)

    def test_parses_deleted_audit_with_staff_time_reason(self):
        deleted = _parsed("D-SEK20")["deleted_items"]
        self.assertTrue(deleted)
        first = deleted[0]
        self.assertEqual(first["item_name"], "Roti Canai")
        self.assertEqual(first["staff"], "S SATHISH")
        self.assertEqual(first["time"], "08:11:17")
        self.assertEqual(first["reason"], "WRONG ORDER BY WAITER")
        # A KITCHEN deletion with no reason parses with reason None.
        kitchen = next((d for d in deleted if d["staff"] == "KITCHEN"), None)
        self.assertIsNotNone(kitchen)
        self.assertIsNone(kitchen["reason"])

    def test_parses_top_30_food_items_with_quantities(self):
        food = _parsed("D-SEK20")["top_30_food"]
        self.assertEqual(len(food), 30)
        self.assertEqual(food[0]["name"], "Roti Canai")
        self.assertEqual(food[0]["qty"], 174)
        self.assertAlmostEqual(food[0]["amount"], 261.93, places=2)

    def test_parses_itemwise_categories(self):
        itemwise = _parsed("D-SEK20")["itemwise_sales"]
        self.assertIn("MAKANAN", itemwise)
        self.assertIn("MAMAK GORENG", itemwise)
        self.assertIn("MINUM(T)", itemwise)
        # Categories hold item rows, never bleed numbers into the category name.
        self.assertTrue(all(not any(c.isdigit() for c in cat) for cat in itemwise))
        self.assertTrue(itemwise["MAKANAN"])


if __name__ == "__main__":
    unittest.main()
