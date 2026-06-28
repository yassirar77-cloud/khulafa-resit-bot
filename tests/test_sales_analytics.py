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


class DailyFallbackTests(unittest.TestCase):
    """PR #63: /sales_*_today prefer today, fall back to yesterday's D-files."""

    def test_prefers_today_when_present(self):
        rows, label = sales_analytics.select_daily_dataset(
            [{"outlet_canonical": "SEK-20"}], [{"outlet_canonical": "Bistro"}],
            "2026-05-27", "yesterday (2026-05-26)")
        self.assertEqual(label, "2026-05-27")
        self.assertEqual(rows[0]["outlet_canonical"], "SEK-20")

    def test_falls_back_to_yesterday_when_today_empty(self):
        rows, label = sales_analytics.select_daily_dataset(
            [], [{"outlet_canonical": "Bistro"}],
            "2026-05-27", "yesterday (2026-05-26)")
        self.assertEqual(label, "yesterday (2026-05-26)")
        self.assertEqual(rows[0]["outlet_canonical"], "Bistro")

    def test_empty_when_both_missing_keeps_today_label(self):
        rows, label = sales_analytics.select_daily_dataset(
            [], [], "2026-05-27", "yesterday (2026-05-26)")
        self.assertEqual(rows, [])
        self.assertEqual(label, "2026-05-27")
        # The formatter then renders the "No D-file data yet" message.
        self.assertIn("No D-file data yet", sales_analytics.format_daily_summary(label, rows))


if __name__ == "__main__":
    unittest.main()


# --- /sales_ingest_latency formatting (send-side vs poll-side verdict) --------

def test_format_ingest_latency_sendside_verdict():
    import sales_analytics as sa
    # recv ≈ ingest (pulled within minutes) but both hours after close -> send-side
    rows = [{
        "shift_business_date": "2026-06-26", "shift_type": "overnight",
        "shift_close_at": "2026-06-27T07:00:00+08:00",
        "received_at": "2026-06-27T23:00:08+08:00",   # POS dated it 23:00
        "created_at": "2026-06-27T23:05:00+08:00",     # we ingested 5 min later
    }]
    text = sa.format_ingest_latency("KLANG", rows)
    assert "send-side" in text.lower()
    assert "overnight" in text
    assert "POLL-SIDE" not in text


def test_format_ingest_latency_pollside_verdict():
    import sales_analytics as sa
    # email dated 07:05 but not ingested until 23:00 -> big pull lag -> poll-side
    rows = [{
        "shift_business_date": "2026-06-26", "shift_type": "overnight",
        "shift_close_at": "2026-06-27T07:00:00+08:00",
        "received_at": "2026-06-27T07:05:00+08:00",
        "created_at": "2026-06-27T23:00:00+08:00",
    }]
    text = sa.format_ingest_latency("KLANG", rows)
    assert "POLL-SIDE" in text


def test_format_ingest_latency_empty():
    import sales_analytics as sa
    assert "No sales_daily rows" in sa.format_ingest_latency("KLANG", [])
