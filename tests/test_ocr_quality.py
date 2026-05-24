"""Unit tests for ``ocr_quality``.

Run with::

    python -m unittest tests.test_ocr_quality
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocr_quality import (  # noqa: E402
    correct_total_with_items,
    has_rm_sen_split_column,
    is_date_in_window,
    normalize_amount_locale_aware,
    validate_date,
)


class NormalizeAmountMalaysian(unittest.TestCase):
    def test_plain_decimal(self):
        self.assertEqual(normalize_amount_locale_aware("13.50"), 13.50)

    def test_rm_prefix(self):
        self.assertEqual(normalize_amount_locale_aware("RM13.50"), 13.50)

    def test_rm_prefix_with_space(self):
        self.assertEqual(normalize_amount_locale_aware("RM 13.50"), 13.50)

    def test_myr_suffix(self):
        self.assertEqual(normalize_amount_locale_aware("13.50 MYR"), 13.50)

    def test_us_thousand_separator(self):
        self.assertEqual(normalize_amount_locale_aware("1,234.56"), 1234.56)

    def test_us_thousand_separator_with_rm(self):
        self.assertEqual(normalize_amount_locale_aware("RM 1,234.50"), 1234.50)

    def test_space_thousand_separator(self):
        self.assertEqual(normalize_amount_locale_aware("1 234.56"), 1234.56)

    def test_no_decimal(self):
        self.assertEqual(normalize_amount_locale_aware("100"), 100.0)

    def test_negative(self):
        self.assertEqual(normalize_amount_locale_aware("-13.50"), -13.50)


class NormalizeAmountEuropean(unittest.TestCase):
    def test_european_decimal_full(self):
        self.assertEqual(normalize_amount_locale_aware("1.234,56"), 1234.56)

    def test_european_simple_decimal(self):
        self.assertEqual(normalize_amount_locale_aware("12,50"), 12.50)

    def test_european_with_rm(self):
        self.assertEqual(normalize_amount_locale_aware("RM 1.234,56"), 1234.56)

    def test_us_with_three_digit_thousands_not_european(self):
        # "1,234" has 3 digits after comma — definitely thousand sep, not decimal.
        self.assertEqual(normalize_amount_locale_aware("1,234"), 1234.0)


class NormalizeAmountRejects(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(normalize_amount_locale_aware(None))

    def test_empty(self):
        self.assertIsNone(normalize_amount_locale_aware(""))

    def test_bool_rejected(self):
        self.assertIsNone(normalize_amount_locale_aware(True))
        self.assertIsNone(normalize_amount_locale_aware(False))

    def test_garbage(self):
        self.assertIsNone(normalize_amount_locale_aware("abc"))

    def test_multiple_dots(self):
        self.assertIsNone(normalize_amount_locale_aware("13.00.50"))

    def test_only_currency(self):
        self.assertIsNone(normalize_amount_locale_aware("RM"))


class NormalizeAmountPassthrough(unittest.TestCase):
    def test_int(self):
        self.assertEqual(normalize_amount_locale_aware(13), 13.0)

    def test_float(self):
        self.assertEqual(normalize_amount_locale_aware(13.50), 13.50)


class CorrectTotalDecimalLoss(unittest.TestCase):
    """The PVS SANTAN and NASI LEMAK regression cases."""

    def test_pvs_santan_18000_correctable_to_180(self):
        corrected, was_fixed = correct_total_with_items(
            18000.0, [{"name": "santan", "qty": 1, "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(was_fixed)

    def test_nasi_lemak_8250_correctable_to_82_50(self):
        corrected, was_fixed = correct_total_with_items(
            8250.0,
            [
                {"name": "nasi lemak", "qty": 1, "price": 50.00},
                {"name": "teh", "qty": 1, "price": 32.50},
            ],
        )
        self.assertEqual(corrected, 82.50)
        self.assertTrue(was_fixed)

    def test_no_correction_when_total_matches_sum(self):
        corrected, was_fixed = correct_total_with_items(
            180.0, [{"name": "santan", "qty": 1, "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertFalse(was_fixed)

    def test_no_correction_when_within_10_percent(self):
        # Real receipts have tax/rounding noise — don't correct small drift.
        corrected, was_fixed = correct_total_with_items(
            105.0, [{"name": "x", "qty": 1, "price": 100.0}],
        )
        self.assertEqual(corrected, 105.0)
        self.assertFalse(was_fixed)

    def test_correction_undershoot_times_100(self):
        corrected, was_fixed = correct_total_with_items(
            1.80, [{"name": "x", "qty": 1, "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(was_fixed)


class CorrectTotalEdgeCases(unittest.TestCase):
    def test_total_none(self):
        corrected, was_fixed = correct_total_with_items(None, [{"price": 100.0}])
        self.assertIsNone(corrected)
        self.assertFalse(was_fixed)

    def test_empty_items(self):
        corrected, was_fixed = correct_total_with_items(100.0, [])
        self.assertEqual(corrected, 100.0)
        self.assertFalse(was_fixed)

    def test_none_items(self):
        corrected, was_fixed = correct_total_with_items(100.0, None)
        self.assertEqual(corrected, 100.0)
        self.assertFalse(was_fixed)

    def test_items_with_null_prices(self):
        corrected, was_fixed = correct_total_with_items(
            100.0,
            [{"name": "x", "price": None}, {"name": "y", "price": None}],
        )
        # No usable line items — leave total alone.
        self.assertEqual(corrected, 100.0)
        self.assertFalse(was_fixed)

    def test_items_mixed_some_priced(self):
        corrected, was_fixed = correct_total_with_items(
            18000.0,
            [{"name": "x", "price": None}, {"name": "y", "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(was_fixed)

    def test_bool_price_ignored(self):
        # bool is an int subclass — must not be summed.
        corrected, was_fixed = correct_total_with_items(
            18000.0, [{"name": "x", "price": True}],
        )
        self.assertEqual(corrected, 18000.0)
        self.assertFalse(was_fixed)


class ValidateDateBasic(unittest.TestCase):
    TODAY = date(2026, 5, 23)

    def test_label_anchored_dmy_in_window(self):
        text = "Tarikh: 10/05/2026\nGRAND TOTAL: RM50.00"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2026-05-10")
        self.assertFalse(flagged)

    def test_label_anchored_ymd_in_window(self):
        text = "Date: 2026-05-10\nTotal: RM50"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2026-05-10")
        self.assertFalse(flagged)

    def test_picks_in_window_over_out_of_window(self):
        # First candidate would be a future date; should skip to the valid one.
        text = "Quote ref 2027-12-01\nTarikh: 10/05/2026"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2026-05-10")
        self.assertFalse(flagged)

    def test_dmy_short_year(self):
        text = "Tarikh: 10/05/26"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2026-05-10")
        self.assertFalse(flagged)


class ValidateDateOutOfWindow(unittest.TestCase):
    TODAY = date(2026, 5, 23)

    def test_far_future_flagged(self):
        text = "Date: 2028-01-15"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2028-01-15")
        self.assertTrue(flagged)

    def test_far_past_flagged(self):
        text = "Date: 2020-01-15"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2020-01-15")
        self.assertTrue(flagged)

    def test_seven_days_future_still_in_window(self):
        text = "Date: 2026-05-30"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2026-05-30")
        self.assertFalse(flagged)

    def test_eight_days_future_out_of_window(self):
        text = "Date: 2026-05-31"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertTrue(flagged)


class ValidateDateAmbiguous(unittest.TestCase):
    TODAY = date(2026, 5, 23)

    def test_dmy_preferred_when_label_anchored(self):
        # The original FIXME case: glm-ocr might emit "2026-10-05" when the
        # receipt actually said "10/05/2026" (10-May-2026). Label-anchored
        # in-window candidate wins.
        text = "Tarikh: 10/05/2026\nNote: ref 2026-10-05"
        result, flagged = validate_date(text, today=self.TODAY)
        self.assertEqual(result, "2026-05-10")
        self.assertFalse(flagged)

    def test_no_dates_returns_none(self):
        result, flagged = validate_date("no date here at all", today=self.TODAY)
        self.assertIsNone(result)
        self.assertFalse(flagged)

    def test_empty_input(self):
        result, flagged = validate_date("", today=self.TODAY)
        self.assertIsNone(result)
        self.assertFalse(flagged)


class IsDateInWindow(unittest.TestCase):
    TODAY = date(2026, 5, 23)

    def test_today(self):
        self.assertTrue(is_date_in_window(self.TODAY, self.TODAY))

    def test_yesterday(self):
        self.assertTrue(is_date_in_window(date(2026, 5, 22), self.TODAY))

    def test_365_days_ago(self):
        self.assertTrue(is_date_in_window(date(2025, 5, 23), self.TODAY))

    def test_366_days_ago_out(self):
        self.assertFalse(is_date_in_window(date(2025, 5, 22), self.TODAY))

    def test_7_days_future(self):
        self.assertTrue(is_date_in_window(date(2026, 5, 30), self.TODAY))

    def test_8_days_future_out(self):
        self.assertFalse(is_date_in_window(date(2026, 5, 31), self.TODAY))


class HasRMSenSplitColumn(unittest.TestCase):
    def test_detects_table_header(self):
        md = "| Item | RM | Sen |\n|---|---|---|\n| Santan | 18 | 00 |"
        self.assertTrue(has_rm_sen_split_column(md))

    def test_detects_lowercase_header(self):
        md = "| item | rm | sen |"
        self.assertTrue(has_rm_sen_split_column(md))

    def test_no_match_on_inline_text(self):
        # "RM" and "Sen" in flowing text should not trigger.
        md = "Total: RM 18.00 (eighteen ringgit, zero sen)"
        self.assertFalse(has_rm_sen_split_column(md))

    def test_no_match_when_only_rm(self):
        md = "| Item | RM | Qty |"
        self.assertFalse(has_rm_sen_split_column(md))

    def test_none_input(self):
        self.assertFalse(has_rm_sen_split_column(None))


class ParseMarkdownReceiptIntegration(unittest.TestCase):
    """Integration tests that exercise ocr_glm.parse_markdown_receipt
    end-to-end with the quality module wired in. Locks the contract
    that wiring stays in place even if the helpers move around."""

    def test_pvs_santan_decimal_loss_corrected(self):
        from ocr_glm import parse_markdown_receipt
        md = (
            "# PVS SANTAN SDN BHD\n"
            "| Item | Qty | Amount |\n"
            "|---|---|---|\n"
            "| Santan | 1 | 180.00 |\n"
            "GRAND TOTAL: RM 18,000\n"
            "Tarikh: 22/05/2026\n"
        )
        result = parse_markdown_receipt(md)
        self.assertEqual(result["total"], 180.0)
        self.assertEqual(result["receipt_date"], "2026-05-22")
        self.assertLess(
            result["confidence"], 100,
            "decimal-fix penalty should have docked confidence",
        )

    def test_clean_receipt_no_penalty(self):
        from ocr_glm import parse_markdown_receipt
        md = (
            "# BABAS MASALA\n"
            "| Item | Qty | Amount |\n"
            "|---|---|---|\n"
            "| Curry powder | 1 | 25.00 |\n"
            "GRAND TOTAL: RM 25.00\n"
            "Date: 20/05/2026\n"
        )
        result = parse_markdown_receipt(md)
        self.assertEqual(result["total"], 25.0)
        self.assertEqual(result["receipt_date"], "2026-05-20")
        self.assertEqual(result["confidence"], 100)


if __name__ == "__main__":
    unittest.main()
