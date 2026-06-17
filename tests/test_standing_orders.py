"""Standing orders: pure builder + clean formatting + generator integration."""
import unittest
from datetime import date, timedelta

import order_draft
import order_generator
import standing_orders
from tests.fake_supabase import FakeSupabase


class GroupAndBuildTests(unittest.TestCase):
    def test_group_drops_inactive_and_bad_rows(self):
        rows = standing_orders.group_by_outlet([
            {"outlet": "SEK6", "item": "ROTI", "default_qty": 6, "unit": "pack",
             "supplier": "DIAMOND BALL", "cadence": "DAILY"},
            {"outlet": "SEK6", "item": "gas", "default_qty": 0},   # non-positive -> drop
            {"outlet": "", "item": "roti", "default_qty": 3},      # no outlet -> drop
            {"outlet": "VISTA", "item": "", "default_qty": 3},     # no item -> drop
        ])
        self.assertEqual(set(rows), {"SEK6"})
        self.assertEqual(len(rows["SEK6"]), 1)
        self.assertEqual(rows["SEK6"][0]["item"], "roti")  # lower-cased

    def test_fetch_filters_active_false_only(self):
        fake = FakeSupabase()
        for r in [
            {"outlet": "SEK6", "item": "roti", "default_qty": 6, "active": True},
            {"outlet": "SEK6", "item": "gas", "default_qty": 2, "active": False},
            {"outlet": "VISTA", "item": "capati", "default_qty": 4},  # null active -> kept
        ]:
            fake.table("standing_orders").insert(r).execute()
        rows = standing_orders.fetch_standing_orders(fake)
        items = sorted(r["item"] for r in rows)
        self.assertEqual(items, ["capati", "roti"])

    def test_build_line_is_clean(self):
        line = standing_orders.build_standing_line(
            {"item": "roti", "default_qty": 6, "unit": "pack",
             "supplier": "DIAMOND BALL", "cadence": "DAILY"})
        self.assertTrue(line["standing"])
        self.assertTrue(line["pack_known"])
        self.assertFalse(line["cadence_info"]["needs_review"])
        txt = order_draft.format_item_line(line)
        self.assertIn("Roti — 6 pack", txt)
        self.assertIn("standing order", txt)
        self.assertIn("DIAMOND BALL", txt)
        # None of the OCR-path noise:
        self.assertNotIn("NEEDS REVIEW", txt)
        self.assertNotIn("confirm pack size", txt)
        self.assertNotIn("cadence:", txt)
        # Compact form is equally clean.
        self.assertIn("tetap", order_draft.format_item_line_compact(line))


def _seed_item_prices(fake, rows):
    for r in rows:
        fake.table("item_prices").insert(r).execute()


class GeneratorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 6, 17)
        self.fake = FakeSupabase()

    def _seed_standing(self, rows):
        for r in rows:
            self.fake.table("standing_orders").insert(r).execute()

    def test_standing_emitted_with_no_history_at_all(self):
        # No item_prices for the outlet — the standing order must still appear.
        self._seed_standing([{"outlet": "SEK6", "item": "roti", "default_qty": 6,
                              "unit": "pack", "supplier": "DIAMOND BALL"}])
        out = order_generator.gather_order_drafts(self.fake, today=self.today)
        self.assertTrue(out["has_data"])
        o = next(o for o in out["outlets"] if o["outlet_code"] == "SEK6")
        self.assertIn("Roti — 6 pack", o["message"])
        drafts = self.fake.rows("order_drafts")
        roti = next(d for d in drafts if d["item"] == "roti")
        self.assertIn("STANDING", roti["flags"])
        self.assertEqual(float(roti["qty"]), 6.0)

    def test_standing_supersedes_forecast_no_double_no_review(self):
        # Corrupt/sparse roti history would otherwise go NEEDS_REVIEW; the
        # standing order replaces it — exactly one clean roti line, no review.
        _seed_item_prices(self.fake, [{
            "outlet_code": "SEK6", "canonical_item": "roti", "qty": 99,
            "unit_price": 5.0, "merchant": "DIAMOND BALL", "raw_item_name": "ROTI",
            "receipt_date": (self.today - timedelta(days=40)).isoformat(),
        }])
        self._seed_standing([{"outlet": "SEK6", "item": "roti", "default_qty": 6,
                              "unit": "pack", "supplier": "DIAMOND BALL"}])
        order_generator.gather_order_drafts(self.fake, today=self.today)
        roti = [d for d in self.fake.rows("order_drafts") if d["item"] == "roti"]
        self.assertEqual(len(roti), 1)
        self.assertIn("STANDING", roti[0]["flags"])
        self.assertNotIn("NEEDS_REVIEW", roti[0]["flags"])
        self.assertEqual(float(roti[0]["qty"]), 6.0)   # config qty, not the 99 OCR

    def test_non_standing_items_still_forecast(self):
        # A normal daily item alongside a standing one keeps its forecast path.
        rows = [{
            "outlet_code": "SEK6", "canonical_item": "ayam", "qty": 10,
            "unit_price": 9.0, "merchant": "BESTARI FARM", "raw_item_name": "AYAM",
            "receipt_date": (self.today - timedelta(days=i)).isoformat(),
        } for i in range(14)]
        _seed_item_prices(self.fake, rows)
        self._seed_standing([{"outlet": "SEK6", "item": "gas", "default_qty": 2,
                              "unit": "tong", "supplier": "INBOIS"}])
        order_generator.gather_order_drafts(self.fake, today=self.today)
        drafts = {d["item"]: d for d in self.fake.rows("order_drafts")}
        self.assertIn("STANDING", drafts["gas"]["flags"])
        self.assertEqual(drafts["ayam"]["cadence"], "DAILY")  # forecast path intact


if __name__ == "__main__":
    unittest.main()
