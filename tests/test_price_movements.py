"""Tests for the price_movements view + analytics (PR #33).

A materialised view can't run in CI, so the view's filter/maths semantics are
covered by a Python reference (analytics.row_passes_filters / compute_line) that
a migration-content test pins to the SQL. The aggregation helpers the bot
actually runs on fetched view rows are tested directly.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _receipt(**kw):
    base = {"merchant_canonical_id": 1, "confidence": 90, "receipt_type": "SUPPLIER_PURCHASE"}
    base.update(kw)
    return base


def _resolution(canonical_id=5):
    return {"canonical_id": canonical_id}


class ViewFilters(unittest.TestCase):
    def test_includes_valid_row(self):
        self.assertTrue(analytics.row_passes_filters(_receipt(), _resolution()))

    def test_view_excludes_low_confidence_receipts(self):
        self.assertFalse(analytics.row_passes_filters(_receipt(confidence=59), _resolution()))
        self.assertTrue(analytics.row_passes_filters(_receipt(confidence=60), _resolution()))

    def test_view_excludes_unknown_receipt_types(self):
        for rt in ("UNKNOWN", "STAFF_ADVANCE", "PETTY_CASH"):
            self.assertFalse(analytics.row_passes_filters(_receipt(receipt_type=rt), _resolution()))
        for rt in analytics.VIEW_RECEIPT_TYPES:
            self.assertTrue(analytics.row_passes_filters(_receipt(receipt_type=rt), _resolution()))

    def test_view_excludes_unresolved_items(self):
        self.assertFalse(analytics.row_passes_filters(_receipt(), {"canonical_id": None}))

    def test_view_excludes_null_merchant(self):
        self.assertFalse(analytics.row_passes_filters(_receipt(merchant_canonical_id=None), _resolution()))


class LineComputation(unittest.TestCase):
    def test_line_total_computation_qty_x_price(self):
        # items.price is the LINE TOTAL; unit_price = price/qty; invariant holds.
        qty, unit_price, line_total = analytics.compute_line({"qty": 4, "price": 120})
        self.assertEqual(qty, 4.0)
        self.assertEqual(unit_price, 30.0)
        self.assertEqual(line_total, 120.0)
        self.assertAlmostEqual(line_total, qty * unit_price)

    def test_qty_defaults_to_one(self):
        qty, unit_price, line_total = analytics.compute_line({"qty": None, "price": 50})
        self.assertEqual((qty, unit_price, line_total), (1.0, 50.0, 50.0))
        qty, _, _ = analytics.compute_line({"qty": 0, "price": 50})
        self.assertEqual(qty, 1.0)

    def test_null_price_yields_none(self):
        self.assertEqual(analytics.compute_line({"qty": 2, "price": None}), (2.0, None, None))


def _row(item_id, item_name, merch_id, merch_name, total, **kw):
    base = {
        "item_canonical_id": item_id, "item_display_name": item_name, "item_category": "spices",
        "merchant_canonical_id": merch_id, "merchant_display_name": merch_name,
        "merchant_category": "supplier", "line_total": total,
    }
    base.update(kw)
    return base


class Aggregations(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _row(1, "jintan putih", 10, "BABAS", 100),
            _row(1, "jintan putih", 11, "SAIDA", 50),
            _row(2, "ayam bersih", 10, "BABAS", 500),
            _row(3, "santan", 11, "SAIDA", 25),
        ]

    def test_top_items_returns_correct_order(self):
        result = analytics.top_items(self.rows, 10)
        self.assertEqual([r["item_canonical_id"] for r in result], [2, 1, 3])
        self.assertEqual(result[0]["total_spend"], 500.0)
        self.assertEqual(result[1]["total_spend"], 150.0)  # 100 + 50 aggregated
        self.assertEqual(result[1]["line_count"], 2)

    def test_top_items_respects_limit(self):
        self.assertEqual(len(analytics.top_items(self.rows, 1)), 1)

    def test_top_suppliers_returns_correct_order(self):
        result = analytics.top_suppliers(self.rows, 10)
        self.assertEqual([r["merchant_canonical_id"] for r in result], [10, 11])
        self.assertEqual(result[0]["total_spend"], 600.0)  # 100 + 500
        self.assertEqual(result[1]["total_spend"], 75.0)   # 50 + 25

    def test_price_history_for_item(self):
        rows = [
            _row(1, "jintan putih", 10, "BABAS", 100, receipt_date="2026-03-01", qty=2, unit_price=50),
            _row(1, "jintan putih", 11, "SAIDA", 60, receipt_date="2026-01-15", qty=1, unit_price=60),
            _row(2, "ayam bersih", 10, "BABAS", 500, receipt_date="2026-02-01", qty=10, unit_price=50),
        ]
        hist = analytics.price_history(rows, 1)
        self.assertEqual(len(hist), 2)  # only item 1
        self.assertEqual([h["receipt_date"] for h in hist], ["2026-01-15", "2026-03-01"])  # date asc
        self.assertEqual(hist[0]["supplier"], "SAIDA")
        self.assertEqual(hist[1]["unit_price"], 50)

    def test_summarise_status(self):
        rows = [{"receipt_date": "2026-01-01"}, {"receipt_date": "2026-05-20"}, {"receipt_date": None}]
        s = analytics.summarise_status(rows)
        self.assertEqual(s, {"row_count": 3, "earliest": "2026-01-01", "latest": "2026-05-20"})


class Refresh(unittest.TestCase):
    def test_refresh_function_callable(self):
        calls = []

        class _RpcResult:
            def execute(self_inner):
                return types.SimpleNamespace(data=None)

        class FakeClient:
            def rpc(self_inner, name, *a, **k):
                calls.append(name)
                return _RpcResult()

        analytics.refresh(FakeClient())
        self.assertEqual(calls, ["refresh_price_movements"])


class Migration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "migrations", "0011_price_movements_view.sql")) as f:
            cls.sql = f.read()

    def test_view_and_joins(self):
        self.assertIn("CREATE MATERIALIZED VIEW IF NOT EXISTS public.price_movements", self.sql)
        self.assertIn("JOIN public.merchant_canonical mc", self.sql)
        self.assertIn("JOIN public.item_resolutions ir", self.sql)
        self.assertIn("JOIN public.item_canonical ic", self.sql)

    def test_where_filters_match_reference(self):
        self.assertIn("r.merchant_canonical_id IS NOT NULL", self.sql)
        self.assertIn("r.confidence >= 60", self.sql)
        self.assertIn("ir.canonical_id IS NOT NULL", self.sql)
        for rt in analytics.VIEW_RECEIPT_TYPES:
            self.assertIn(f"'{rt}'", self.sql)

    def test_unique_index_on_grain(self):
        # Keyed on item_index (the safe grain), not item_canonical_id.
        self.assertIn("CREATE UNIQUE INDEX", self.sql)
        self.assertIn("(receipt_id, item_index)", self.sql)

    def test_secondary_indexes(self):
        self.assertIn("(item_canonical_id, receipt_date DESC)", self.sql)
        self.assertIn("(merchant_canonical_id, receipt_date DESC)", self.sql)
        self.assertIn("(merchant_category)", self.sql)

    def test_refresh_function_concurrently(self):
        self.assertIn("CREATE OR REPLACE FUNCTION public.refresh_price_movements()", self.sql)
        self.assertIn("REFRESH MATERIALIZED VIEW CONCURRENTLY public.price_movements", self.sql)


class BotCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_commands_owner_only(self):
        for fn in (
            "refresh_analytics_command", "price_movements_status_command",
            "top_items_command", "top_suppliers_command", "price_history_command",
        ):
            idx = self.src.index(f"async def {fn}(")
            self.assertIn("is_reviewer(_command_owner_id(update))", self.src[idx:idx + 600], f"{fn} not gated")

    def test_commands_registered(self):
        for cmd, fn in (
            ("refresh_analytics", "refresh_analytics_command"),
            ("price_movements_status", "price_movements_status_command"),
            ("top_items", "top_items_command"),
            ("top_suppliers", "top_suppliers_command"),
            ("price_history", "price_history_command"),
        ):
            self.assertIn(f'CommandHandler("{cmd}", {fn})', self.src)


if __name__ == "__main__":
    unittest.main()
