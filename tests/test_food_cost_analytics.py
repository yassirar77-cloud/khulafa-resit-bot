"""Tests for food cost % analytics (PR #37)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import food_cost_analytics as fca


def _recon(outlet, sales, purchases, pct):
    return {
        "outlet_canonical": outlet,
        "sales_total": sales,
        "total_food_purchases": purchases,
        "food_cost_percent": pct,
    }


class StatusBands(unittest.TestCase):
    def test_status_green_under_30(self):
        self.assertEqual(fca.food_cost_status(26.1), "green")
        self.assertEqual(fca.food_cost_status(29.99), "green")

    def test_status_yellow_30_to_35(self):
        self.assertEqual(fca.food_cost_status(30.0), "yellow")
        self.assertEqual(fca.food_cost_status(33.9), "yellow")
        self.assertEqual(fca.food_cost_status(35.0), "yellow")

    def test_status_red_over_35(self):
        self.assertEqual(fca.food_cost_status(35.2), "red")
        self.assertEqual(fca.food_cost_status(38.1), "red")

    def test_status_incomplete_when_none(self):
        self.assertEqual(fca.food_cost_status(None), "incomplete")

    def test_status_emoji_mapping(self):
        self.assertEqual(fca.status_emoji("green"), "🟢")
        self.assertEqual(fca.status_emoji("yellow"), "🟡")
        self.assertEqual(fca.status_emoji("red"), "🔴")
        self.assertEqual(fca.status_emoji("incomplete"), "⚪")


class GroupAndPerOutlet(unittest.TestCase):
    def test_group_food_cost(self):
        rows = [
            _recon("Vista", 1000.0, 260.0, 26.0),
            _recon("Jakel", 1000.0, 380.0, 38.0),
        ]
        sales, purchases, pct = fca.group_food_cost(rows)
        self.assertEqual(sales, 2000.0)
        self.assertEqual(purchases, 640.0)
        self.assertEqual(pct, 32.0)

    def test_group_food_cost_no_sales_returns_none(self):
        rows = [_recon("SBESI", None, 500.0, None)]
        sales, purchases, pct = fca.group_food_cost(rows)
        self.assertIsNone(pct)

    def test_per_outlet_sorted_best_first_incomplete_last(self):
        rows = [
            _recon("Jakel", 1000.0, 380.0, 38.0),
            _recon("Vista", 1000.0, 260.0, 26.0),
            _recon("SBESI", None, 0.0, None),
        ]
        out = fca.per_outlet_food_cost(rows)
        self.assertEqual([o["outlet"] for o in out], ["Vista", "Jakel", "SBESI"])
        self.assertEqual(out[0]["status"], "green")
        self.assertEqual(out[1]["status"], "red")
        self.assertEqual(out[2]["status"], "incomplete")


class AnomalyDetection(unittest.TestCase):
    def test_severity_thresholds(self):
        self.assertIsNone(fca.anomaly_severity(1.0))    # below info floor
        self.assertEqual(fca.anomaly_severity(2.5), "info")
        self.assertEqual(fca.anomaly_severity(4.5), "warning")
        self.assertEqual(fca.anomaly_severity(6.5), "critical")
        self.assertEqual(fca.anomaly_severity(-6.5), "critical")  # absolute

    def test_uses_lookback_average(self):
        today = {"Jakel": 38.1, "Vista": 26.1}
        history = [
            _recon("Jakel", None, None, 32.0),
            _recon("Jakel", None, None, 33.0),
            _recon("Vista", None, None, 27.0),
            _recon("Vista", None, None, 28.0),
        ]
        anomalies = fca.compute_anomalies(today, history)
        # Jakel: 38.1 vs 32.5 avg = +5.6 (warning). Vista: 26.1 vs 27.5 = -1.4 (none).
        self.assertEqual(len(anomalies), 1)
        a = anomalies[0]
        self.assertEqual(a.outlet, "Jakel")
        self.assertEqual(a.avg_pct, 32.5)
        self.assertEqual(a.delta_pct, 5.6)
        self.assertEqual(a.severity, "warning")

    def test_no_history_skips_outlet(self):
        anomalies = fca.compute_anomalies({"New": 40.0}, [])
        self.assertEqual(anomalies, [])


class SalesSummary(unittest.TestCase):
    def test_sales_summary_totals(self):
        rows = [
            {"day_sales": 3000.0, "customers": 300, "take_away": 1200.0, "dine_in": 1800.0},
            {"day_sales": 1000.0, "customers": 100, "take_away": 400.0, "dine_in": 600.0},
        ]
        s = fca.sales_summary(rows)
        self.assertEqual(s["outlets"], 2)
        self.assertEqual(s["revenue"], 4000.0)
        self.assertEqual(s["customers"], 400)
        self.assertEqual(s["avg_per_customer"], 10.0)
        self.assertEqual(s["takeaway_pct"], 40.0)
        self.assertEqual(s["dine_in_pct"], 60.0)

    def test_sales_summary_zero_customers(self):
        s = fca.sales_summary([{"day_sales": 0.0, "customers": 0}])
        self.assertIsNone(s["avg_per_customer"])


class Formatters(unittest.TestCase):
    def test_food_cost_today_renders_group_and_outlets(self):
        rows = [
            _recon("Vista", 1000.0, 260.0, 26.0),
            _recon("Jakel", 1000.0, 380.0, 38.0),
        ]
        out = fca.format_food_cost_today("2026-05-29", rows)
        self.assertIn("Khulafa Group", out)
        self.assertIn("Vista", out)
        self.assertIn("Jakel", out)
        self.assertIn("🟢", out)
        self.assertIn("🔴", out)

    def test_food_cost_today_empty(self):
        out = fca.format_food_cost_today("2026-05-29", [])
        self.assertIn("/reconcile_now", out)

    def test_cash_no_receipt_empty_is_positive(self):
        out = fca.format_cash_no_receipt("2026-05-29", [])
        self.assertIn("✅", out)

    def test_cash_no_receipt_lists_alerts_with_total(self):
        alerts = [
            {"outlet": "Klang B.Emas", "amount": 200.0, "description": "PAY TO BABAS",
             "paid_at": "13:42"},
            {"outlet": "D.U", "amount": 82.0, "description": "PAY TO NASI LEMAK"},
        ]
        out = fca.format_cash_no_receipt("2026-05-29", alerts)
        self.assertIn("RM282.00", out)
        self.assertIn("BABAS", out)
        self.assertIn("13:42", out)

    def test_outlet_trend_includes_average(self):
        rows = [
            _recon("Jakel", 3950.0, 1420.0, 35.9),
            _recon("Jakel", 4200.0, 1510.0, 35.9),
        ]
        for i, r in enumerate(rows):
            r["business_date"] = f"2026-05-2{3 + i}"
        out = fca.format_outlet_trend("Jakel", rows, group_pct=28.3)
        self.assertIn("7-day average", out)
        self.assertIn("Group average", out)


if __name__ == "__main__":
    unittest.main()
