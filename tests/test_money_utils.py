"""Unit tests for ``money_utils.normalize_total``.

Run with::

    python -m unittest tests.test_money_utils
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from money_utils import normalize_total  # noqa: E402


class NormalizeTotalCurrencyPrefixes(unittest.TestCase):
    def test_rm_prefix(self):
        self.assertEqual(normalize_total("RM13.00"), 13.00)

    def test_rm_prefix_with_space(self):
        self.assertEqual(normalize_total("RM 13.00"), 13.00)

    def test_myr_prefix(self):
        self.assertEqual(normalize_total("MYR13.00"), 13.00)

    def test_dollar_prefix(self):
        self.assertEqual(normalize_total("$13.00"), 13.00)

    def test_rm_prefix_lowercase(self):
        self.assertEqual(normalize_total("rm13.00"), 13.00)


class NormalizeTotalThousandSeparators(unittest.TestCase):
    def test_rm_with_thousand_separator(self):
        self.assertEqual(normalize_total("RM 1,234.50"), 1234.50)

    def test_thousand_separator_no_currency(self):
        self.assertEqual(normalize_total("1,234.50"), 1234.50)

    def test_multiple_thousand_separators(self):
        self.assertEqual(normalize_total("1,234,567.89"), 1234567.89)


class NormalizeTotalWhitespace(unittest.TestCase):
    def test_leading_and_trailing_whitespace(self):
        self.assertEqual(normalize_total(" 13.00 "), 13.00)

    def test_internal_whitespace_around_currency(self):
        self.assertEqual(normalize_total("  RM  13.00  "), 13.00)


class NormalizeTotalPlainNumbers(unittest.TestCase):
    def test_plain_string_decimal(self):
        self.assertEqual(normalize_total("13.0"), 13.0)

    def test_int_input(self):
        self.assertEqual(normalize_total(13), 13.0)
        self.assertIsInstance(normalize_total(13), float)

    def test_float_input(self):
        self.assertEqual(normalize_total(13.50), 13.50)

    def test_zero(self):
        self.assertEqual(normalize_total(0), 0.0)

    def test_zero_string(self):
        self.assertEqual(normalize_total("0"), 0.0)


class NormalizeTotalEdgeCases(unittest.TestCase):
    def test_negative_with_currency(self):
        self.assertEqual(normalize_total("RM-13.00"), -13.00)

    def test_negative_plain(self):
        self.assertEqual(normalize_total("-13.00"), -13.00)

    def test_no_decimal_with_currency(self):
        self.assertEqual(normalize_total("RM 100"), 100.0)
        self.assertIsInstance(normalize_total("RM 100"), float)

    def test_trailing_currency(self):
        self.assertEqual(normalize_total("13.00 MYR"), 13.00)

    def test_trailing_currency_rm(self):
        self.assertEqual(normalize_total("13.00 RM"), 13.00)


class NormalizeTotalRejectsInvalid(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(normalize_total(None))

    def test_empty_string(self):
        self.assertIsNone(normalize_total(""))

    def test_whitespace_only(self):
        self.assertIsNone(normalize_total("   "))

    def test_garbage_text(self):
        self.assertIsNone(normalize_total("abc"))

    def test_malformed_two_decimals(self):
        self.assertIsNone(normalize_total("13.00.50"))

    def test_just_currency_prefix_no_number(self):
        self.assertIsNone(normalize_total("RM"))

    def test_just_currency_prefix_with_space(self):
        self.assertIsNone(normalize_total("RM "))

    def test_just_myr(self):
        self.assertIsNone(normalize_total("MYR"))

    def test_list_input(self):
        self.assertIsNone(normalize_total([13.00]))

    def test_dict_input(self):
        self.assertIsNone(normalize_total({"total": 13.00}))

    def test_bool_true_rejected(self):
        # bool is a subclass of int; we don't want True silently becoming 1.0.
        self.assertIsNone(normalize_total(True))

    def test_bool_false_rejected(self):
        self.assertIsNone(normalize_total(False))

    def test_currency_with_garbage(self):
        self.assertIsNone(normalize_total("RM abc"))


if __name__ == "__main__":
    unittest.main()
