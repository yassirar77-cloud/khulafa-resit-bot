"""Analytics tests for PR #35 (pure aggregation / food cost).

Run with::

    python -m unittest tests.test_sales_analytics
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sales_analytics  # noqa: E402


class SalesAnalyticsTests(unittest.TestCase):
    def test_sales_today_aggregates_across_outlets(self):
        rows = [
            {"outlet_canonical": "Klang B.Emas", "total_sales": 4758.20, "shift_type": "overnight"},
            {"outlet_canonical": "Klang B.Emas", "total_sales": 1000.00, "shift_type": "day"},
            {"outlet_canonical": "Bistro", "total_sales": 6563.75, "shift_type": "day"},
        ]
        by_outlet = sales_analytics.aggregate_sales_by_outlet(rows)
        self.assertAlmostEqual(by_outlet["Klang B.Emas"], 5758.20, places=2)
        self.assertAlmostEqual(by_outlet["Bistro"], 6563.75, places=2)
        self.assertAlmostEqual(sales_analytics.total_sales(rows), 12321.95, places=2)

    def test_food_cost_calculation_purchases_over_sales(self):
        # RM2,000 purchases against RM8,000 sales -> 25%.
        self.assertAlmostEqual(sales_analytics.food_cost_pct(2000.0, 8000.0), 25.0, places=4)

    def test_food_cost_handles_zero_sales_division(self):
        self.assertIsNone(sales_analytics.food_cost_pct(500.0, 0.0))
        self.assertIsNone(sales_analytics.food_cost_pct(500.0, None))


if __name__ == "__main__":
    unittest.main()
