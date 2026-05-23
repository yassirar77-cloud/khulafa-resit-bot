"""Unit tests for ``receipt_classifier`` (PR #24).

Covers every test-case row from the PR brief plus the priority-order
edge cases. Hermetic — no Supabase, no Telegram.

Run with::

    python -m unittest tests.test_receipt_classifier
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from receipt_classifier import (  # noqa: E402
    ClassificationResult,
    ReceiptType,
    classify_receipt,
    extract_issued_by,
    extract_staff_name,
)


class ExtractStaffNameTests(unittest.TestCase):
    def test_to_pinjam_to_pattern(self):
        self.assertEqual(extract_staff_name("TO PINJAM TO DINA"), "Dina")

    def test_pinjam_name(self):
        self.assertEqual(extract_staff_name("PINJAM SITI 150"), "Siti")

    def test_advance_name(self):
        self.assertEqual(extract_staff_name("ADVANCE KUMAR 200.00"), "Kumar")

    def test_payout_to_name(self):
        # PAYOUT then TO NAME with stuff in between, as in real Khulafa receipts.
        text = "PAYOUT\nTO PINJAM\nTO DINA\nBY CASH"
        self.assertEqual(extract_staff_name(text), "Dina")

    def test_no_match(self):
        self.assertIsNone(extract_staff_name("BABAS ENTERPRISE JINTAN 22.00"))

    def test_stopwords_rejected(self):
        # `TO CASH` should NOT yield "Cash".
        self.assertIsNone(extract_staff_name("PAYOUT TO CASH"))

    def test_empty_input(self):
        self.assertIsNone(extract_staff_name(""))
        self.assertIsNone(extract_staff_name(None))  # type: ignore[arg-type]


class ExtractIssuedByTests(unittest.TestCase):
    def test_admin_issued_by(self):
        self.assertEqual(
            extract_issued_by("ADMIN ISSUED BY ARIFFIN\nBY CASH"),
            "Ariffin",
        )

    def test_no_match(self):
        self.assertIsNone(extract_issued_by("BABAS JINTAN 22.00"))


class BriefTestCases(unittest.TestCase):
    """The exact 10 test-case rows from the PR brief — must all pass."""

    def test_dina_payout_pinjam(self):
        r = classify_receipt(
            "NASI KANDAR HAJI SHARFUDDIN ... PAYOUT ... TO PINJAM TO DINA ... BY CASH 500.00",
            parsed_items=[{"name": "Payout", "qty": 1, "price": 500.0}],
            total=500.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.STAFF_ADVANCE)
        self.assertEqual(r.extracted_staff_name, "Dina")

    def test_babas_supplier_purchase(self):
        r = classify_receipt(
            "BABAS ENTERPRISE ... JINTAN PUTIH 1KG ... 22.00",
            parsed_items=[{"name": "JINTAN PUTIH 1KG", "qty": 1, "price": 22.0}],
            total=22.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)
        self.assertEqual(r.extracted_vendor, "BABAS")

    def test_tnb_utility(self):
        r = classify_receipt(
            "TENAGA NASIONAL BERHAD ... INVOIS ELEKTRIK ... 1,234.50",
            parsed_items=[],
            total=1234.50,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UTILITY)

    def test_mbsa_rent_license(self):
        r = classify_receipt(
            "MBSA LESEN PREMIS MAKANAN 2026 ... 450.00",
            parsed_items=[],
            total=450.00,
        )
        self.assertEqual(r.receipt_type, ReceiptType.RENT_LICENSE)

    def test_shell_petty_cash(self):
        r = classify_receipt(
            "SHELL SEKSYEN 7 ... PETROL ... RM50.00",
            parsed_items=[{"name": "PETROL", "qty": 1, "price": 50.0}],
            total=50.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.PETTY_CASH)

    def test_random_shop_unknown(self):
        r = classify_receipt(
            "RANDOM SHOP ... THING ... RM30.00",
            parsed_items=[{"name": "THING", "qty": 1, "price": 30.0}],
            total=30.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)

    def test_advance_kumar(self):
        r = classify_receipt(
            "ADVANCE KUMAR ... 200.00",
            parsed_items=[],
            total=200.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.STAFF_ADVANCE)
        self.assertEqual(r.extracted_staff_name, "Kumar")

    def test_pinjam_siti(self):
        r = classify_receipt(
            "PINJAM SITI 150",
            parsed_items=[],
            total=150.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.STAFF_ADVANCE)
        self.assertEqual(r.extracted_staff_name, "Siti")

    def test_kwsp_rent_license(self):
        r = classify_receipt(
            "KWSP CARUMAN MEI 2026 ... 3,450.00",
            parsed_items=[],
            total=3450.00,
        )
        self.assertEqual(r.receipt_type, ReceiptType.RENT_LICENSE)

    def test_saida_supplier_purchase(self):
        r = classify_receipt(
            "SAIDA SPICES ... LADA HITAM 500G ... 35.00",
            parsed_items=[{"name": "LADA HITAM 500G", "qty": 1, "price": 35.0}],
            total=35.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)
        self.assertEqual(r.extracted_vendor, "SAIDA")


class PriorityOrderTests(unittest.TestCase):
    """STAFF_ADVANCE comes first because POS prints PAYOUT as a SKU."""

    def test_staff_advance_beats_supplier(self):
        # PAYOUT keyword on a receipt that also mentions a whitelisted supplier
        # should still classify as STAFF_ADVANCE (priority order).
        r = classify_receipt(
            "BABAS PAYOUT TO PINJAM TO DINA BY CASH 500",
            parsed_items=[{"name": "Payout", "qty": 1, "price": 500.0}],
            total=500.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.STAFF_ADVANCE)
        self.assertEqual(r.extracted_staff_name, "Dina")

    def test_utility_beats_supplier(self):
        # TNB header with BABAS appearing in raw_text should still be UTILITY.
        r = classify_receipt(
            "TNB TENAGA NASIONAL ... BABAS RESIT ... 1200.00",
            parsed_items=[],
            total=1200.00,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UTILITY)

    def test_petty_cash_requires_low_total(self):
        # SHELL keyword but RM250 total -> NOT PETTY_CASH (over the 200 cap).
        # Falls through to itemised heuristic (which won't match without unit
        # tokens + total>50) -> UNKNOWN.
        r = classify_receipt(
            "SHELL DIESEL FLEET CARD ... 250.00",
            parsed_items=[],
            total=250.0,
        )
        self.assertNotEqual(r.receipt_type, ReceiptType.PETTY_CASH)


class ItemisedFallbackTests(unittest.TestCase):
    def test_itemised_with_high_total_triggers_supplier(self):
        # No whitelisted supplier, but itemised SKUs with kg/pcs units and
        # total > RM50 should classify as SUPPLIER_PURCHASE.
        r = classify_receipt(
            "GROCER X SDN BHD",
            parsed_items=[
                {"name": "BAWANG MERAH 2KG", "qty": 2, "price": 30.0},
                {"name": "GARAM 1PKT", "qty": 1, "price": 5.0},
            ],
            total=65.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)

    def test_itemised_low_total_is_unknown(self):
        # Itemised but RM30 total -> below threshold -> UNKNOWN.
        r = classify_receipt(
            "MINI SHOP",
            parsed_items=[{"name": "BISKUT 1PKT", "qty": 1, "price": 30.0}],
            total=30.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)


class StaffAdvanceNameMissingTests(unittest.TestCase):
    def test_payout_without_name_still_classifies(self):
        # PAYOUT receipt where we can't extract a name should still be
        # STAFF_ADVANCE — the brief says store staff_name=NULL and prompt
        # manager later.
        r = classify_receipt(
            "PAYOUT BY CASH 300",
            parsed_items=[{"name": "Payout", "qty": 1, "price": 300.0}],
            total=300.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.STAFF_ADVANCE)
        self.assertIsNone(r.extracted_staff_name)
        # Confidence is lower when name extraction failed.
        self.assertLess(r.confidence, 0.90)


class ResultShapeTests(unittest.TestCase):
    def test_result_is_dataclass(self):
        r = classify_receipt("", [], 0.0)
        self.assertIsInstance(r, ClassificationResult)
        self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)
        self.assertEqual(r.matched_keywords, [])

    def test_receipt_type_is_str_enum(self):
        # Important: must be usable as a plain string for DB inserts.
        self.assertEqual(ReceiptType.STAFF_ADVANCE.value, "STAFF_ADVANCE")
        self.assertEqual(str(ReceiptType.STAFF_ADVANCE), "ReceiptType.STAFF_ADVANCE")
        self.assertEqual(ReceiptType.STAFF_ADVANCE, "STAFF_ADVANCE")


if __name__ == "__main__":
    unittest.main()
