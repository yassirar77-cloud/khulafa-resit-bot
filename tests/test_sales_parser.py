"""Parser tests for PR #35 against the 10 REAL shift-close fixtures.

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
    EXPECTED,
    EXPECTED_BUSINESS_DATE,
    EXPECTED_GRAND_TOTAL,
    by_code,
    path_for_code,
)


def _parsed(code):
    return parse_shift_close(read_shift_close_file(path_for_code(code)))


class ParserTests(unittest.TestCase):
    def test_parses_all_10_outlets_without_error(self):
        self.assertEqual(len(EXPECTED), 10)
        grand = 0.0
        for e in EXPECTED:
            parsed = _parsed(e["code"])
            self.assertIsNotNone(parsed["total_sales"], e["code"])
            self.assertAlmostEqual(parsed["total_sales"], e["total"], places=2, msg=e["code"])
            self.assertEqual(parsed["shift_type"], "day", e["code"])
            self.assertEqual(str(parsed["shift_business_date"]), EXPECTED_BUSINESS_DATE, e["code"])
            grand += parsed["total_sales"]
        # The aggregate the rollout expects (RM44,000+).
        self.assertAlmostEqual(grand, EXPECTED_GRAND_TOTAL, places=2)
        self.assertGreater(grand, 44000)

    def test_extracts_total_sales_matches_expected_KLANG(self):
        self.assertAlmostEqual(_parsed("S-KLANG")["total_sales"], 4758.20, places=2)

    def test_extracts_total_sales_matches_expected_BISTRO7(self):
        self.assertAlmostEqual(_parsed("S-BISTRO7")["total_sales"], 6563.75, places=2)

    def test_handles_missing_deleted_section(self):
        # BISTRO7 has no DELETED ITEM BY ADMIN section.
        parsed = _parsed("S-BISTRO7")
        self.assertFalse(by_code("S-BISTRO7")["has_deleted"])
        self.assertEqual(parsed["deleted_items"], [])
        self.assertNotIn("deleted_items", parsed["sections_present"])

    def test_handles_missing_stock_section(self):
        # SEK14 has no STOCK section.
        parsed = _parsed("S-SEK14")
        self.assertFalse(by_code("S-SEK14")["has_stock"])
        self.assertEqual(parsed["stock"], [])
        self.assertNotIn("stock", parsed["sections_present"])

    def test_handles_missing_cashdrawer_section(self):
        # SEK14 and SEK20 have no CASHDRAWER OPEN section.
        for code in ("S-SEK14", "S-SEK20"):
            parsed = _parsed(code)
            self.assertFalse(by_code(code)["has_cashdrawer"], code)
            self.assertEqual(parsed["cashdrawer"], [], code)
            self.assertNotIn("cashdrawer", parsed["sections_present"], code)

    def test_handles_utf16_encoding(self):
        # Production attachments are UTF-16 with a BOM and CRLF (variance #3):
        # decode_shift_close_bytes must consume the BOM and normalise newlines.
        sample = "SHIFTNO : 1499\r\nTODAY SALES :   4,758.20\r\n"
        u16 = sample.encode("utf-16")  # encode() prepends the BOM
        self.assertIn(u16[:2], (b"\xff\xfe", b"\xfe\xff"))
        decoded = decode_shift_close_bytes(u16)
        self.assertFalse(decoded.startswith("﻿"))  # BOM stripped
        self.assertNotIn("\r", decoded)                  # CRLF normalised
        self.assertIn("TODAY SALES", decoded)
        # The real uploaded fixture (here UTF-8/CRLF) also decodes cleanly, and
        # the bytes path agrees with the file read.
        path = path_for_code("S-KLANG")
        content = read_shift_close_file(path)
        self.assertIn("SHIFTNO", content)
        self.assertNotIn("\r", content)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertEqual(decode_shift_close_bytes(raw), content)

    def test_handles_tax_present_BISTRO7(self):
        self.assertAlmostEqual(_parsed("S-BISTRO7")["tax"], 382.62, places=2)

    def test_handles_tax_absent_KLANG(self):
        self.assertAlmostEqual(_parsed("S-KLANG")["tax"], 0.00, places=2)

    def test_handles_negative_stock_values_KLANG(self):
        stock = {s["item"]: s["qty"] for s in _parsed("S-KLANG")["stock"]}
        # Real KLANG line: "Kacang 2.00            -1218  -2436.00"
        kacang = next((q for name, q in stock.items() if name.startswith("Kacang")), None)
        self.assertEqual(kacang, -1218)

    def test_extracts_items_with_quantities(self):
        # The GROUP WISE ITEM SALES (<shiftno>) block yields item-level qty/amount.
        items = _parsed("S-KLANG")["items"]
        self.assertGreater(len(items), 10)
        ayam = next((i for i in items if i["name"] == "AYAM"), None)
        self.assertIsNotNone(ayam)
        self.assertEqual(ayam["qty"], 168)
        self.assertAlmostEqual(ayam["amount"], 1389.50, places=2)


if __name__ == "__main__":
    unittest.main()
