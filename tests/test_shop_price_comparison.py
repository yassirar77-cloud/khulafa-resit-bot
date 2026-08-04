"""Unit tests for ``shop_price_comparison``.

Hermetic — uses the shared in-memory ``FakeSupabase`` double. Covers the
per-shop aggregation, the alert block, free-text item resolution (so the
feature works for ANY item, not just ayam), and the /shop_prices report.

Run with::

    python -m unittest tests.test_shop_price_comparison
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402

from shop_price_comparison import (  # noqa: E402
    build_shop_price_report,
    format_shop_comparison,
    get_shop_prices,
    resolve_item_query,
)

TODAY = date(2026, 8, 4)


def _row(canonical, shop, price, receipt_id, days_ago=1):
    return {
        "canonical_item": canonical,
        "merchant": shop,
        "unit_price": price,
        "receipt_id": receipt_id,
        "receipt_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "qty": 1,
        "outlet_code": "SEK6",
    }


def _client(rows):
    client = FakeSupabase()
    for row in rows:
        client.table("item_prices").insert(row).execute()
    return client


class GetShopPrices(unittest.TestCase):

    def test_groups_by_shop_cheapest_first(self):
        client = _client([
            _row("ayam", "BESTARI", 12.0, 1, days_ago=2),
            _row("ayam", "BESTARI", 12.5, 2, days_ago=1),
            _row("ayam", "SEGAR MART", 9.0, 3, days_ago=3),
            _row("ayam", "SEGAR MART", 8.5, 4, days_ago=1),
            _row("ayam", "PASAR BORONG", 10.0, 5, days_ago=1),
        ])
        shops = get_shop_prices(client, "ayam", today=TODAY)
        self.assertEqual(
            [s["shop"] for s in shops],
            ["SEGAR MART", "PASAR BORONG", "BESTARI"],
        )
        segar = shops[0]
        self.assertAlmostEqual(segar["latest_price"], 8.5)
        self.assertAlmostEqual(segar["avg_price"], 8.75)
        self.assertAlmostEqual(segar["min_price"], 8.5)
        self.assertAlmostEqual(segar["max_price"], 9.0)
        self.assertEqual(segar["sample_count"], 2)
        self.assertEqual(segar["latest_date"], (TODAY - timedelta(days=1)).isoformat())

    def test_latest_price_is_the_newest_receipt_not_the_lowest(self):
        client = _client([
            _row("kopi", "KEDAI A", 20.0, 1, days_ago=10),
            _row("kopi", "KEDAI A", 26.0, 2, days_ago=1),
        ])
        shops = get_shop_prices(client, "kopi", today=TODAY)
        self.assertAlmostEqual(shops[0]["latest_price"], 26.0)

    def test_same_day_ties_break_on_receipt_id(self):
        client = _client([
            _row("kopi", "KEDAI A", 20.0, 5, days_ago=1),
            _row("kopi", "KEDAI A", 22.0, 9, days_ago=1),
        ])
        shops = get_shop_prices(client, "kopi", today=TODAY)
        self.assertAlmostEqual(shops[0]["latest_price"], 22.0)

    def test_lookback_window_excludes_old_rows(self):
        client = _client([
            _row("ayam", "OLD SHOP", 5.0, 1, days_ago=400),
            _row("ayam", "NEW SHOP", 9.0, 2, days_ago=3),
        ])
        shops = get_shop_prices(client, "ayam", lookback_days=90, today=TODAY)
        self.assertEqual([s["shop"] for s in shops], ["NEW SHOP"])

    def test_lookback_none_reads_full_history(self):
        client = _client([
            _row("ayam", "OLD SHOP", 5.0, 1, days_ago=400),
            _row("ayam", "NEW SHOP", 9.0, 2, days_ago=3),
        ])
        shops = get_shop_prices(client, "ayam", lookback_days=None, today=TODAY)
        self.assertEqual([s["shop"] for s in shops], ["OLD SHOP", "NEW SHOP"])

    def test_skips_zero_and_negative_prices(self):
        client = _client([
            _row("ayam", "KEDAI A", 0.0, 1),
            _row("ayam", "KEDAI A", -3.0, 2),
            _row("ayam", "KEDAI A", 10.0, 3),
        ])
        shops = get_shop_prices(client, "ayam", today=TODAY)
        self.assertEqual(len(shops), 1)
        self.assertEqual(shops[0]["sample_count"], 1)

    def test_blank_merchant_becomes_unknown_shop(self):
        client = _client([
            _row("ayam", "", 10.0, 1),
            _row("ayam", None, 11.0, 2),
        ])
        shops = get_shop_prices(client, "ayam", today=TODAY)
        self.assertEqual([s["shop"] for s in shops], ["Unknown shop"])
        self.assertEqual(shops[0]["sample_count"], 2)

    def test_other_items_are_not_mixed_in(self):
        client = _client([
            _row("ayam", "KEDAI A", 10.0, 1),
            _row("kopi", "KEDAI B", 20.0, 2),
        ])
        shops = get_shop_prices(client, "ayam", today=TODAY)
        self.assertEqual([s["shop"] for s in shops], ["KEDAI A"])

    def test_exclude_receipt_id(self):
        client = _client([
            _row("ayam", "KEDAI A", 10.0, 1),
            _row("ayam", "KEDAI B", 20.0, 99),
        ])
        shops = get_shop_prices(client, "ayam", exclude_receipt_id=99, today=TODAY)
        self.assertEqual([s["shop"] for s in shops], ["KEDAI A"])

    def test_garbage_input_returns_empty_no_raise(self):
        client = _client([])
        try:
            self.assertEqual(get_shop_prices(client, None), [])
            self.assertEqual(get_shop_prices(client, ""), [])
            self.assertEqual(get_shop_prices(client, "   "), [])
            self.assertEqual(get_shop_prices(client, 42), [])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"get_shop_prices raised: {e}")

    def test_query_failure_returns_empty_no_raise(self):
        class Boom:
            def table(self, _name):
                raise RuntimeError("connection refused")

        try:
            self.assertEqual(get_shop_prices(Boom(), "ayam"), [])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"get_shop_prices raised: {e}")


def _shop(name, latest, avg=None, n=3, days_ago=1):
    return {
        "shop": name,
        "latest_price": latest,
        "avg_price": avg if avg is not None else latest,
        "min_price": latest,
        "max_price": latest,
        "sample_count": n,
        "latest_date": (TODAY - timedelta(days=days_ago)).isoformat(),
    }


class FormatShopComparison(unittest.TestCase):

    def test_block_lists_every_shop_and_flags_current(self):
        shops = [
            _shop("SEGAR MART", 8.50, avg=8.70, n=12),
            _shop("PASAR BORONG", 9.20, n=6),
            _shop("BESTARI", 12.50, avg=10.00, n=8),
        ]
        out = format_shop_comparison("ayam", shops, current_shop="BESTARI")
        self.assertIn("🏪 Price at all shops — Ayam (last 90 days):", out)
        self.assertIn("🥇 SEGAR MART — RM8.50 (avg RM8.70, 12 receipts, last 03 Aug)", out)
        self.assertIn("• PASAR BORONG — RM9.20", out)
        self.assertIn("👉 BESTARI — RM12.50", out)
        self.assertIn("← this receipt", out)
        self.assertIn(
            "💡 Cheapest: SEGAR MART at RM8.50 — RM4.00 (32%) below BESTARI.", out
        )

    def test_current_shop_cheapest_says_so(self):
        shops = [_shop("KEDAI A", 8.0), _shop("KEDAI B", 9.0)]
        out = format_shop_comparison("kopi", shops, current_shop="KEDAI A")
        self.assertIn("✅ KEDAI A is still the cheapest shop.", out)

    def test_single_shop_has_nothing_to_compare(self):
        self.assertEqual(
            format_shop_comparison("ayam", [_shop("KEDAI A", 8.0)], current_shop="KEDAI A"),
            "",
        )

    def test_tail_is_summarised_but_current_shop_always_shown(self):
        shops = [_shop(f"SHOP {i}", float(i)) for i in range(1, 9)]
        out = format_shop_comparison(
            "ayam", shops, current_shop="SHOP 8", max_shops=3
        )
        self.assertIn("SHOP 1", out)
        self.assertIn("SHOP 3", out)
        self.assertNotIn("SHOP 5", out)
        self.assertIn("👉 SHOP 8", out)
        self.assertIn("… +4 more shop(s)", out)

    def test_no_current_shop_still_names_the_cheapest(self):
        shops = [_shop("KEDAI A", 8.0), _shop("KEDAI B", 9.0)]
        out = format_shop_comparison("ayam", shops)
        self.assertIn("💡 Cheapest: KEDAI A at RM8.00.", out)

    def test_garbage_input_returns_empty_string_no_raise(self):
        try:
            self.assertEqual(format_shop_comparison("ayam", None), "")
            self.assertEqual(format_shop_comparison("ayam", []), "")
            self.assertEqual(format_shop_comparison(None, "string"), "")
            self.assertEqual(format_shop_comparison("ayam", [{}, {}]), "")
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"format_shop_comparison raised: {e}")


class ResolveItemQuery(unittest.TestCase):

    def test_exact_canonical_key(self):
        self.assertEqual(resolve_item_query("ayam")["canonical"], "ayam")

    def test_english_synonym(self):
        self.assertEqual(resolve_item_query("chicken")["canonical"], "ayam")
        self.assertEqual(resolve_item_query("PRAWNS")["canonical"], "udang")

    def test_spaces_stand_in_for_underscores(self):
        self.assertEqual(resolve_item_query("ais batu")["canonical"], "ais_batu")
        self.assertEqual(resolve_item_query("ikan-bilis")["canonical"], "ikan_bilis")

    def test_raw_receipt_line_resolves(self):
        self.assertEqual(
            resolve_item_query("AYAM BERSIH 30KG")["canonical"], "ayam"
        )

    def test_ambiguous_prefix_returns_suggestions(self):
        result = resolve_item_query("sos")
        self.assertIsNone(result["canonical"])
        self.assertEqual(
            result["suggestions"], ["sos_cili", "sos_tiram", "sos_tomato"]
        )

    def test_unknown_text_returns_no_canonical(self):
        result = resolve_item_query("zzzzqqq")
        self.assertIsNone(result["canonical"])
        self.assertEqual(result["suggestions"], [])

    def test_garbage_input_no_raise(self):
        try:
            self.assertIsNone(resolve_item_query(None)["canonical"])
            self.assertIsNone(resolve_item_query("")["canonical"])
            self.assertIsNone(resolve_item_query(42)["canonical"])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"resolve_item_query raised: {e}")


class BuildShopPriceReport(unittest.TestCase):

    def test_report_for_any_item(self):
        client = _client([
            _row("ayam", "SEGAR MART", 8.50, 1),
            _row("ayam", "BESTARI", 12.50, 2),
        ])
        out = build_shop_price_report(client, "chicken", today=TODAY)
        self.assertIn("🏪 Ayam — price at all shops (last 90 days):", out)
        self.assertIn("🥇 SEGAR MART — RM8.50", out)
        self.assertIn("• BESTARI — RM12.50", out)
        self.assertIn(
            "💡 Cheapest: SEGAR MART RM8.50 — RM4.00 (32%) below BESTARI RM12.50.",
            out,
        )

    def test_single_shop_still_reports(self):
        client = _client([_row("kopi", "KEDAI A", 20.0, 1)])
        out = build_shop_price_report(client, "kopi", today=TODAY)
        self.assertIn("🥇 KEDAI A — RM20.00", out)
        self.assertNotIn("Cheapest:", out)

    def test_item_with_no_history(self):
        client = _client([_row("ayam", "KEDAI A", 8.0, 1)])
        out = build_shop_price_report(client, "kopi", today=TODAY)
        self.assertEqual(out, "No price history for Kopi (last 90 days).")

    def test_unknown_item_explains_itself(self):
        client = _client([])
        out = build_shop_price_report(client, "zzzzqqq", today=TODAY)
        self.assertIn("No price history", out)
        self.assertIn("/shop_prices ayam", out)

    def test_empty_query_shows_usage(self):
        client = _client([])
        self.assertIn("Usage: /shop_prices", build_shop_price_report(client, ""))

    def test_failure_returns_message_no_raise(self):
        class Boom:
            def table(self, _name):
                raise RuntimeError("db down")

        try:
            out = build_shop_price_report(Boom(), "ayam", today=TODAY)
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"build_shop_price_report raised: {e}")
        self.assertIn("No price history", out)


if __name__ == "__main__":
    unittest.main()
