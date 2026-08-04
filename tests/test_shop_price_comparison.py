"""Unit tests for ``shop_price_comparison``.

Hermetic — uses the shared in-memory ``FakeSupabase`` double. Covers the
four ways the raw ``item_prices`` table lies (internal transfers, OCR
merchant variants, mixed cuts/pack sizes, corrupt future dates), the
per-shop aggregation, the alert block, free-text item resolution (so the
feature works for ANY item), and the /shop_prices report.

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
    get_variant_prices,
    item_variant,
    load_price_rows,
    pick_variant,
    resolve_item_query,
    shop_display,
    shop_key,
)

TODAY = date(2026, 8, 4)


def _row(canonical, shop, price, receipt_id, days_ago=1, item="AYAM BERSIH 30KG"):
    return {
        "canonical_item": canonical,
        "merchant": shop,
        "unit_price": price,
        "receipt_id": receipt_id,
        "receipt_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "raw_item_name": item,
        "qty": 1,
        "outlet_code": "SEK6",
    }


def _client(rows, receipts=None, canonicals=None):
    client = FakeSupabase()
    for row in rows:
        client.table("item_prices").insert(row).execute()
    for receipt in receipts or []:
        client.table("receipts").insert(receipt).execute()
    for canonical in canonicals or []:
        client.table("merchant_canonical").insert(canonical).execute()
    return client


class ItemVariant(unittest.TestCase):
    """Different cuts must never be averaged into one 'ayam' price."""

    def test_pack_size_stripped(self):
        self.assertEqual(item_variant("AYAM BERSIH 30KG", "ayam"), "AYAM BERSIH")
        self.assertEqual(item_variant("Ayam Bersih 1 kg", "ayam"), "AYAM BERSIH")
        self.assertEqual(item_variant("AYAM BERSIH x2", "ayam"), "AYAM BERSIH")

    def test_cuts_stay_distinct(self):
        self.assertNotEqual(
            item_variant("PAHA AYAM 10KG", "ayam"),
            item_variant("AYAM BERSIH 10KG", "ayam"),
        )
        self.assertEqual(item_variant("PAHA AYAM 10KG", "ayam"), "PAHA AYAM")

    def test_bare_line_falls_back_to_item_name(self):
        self.assertEqual(item_variant("30KG", "ayam"), "AYAM")
        self.assertEqual(item_variant(None, "ais_batu"), "AIS BATU")


class ShopKey(unittest.TestCase):

    def test_legal_suffixes_collapse(self):
        self.assertEqual(
            shop_key("BESTARI FARM (M) SDN BHD"), shop_key("BESTARI FARM")
        )

    def test_different_shops_stay_apart(self):
        self.assertNotEqual(shop_key("BESTARI FARM"), shop_key("AYAM BERLIAN"))

    def test_display_keeps_the_trade_name(self):
        # Grouping is aggressive; the label the director reads is not.
        self.assertEqual(shop_key("BALAJI ENTERPRISE SDN BHD"), "BALAJI")
        self.assertEqual(
            shop_display("BALAJI ENTERPRISE SDN BHD"), "BALAJI ENTERPRISE"
        )
        self.assertEqual(shop_display("BESTARI FARM (M) SDN BHD"), "BESTARI FARM")


class LoadPriceRows(unittest.TestCase):
    """The filters that stop the report being a pile of unrelated bills."""

    def test_internal_transfers_are_not_shops(self):
        client = _client(
            [
                _row("ayam", "RESTORAN KHULAFA SDN. BHD.", 1.70, 1),
                _row("ayam", "KHULAPA SIGNATURE RESTAURANT", 54.00, 2),
                _row("ayam", "BESTARI FARM", 15.20, 3),
            ],
        )
        rows = load_price_rows(client, "ayam", today=TODAY)
        self.assertEqual([r["shop"] for r in rows], ["BESTARI FARM"])

    def test_non_supplier_receipt_types_dropped(self):
        client = _client(
            [
                _row("ayam", "MYMOON'S KITCHEN", 2.30, 10),
                _row("ayam", "BESTARI FARM", 15.20, 11),
            ],
            receipts=[
                {"id": 10, "receipt_type": "INTERNAL_TRANSFER"},
                {"id": 11, "receipt_type": "SUPPLIER_PURCHASE"},
            ],
        )
        rows = load_price_rows(client, "ayam", today=TODAY)
        self.assertEqual([r["shop"] for r in rows], ["BESTARI FARM"])

    def test_non_supplier_merchant_category_dropped(self):
        client = _client(
            [
                _row("ayam", "SOME OUTLET", 2.30, 10),
                _row("ayam", "BESTARI FARM", 15.20, 11),
            ],
            receipts=[
                {"id": 10, "merchant_canonical_id": 1},
                {"id": 11, "merchant_canonical_id": 2},
            ],
            canonicals=[
                {"id": 1, "display_name": "SOME OUTLET", "category": "internal_transfer"},
                {"id": 2, "display_name": "BESTARI FARM", "category": "supplier"},
            ],
        )
        rows = load_price_rows(client, "ayam", today=TODAY)
        self.assertEqual([r["shop"] for r in rows], ["BESTARI FARM"])

    def test_canonical_display_name_collapses_ocr_variants(self):
        client = _client(
            [
                _row("ayam", "MYMOON'S KITCHEN", 2.30, 10),
                _row("ayam", "MYMOOK'S KITCHEN", 2.40, 11),
            ],
            receipts=[
                {"id": 10, "merchant_canonical_id": 1},
                {"id": 11, "merchant_canonical_id": 1},
            ],
            canonicals=[
                {"id": 1, "display_name": "MYMOON SUPPLY", "category": "supplier"},
            ],
        )
        shops = get_shop_prices(client, "ayam", today=TODAY)
        self.assertEqual([s["shop"] for s in shops], ["MYMOON SUPPLY"])
        self.assertEqual(shops[0]["sample_count"], 2)

    def test_future_dated_rows_dropped(self):
        client = _client([
            _row("ayam", "BESTARI FARM", 15.20, 1, days_ago=2),
            _row("ayam", "PHANTOM SUPPLY", 9.99, 2, days_ago=-140),  # 26 Dec
        ])
        rows = load_price_rows(client, "ayam", today=TODAY)
        self.assertEqual([r["shop"] for r in rows], ["BESTARI FARM"])

    def test_receipt_lookup_failure_keeps_rows(self):
        class HalfBroken(FakeSupabase):
            def table(self, name):
                if name == "receipts":
                    raise RuntimeError("receipts unavailable")
                return super().table(name)

        client = HalfBroken()
        client.table("item_prices").insert(_row("ayam", "BESTARI FARM", 15.2, 1)).execute()
        rows = load_price_rows(client, "ayam", today=TODAY)
        self.assertEqual([r["shop"] for r in rows], ["BESTARI FARM"])


class GetShopPrices(unittest.TestCase):

    def test_groups_by_shop_cheapest_first(self):
        client = _client([
            _row("ayam", "BESTARI FARM", 12.0, 1, days_ago=2),
            _row("ayam", "BESTARI FARM", 12.5, 2, days_ago=1),
            _row("ayam", "SEGAR MART", 9.0, 3, days_ago=3),
            _row("ayam", "SEGAR MART", 8.5, 4, days_ago=1),
            _row("ayam", "PASAR BORONG", 10.0, 5, days_ago=1),
        ])
        shops = get_shop_prices(client, "ayam", today=TODAY)
        self.assertEqual(
            [s["shop"] for s in shops],
            ["SEGAR MART", "PASAR BORONG", "BESTARI FARM"],
        )
        segar = shops[0]
        self.assertAlmostEqual(segar["latest_price"], 8.5)
        self.assertAlmostEqual(segar["avg_price"], 8.75)
        self.assertAlmostEqual(segar["min_price"], 8.5)
        self.assertAlmostEqual(segar["max_price"], 9.0)
        self.assertEqual(segar["sample_count"], 2)
        self.assertEqual(segar["latest_date"], (TODAY - timedelta(days=1)).isoformat())

    def test_shop_spelling_variants_group_together(self):
        client = _client([
            _row("ayam", "BESTARI FARM (M) SDN BHD", 15.20, 1, days_ago=3),
            _row("ayam", "BESTARI FARM", 15.50, 2, days_ago=1),
        ])
        shops = get_shop_prices(client, "ayam", today=TODAY)
        self.assertEqual(len(shops), 1)
        self.assertEqual(shops[0]["sample_count"], 2)
        self.assertAlmostEqual(shops[0]["latest_price"], 15.50)

    def test_variant_filter_keeps_cuts_apart(self):
        client = _client([
            _row("ayam", "BESTARI FARM", 15.20, 1, item="AYAM BERSIH 30KG"),
            _row("ayam", "BESTARI FARM", 7.80, 2, item="PAHA AYAM 10KG"),
            _row("ayam", "AYAM BERLIAN", 8.20, 3, item="PAHA AYAM"),
        ])
        legs = get_shop_prices(client, "ayam", today=TODAY, variant="PAHA AYAM 5KG")
        self.assertEqual([s["shop"] for s in legs], ["BESTARI FARM", "AYAM BERLIAN"])
        self.assertAlmostEqual(legs[0]["latest_price"], 7.80)

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


class GetVariantPrices(unittest.TestCase):

    def test_one_entry_per_cut_most_recent_first(self):
        client = _client([
            _row("ayam", "BESTARI FARM", 15.20, 1, days_ago=9, item="AYAM BERSIH 30KG"),
            _row("ayam", "BESTARI FARM", 7.80, 2, days_ago=1, item="PAHA AYAM 10KG"),
            _row("ayam", "AYAM BERLIAN", 8.20, 3, days_ago=2, item="PAHA AYAM"),
        ])
        variants = get_variant_prices(client, "ayam", today=TODAY)
        self.assertEqual([v["variant"] for v in variants], ["PAHA AYAM", "AYAM BERSIH"])
        legs = variants[0]
        self.assertEqual(legs["sample_count"], 2)
        self.assertEqual([s["shop"] for s in legs["shops"]], ["BESTARI FARM", "AYAM BERLIAN"])

    def test_pick_variant(self):
        variants = [{"variant": "PAHA AYAM"}, {"variant": "AYAM BERSIH"}]
        self.assertEqual(pick_variant("paha ayam", variants), "PAHA AYAM")
        self.assertEqual(pick_variant("PAHA", variants), "PAHA AYAM")
        self.assertIsNone(pick_variant("ayam", variants))
        self.assertIsNone(pick_variant("", variants))


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
            _shop("SEGAR MART", 8.50, n=12),
            _shop("PASAR BORONG", 9.20, n=1),
            _shop("BESTARI FARM", 12.50, n=8),
        ]
        out = format_shop_comparison("ayam", shops, current_shop="BESTARI FARM")
        self.assertIn("🏪 Ayam — price at all shops (last 90 days):", out)
        self.assertIn("🥇 SEGAR MART — RM8.50 · 03 Aug (12x)", out)
        self.assertIn("• PASAR BORONG — RM9.20 · 03 Aug", out)
        self.assertIn("👉 BESTARI FARM — RM12.50", out)
        self.assertIn("← this receipt", out)
        self.assertIn(
            "💡 Cheapest: SEGAR MART at RM8.50 — RM4.00 (32%) below BESTARI FARM.", out
        )

    def test_variant_label_titles_the_block(self):
        shops = [_shop("KEDAI A", 7.80), _shop("KEDAI B", 8.20)]
        out = format_shop_comparison(
            "ayam", shops, current_shop="KEDAI B", variant="PAHA AYAM"
        )
        self.assertIn("🏪 Paha Ayam — price at all shops", out)

    def test_current_shop_matches_through_name_variants(self):
        shops = [_shop("KEDAI A", 8.0), _shop("BESTARI FARM", 9.0)]
        out = format_shop_comparison(
            "ayam", shops, current_shop="BESTARI FARM (M) SDN BHD"
        )
        self.assertIn("👉 BESTARI FARM", out)

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

    def test_report_is_grouped_by_cut(self):
        client = _client([
            _row("ayam", "BESTARI FARM", 7.80, 1, days_ago=1, item="PAHA AYAM 10KG"),
            _row("ayam", "AYAM BERLIAN", 8.20, 2, days_ago=2, item="PAHA AYAM"),
            _row("ayam", "BESTARI FARM", 15.20, 3, days_ago=5, item="AYAM BERSIH 30KG"),
        ])
        out = build_shop_price_report(client, "chicken", today=TODAY)
        self.assertIn("🏪 Ayam — supplier prices (last 90 days)", out)
        self.assertIn("Paha Ayam", out)
        self.assertIn("🥇 BESTARI FARM — RM7.80 · 03 Aug", out)
        self.assertIn("• AYAM BERLIAN — RM8.20", out)
        self.assertIn("Ayam Bersih", out)
        self.assertIn("🥇 BESTARI FARM — RM15.20", out)

    def test_query_can_name_one_cut(self):
        client = _client([
            _row("ayam", "BESTARI FARM", 7.80, 1, item="PAHA AYAM 10KG"),
            _row("ayam", "BESTARI FARM", 15.20, 2, item="AYAM BERSIH 30KG"),
        ])
        out = build_shop_price_report(client, "paha ayam", today=TODAY)
        self.assertIn("Paha Ayam", out)
        self.assertNotIn("Ayam Bersih", out)

    def test_internal_transfers_never_reach_the_report(self):
        client = _client([
            _row("ayam", "RESTORAN KHULAFA SDN. BHD.", 1.70, 1),
            _row("ayam", "BESTARI FARM", 15.20, 2),
        ])
        out = build_shop_price_report(client, "ayam", today=TODAY)
        self.assertNotIn("KHULAFA", out)
        self.assertIn("BESTARI FARM", out)

    def test_extra_cuts_are_summarised(self):
        cuts = ["PAHA", "DADA", "SAYAP", "HATI", "KEPALA", "KAKI", "BERSIH"]
        client = _client([
            _row("ayam", "KEDAI A", float(i + 1), i + 1, days_ago=i + 1,
                 item=f"AYAM {cut} 5KG")
            for i, cut in enumerate(cuts)
        ])
        out = build_shop_price_report(client, "ayam", max_variants=2, today=TODAY)
        self.assertIn("… +5 more type(s):", out)

    def test_item_with_no_history(self):
        client = _client([_row("ayam", "KEDAI A", 8.0, 1)])
        out = build_shop_price_report(client, "kopi", today=TODAY)
        self.assertEqual(out, "No supplier prices for Kopi (last 90 days).")

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
        self.assertIn("No supplier prices", out)


if __name__ == "__main__":
    unittest.main()
