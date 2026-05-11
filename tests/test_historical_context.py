"""Unit tests for ``historical_context``.

Run with::

    python -m unittest tests.test_historical_context
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from historical_context import (  # noqa: E402
    detect_anomaly,
    get_outlet_baseline,
)


class GetOutletBaselineExistsTests(unittest.TestCase):
    def test_bistro_gas_march_total(self):
        b = get_outlet_baseline("BISTRO7", "gas")
        self.assertIsNotNone(b)
        self.assertEqual(b["march_total"], 6890.0)

    def test_bistro_gas_display_name(self):
        b = get_outlet_baseline("BISTRO7", "gas")
        self.assertEqual(b["outlet_display"], "Bistro")

    def test_bistro_gas_vs_group_pct(self):
        b = get_outlet_baseline("BISTRO7", "gas")
        self.assertEqual(b["vs_group_avg_pct"], 91.3)

    def test_bistro_gas_group_avg(self):
        b = get_outlet_baseline("BISTRO7", "gas")
        self.assertEqual(b["group_avg_per_outlet"], 3601.33)

    def test_klang_plastic(self):
        b = get_outlet_baseline("KLANG", "plastic")
        self.assertEqual(b["march_total"], 4618.2)
        self.assertEqual(b["outlet_display"], "Klang")

    def test_damansara_ayam(self):
        b = get_outlet_baseline("D", "ayam")
        self.assertEqual(b["march_total"], 13487.0)
        self.assertEqual(b["outlet_display"], "Damansara")

    def test_sek14_gas(self):
        b = get_outlet_baseline("SEK14", "gas")
        self.assertEqual(b["march_total"], 3406.0)
        self.assertEqual(b["outlet_display"], "SEK 14")

    def test_jakel_minimart(self):
        b = get_outlet_baseline("JAKEL", "minimart")
        self.assertEqual(b["march_total"], 2964.85)
        self.assertEqual(b["vs_group_avg_pct"], 270.0)

    def test_case_insensitive_lowercase(self):
        b = get_outlet_baseline("bistro7", "gas")
        self.assertIsNotNone(b)
        self.assertEqual(b["march_total"], 6890.0)

    def test_case_insensitive_mixed(self):
        b = get_outlet_baseline("Bistro7", "gas")
        self.assertIsNotNone(b)
        self.assertEqual(b["march_total"], 6890.0)

    def test_case_insensitive_uppercase(self):
        b = get_outlet_baseline("BISTRO7", "gas")
        self.assertIsNotNone(b)

    def test_outlet_code_d_lowercase(self):
        b = get_outlet_baseline("d", "ayam")
        self.assertIsNotNone(b)
        self.assertEqual(b["march_total"], 13487.0)

    def test_whitespace_stripped(self):
        b = get_outlet_baseline("  BISTRO7  ", "gas")
        self.assertIsNotNone(b)


class GetOutletBaselineMissingTests(unittest.TestCase):
    def test_unknown_outlet(self):
        self.assertIsNone(get_outlet_baseline("FOOBAR", "gas"))

    def test_unknown_category(self):
        self.assertIsNone(get_outlet_baseline("BISTRO7", "xyz"))

    def test_outlet_exists_but_category_missing(self):
        # Bistro has no "telur" category in baselines.
        self.assertIsNone(get_outlet_baseline("BISTRO7", "telur"))

    def test_empty_outlet(self):
        self.assertIsNone(get_outlet_baseline("", "gas"))

    def test_none_outlet(self):
        self.assertIsNone(get_outlet_baseline(None, "gas"))  # type: ignore[arg-type]

    def test_non_string_outlet(self):
        self.assertIsNone(get_outlet_baseline(123, "gas"))  # type: ignore[arg-type]

    def test_canonical_is_case_sensitive(self):
        # Categories are exact match (caller is expected to canonicalize first).
        self.assertIsNone(get_outlet_baseline("BISTRO7", "GAS"))


class DetectAnomalyThresholdsTests(unittest.TestCase):
    def test_critical_bistro_gas_3500(self):
        r = detect_anomaly("BISTRO7", "gas", 3500.0)
        self.assertEqual(r["severity"], "critical")
        self.assertTrue(r["is_anomaly"])

    def test_high_bistro_gas_2200(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertEqual(r["severity"], "high")
        self.assertTrue(r["is_anomaly"])

    def test_elevated_bistro_gas_1500(self):
        r = detect_anomaly("BISTRO7", "gas", 1500.0)
        self.assertEqual(r["severity"], "elevated")
        self.assertTrue(r["is_anomaly"])

    def test_normal_bistro_gas_500(self):
        r = detect_anomaly("BISTRO7", "gas", 500.0)
        self.assertEqual(r["severity"], "normal")
        self.assertFalse(r["is_anomaly"])

    def test_critical_exact_50_pct(self):
        # 3445 / 6890 = 50.0% exactly → critical (>=50)
        r = detect_anomaly("BISTRO7", "gas", 3445.0)
        self.assertEqual(r["severity"], "critical")

    def test_high_exact_30_pct(self):
        # 2067 / 6890 = 30.0% exactly → high (>=30)
        r = detect_anomaly("BISTRO7", "gas", 2067.0)
        self.assertEqual(r["severity"], "high")

    def test_elevated_exact_15_pct(self):
        # 1033.5 / 6890 = 15.0% exactly → elevated (>=15)
        r = detect_anomaly("BISTRO7", "gas", 1033.5)
        self.assertEqual(r["severity"], "elevated")

    def test_monthly_pct_used_value(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertAlmostEqual(r["monthly_pct_used"], 2200.0 / 6890.0 * 100.0, places=4)

    def test_vs_group_pct_passthrough(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertEqual(r["vs_group_pct"], 91.3)

    def test_baseline_included_in_result(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIsNotNone(r["baseline"])
        self.assertEqual(r["baseline"]["march_total"], 6890.0)


class DetectAnomalyEdgeCasesTests(unittest.TestCase):
    def test_zero_amount(self):
        r = detect_anomaly("BISTRO7", "gas", 0.0)
        self.assertFalse(r["is_anomaly"])
        self.assertEqual(r["severity"], "normal")

    def test_negative_amount(self):
        r = detect_anomaly("BISTRO7", "gas", -100.0)
        self.assertFalse(r["is_anomaly"])
        self.assertEqual(r["severity"], "normal")

    def test_unknown_outlet_no_anomaly(self):
        r = detect_anomaly("FOOBAR", "gas", 5000.0)
        self.assertFalse(r["is_anomaly"])
        self.assertEqual(r["severity"], "normal")
        self.assertIsNone(r["baseline"])

    def test_unknown_category_no_anomaly(self):
        r = detect_anomaly("BISTRO7", "xyz", 5000.0)
        self.assertFalse(r["is_anomaly"])
        self.assertEqual(r["severity"], "normal")
        self.assertIsNone(r["baseline"])

    def test_unknown_baseline_empty_messages(self):
        r = detect_anomaly("FOOBAR", "gas", 5000.0)
        self.assertEqual(r["message_short"], "")
        self.assertEqual(r["message_detail"], "")

    def test_zero_amount_empty_messages(self):
        r = detect_anomaly("BISTRO7", "gas", 0.0)
        self.assertEqual(r["message_short"], "")
        self.assertEqual(r["message_detail"], "")

    def test_non_numeric_amount(self):
        r = detect_anomaly("BISTRO7", "gas", "not a number")  # type: ignore[arg-type]
        self.assertFalse(r["is_anomaly"])
        self.assertEqual(r["severity"], "normal")


class MessageFormatTests(unittest.TestCase):
    def test_short_message_contains_rm(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("RM", r["message_short"])

    def test_short_message_contains_category_name(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("GAS", r["message_short"])

    def test_short_message_contains_amount(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("2200", r["message_short"])

    def test_short_message_contains_pct(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("32%", r["message_short"])

    def test_short_message_one_line(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertNotIn("\n", r["message_short"])

    def test_detail_message_has_five_bullets(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertEqual(r["message_detail"].count("• "), 5)

    def test_detail_message_contains_today_line(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("Today:", r["message_detail"])

    def test_detail_message_contains_march_line(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("March 2026 avg", r["message_detail"])

    def test_detail_message_contains_monthly_budget_line(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("monthly budget", r["message_detail"])

    def test_detail_message_contains_group_avg_line(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("Group avg", r["message_detail"])

    def test_detail_message_contains_outlet_display(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("Bistro", r["message_detail"])

    def test_detail_message_contains_question(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("Question:", r["message_detail"])

    def test_detail_message_more_than_when_positive(self):
        r = detect_anomaly("BISTRO7", "gas", 2200.0)
        self.assertIn("more than group avg", r["message_detail"])

    def test_detail_message_less_than_when_negative(self):
        # Damansara plastic vs_group_avg_pct = -91.8 (less than avg).
        # Force anomaly: 200 / 250 = 80% → critical, so message_detail populated.
        r = detect_anomaly("D", "plastic", 200.0)
        self.assertIn("less than group avg", r["message_detail"])


if __name__ == "__main__":
    unittest.main()
