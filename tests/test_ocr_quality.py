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
    line_items_incomplete,
    normalize_amount_locale_aware,
    total_conflicts_with_item_sum,
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


class IncompleteItemsGuardTests(unittest.TestCase):
    """Row 1588 regression: when OCR drops a line item, sum(items) is
    understated and must NOT be used to 'correct' an already-correct total."""

    ROW_1588_RAW = (
        "# EVEREST AISVARAM SDN BHD\n"
        "1. Tube Ice RM 90.00\n"
        "2. Crush Ice RM 9.00\n"
        "TOTAL RM 99.00\n"
    )

    def test_detects_missing_row(self):
        # 2 numbered rows in raw, only 1 parsed -> incomplete.
        self.assertTrue(
            line_items_incomplete(
                self.ROW_1588_RAW, [{"name": "Tube Ice", "price": 90.0}]
            )
        )

    def test_complete_when_counts_match(self):
        self.assertFalse(
            line_items_incomplete(
                self.ROW_1588_RAW,
                [
                    {"name": "Tube Ice", "price": 90.0},
                    {"name": "Crush Ice", "price": 9.0},
                ],
            )
        )

    def test_no_numbered_rows_never_incomplete(self):
        # RM/Sen table format has no "1." rows -> gate inert (BESTARI shape).
        md = "# BESTARI FARM\n| Item | RM | Sen |\n|---|---|---|\n| Eggs | 2000 | 00 |\n"
        self.assertFalse(
            line_items_incomplete(md, [{"name": "Eggs", "price": 2000.0}])
        )

    def test_empty_raw_text_never_incomplete(self):
        self.assertFalse(line_items_incomplete("", [{"price": 1.0}]))
        self.assertFalse(line_items_incomplete(None, [{"price": 1.0}]))

    def test_row_1588_total_not_corrected(self):
        # The literal reported numbers: total 99 vs parsed-only 90 stays 99.
        corrected, was_fixed = correct_total_with_items(
            99.0,
            [{"name": "Tube Ice", "price": 90.0}],
            raw_text=self.ROW_1588_RAW,
        )
        self.assertEqual(corrected, 99.0)
        self.assertFalse(was_fixed)

    def test_guard_blocks_wrong_large_correction(self):
        # The failure mode that actually corrupts data: a real total in the
        # thousands with only one item parsed. Without the guard this would
        # "correct" 9000 -> 90; the guard suppresses it.
        raw = (
            "1. Tube Ice RM 90.00\n"
            "2. Crush Ice RM 8910.00\n"
            "TOTAL RM 9000.00\n"
        )
        guarded, fixed = correct_total_with_items(
            9000.0, [{"name": "Tube Ice", "price": 90.0}], raw_text=raw,
        )
        self.assertEqual(guarded, 9000.0)
        self.assertFalse(fixed)

    def test_without_raw_text_old_behaviour_preserved(self):
        # Same incomplete inputs, but no raw_text to assess completeness:
        # the function can't tell, so the legacy correction still fires.
        # This documents WHY raw_text must be threaded through from the caller.
        legacy, fixed = correct_total_with_items(
            9000.0, [{"name": "Tube Ice", "price": 90.0}],
        )
        self.assertEqual(legacy, 90.0)
        self.assertTrue(fixed)

    def test_bestari_rm_sen_complete_unaffected(self):
        # Row 1586: complete items, RM/Sen table, correct total 2170.76.
        # Guard is inert (no numbered rows); total left untouched.
        md = (
            "# BESTARI FARM\n| Item | RM | Sen |\n|---|---|---|\n"
            "| Eggs | 2000 | 00 |\n| Flour | 170 | 76 |\n"
            "GRAND TOTAL: RM 2,170.76\n"
        )
        items = [
            {"name": "Eggs", "price": 2000.0},
            {"name": "Flour", "price": 170.76},
        ]
        self.assertFalse(line_items_incomplete(md, items))
        corrected, was_fixed = correct_total_with_items(2170.76, items, raw_text=md)
        self.assertEqual(corrected, 2170.76)
        self.assertFalse(was_fixed)


class IncompleteItemsIntegration(unittest.TestCase):
    """End-to-end through parse_markdown_receipt: incomplete parse keeps the
    total and docks confidence by the incomplete-items penalty (15)."""

    def test_row_1588_total_preserved_confidence_docked(self):
        from ocr_glm import parse_markdown_receipt

        result = parse_markdown_receipt(
            "# EVEREST AISVARAM SDN BHD\n"
            "1. Tube Ice RM 90.00\n"
            "2. Crush Ice\n"  # no price -> parser drops it, raw still shows row 2
            "TOTAL RM 99.00\n"
            "Date: 20/05/2026\n"
        )
        self.assertEqual(result["total"], 99.0)
        self.assertEqual(len(result["items"]), 1)
        # 30 merchant + 30 total + 20 date + 20 items = 100, minus 15 incomplete.
        self.assertEqual(result["confidence"], 85)

    def test_complete_parse_no_incomplete_penalty(self):
        from ocr_glm import parse_markdown_receipt

        result = parse_markdown_receipt(
            "# EVEREST AISVARAM SDN BHD\n"
            "1. Tube Ice RM 90.00\n"
            "2. Crush Ice RM 9.00\n"
            "TOTAL RM 99.00\n"
            "Date: 20/05/2026\n"
        )
        self.assertEqual(result["total"], 99.0)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["confidence"], 100)


class ConservativeCorrectionTests(unittest.TestCase):
    """Row 1613 regression: only correct CLEAN power-of-ten decimal flips,
    and never let FOC / null-priced lines contribute to the item sum."""

    # --- clean decimal flips still correct -------------------------------

    def test_flip_100x_too_large(self):
        # sum 18, total 1800 -> ratio 0.01 -> 18.00 (PVS SANTAN shape).
        corrected, fixed = correct_total_with_items(
            1800.0, [{"name": "santan", "price": 18.0}],
        )
        self.assertEqual(corrected, 18.0)
        self.assertTrue(fixed)

    def test_flip_100x_too_large_ice(self):
        corrected, fixed = correct_total_with_items(
            9000.0, [{"name": "ice", "price": 90.0}],
        )
        self.assertEqual(corrected, 90.0)
        self.assertTrue(fixed)

    def test_flip_10x(self):
        # ratio 0.1 -> divide by 10.
        corrected, fixed = correct_total_with_items(
            420.0, [{"name": "x", "price": 42.0}],
        )
        self.assertEqual(corrected, 42.0)
        self.assertTrue(fixed)

    def test_flip_times_100_undershoot(self):
        corrected, fixed = correct_total_with_items(
            1.80, [{"name": "x", "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(fixed)

    # --- vague mismatches are NOT corrected ------------------------------

    def test_row_1613_ratio_not_a_flip_left_alone(self):
        # sum 42 vs total 40 -> ratio 1.05 -> NOT a clean flip -> keep 40.
        corrected, fixed = correct_total_with_items(
            40.0, [{"name": "Tube Ice", "price": 42.0}],
        )
        self.assertEqual(corrected, 40.0)
        self.assertFalse(fixed)

    def test_ratio_1_075_left_alone(self):
        corrected, fixed = correct_total_with_items(
            40.0,
            [{"name": "Tube Ice", "price": 42.0}, {"name": "Soda", "price": 1.0}],
        )
        self.assertEqual(corrected, 40.0)
        self.assertFalse(fixed)

    # --- FOC / null exclusion --------------------------------------------

    def test_foc_keyword_excluded_from_items_sum(self):
        # Tube Ice 42 + "Block Ice Foc" RM1: the FOC line is ignored, so the
        # sum is 42 and matches a real total of 42 (no phantom conflict).
        items = [
            {"name": "Tube Ice", "price": 42.0},
            {"name": "Block Ice Foc", "price": 1.0},
        ]
        self.assertFalse(total_conflicts_with_item_sum(42.0, items))

    def test_foc_spellings_excluded(self):
        for name in ("FOC", "F.O.C", "free gift", "Air Percuma", "block ice foc=1"):
            with self.subTest(name=name):
                items = [
                    {"name": "Tea", "price": 10.0},
                    {"name": name, "price": 5.0},
                ]
                self.assertFalse(total_conflicts_with_item_sum(10.0, items))

    def test_foc_line_does_not_trigger_correction(self):
        # Without FOC exclusion, sum would be 43 vs total 40 (ratio 1.075) —
        # still not a flip, but the exclusion keeps the sum honest at 42.
        items = [
            {"name": "Tube Ice", "price": 42.0},
            {"name": "Block Ice FOC", "price": 1.0},
        ]
        corrected, fixed = correct_total_with_items(40.0, items)
        self.assertEqual(corrected, 40.0)
        self.assertFalse(fixed)

    def test_non_foc_name_still_counts(self):
        items = [{"name": "Tea", "price": 10.0}, {"name": "Coffee", "price": 5.0}]
        self.assertTrue(total_conflicts_with_item_sum(10.0, items))

    # --- conflict predicate edges ----------------------------------------

    def test_no_conflict_when_equal(self):
        self.assertFalse(
            total_conflicts_with_item_sum(15.0, [{"name": "x", "price": 15.0}])
        )

    def test_conflict_when_unequal(self):
        self.assertTrue(
            total_conflicts_with_item_sum(40.0, [{"name": "x", "price": 42.0}])
        )

    def test_no_conflict_without_priced_items(self):
        self.assertFalse(
            total_conflicts_with_item_sum(15.0, [{"name": "x", "price": None}])
        )
        self.assertFalse(total_conflicts_with_item_sum(15.0, None))


class ConservativeCorrectionIntegration(unittest.TestCase):
    """End-to-end confidence wiring for the conservative-correction rules."""

    def test_row_1613_total_40_with_foc_items_no_correction(self):
        from ocr_glm import parse_markdown_receipt

        result = parse_markdown_receipt(
            "# EVEREST AISVARAM SDN BHD\n"
            "1. Tube Ice RM 42.00\n"
            "2. Block Ice Foc RM 1.00\n"
            "3. Crush Ice RM 0.00\n"
            "TOTAL RM 40.00\n"
            "Date: 20/05/2026\n"
        )
        # OCR misread the TOTAL line (paper said 42); we keep what was read
        # rather than guess, and dock confidence for review.
        self.assertEqual(result["total"], 40.0)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["confidence"], 90)  # 100 - 10 conflict

    def test_pvs_santan_decimal_flip_still_works(self):
        from ocr_glm import parse_markdown_receipt

        result = parse_markdown_receipt(
            "# PVS SANTAN SDN BHD\n"
            "| Item | Qty | Amount |\n"
            "|---|---|---|\n"
            "| Santan | 1 | 18.00 |\n"
            "GRAND TOTAL: RM 1,800.00\n"
            "Tarikh: 22/05/2026\n"
        )
        self.assertEqual(result["total"], 18.0)
        self.assertEqual(result["confidence"], 80)  # 100 - 20 decimal fix

    def test_clean_total_no_penalty(self):
        from ocr_glm import parse_markdown_receipt

        result = parse_markdown_receipt(
            "# BABAS MASALA\n"
            "1. Curry powder RM 25.00\n"
            "TOTAL RM 25.00\n"
            "Date: 20/05/2026\n"
        )
        self.assertEqual(result["total"], 25.0)
        self.assertEqual(result["confidence"], 100)


class QtyAwareItemSum(unittest.TestCase):
    """Receipt #407 regression: a multi-unit line (qty × unit_price = total)
    must reconcile via qty×price, not be mistaken for a decimal flip. Plus the
    RM5 floor backstop for legacy rows that stored no qty."""

    # --- the bug class: qty×price already equals the total -> no correction ---

    def test_407_qty100_unit150_no_correction(self):
        corrected, fixed = correct_total_with_items(
            150.0, [{"name": "tacang", "qty": 100, "price": 1.50}],
        )
        self.assertEqual(corrected, 150.0)
        self.assertFalse(fixed)

    def test_qty10_unit_priced_no_correction(self):
        corrected, fixed = correct_total_with_items(
            50.0, [{"name": "x", "qty": 10, "price": 5.00}],
        )
        self.assertEqual(corrected, 50.0)
        self.assertFalse(fixed)

    # --- Fix B floor: no qty stored, unit price would flip below RM5 ----------

    def test_qty_none_with_rm5_floor(self):
        corrected, fixed = correct_total_with_items(
            150.0, [{"name": "tacang", "qty": None, "price": 1.50}],
        )
        self.assertEqual(corrected, 150.0)  # 1.50 < RM5 -> suppressed
        self.assertFalse(fixed)

    def test_qty_none_high_value_still_works(self):
        # No qty, but the flip lands at RM18 (>= floor) -> genuine fix applies.
        corrected, fixed = correct_total_with_items(
            1800.0, [{"name": "x", "qty": None, "price": 18.00}],
        )
        self.assertEqual(corrected, 18.0)
        self.assertTrue(fixed)

    # --- regression: genuine single-unit decimal losses still correct ---------

    def test_pvs_santan_single_item_still_corrects(self):
        corrected, fixed = correct_total_with_items(
            18000.0, [{"name": "santan", "qty": 1, "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(fixed)

    def test_nasi_lemak_still_corrects(self):
        corrected, fixed = correct_total_with_items(
            8250.0, [{"name": "nasi lemak", "qty": 1, "price": 82.50}],
        )
        self.assertEqual(corrected, 82.50)
        self.assertTrue(fixed)

    def test_everest_still_corrects(self):
        corrected, fixed = correct_total_with_items(
            9900.0, [{"name": "tube ice", "qty": 1, "price": 99.0}],
        )
        self.assertEqual(corrected, 99.0)
        self.assertTrue(fixed)

    # --- qty coercion edge cases (all treated as 1) ---------------------------

    def test_qty_zero_treated_as_one(self):
        # qty=0 -> 1, so 1x180 vs 18000 is still a clean 100x flip -> 180.
        corrected, fixed = correct_total_with_items(
            18000.0, [{"name": "x", "qty": 0, "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(fixed)

    def test_qty_negative_treated_as_one(self):
        corrected, fixed = correct_total_with_items(
            18000.0, [{"name": "x", "qty": -5, "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(fixed)

    def test_qty_string_invalid_treated_as_one(self):
        corrected, fixed = correct_total_with_items(
            18000.0, [{"name": "x", "qty": "abc", "price": 180.0}],
        )
        self.assertEqual(corrected, 180.0)
        self.assertTrue(fixed)


class QtyAwareLivePipeline(unittest.TestCase):
    """The fix lives in the shared _sum_line_item_prices, so it must protect
    the LIVE parse_markdown_receipt path too — not just PR #29c reparse."""

    def test_live_100x_unit_priced_line_not_corrupted(self):
        from ocr_glm import parse_markdown_receipt

        # "100 tacang @ RM1.50 = RM150" arriving via the OCR pipeline.
        result = parse_markdown_receipt(
            "# KEDAI RUNCIT\n"
            "| Item | Qty | Price |\n"
            "|---|---|---|\n"
            "| Tacang | 100 | 1.50 |\n"
            "TOTAL RM 150.00\n"
            "Date: 20/05/2026\n"
        )
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["qty"], 100.0)
        self.assertEqual(result["items"][0]["price"], 1.50)
        # The bug would have saved 1.50; the fix keeps the real 150.
        self.assertEqual(result["total"], 150.0)
        # qty×price reconciles exactly -> no conflict penalty.
        self.assertEqual(result["confidence"], 100)


if __name__ == "__main__":
    unittest.main()
