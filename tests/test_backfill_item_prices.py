"""Hermetic tests for scripts/backfill_item_prices over a fake Supabase.

Covers the requirements: reads only corrected (SUPPLIER_PURCHASE) headers,
windows by receipt_date, mirrors the live extraction, and is idempotent
(dedup on (receipt_id, canonical_item)).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import backfill_item_prices as bip  # noqa: E402
from tests.fake_supabase import FakeSupabase  # noqa: E402


def _receipt(**over):
    base = {
        "merchant": "DIAMOND BALL ENTERPRISE",
        "receipt_date": "2026-05-25",
        "outlet": "Bistro",
        "chat_id": 111,
        "items": [{"name": "ROTI", "qty": 6, "price": 5.0},
                  {"name": "CAPATI", "qty": 1, "price": 3.0}],
        "receipt_type": "SUPPLIER_PURCHASE",
        "raw_text": "DIAMOND BALL ENTERPRISE ROTI CAPATI",
        "total": 33.0,
    }
    base.update(over)
    return base


class BackfillItemPricesTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()

    def _seed(self, receipts):
        for r in receipts:
            self.fake.table("receipts").insert(r).execute()

    def test_aggregates_only_supplier_in_window_and_is_idempotent(self):
        self._seed([
            _receipt(),  # eligible Diamond Ball
            _receipt(merchant="RANDOM SHOP", receipt_type="UNKNOWN",
                     raw_text="RANDOM SHOP THING"),  # not supplier -> skip
            _receipt(receipt_date="2026-04-01"),  # before window -> skip
        ])
        bip.run(self.fake, since="2026-05-22", apply=True)
        rows = self.fake.rows("item_prices")
        cats = sorted(r["canonical_item"] for r in rows)
        self.assertEqual(cats, ["capati", "roti"])           # only the eligible receipt
        self.assertTrue(all(r["outlet_code"] == "BISTRO7" for r in rows))
        self.assertTrue(all(r["merchant"] == "DIAMOND BALL ENTERPRISE" for r in rows))

        # Idempotent: a second apply inserts nothing new (dedup on receipt+item).
        bip.run(self.fake, since="2026-05-22", apply=True)
        self.assertEqual(len(self.fake.rows("item_prices")), 2)

    def test_dry_run_writes_nothing(self):
        self._seed([_receipt()])
        bip.run(self.fake, since="2026-05-22", apply=False)
        self.assertEqual(self.fake.rows("item_prices"), [])

    def test_does_not_redo_already_present_lines(self):
        # A receipt whose roti line is already in item_prices: only the missing
        # capati line is added.
        self._seed([_receipt()])
        rcpt = self.fake.rows("receipts")[0]
        self.fake.table("item_prices").insert({
            "receipt_id": rcpt["id"], "canonical_item": "roti",
            "merchant": "DIAMOND BALL ENTERPRISE", "qty": 6,
        }).execute()
        bip.run(self.fake, since="2026-05-22", apply=True)
        cats = sorted(r["canonical_item"] for r in self.fake.rows("item_prices"))
        self.assertEqual(cats, ["capati", "roti"])  # roti not duplicated


if __name__ == "__main__":
    unittest.main()
