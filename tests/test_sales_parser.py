"""Parser tests for PR #35 against all 10 shift-close fixtures.

Run with::

    python -m unittest tests.test_sales_parser
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_parser import (  # noqa: E402
    decode_shift_close_bytes,
    parse_shift_close,
    read_shift_close_file,
)
from tests.sales_fixtures import (  # noqa: E402
    EXPECTED_GRAND_TOTAL,
    FIXTURE_DIR,
    FIXTURES,
    by_code,
    write_all,
)


def setUpModule():
    if not os.path.isdir(FIXTURE_DIR) or not os.listdir(FIXTURE_DIR):
        write_all()


def _path(code):
    return os.path.join(FIXTURE_DIR, by_code(code)["filename"])


def _parsed(code):
    return parse_shift_close(read_shift_close_file(_path(code)))


class ParserTests(unittest.TestCase):
    def test_parses_all_10_outlets_without_error(self):
        self.assertEqual(len(FIXTURES), 10)
        for f in FIXTURES:
            parsed = parse_shift_close(read_shift_close_file(os.path.join(FIXTURE_DIR, f["filename"])))
            self.assertIsNotNone(parsed["total_sales"], f["code"])
            self.assertAlmostEqual(parsed["total_sales"], f["total"], places=2, msg=f["code"])
        # Sanity: the aggregate the rollout expects (RM44,000+).
        self.assertGreater(EXPECTED_GRAND_TOTAL, 44000)

    def test_extracts_total_sales_matches_expected_KLANG(self):
        self.assertAlmostEqual(_parsed("S-KLANG")["total_sales"], 4758.20, places=2)

    def test_extracts_total_sales_matches_expected_BISTRO7(self):
        self.assertAlmostEqual(_parsed("S-BISTRO7")["total_sales"], 6563.75, places=2)

    def test_handles_missing_deleted_section(self):
        parsed = _parsed("S-BISTRO7")
        self.assertEqual(parsed["deleted_items"], [])
        self.assertNotIn("deleted_items", parsed["sections_present"])

    def test_handles_missing_stock_section(self):
        parsed = _parsed("S-SEK14")
        self.assertEqual(parsed["stock"], [])
        self.assertNotIn("stock", parsed["sections_present"])

    def test_handles_missing_cashdrawer_section(self):
        for code in ("S-SEK14", "S-SEK20"):
            parsed = _parsed(code)
            self.assertEqual(parsed["cashdrawer"], [], code)
            self.assertNotIn("cashdrawer", parsed["sections_present"], code)

    def test_handles_utf16_encoding(self):
        with open(_path("S-KLANG"), "rb") as fh:
            raw = fh.read()
        # Real POS files: UTF-16 with a BOM.
        self.assertIn(raw[:2], (b"\xff\xfe", b"\xfe\xff"))
        content = read_shift_close_file(_path("S-KLANG"))
        self.assertFalse(content.startswith("﻿"))  # BOM stripped
        self.assertNotIn("\r", content)                  # CRLF normalised
        self.assertIn("SHIFT CLOSE REPORT", content)
        # decode_shift_close_bytes path (attachment bytes) agrees with the file read.
        self.assertEqual(decode_shift_close_bytes(raw), content)

    def test_handles_tax_present_BISTRO7(self):
        self.assertAlmostEqual(_parsed("S-BISTRO7")["tax"], 382.62, places=2)

    def test_handles_tax_absent_KLANG(self):
        self.assertAlmostEqual(_parsed("S-KLANG")["tax"], 0.00, places=2)

    def test_handles_negative_stock_values_KLANG(self):
        stock = {s["item"]: s["qty"] for s in _parsed("S-KLANG")["stock"]}
        self.assertIn("Kacang", stock)
        self.assertEqual(stock["Kacang"], -1218)


if __name__ == "__main__":
    unittest.main()
