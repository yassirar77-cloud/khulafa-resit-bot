"""Outlet-identification tests for PR #35.

Identity comes from the email SUBJECT, never the TXT header. Unknown/unconfirmed
codes log and continue rather than crashing the batch.

Run with::

    python -m unittest tests.test_sales_outlet_identification
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_parser import (  # noqa: E402
    canonical_outlet_for_code,
    extract_outlet_from_subject,
    parse_shift_close,
    read_shift_close_file,
)
from tests.sales_fixtures import path_for_code  # noqa: E402


class OutletIdentificationTests(unittest.TestCase):
    def test_identifies_outlet_from_subject_not_header(self):
        # The SEK6 report's header is "NASI KANDAR HAJI SHARFUDDIN" — the SAME
        # header KLANG uses, which is exactly why the header can't identify the
        # outlet. Delivered under an S-KLANG subject, identity must be KLANG.
        content = read_shift_close_file(path_for_code("S-SEK6"))
        subject = "S-KLANG SHIFTCLOSE (1499)"
        code = extract_outlet_from_subject(subject)
        self.assertEqual(code, "S-KLANG")
        self.assertEqual(canonical_outlet_for_code(code), "Klang B.Emas")
        parsed = parse_shift_close(content)
        self.assertIn("SHARFUDDIN", (parsed["header_outlet_raw"] or "").upper())
        self.assertNotEqual(parsed["header_outlet_raw"], "Klang B.Emas")

    def test_unknown_outlet_code_returns_none_and_warns(self):
        # A code not in the in-code map resolves to None with a warning (the
        # ingestion gate then records it as skipped_unknown — see ingestion tests).
        with self.assertLogs("sales_parser", level="WARNING") as cm:
            self.assertIsNone(canonical_outlet_for_code("S-FOO"))
        self.assertTrue(any("S-FOO" in m for m in cm.output))

    def test_extracts_multi_word_outlet_code(self):
        # Codes may contain a space (e.g. "S-ST KHU"); the old S-\w+ regex
        # returned NULL for those. Internal whitespace is collapsed + uppercased.
        cases = {
            "S-BISTRO7  SHIFTCLOSE (1342)": "S-BISTRO7",
            "S-VISTA  SHIFTCLOSE (2833)": "S-VISTA",
            "S-ST KHU  SHIFTCLOSE (860)": "S-ST KHU",
            "S-MB  SHIFTCLOSE (660)": "S-MB",
            "S-Damansara  SHIFTCLOSE (2386)": "S-DAMANSARA",
        }
        for subject, expected in cases.items():
            self.assertEqual(extract_outlet_from_subject(subject), expected, subject)

    def test_outlet_S_ST_KHU_maps_to_placeholder(self):
        with self.assertLogs("sales_parser", level="WARNING"):
            self.assertEqual(canonical_outlet_for_code("S-ST KHU"), "ST Khulafa")

    def test_outlet_S_MB_maps_to_placeholder(self):
        with self.assertLogs("sales_parser", level="WARNING"):
            self.assertEqual(canonical_outlet_for_code("S-MB"), "MB")

    def test_outlet_S_KLANG_maps_to_Klang_BEmas(self):
        self.assertEqual(canonical_outlet_for_code("S-KLANG"), "Klang B.Emas")

    def test_outlet_S_SEK14_maps_to_Signature(self):
        self.assertEqual(canonical_outlet_for_code("S-SEK14"), "Signature")

    def test_outlet_SBESI_logs_warning_uses_canonical(self):
        with self.assertLogs("sales_parser", level="WARNING") as cm:
            canonical = canonical_outlet_for_code("S-SBESI")
        self.assertEqual(canonical, "SBESI")
        self.assertTrue(any("UNCONFIRMED" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
