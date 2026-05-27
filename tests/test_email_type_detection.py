"""Email-type detection tests for PR #60 (S- vs D- subject routing).

Run with::

    python -m unittest tests.test_email_type_detection
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_email_fetcher import detect_email_type  # noqa: E402


class EmailTypeDetectionTests(unittest.TestCase):
    def test_detects_d_prefix_uppercase(self):
        self.assertEqual(detect_email_type("D-SEK20 ON 26/May/2026 00:09:19"), ("D", "D-SEK20"))

    def test_detects_d_prefix_lowercase_damansara(self):
        self.assertEqual(detect_email_type("D-Damansara ON 26/May/2026"), ("D", "D-DAMANSARA"))

    def test_detects_s_prefix_unchanged(self):
        self.assertEqual(
            detect_email_type("S-KLANG SHIFTCLOSE (1501) ON 26/May/2026 19:00:04"),
            ("S", "S-KLANG"),
        )

    def test_no_match_for_arbitrary_subject(self):
        self.assertIsNone(detect_email_type("Re: lunch order"))
        self.assertIsNone(detect_email_type(""))
        self.assertIsNone(detect_email_type(None))

    def test_detects_multi_word_d_code(self):
        self.assertEqual(detect_email_type("D-ST KHU ON 26/May/2026 00:09:19"), ("D", "D-ST KHU"))


if __name__ == "__main__":
    unittest.main()
