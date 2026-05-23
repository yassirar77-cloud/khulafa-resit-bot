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
        # With the strict-whitelist rule, no supplier match -> UNKNOWN.
        r = classify_receipt(
            "SHELL DIESEL FLEET CARD ... 250.00",
            parsed_items=[],
            total=250.0,
        )
        self.assertNotEqual(r.receipt_type, ReceiptType.PETTY_CASH)


class StrictWhitelistTests(unittest.TestCase):
    """SUPPLIER_PURCHASE now requires a whitelist substring match.
    There is NO itemised-SKU fallback — new merchants stay UNKNOWN until
    manually added to the whitelist."""

    def test_unknown_merchant_with_wholesale_items_is_unknown(self):
        # Pre-hotfix: 2KG markers + total > RM50 would trigger
        # SUPPLIER_PURCHASE. Post-hotfix: UNKNOWN (no whitelist hit).
        r = classify_receipt(
            "GROCER X SDN BHD",
            parsed_items=[
                {"name": "BAWANG MERAH 2KG", "qty": 2, "price": 30.0},
                {"name": "GARAM 1PKT", "qty": 1, "price": 5.0},
            ],
            total=65.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)

    def test_dine_in_receipt_with_no_supplier_match_is_unknown(self):
        # Customer dine-in receipt with no whitelist match — silently
        # skipped, no crash, no misclassification.
        r = classify_receipt(
            "SOME RANDOM CAFE\nTeh O Ais\nNasi Goreng Ayam\nTOTAL 22.00",
            parsed_items=[
                {"name": "Teh O Ais", "qty": 1, "price": 3.0},
                {"name": "Nasi Goreng Ayam", "qty": 1, "price": 12.0},
            ],
            total=22.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)

    def test_all_whitelisted_suppliers_match(self):
        # Smoke test: every whitelist entry must classify as
        # SUPPLIER_PURCHASE when it appears in the merchant header.
        for supplier in [
            "BABAS", "SAIDA", "JASMINE", "MEWAH", "HANEE",
            "CAMELLIAA", "JY RESOURCES", "JUTA RIA", "BS FROZEN",
            "REZA", "BALAJI", "BESTARI", "FOOK LEONG", "DAILY PAY",
            "SHREE MAP", "QUIWAVE", "EVEREST", "MOON", "MYMOO",
        ]:
            with self.subTest(supplier=supplier):
                r = classify_receipt(
                    f"{supplier} TRADING SDN BHD\nItem A\nTOTAL 100",
                    parsed_items=[{"name": "Item A", "qty": 1, "price": 100.0}],
                    total=100.0,
                )
                self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)
                self.assertEqual(r.extracted_vendor, supplier)

    def test_bestari_substring_covers_farm_and_wholesale(self):
        for variant in ("BESTARI FARM SDN BHD", "BESTARI WHOLESALE TRADING"):
            with self.subTest(variant=variant):
                r = classify_receipt(
                    f"{variant}\nAYAM\nTOTAL 100",
                    parsed_items=[{"name": "AYAM", "qty": 1, "price": 100.0}],
                    total=100.0,
                )
                self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)
                self.assertEqual(r.extracted_vendor, "BESTARI")


class MymoonOcrVariantsTests(unittest.TestCase):
    """MYMOON'S KITCHEN is a real nasi lemak supplier — their deliveries
    must classify as SUPPLIER_PURCHASE.

    Two whitelist substrings cover 6 of the 10 observed OCR variants:
    "MOON" (canonical OON sequence preserved) and "MYMOO" (N replaced
    by another letter but the leading MYMOO prefix preserved). The
    remaining 4 (MYROOK / MTMOOK / MYNOOK / MYMCON) are too garbled to
    distinguish from arbitrary text via substring matching — they'd
    need fuzzy matching, deferred to a future PR.
    """

    # Caught by "MOON" — extracted_vendor will be "MOON" because MOON is
    # listed before MYMOO in SUPPLIER_WHITELIST and first match wins.
    CAUGHT_BY_MOON = [
        "MYMOON'S KITCHEN",
        "MTMOON'S KITCHEN",
        "MIMOON'S KITCHEN",
        "MY MOON'S KITCHEN",
    ]

    # Caught by "MYMOO" (N replaced by H or K, MYMOO prefix intact).
    CAUGHT_BY_MYMOO = [
        "MYMOOH'S KITCHEN",   # N -> H
        "MYMOOK'S KITCHEN",   # N -> K
    ]

    # Too garbled — no MOON or MYMOO substring. Classifies as UNKNOWN
    # until a future PR adds fuzzy matching or these substrings:
    NOT_CAUGHT = [
        "MYROOK'S KITCHEN",   # M -> R, N -> K  (no MYMOO, no MOON)
        "MTMOOK'S KITCHEN",   # Y -> T, N -> K
        "MYNOOK'S KITCHEN",   # second M -> N, N -> K
        "MYMCON'S KITCHEN",   # O -> C
    ]

    def _classify_header(self, header: str):
        return classify_receipt(
            f"{header}\nNasi Lemak Bungkus\nTOTAL 65.00",
            parsed_items=[
                {"name": "Nasi Lemak Bungkus", "qty": 10, "price": 6.5},
            ],
            total=65.0,
        )

    def test_moon_substring_catches_four_variants(self):
        for header in self.CAUGHT_BY_MOON:
            with self.subTest(header=header):
                r = self._classify_header(header)
                self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)
                self.assertEqual(r.extracted_vendor, "MOON")

    def test_mymoo_substring_catches_two_more_variants(self):
        for header in self.CAUGHT_BY_MYMOO:
            with self.subTest(header=header):
                r = self._classify_header(header)
                self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)
                self.assertEqual(r.extracted_vendor, "MYMOO")

    def test_total_coverage_is_six_out_of_ten(self):
        # Sanity check on the overall coverage so the numbers in the
        # docstring don't drift if the whitelist is edited.
        caught = self.CAUGHT_BY_MOON + self.CAUGHT_BY_MYMOO
        self.assertEqual(len(caught), 6)
        self.assertEqual(len(self.NOT_CAUGHT), 4)
        self.assertEqual(len(caught) + len(self.NOT_CAUGHT), 10)

    def test_uncaught_variants_currently_fall_through_to_unknown(self):
        # Documents the known gap. If any of these variants appears
        # often in logs, add a new substring to SUPPLIER_WHITELIST.
        for header in self.NOT_CAUGHT:
            with self.subTest(header=header):
                r = self._classify_header(header)
                self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)


class OwnOutletAndProductionMerchantTests(unittest.TestCase):
    """Spot-checks for merchants seen in production logs, including
    Khulafa's own outlets (which must NOT be classified as suppliers)."""

    def test_aikhalman_enterprise_falls_through_to_unknown(self):
        # Not in the whitelist — one-off RM50 receipt from May 2025 per
        # the production data review. Stays UNKNOWN until a human
        # confirms whether to add to the whitelist.
        r = classify_receipt(
            "AIKHALMAN ENTERPRISE\nSomething\nTOTAL 80",
            parsed_items=[{"name": "Something", "qty": 1, "price": 80.0}],
            total=80.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)

    def test_everest_aisvaram_is_supplier(self):
        # Confirmed ice supplier — matches via "EVEREST" substring.
        r = classify_receipt(
            "EVEREST AISVARAM SDN BHD\nAIS BATU 10KG\nTOTAL 120",
            parsed_items=[{"name": "AIS BATU 10KG", "qty": 1, "price": 120.0}],
            total=120.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.SUPPLIER_PURCHASE)
        self.assertEqual(r.extracted_vendor, "EVEREST")

    def test_restoran_hj_sharfuddin_with_payout_is_staff_advance(self):
        # Own outlet header with a PAYOUT body — STAFF_ADVANCE wins on
        # priority order, merchant name doesn't matter.
        r = classify_receipt(
            "RESTORAN HJ SHARFUDDIN\nPAYOUT\nTO PINJAM TO DINA\nBY CASH 500.00",
            parsed_items=[{"name": "Payout", "qty": 1, "price": 500.0}],
            total=500.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.STAFF_ADVANCE)
        self.assertEqual(r.extracted_staff_name, "Dina")

    def test_khulafa_bistro_with_items_is_unknown(self):
        # Khulafa's own outlet — NOT a supplier. No whitelist hit,
        # no PAYOUT/UTILITY/etc., so falls to UNKNOWN. Future PR will
        # add INTERNAL_RECEIPT type for own outlets.
        r = classify_receipt(
            "KHULAFA BISTRO SEK-6\nNasi Briyani Ayam\nTeh Tarik\nTOTAL 18.00",
            parsed_items=[
                {"name": "Nasi Briyani Ayam", "qty": 1, "price": 15.0},
                {"name": "Teh Tarik", "qty": 1, "price": 3.0},
            ],
            total=18.0,
        )
        self.assertEqual(r.receipt_type, ReceiptType.UNKNOWN)

    def test_restoran_khulafa_with_items_is_unknown(self):
        # Another own-outlet variant — must not match RESTORAN as a
        # customer-order keyword (that approach was rejected) and must
        # not match any supplier whitelist entry.
        r = classify_receipt(
            "RESTORAN KHULAFA\nMaggi Goreng\nKopi Ais\nTOTAL 12.00",
            parsed_items=[
                {"name": "Maggi Goreng", "qty": 1, "price": 8.0},
                {"name": "Kopi Ais", "qty": 1, "price": 3.0},
            ],
            total=12.0,
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
