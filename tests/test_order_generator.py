"""Order generator orchestration over a fake Supabase (order_generator)."""
import unittest
from datetime import date, timedelta

import order_generator
from tests.fake_supabase import FakeSupabase


class _Resp:
    def __init__(self, data):
        self.data = data


def _seed_item_prices(fake, rows):
    for r in rows:
        fake.table("item_prices").insert(r).execute()


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 7)
        self.fake = FakeSupabase()

    def _daily_ayam(self, outlet="SEK20", n=20, qty=10):
        return [{
            "outlet_code": outlet, "canonical_item": "ayam",
            "qty": qty, "unit_price": 9.0, "merchant": "BESTARI FARM",
            "raw_item_name": "AYAM", "receipt_date":
                (self.today - timedelta(days=i)).isoformat(),
        } for i in range(n)]

    def test_daily_item_produces_draft(self):
        _seed_item_prices(self.fake, self._daily_ayam())
        out = order_generator.gather_order_drafts(
            self.fake, today=self.today,
            display_for=lambda c: "SEK-20" if c == "SEK20" else c)
        self.assertTrue(out["has_data"])
        self.assertEqual(len(out["outlets"]), 1)
        o = out["outlets"][0]
        self.assertEqual(o["outlet_code"], "SEK20")
        self.assertIn("Ayam", o["message"])
        # drafts persisted
        drafts = self.fake.rows("order_drafts")
        self.assertTrue(any(d["item"] == "ayam" and d["status"] == "draft"
                            for d in drafts))
        # cadence snapshot persisted
        cad = self.fake.rows("item_cadence")
        self.assertTrue(any(c["item"] == "ayam" and c["cadence"] == "DAILY"
                            for c in cad))

    def test_excluded_items_not_drafted(self):
        rows = [{
            "outlet_code": "SEK20", "canonical_item": "transport",
            "qty": 1, "unit_price": 30.0, "merchant": "GRAB",
            "raw_item_name": "DELIVERY", "receipt_date":
                (self.today - timedelta(days=i)).isoformat(),
        } for i in range(20)]
        _seed_item_prices(self.fake, rows)
        out = order_generator.gather_order_drafts(self.fake, today=self.today)
        self.assertEqual(out["outlets"], [])

    def test_lookback_excludes_old_rows(self):
        old = [{
            "outlet_code": "SEK20", "canonical_item": "ayam",
            "qty": 10, "unit_price": 9.0, "merchant": "BESTARI FARM",
            "raw_item_name": "AYAM",
            "receipt_date": (self.today - timedelta(days=200 + i)).isoformat(),
        } for i in range(20)]
        _seed_item_prices(self.fake, old)
        out = order_generator.gather_order_drafts(self.fake, today=self.today)
        self.assertFalse(out["has_data"])

    def test_rerun_replaces_drafts(self):
        _seed_item_prices(self.fake, self._daily_ayam())
        order_generator.gather_order_drafts(self.fake, today=self.today)
        order_generator.gather_order_drafts(self.fake, today=self.today)
        ayam_drafts = [d for d in self.fake.rows("order_drafts")
                       if d["item"] == "ayam"]
        self.assertEqual(len(ayam_drafts), 1)  # not duplicated

    def test_spike_flag_set(self):
        rows = self._daily_ayam(n=10, qty=10)
        # latest receipt has a spiked unit price (most recent date).
        rows[0]["unit_price"] = 20.0
        _seed_item_prices(self.fake, rows)
        out = order_generator.gather_order_drafts(self.fake, today=self.today)
        drafts = self.fake.rows("order_drafts")
        ayam = next(d for d in drafts if d["item"] == "ayam")
        self.assertIn("PRICE_SPIKE", ayam["flags"])

    def test_dominant_supplier(self):
        recs = ([{"merchant": "A"}] * 3) + ([{"merchant": "B"}] * 5)
        self.assertEqual(order_generator.dominant_supplier(recs), "B")


if __name__ == "__main__":
    unittest.main()
