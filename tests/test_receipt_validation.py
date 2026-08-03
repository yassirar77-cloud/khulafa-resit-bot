"""Unit tests for ``receipt_validation``.

Run with::

    python -m unittest tests.test_receipt_validation
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from receipt_validation import (  # noqa: E402
    SUBTOTAL_TOLERANCE,
    check_line_arithmetic,
    check_subtotal,
    format_subtotal_flag_message,
)


def line(total, qty=None, unit_price=None):
    return {"line_total": total, "qty": qty, "unit_price": unit_price}


class CheckSubtotal(unittest.TestCase):

    def test_exact_match_passes(self):
        result = check_subtotal([line(10.0), line(15.5)], 25.5)
        self.assertTrue(result["checked"])
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["line_sum"], 25.5)
        self.assertEqual(result["delta"], 0.0)

    def test_two_sen_gap_is_tolerated(self):
        self.assertFalse(check_subtotal([line(10.0), line(15.52)], 25.5)["needs_review"])

    def test_three_sen_gap_is_flagged(self):
        result = check_subtotal([line(10.0), line(15.53)], 25.5)
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["delta"], 0.03)

    def test_negative_gap_is_flagged_too(self):
        result = check_subtotal([line(10.0)], 25.5)
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["delta"], -15.5)

    def test_float_noise_does_not_flag(self):
        # 0.1 + 0.2 != 0.3 in binary floating point; rounding must absorb it.
        result = check_subtotal([line(0.1), line(0.2)], 0.3)
        self.assertFalse(result["needs_review"])

    def test_tolerance_is_two_sen(self):
        self.assertEqual(SUBTOTAL_TOLERANCE, 0.02)

    def test_custom_tolerance_is_honoured(self):
        rows = [line(10.0), line(15.6)]
        self.assertTrue(check_subtotal(rows, 25.5)["needs_review"])
        self.assertFalse(check_subtotal(rows, 25.5, tolerance=0.5)["needs_review"])

    def test_no_subtotal_means_unchecked_not_failed(self):
        result = check_subtotal([line(10.0)], None)
        self.assertFalse(result["checked"])
        self.assertFalse(result["needs_review"])
        self.assertIsNone(result["delta"])

    def test_no_numeric_line_totals_means_unchecked(self):
        result = check_subtotal([line(None), line("RM5")], 25.5)
        self.assertFalse(result["checked"])
        self.assertIsNone(result["line_sum"])
        self.assertEqual(result["missing_line_totals"], 2)

    def test_missing_line_totals_are_counted_but_do_not_block_the_check(self):
        result = check_subtotal([line(10.0), line(None), line(15.5)], 25.5)
        self.assertTrue(result["checked"])
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["missing_line_totals"], 1)

    def test_missing_line_makes_the_sum_short_and_flags(self):
        # The realistic OCR failure: a whole row was not read.
        result = check_subtotal([line(10.0), line(15.5)], 48.0)
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["delta"], -22.5)

    def test_bool_is_not_a_number(self):
        result = check_subtotal([line(True), line(10.0)], 10.0)
        self.assertEqual(result["missing_line_totals"], 1)
        self.assertFalse(result["needs_review"])

    def test_non_list_and_junk_entries_are_safe(self):
        self.assertFalse(check_subtotal(None, 10.0)["checked"])
        self.assertFalse(check_subtotal(["x", None, 5], 10.0)["checked"])


class CheckLineArithmetic(unittest.TestCase):

    def test_consistent_line_is_fine(self):
        self.assertFalse(check_line_arithmetic(line(30.0, qty=2, unit_price=15.0)))

    def test_rounding_within_tolerance_is_fine(self):
        self.assertFalse(check_line_arithmetic(line(30.02, qty=2, unit_price=15.0)))

    def test_inconsistent_line_is_flagged(self):
        self.assertTrue(check_line_arithmetic(line(35.0, qty=2, unit_price=15.0)))

    def test_missing_field_is_unverifiable_not_wrong(self):
        self.assertFalse(check_line_arithmetic(line(30.0, qty=None, unit_price=15.0)))
        self.assertFalse(check_line_arithmetic(line(None, qty=2, unit_price=15.0)))
        self.assertFalse(check_line_arithmetic(line(30.0, qty=2, unit_price=None)))

    def test_non_dict_is_safe(self):
        self.assertFalse(check_line_arithmetic(None))
        self.assertFalse(check_line_arithmetic("2 x 15"))


class FormatSubtotalFlagMessage(unittest.TestCase):

    def test_message_names_the_gap_and_the_invoice(self):
        result = check_subtotal([line(10.0), line(15.5)], 48.0)
        text = format_subtotal_flag_message(
            result,
            supplier="BESTARI FARM",
            invoice_no="INV-993",
            invoice_date="2026-08-02",
            outlet="Klang B.Emas",
            line_count=2,
        )
        self.assertIn("BESTARI FARM", text)
        self.assertIn("INV-993", text)
        self.assertIn("2026-08-02", text)
        self.assertIn("Klang B.Emas", text)
        self.assertIn("RM25.50", text)   # line sum
        self.assertIn("RM48.00", text)   # printed subtotal
        self.assertIn("RM-22.50", text)  # signed difference

    def test_missing_fields_render_as_dashes_not_none(self):
        result = check_subtotal([line(10.0)], 48.0)
        text = format_subtotal_flag_message(result, line_count=1)
        self.assertNotIn("None", text)
        self.assertIn("—", text)

    def test_missing_line_totals_are_called_out(self):
        result = check_subtotal([line(10.0), line(None)], 48.0)
        text = format_subtotal_flag_message(result, line_count=2)
        self.assertIn("1 baris tiada jumlah", text)

    def test_unchecked_result_still_renders(self):
        text = format_subtotal_flag_message(check_subtotal([], None), line_count=0)
        self.assertNotIn("None", text)


if __name__ == "__main__":
    unittest.main()
