"""Unit tests for ``date_utils.normalize_date``.

Run with::

    python -m unittest tests.test_date_utils
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date  # noqa: E402

from date_utils import (  # noqa: E402
    clamp_business_date,
    effective_purchase_date,
    normalize_date,
)


class EffectivePurchaseDate(unittest.TestCase):
    """Trustworthy purchase date: trust receipt_date unless implausible, then
    fall back to the ingestion day."""

    def setUp(self):
        self.today = date(2026, 6, 17)
        self.ingested = "2026-06-15T02:00:00+00:00"  # MY-local 2026-06-15

    def test_plausible_date_kept(self):
        eff, corrected, reason = effective_purchase_date(
            "2026-06-14", self.ingested, today=self.today)
        self.assertEqual(eff, date(2026, 6, 14))
        self.assertFalse(corrected)
        self.assertIsNone(reason)

    def test_future_date_falls_back_to_ingestion(self):
        eff, corrected, _ = effective_purchase_date(
            "2026-08-22", self.ingested, today=self.today)
        self.assertEqual(eff, date(2026, 6, 15))
        self.assertTrue(corrected)

    def test_far_future_year_falls_back(self):
        eff, corrected, _ = effective_purchase_date(
            "2029-05-29", self.ingested, today=self.today)
        self.assertEqual(eff, date(2026, 6, 15))
        self.assertTrue(corrected)

    def test_stale_year_beyond_drift_falls_back(self):
        eff, corrected, _ = effective_purchase_date(
            "2024-05-08", self.ingested, today=self.today)
        self.assertEqual(eff, date(2026, 6, 15))
        self.assertTrue(corrected)

    def test_missing_receipt_date_uses_ingestion(self):
        eff, corrected, _ = effective_purchase_date(None, self.ingested, today=self.today)
        self.assertEqual(eff, date(2026, 6, 15))
        self.assertTrue(corrected)

    def test_implausible_without_ingestion_is_flagged_not_changed(self):
        eff, corrected, reason = effective_purchase_date(
            "2026-08-22", None, today=self.today)
        self.assertEqual(eff, date(2026, 8, 22))  # unchanged — nothing to anchor to
        self.assertFalse(corrected)
        self.assertIn("no ingestion", reason)

    def test_small_recent_drift_kept(self):
        # Ingested a few days after a real purchase — not corruption.
        eff, corrected, _ = effective_purchase_date(
            "2026-06-13", self.ingested, today=self.today)
        self.assertEqual(eff, date(2026, 6, 13))
        self.assertFalse(corrected)


class ClampBusinessDate(unittest.TestCase):
    """PR #64: future OCR dates fall back to the upload day, never dropped."""

    def test_future_date_beyond_3_days_is_clamped(self):
        # Uploaded 2026-05-29, OCR read 2026-06-15 (~17 days ahead) -> clamp.
        eff, clamped = clamp_business_date("2026-06-15", "2026-05-29T03:00:00+00:00")
        self.assertEqual(eff, date(2026, 5, 29))
        self.assertTrue(clamped)

    def test_date_within_3_days_not_clamped(self):
        # 2 days ahead is within tolerance — keep the OCR'd date as-is.
        eff, clamped = clamp_business_date("2026-05-31", "2026-05-29T03:00:00+00:00")
        self.assertEqual(eff, date(2026, 5, 31))
        self.assertFalse(clamped)

    def test_past_date_never_clamped(self):
        # A receipt dated before its upload is normal (delivered then uploaded).
        eff, clamped = clamp_business_date("2026-05-20", "2026-05-29T03:00:00+00:00")
        self.assertEqual(eff, date(2026, 5, 20))
        self.assertFalse(clamped)

    def test_null_receipt_date_falls_back_without_clamp_flag(self):
        # No OCR date -> use the upload day, but that's the ordinary fallback,
        # not a future-date clamp, so it isn't flagged.
        eff, clamped = clamp_business_date(None, "2026-05-29T03:00:00+00:00")
        self.assertEqual(eff, date(2026, 5, 29))
        self.assertFalse(clamped)

    def test_utc_late_night_maps_to_next_my_day(self):
        # 2026-05-29 18:00 UTC = 2026-05-30 02:00 MY; a same-MY-day OCR date for
        # the 30th must NOT be seen as future.
        eff, clamped = clamp_business_date("2026-05-30", "2026-05-29T18:00:00+00:00")
        self.assertEqual(eff, date(2026, 5, 30))
        self.assertFalse(clamped)


class NormalizeDateAcceptedFormats(unittest.TestCase):
    def test_dd_slash_mm_slash_yy(self):
        self.assertEqual(normalize_date("25/4/26"), "2026-04-25")

    def test_dd_slash_mm_slash_yyyy(self):
        self.assertEqual(normalize_date("25/04/2026"), "2026-04-25")

    def test_dd_dash_mm_dash_yy(self):
        self.assertEqual(normalize_date("25-4-26"), "2026-04-25")

    def test_dd_dash_mm_dash_yyyy(self):
        self.assertEqual(normalize_date("25-04-2026"), "2026-04-25")

    def test_iso_passthrough(self):
        self.assertEqual(normalize_date("2026-04-25"), "2026-04-25")

    def test_iso_with_single_digit_month_and_day(self):
        self.assertEqual(normalize_date("2026-4-5"), "2026-04-05")

    def test_zero_padded_dd_mm(self):
        self.assertEqual(normalize_date("05/04/26"), "2026-04-05")

    def test_whitespace_is_trimmed(self):
        self.assertEqual(normalize_date("  25/4/26  "), "2026-04-25")


class NormalizeDateTwoDigitYearRules(unittest.TestCase):
    def test_yy_just_under_50_maps_to_2000s(self):
        self.assertEqual(normalize_date("25/4/49"), "2049-04-25")

    def test_yy_50_maps_to_1900s(self):
        self.assertEqual(normalize_date("25/4/50"), "1950-04-25")

    def test_yy_99_maps_to_1999(self):
        self.assertEqual(normalize_date("25/4/99"), "1999-04-25")

    def test_yy_00_maps_to_2000(self):
        self.assertEqual(normalize_date("25/4/00"), "2000-04-25")


class NormalizeDateDefensiveYearBump(unittest.TestCase):
    """ISO years older than ``MIN_PLAUSIBLE_YEAR`` are bumped to ``FALLBACK_YEAR``.

    Preserves the existing behaviour that protects against OCR misreads where
    the year digits are dropped (e.g. ``"0026-04-25"``).
    """

    def test_year_zero_bumped_to_fallback(self):
        self.assertEqual(normalize_date("0026-04-25"), "2026-04-25")

    def test_year_2023_bumped_to_fallback(self):
        self.assertEqual(normalize_date("2023-04-25"), "2026-04-25")

    def test_year_2024_passthrough(self):
        self.assertEqual(normalize_date("2024-04-25"), "2024-04-25")


class NormalizeDateRejectsInvalid(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(normalize_date(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(normalize_date(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(normalize_date("   "))

    def test_non_string_int_returns_none(self):
        self.assertIsNone(normalize_date(20260425))

    def test_non_string_list_returns_none(self):
        self.assertIsNone(normalize_date(["2026-04-25"]))

    def test_non_string_dict_returns_none(self):
        self.assertIsNone(normalize_date({"date": "2026-04-25"}))

    def test_garbage_text(self):
        self.assertIsNone(normalize_date("not a date"))

    def test_month_13_in_dmy_returns_none(self):
        # The user's example: "5/13/26" is invalid as DD/MM/YY (month 13).
        self.assertIsNone(normalize_date("5/13/26"))

    def test_day_32_returns_none(self):
        self.assertIsNone(normalize_date("32/4/26"))

    def test_invalid_iso_month_returns_none(self):
        self.assertIsNone(normalize_date("2026-13-01"))

    def test_invalid_iso_day_returns_none(self):
        self.assertIsNone(normalize_date("2026-02-30"))

    def test_partial_only_two_components_returns_none(self):
        self.assertIsNone(normalize_date("25/4"))

    def test_dot_separator_unsupported(self):
        # Only "/" and "-" are supported; dots fall through to None.
        self.assertIsNone(normalize_date("25.4.26"))


if __name__ == "__main__":
    unittest.main()
