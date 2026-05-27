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

    def test_strips_null_bytes_from_decoded_text(self):
        # NUL chars survive a correct UTF-16 decode (POS field padding) and
        # Postgres TEXT rejects U+0000 — it must be gone after decode + parse.
        raw = "SHIFTNO : 1\x00499\nTODAY SALES :  4,758.20\n".encode("utf-16")
        decoded = decode_shift_close_bytes(raw)
        self.assertNotIn("\x00", decoded)
        parsed = parse_shift_close(decoded)
        self.assertEqual(parsed["shift_no"], "1499")
        # No string field anywhere in the real KLANG parse retains a NUL byte.
        klang = parse_shift_close(read_shift_close_file(path_for_code("S-KLANG")))
        self.assertNotIn("\x00", repr(klang))

    def test_extracts_items_with_quantities(self):
        # The GROUP WISE ITEM SALES (<shiftno>) block yields item-level qty/amount.
        items = _parsed("S-KLANG")["items"]
        self.assertGreater(len(items), 10)
        ayam = next((i for i in items if i["name"] == "AYAM"), None)
        self.assertIsNotNone(ayam)
        self.assertEqual(ayam["qty"], 168)
        self.assertAlmostEqual(ayam["amount"], 1389.50, places=2)


class PaymentRoutingTests(unittest.TestCase):
    """PR #36: MOBILE CASH / cash labels route to payments; SALE auto-opens do
    not pollute the cashdrawer log."""

    def test_cashdrawer_only_captures_drawer_opens_not_sales(self):
        # No cashdrawer entry in ANY file is a transaction-trigger auto-open.
        for e in EXPECTED:
            for entry in _parsed(e["code"])["cashdrawer"]:
                self.assertNotRegex(entry["label"], r"(?i)\b(SALE|SPLIT)\b", e["code"])
        # BISTRO7 has real staff drawer-opens (e.g. SHEIK) — those ARE captured.
        bistro = _parsed("S-BISTRO7")["cashdrawer"]
        staff_opens = [c for c in bistro if c["label"] != "TOTAL TIMES OPEN"]
        self.assertGreaterEqual(len(staff_opens), 5)

    def test_mobile_cash_section_populates_payments_table(self):
        qr = [p for p in _parsed("S-KLANG")["payments"] if p["method"] == "qr_pay"]
        self.assertEqual(len(qr), 171)
        self.assertAlmostEqual(sum(p["amount"] for p in qr), 2907.40, places=2)
        # Every QR transaction carries its id + timestamp.
        self.assertTrue(all(p["transaction_id"] and p["transaction_at"] for p in qr))

    def test_qr_pay_label_value_creates_aggregate_payment_row(self):
        agg = [p for p in _parsed("S-KLANG")["payments"] if p["method"] == "qr_pay_total"]
        self.assertEqual(len(agg), 1)
        self.assertAlmostEqual(agg[0]["amount"], 2907.40, places=2)
        self.assertIsNone(agg[0]["transaction_id"])

    def test_cash_label_value_creates_aggregate_payment_row(self):
        # KLANG "NET CASH : 1,025.30" -> method='cash'.
        cash = [p for p in _parsed("S-KLANG")["payments"] if p["method"] == "cash"]
        self.assertEqual(len(cash), 1)
        self.assertAlmostEqual(cash[0]["amount"], 1025.30, places=2)

    def test_existing_sales_daily_unchanged(self):
        # The payment-routing change must not perturb the core shift fields.
        p = _parsed("S-KLANG")
        self.assertAlmostEqual(p["total_sales"], 4758.20, places=2)
        self.assertAlmostEqual(p["tax"], 0.00, places=2)
        self.assertEqual(p["shift_type"], "day")
        self.assertEqual(str(p["shift_business_date"]), "2026-05-25")
        self.assertEqual(p["shift_no"], "1499")


if __name__ == "__main__":
    unittest.main()
