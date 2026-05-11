"""Unit tests for ``audit_messages.build_big_purchase_message``.

Run with::

    python -m unittest tests.test_audit_messages
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit_messages import build_big_purchase_message  # noqa: E402


class Tier1QuestionOnlyTests(unittest.TestCase):
    """sample_size <= 2: question-only, no average shown.

    Reserved for future use — currently gated by the ``< 3`` guard in
    ``_check_big_purchase`` so this branch does not fire today. The
    tests still pin the format so the path is safe to enable later.
    """

    def test_sample_size_zero_uses_tier_1(self):
        msg = build_big_purchase_message(500.00, 0.0, 0)
        self.assertIn("Belian besar hari ni RM500.00", msg)
        self.assertNotIn("purata", msg)
        self.assertIn("Stok habis", msg)

    def test_sample_size_one_uses_tier_1(self):
        msg = build_big_purchase_message(300.00, 100.00, 1)
        self.assertIn("RM300.00", msg)
        self.assertNotIn("purata", msg)
        self.assertNotIn("data masih sikit", msg)

    def test_sample_size_two_uses_tier_1(self):
        msg = build_big_purchase_message(450.50, 150.00, 2)
        self.assertIn("RM450.50", msg)
        self.assertNotIn("purata", msg)


class Tier2DisclaimerTests(unittest.TestCase):
    """sample_size 3-4: average shown with low-confidence disclaimer."""

    def test_sample_size_three_uses_tier_2(self):
        msg = build_big_purchase_message(500.00, 200.00, 3)
        self.assertIn("purata RM200.00 dari 3 receipt sebelum", msg)
        self.assertIn("data masih sikit", msg)
        self.assertIn("Hari ni RM500.00", msg)

    def test_sample_size_four_uses_tier_2(self):
        msg = build_big_purchase_message(800.00, 250.00, 4)
        self.assertIn("purata RM250.00 dari 4 receipt sebelum", msg)
        self.assertIn("data masih sikit", msg)
        self.assertIn("Hari ni RM800.00", msg)


class Tier3ConfidentTests(unittest.TestCase):
    """sample_size >= 5: confident 14-day average (original format)."""

    def test_sample_size_five_uses_tier_3(self):
        msg = build_big_purchase_message(600.00, 220.00, 5)
        self.assertIn("purata 14 hari RM220.00", msg)
        self.assertIn("hari ni RM600.00", msg)
        self.assertNotIn("data masih sikit", msg)
        self.assertNotIn("receipt sebelum", msg)

    def test_sample_size_ten_uses_tier_3(self):
        msg = build_big_purchase_message(1000.00, 300.00, 10)
        self.assertIn("purata 14 hari RM300.00", msg)
        self.assertNotIn("data masih sikit", msg)

    def test_sample_size_hundred_uses_tier_3(self):
        msg = build_big_purchase_message(2000.00, 500.00, 100)
        self.assertIn("purata 14 hari RM500.00", msg)
        self.assertNotIn("data masih sikit", msg)


class FormattingTests(unittest.TestCase):
    """Money formatting and shared message conventions."""

    def test_amounts_formatted_to_two_decimals(self):
        msg = build_big_purchase_message(221.4700, 100.4700, 7)
        self.assertIn("RM100.47", msg)
        self.assertIn("RM221.47", msg)
        self.assertNotIn("221.4700", msg)

    def test_tamil_prefix_present_on_all_tiers(self):
        prefix = "வாங்கினது அதிகம்"
        self.assertIn(prefix, build_big_purchase_message(300.0, 0.0, 1))
        self.assertIn(prefix, build_big_purchase_message(500.0, 200.0, 3))
        self.assertIn(prefix, build_big_purchase_message(600.0, 220.0, 5))


if __name__ == "__main__":
    unittest.main()
