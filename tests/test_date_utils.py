"""Unit tests for ``date_utils.normalize_date``.

Run with::

    python -m unittest tests.test_date_utils
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from date_utils import normalize_date  # noqa: E402


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
