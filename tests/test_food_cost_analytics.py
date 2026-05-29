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

    def test_compares_rolling_vs_prior_rolling(self):
        # Both inputs are 7-day rolling (sales-weighted) figures, not single days.
        current = {"Jakel": 38.1, "Vista": 26.1}
        prior = {"Jakel": 32.5, "Vista": 27.5}
        anomalies = fca.compute_anomalies(current, prior)
        # Jakel: 38.1 vs 32.5 = +5.6 (warning). Vista: 26.1 vs 27.5 = -1.4 (none).
        self.assertEqual(len(anomalies), 1)
        a = anomalies[0]
        self.assertEqual(a.outlet, "Jakel")
        self.assertEqual(a.current_pct, 38.1)
        self.assertEqual(a.baseline_pct, 32.5)
        self.assertEqual(a.delta_pct, 5.6)
        self.assertEqual(a.severity, "warning")

    def test_no_prior_window_skips_outlet(self):
        self.assertEqual(fca.compute_anomalies({"New": 40.0}, {}), [])


class Rolling(unittest.TestCase):
    def test_rolling_is_sales_weighted_not_mean_of_daily(self):
        # Burst day RM6,000 in / RM2,000 sales (300%!) then RM200 / RM3,000 — the
        # natural mamak delivery pattern. The rolling % must weight by sales.
        rows = [
            _recon("Vista", 2000.0, 6000.0, 300.0),
            _recon("Vista", 3000.0, 200.0, 6.7),
        ]
        v = fca.rolling_food_cost_by_outlet(rows)["Vista"]
        self.assertEqual(v["sales"], 5000.0)
        self.assertEqual(v["purchases"], 6200.0)
        self.assertEqual(v["pct"], 124.0)   # 6200/5000, NOT the 153.4 mean of dailies
        self.assertEqual(v["days"], 2)

    def test_rolling_none_pct_when_no_sales(self):
        v = fca.rolling_food_cost_by_outlet([_recon("SBESI", None, 500.0, None)])["SBESI"]
        self.assertIsNone(v["pct"])
        self.assertEqual(v["purchases"], 500.0)


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
    def test_food_cost_today_shows_raw_figures_not_percent(self):
        # Repurposed: raw sales + purchases, NO daily food cost %, with a note
        # pointing to the weekly/monthly reports.
        rows = [
            _recon("Vista", 1000.0, 260.0, 26.0),
            _recon("Jakel", 1000.0, 380.0, 38.0),
        ]
        out = fca.format_food_cost_today("2026-05-29", rows)
        self.assertIn("Khulafa Group", out)
        self.assertIn("RM1,000.00 sales", out)
        self.assertIn("RM260.00 purch", out)
        self.assertIn("reported weekly", out)
        # No daily food cost % value, and no status emoji band.
        self.assertNotIn("26.0%", out)
        self.assertNotIn("38.0%", out)
        self.assertNotIn("🟢", out)
        self.assertNotIn("🔴", out)

    def test_food_cost_today_empty(self):
        out = fca.format_food_cost_today("2026-05-29", [])
        self.assertIn("/reconcile_now", out)

    def test_food_cost_month_renders_rolling_per_outlet(self):
        rows = [
            _recon("Vista", 2000.0, 6000.0, 300.0),
            _recon("Vista", 3000.0, 200.0, 6.7),
            _recon("Jakel", 4000.0, 1400.0, 35.0),
        ]
        out = fca.format_food_cost_month("2026-05-01 → 2026-05-29", rows)
        self.assertIn("Month-to-date", out)
        self.assertIn("Khulafa Group", out)
        self.assertIn("Vista", out)
        self.assertIn("Jakel", out)
        # Vista rolling = (6000+200)/(2000+3000) = 124% — sales-weighted, not a
        # mean of the daily %s (300% / 6.7%).
        self.assertIn("124.0%", out)

    def test_incomplete_period_flags_closure_day(self):
        # 6 normal RM4,000 days + one RM200 closure (Raya) -> the closure flags.
        rows = [_recon("Vista", 4000.0, 1200.0, 30.0) for _ in range(6)]
        for i, r in enumerate(rows):
            r["business_date"] = f"2026-05-2{i}"
        closed = _recon("Vista", 200.0, 60.0, 30.0)
        closed["business_date"] = "2026-05-27"
        rows.append(closed)
        flagged = fca.incomplete_period_dates(rows)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["outlet"], "Vista")
        self.assertEqual(flagged[0]["business_date"], "2026-05-27")

    def test_incomplete_period_skips_outlet_with_little_history(self):
        # Only 2 days of data -> no reliable baseline -> never flag.
        rows = [_recon("Vista", 4000.0, 1200.0, 30.0), _recon("Vista", 50.0, 15.0, 30.0)]
        for i, r in enumerate(rows):
            r["business_date"] = f"2026-05-2{i}"
        self.assertEqual(fca.incomplete_period_dates(rows), [])

    def test_incomplete_period_empty_when_all_normal(self):
        rows = [_recon("Vista", 4000.0, 1200.0, 30.0) for _ in range(5)]
        for i, r in enumerate(rows):
            r["business_date"] = f"2026-05-2{i}"
        self.assertEqual(fca.incomplete_period_dates(rows), [])

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

    def test_outlet_trend_includes_rolling(self):
        rows = [
            _recon("Jakel", 3950.0, 1420.0, 35.9),
            _recon("Jakel", 4200.0, 1510.0, 35.9),
        ]
        for i, r in enumerate(rows):
            r["business_date"] = f"2026-05-2{3 + i}"
        out = fca.format_outlet_trend("Jakel", rows, group_pct=28.3)
        self.assertIn("7-day rolling", out)
        self.assertIn("Group 7-day", out)

    def test_outlet_trend_headlines_week_and_month_with_raw_daily(self):
        week = [_recon("Jakel", 4000.0, 1400.0, 35.0)]
        week[0]["business_date"] = "2026-05-28"
        month = [
            _recon("Jakel", 4000.0, 1400.0, 35.0),
            _recon("Jakel", 5000.0, 1500.0, 30.0),
        ]
        for i, r in enumerate(month):
            r["business_date"] = f"2026-05-1{i}"
        out = fca.format_outlet_trend("Jakel", week, group_pct=28.3, month_rows=month)
        self.assertIn("7-day rolling:", out)
        self.assertIn("Month-to-date:", out)
        # Daily breakdown is explicitly raw, not a %.
        self.assertIn("NOT food cost %", out)
        self.assertIn("RM4,000.00 sales", out)

    def test_food_cost_week_renders_rolling_per_outlet(self):
        rows = [
            _recon("Vista", 2000.0, 6000.0, 300.0),
            _recon("Vista", 3000.0, 200.0, 6.7),
            _recon("Jakel", 4000.0, 1400.0, 35.0),
        ]
        out = fca.format_food_cost_week("2026-05-21 → 2026-05-27", rows)
        self.assertIn("7-day rolling", out)
        self.assertIn("Khulafa Group", out)
        self.assertIn("Vista", out)
        self.assertIn("Jakel", out)

    def test_food_cost_week_empty(self):
        out = fca.format_food_cost_week("2026-05-21 → 2026-05-27", [])
        self.assertIn("/reconcile_now", out)


if __name__ == "__main__":
    unittest.main()
