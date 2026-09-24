"""Unit tests for ``bill_analysis`` (and the bulk loader it rides on).

Hermetic — the shared in-memory ``FakeSupabase`` double. Covers: which
rows count as today's bills, the same-shop previous-price comparison
(threshold, implausible, decreases, per-outlet entries), the cross-outlet
comparison (cheapest first, gaps, unit mismatch), the alternatives each
increase carries, the per-manager slice, every formatter (owner English,
manager Tamil, tone guard), the on-demand report, the end-to-end gather
through the cleaning pass, and the never-raises contract.

Run with::

    python -m unittest tests.test_bill_analysis
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402

import bill_analysis as ba  # noqa: E402
from shop_price_comparison import load_all_price_rows_with_stats  # noqa: E402
from weekly_manager_reports import contains_accusatory  # noqa: E402

TODAY = date(2026, 9, 6)
NOW = datetime(2026, 9, 6, 13, 30, tzinfo=timezone.utc)


def _iso(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def _row(item, variant, shop, outlet, price, receipt_id, days_ago=1,
         new=False, created_at=None):
    """A cleaned row as ``load_all_price_rows`` would emit it."""
    if created_at is None:
        created_at = (
            (NOW - timedelta(hours=2)).isoformat() if new
            else (NOW - timedelta(days=days_ago, hours=2)).isoformat()
        )
    return {
        "shop": shop,
        "shop_key": shop.upper(),
        "variant": variant,
        "unit_price": price,
        "receipt_date": _iso(days_ago),
        "receipt_id": receipt_id,
        "canonical_item": item,
        "outlet_code": outlet,
        "qty": 1.0,
        "raw_item_name": variant,
        "created_at": created_at,
    }


def _db_row(item, shop, outlet, price, receipt_id, days_ago=1, raw="TELUR GRED A 30 BIJI",
            created_at=None):
    """A raw item_prices row for the FakeSupabase."""
    if created_at is None:
        created_at = (NOW - timedelta(days=days_ago, hours=2)).isoformat()
    return {
        "canonical_item": item,
        "merchant": shop,
        "unit_price": price,
        "qty": 1,
        "receipt_id": receipt_id,
        "receipt_date": _iso(days_ago),
        "raw_item_name": raw,
        "outlet_code": outlet,
        "created_at": created_at,
    }


class NewRows(unittest.TestCase):
    def test_created_at_within_window_is_new(self):
        fresh = _row("telur", "TELUR", "SAIDA", "VISTA", 0.45, 1, new=True)
        stale = _row("telur", "TELUR", "SAIDA", "VISTA", 0.40, 2, days_ago=5)
        new, base = ba.split_new_rows([fresh, stale], NOW)
        self.assertEqual([r["receipt_id"] for r in new], [1])
        self.assertEqual([r["receipt_id"] for r in base], [2])

    def test_z_suffix_and_naive_timestamps_parse(self):
        z = _row("telur", "TELUR", "SAIDA", "VISTA", 0.45, 1,
                 created_at="2026-09-06T12:00:00Z")
        naive = _row("telur", "TELUR", "SAIDA", "VISTA", 0.45, 2,
                     created_at="2026-09-06T12:00:00")
        old = _row("telur", "TELUR", "SAIDA", "VISTA", 0.45, 3,
                   created_at="2026-09-01T12:00:00Z")
        self.assertTrue(ba.is_new_row(z, NOW))
        self.assertTrue(ba.is_new_row(naive, NOW))
        self.assertFalse(ba.is_new_row(old, NOW))

    def test_missing_created_at_falls_back_to_receipt_date(self):
        yesterday = _row("telur", "TELUR", "SAIDA", "VISTA", 0.45, 1, days_ago=1,
                         created_at="")
        last_week = _row("telur", "TELUR", "SAIDA", "VISTA", 0.45, 2, days_ago=7,
                         created_at="garbage")
        self.assertTrue(ba.is_new_row(yesterday, NOW, today=TODAY))
        self.assertFalse(ba.is_new_row(last_week, NOW, today=TODAY))

    def test_garbage_rows_are_skipped(self):
        new, base = ba.split_new_rows([None, "x", 3], NOW)
        self.assertEqual((new, base), ([], []))


class PriceChanges(unittest.TestCase):
    def _history(self):
        return [
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.40, 10, days_ago=20),
            _row("telur", "TELUR GRED A", "SAIDA", "SEK20", 0.40, 11, days_ago=10),
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.41, 12, days_ago=4),
        ]

    def test_increase_vs_same_shop_previous_price(self):
        rows = self._history() + [
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.46, 20, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(len(out["increases"]), 1)
        e = out["increases"][0]
        self.assertEqual(e["label"], "Telur Gred A")
        self.assertEqual(e["shop"], "SAIDA")
        self.assertEqual(e["outlet_code"], "VISTA")
        self.assertEqual(e["outlet"], "Vista")
        self.assertAlmostEqual(e["previous_price"], 0.41)
        self.assertEqual(e["previous_date"], _iso(4))
        self.assertAlmostEqual(e["new_price"], 0.46)
        self.assertAlmostEqual(e["change_rm"], 0.05)
        self.assertAlmostEqual(e["change_pct"], 0.05 / 0.41 * 100)
        self.assertEqual(e["sample_count"], 3)
        self.assertEqual(out["stats"]["new_bills"], 1)
        self.assertEqual(out["stats"]["increases"], 1)

    def test_small_change_is_noise(self):
        rows = self._history() + [
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.42, 20, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(out["increases"], [])
        self.assertEqual(out["stats"]["unchanged"], 1)

    def test_rm_floor_blocks_tiny_absolute_moves(self):
        rows = [
            _row("garam", "GARAM", "SAIDA", "VISTA", 0.20, 1, days_ago=5),
            _row("garam", "GARAM", "SAIDA", "VISTA", 0.21, 2, new=True),  # +5% but 1 sen
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(out["increases"], [])

    def test_implausible_jump_is_dropped_not_alerted(self):
        rows = self._history() + [
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 14.0, 20, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(out["increases"], [])
        self.assertEqual(out["stats"]["implausible"], 1)

    def test_decrease_is_kept_separately(self):
        rows = self._history() + [
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.35, 20, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(out["increases"], [])
        self.assertEqual(len(out["decreases"]), 1)
        self.assertLess(out["decreases"][0]["change_pct"], 0)

    def test_other_shop_is_not_a_baseline(self):
        rows = self._history() + [
            _row("telur", "TELUR GRED A", "HANEE", "VISTA", 0.60, 20, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(out["increases"], [])
        self.assertEqual(out["stats"]["no_history"], 1)

    def test_other_cut_is_not_a_baseline(self):
        rows = self._history() + [
            _row("telur", "TELUR KAMPUNG", "SAIDA", "VISTA", 0.90, 20, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(out["increases"], [])

    def test_two_outlets_same_shop_get_their_own_entry(self):
        rows = self._history() + [
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.46, 20, new=True),
            _row("telur", "TELUR GRED A", "SAIDA", "SEK20", 0.50, 21, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        by_outlet = {e["outlet_code"]: e for e in out["increases"]}
        self.assertEqual(set(by_outlet), {"VISTA", "SEK20"})
        # Both are judged against the shop's last price BEFORE today's bills,
        # never against each other.
        self.assertAlmostEqual(by_outlet["SEK20"]["previous_price"], 0.41)
        # Sorted biggest jump first.
        self.assertEqual(out["increases"][0]["outlet_code"], "SEK20")

    def test_same_outlet_twice_today_is_judged_on_the_later_bill(self):
        rows = self._history() + [
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.60, 20, new=True),
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.46, 21, new=True),
        ]
        out = ba.find_price_changes(rows, NOW, today=TODAY)
        self.assertEqual(len(out["increases"]), 1)
        self.assertAlmostEqual(out["increases"][0]["new_price"], 0.46)

    def test_no_new_bills_is_empty_with_stats(self):
        out = ba.find_price_changes(self._history(), NOW, today=TODAY)
        self.assertEqual(out["increases"], [])
        self.assertEqual(out["stats"]["new_rows"], 0)

    def test_garbage_never_raises(self):
        self.assertEqual(ba.find_price_changes(None, NOW)["increases"], [])
        self.assertEqual(ba.find_price_changes([None, {"x": 1}], NOW)["increases"], [])


class OutletComparison(unittest.TestCase):
    def _rows(self):
        return [
            _row("telur", "TELUR GRED A", "HANEE", "BISTRO7", 0.38, 1, days_ago=3),
            _row("telur", "TELUR GRED A", "HANEE", "BISTRO7", 0.40, 2, days_ago=9),
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.45, 3, days_ago=2),
            _row("telur", "TELUR GRED A", "SAIDA", "SEK20", 0.42, 4, days_ago=1),
            _row("bawang", "BAWANG BESAR", "SAIDA", "VISTA", 3.0, 5, days_ago=1),
        ]

    def test_cheapest_first_with_gaps(self):
        comps = ba.compare_outlets(self._rows())
        self.assertEqual(len(comps), 1)  # bawang: one outlet only
        comp = comps[0]
        self.assertEqual(comp["label"], "Telur Gred A")
        self.assertEqual(
            [o["outlet_code"] for o in comp["outlets"]], ["BISTRO7", "SEK20", "VISTA"]
        )
        cheapest = comp["cheapest"]
        self.assertEqual(cheapest["outlet"], "Bistro")
        self.assertEqual(cheapest["shop"], "HANEE")
        self.assertAlmostEqual(cheapest["latest_price"], 0.38)  # latest, not lowest ever
        self.assertAlmostEqual(cheapest["avg_price"], 0.39)
        self.assertEqual(cheapest["sample_count"], 2)
        vista = comp["outlets"][2]
        self.assertAlmostEqual(vista["gap_rm"], 0.07)
        self.assertAlmostEqual(vista["gap_pct"], 0.07 / 0.38 * 100)
        self.assertTrue(vista["pays_more"])
        self.assertFalse(cheapest["pays_more"])
        self.assertTrue(comp["has_gap"])
        self.assertFalse(comp["unit_mismatch"])

    def test_small_spread_is_not_a_gap(self):
        rows = [
            _row("telur", "TELUR", "HANEE", "BISTRO7", 0.40, 1),
            _row("telur", "TELUR", "SAIDA", "VISTA", 0.41, 2),
        ]
        comp = ba.compare_outlets(rows)[0]
        self.assertFalse(comp["has_gap"])
        self.assertFalse(any(o["pays_more"] for o in comp["outlets"]))

    def test_unit_mismatch_is_flagged(self):
        rows = [
            _row("telur", "TELUR", "HANEE", "BISTRO7", 0.40, 1),   # per egg
            _row("telur", "TELUR", "SAIDA", "VISTA", 12.0, 2),     # per tray
        ]
        comp = ba.compare_outlets(rows)[0]
        self.assertTrue(comp["unit_mismatch"])

    def test_rows_without_outlet_are_ignored(self):
        rows = [
            _row("telur", "TELUR", "HANEE", "BISTRO7", 0.40, 1),
            _row("telur", "TELUR", "SAIDA", None, 0.50, 2),
            _row("telur", "TELUR", "SAIDA", "", 0.50, 3),
        ]
        self.assertEqual(ba.compare_outlets(rows), [])

    def test_gaps_sort_before_no_gaps_widest_first_mismatch_last(self):
        rows = [
            _row("a", "A", "S", "BISTRO7", 1.0, 1), _row("a", "A", "S", "VISTA", 1.5, 2),
            _row("b", "B", "S", "BISTRO7", 1.0, 3), _row("b", "B", "S", "VISTA", 1.01, 4),
            _row("c", "C", "S", "BISTRO7", 1.0, 5), _row("c", "C", "S", "VISTA", 1.2, 6),
            _row("d", "D", "S", "BISTRO7", 1.0, 7), _row("d", "D", "S", "VISTA", 9.0, 8),
        ]
        labels = [c["label"] for c in ba.compare_outlets(rows)]
        self.assertEqual(labels, ["A", "C", "B", "D"])

    def test_garbage_never_raises(self):
        self.assertEqual(ba.compare_outlets(None), [])
        self.assertEqual(ba.compare_outlets([None, {"unit_price": "x"}]), [])


class Alternatives(unittest.TestCase):
    def test_cheaper_shops_and_outlets_attached(self):
        rows = [
            _row("telur", "TELUR GRED A", "HANEE", "BISTRO7", 0.38, 1, days_ago=3),
            _row("telur", "TELUR GRED A", "SAIDA (M) SDN BHD", "SEK20", 0.42, 2, days_ago=2),
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.41, 3, days_ago=4),
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.48, 4, new=True),
            _row("telur", "TELUR GRED A", "MAHAL", "JAKEL", 0.60, 5, days_ago=2),
        ]
        for r in rows:
            r["shop_key"] = r["shop"].replace(" (M) SDN BHD", "").upper()
        comps = ba.compare_outlets(rows)
        changes = ba.find_price_changes(rows, NOW, today=TODAY)
        ba.attach_alternatives(changes["increases"], rows, comps)
        e = changes["increases"][0]
        # Only shops below today's price, cheapest first, never SAIDA itself
        # (even under its legal-suffix variant).
        self.assertEqual(
            [(s["shop"], s["latest_price"]) for s in e["cheaper_shops"]],
            [("HANEE", 0.38)],
        )
        # Branches paying less than Vista's new price, with their supplier.
        self.assertEqual(
            [(o["outlet"], o["latest_price"], o["shop"]) for o in e["cheaper_outlets"]],
            [("Bistro", 0.38, "HANEE"), ("SEK-20", 0.42, "SAIDA (M) SDN BHD")],
        )

    def test_mismatched_units_never_become_an_alternative(self):
        rows = [
            _row("telur", "TELUR", "HANEE", "BISTRO7", 0.10, 1, days_ago=3),
            _row("telur", "TELUR", "SAIDA", "VISTA", 10.0, 2, days_ago=4),
            _row("telur", "TELUR", "SAIDA", "VISTA", 12.0, 3, new=True),
        ]
        comps = ba.compare_outlets(rows)
        changes = ba.find_price_changes(rows, NOW, today=TODAY)
        ba.attach_alternatives(changes["increases"], rows, comps)
        self.assertEqual(changes["increases"][0]["cheaper_outlets"], [])

    def test_garbage_never_raises(self):
        ba.attach_alternatives(None, None, None)
        ba.attach_alternatives([{"x": 1}], [None], [])


class ManagerSlice(unittest.TestCase):
    def _bundle(self):
        rows = [
            _row("telur", "TELUR", "HANEE", "BISTRO7", 0.38, 1, days_ago=3),
            _row("telur", "TELUR", "SAIDA", "VISTA", 0.45, 2, days_ago=2),
            _row("bawang", "BAWANG", "SAIDA", "VISTA", 3.0, 3, days_ago=2),
            _row("bawang", "BAWANG", "PASAR", "BISTRO7", 3.6, 4, days_ago=1),
            _row("minyak", "MINYAK", "S", "VISTA", 0.10, 5, days_ago=1),
            _row("minyak", "MINYAK", "S", "BISTRO7", 5.0, 6, days_ago=1),  # unit mismatch
            _row("telur", "TELUR", "SAIDA", "VISTA", 0.50, 7, new=True),
        ]
        comps = ba.compare_outlets(rows)
        changes = ba.find_price_changes(rows, NOW, today=TODAY)
        return {"increases": changes["increases"], "comparisons": comps}

    def test_vista_hears_its_increase_and_where_it_pays_more(self):
        s = ba.entries_for_outlet(self._bundle(), "VISTA")
        self.assertEqual([e["label"] for e in s["increases"]], ["Telur"])
        self.assertEqual([p["label"] for p in s["pays_more"]], ["Telur"])
        self.assertEqual(s["pays_more"][0]["cheapest"]["outlet"], "Bistro")
        # Vista is the cheapest bawang buyer; minyak is a unit mismatch and
        # must not be praised or blamed.
        self.assertEqual(s["cheapest"], ["Bawang"])

    def test_bistro_hears_bawang_only(self):
        s = ba.entries_for_outlet(self._bundle(), "bistro7")
        self.assertEqual(s["increases"], [])
        self.assertEqual([p["label"] for p in s["pays_more"]], ["Bawang"])
        self.assertEqual(s["cheapest"], ["Telur"])

    def test_unknown_outlet_is_empty(self):
        s = ba.entries_for_outlet(self._bundle(), "KLANG")
        self.assertEqual(s, {"increases": [], "pays_more": [], "cheapest": []})

    def test_garbage_never_raises(self):
        self.assertEqual(
            ba.entries_for_outlet(None, None),
            {"increases": [], "pays_more": [], "cheapest": []},
        )


class Formatting(unittest.TestCase):
    def _bundle(self):
        rows = [
            _row("telur", "TELUR GRED A", "HANEE", "BISTRO7", 0.38, 1, days_ago=3),
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.41, 2, days_ago=4),
            _row("telur", "TELUR GRED A", "SAIDA", "VISTA", 0.46, 3, new=True),
            _row("bawang", "BAWANG", "SAIDA", "VISTA", 3.0, 4, days_ago=2),
            _row("bawang", "BAWANG", "PASAR", "BISTRO7", 3.6, 5, days_ago=1),
            _row("gula", "GULA", "SAIDA", "VISTA", 3.0, 6, days_ago=8),
            _row("gula", "GULA", "SAIDA", "VISTA", 2.5, 7, new=True),
        ]
        comps = ba.compare_outlets(rows)
        changes = ba.find_price_changes(rows, NOW, today=TODAY)
        ba.attach_alternatives(changes["increases"], rows, comps)
        return {
            "increases": changes["increases"],
            "decreases": changes["decreases"],
            "comparisons": comps,
            "stats": changes["stats"],
            "window_days": 30,
            "hours": 24,
        }

    def test_owner_price_report(self):
        text = ba.format_owner_price_report(self._bundle())
        self.assertIn("📈 Bill analysis — price changes", text)
        self.assertIn("• Telur Gred A — SAIDA [Vista]", text)
        self.assertIn("RM0.41 (02 Sep) → RM0.46 (05 Sep)  +RM0.05 (+12.2%)", text)
        self.assertIn("💡 cheaper at HANEE RM0.38 · Bistro pays RM0.38 (HANEE)", text)
        self.assertIn("PRICE DROPS:", text)
        self.assertIn("• Gula — SAIDA: RM3.00 → RM2.50 (-16.7%)", text)
        self.assertIn("Bills analysed: 2 · line items: 2", text)

    def test_owner_price_report_no_increases_says_so(self):
        bundle = self._bundle()
        bundle["increases"] = []
        text = ba.format_owner_price_report(bundle)
        self.assertIn("✅ No price increases on today's bills.", text)

    def test_owner_price_report_silent_when_no_bills(self):
        self.assertEqual(
            ba.format_owner_price_report({"stats": {"new_rows": 0}, "increases": []}),
            "",
        )

    def test_owner_outlet_report(self):
        text = ba.format_owner_outlet_report(self._bundle())
        self.assertIn("🏪 Outlet price comparison — every item (last 30 days)", text)
        self.assertIn("Telur Gred A:", text)
        self.assertIn("🥇 Bistro RM0.38 (HANEE) · Vista RM0.46 (SAIDA) +21%", text)
        self.assertIn("Bawang:", text)
        self.assertIn("🥇 Vista RM3.00 (SAIDA) · Bistro RM3.60 (PASAR) +20%", text)
        self.assertIn("Items compared: 2 · a branch pays ≥5% more: 2", text)

    def test_owner_outlet_report_can_be_capped(self):
        text = ba.format_owner_outlet_report(self._bundle(), max_items=1)
        self.assertIn("… +1 more item(s)", text)

    def test_unit_mismatch_shows_as_check_unit(self):
        comps = ba.compare_outlets([
            _row("minyak", "MINYAK", "S", "VISTA", 0.10, 1),
            _row("minyak", "MINYAK", "S", "BISTRO7", 5.0, 2),
        ])
        text = ba.format_owner_outlet_report({"comparisons": comps, "window_days": 30})
        self.assertIn("Minyak ⚠️ check unit:", text)
        self.assertIn("⚠️ unit mismatch: 1", text)

    def test_manager_note_tamil(self):
        bundle = self._bundle()
        text = ba.format_manager_note("VISTA", ba.entries_for_outlet(bundle, "VISTA"))
        self.assertTrue(text.startswith("🧾 Bill analysis — Vista"))
        self.assertIn("📈 Unga bill-la intha items vilai eriyirukku:", text)
        self.assertIn("• Telur Gred A (SAIDA): RM0.41 → RM0.46 (+12%) · 05 Sep", text)
        self.assertIn("Vera kadaiyila cheap: HANEE RM0.38", text)
        self.assertIn("Vera branch: Bistro RM0.38 (HANEE)", text)
        self.assertIn("👉 Supplier-kitta yen vilai eruchu-nu kelunga.", text)
        self.assertIn("🏪 Ithe item vera branch cheap-aa vaanguthu:", text)
        self.assertIn(
            "• Telur Gred A: Neenga RM0.46 (SAIDA) · Bistro RM0.38 (HANEE) — 21% cheap",
            text,
        )
        self.assertIn("✅ Neenga cheapest-aa vaangurathu: Bawang — super! 👍", text)
        self.assertFalse(contains_accusatory(text))

    def test_manager_note_pays_more_only(self):
        bundle = self._bundle()
        text = ba.format_manager_note("BISTRO7", ba.entries_for_outlet(bundle, "BISTRO7"))
        self.assertIn("🧾 Bill analysis — Bistro", text)
        self.assertNotIn("vilai eriyirukku", text)
        self.assertIn("• Bawang: Neenga RM3.60 (PASAR) · Vista RM3.00 (SAIDA) — 20% cheap", text)
        self.assertIn("cheapest-aa vaangurathu: Telur Gred A", text)
        self.assertFalse(contains_accusatory(text))

    def test_manager_note_empty_when_nothing_to_say(self):
        self.assertEqual(
            ba.format_manager_note("KLANG", {"increases": [], "pays_more": [], "cheapest": ["X"]}),
            "",
        )

    def test_manager_note_caps_long_lists(self):
        increases = [
            {"label": f"Item {i}", "shop": "S", "previous_price": 1.0,
             "new_price": 1.2, "change_pct": 20.0, "new_date": _iso(0)}
            for i in range(ba.MAX_MANAGER_ITEMS + 3)
        ]
        text = ba.format_manager_note("VISTA", {"increases": increases, "pays_more": [],
                                                "cheapest": []})
        self.assertIn("… innum 3 items", text)

    def test_delivery_summary(self):
        text = ba.format_owner_delivery_summary([
            {"outlet_code": "VISTA", "display": "Vista", "reason": "manager",
             "manager_name": "Kumar", "increases": 1, "pays_more": 2},
            {"outlet_code": "D", "display": "D.U", "reason": "no_manager",
             "increases": 0, "pays_more": 1},
        ], enabled=True)
        self.assertIn("• Vista: 1 increase(s), 2 item(s) cheaper elsewhere → Kumar", text)
        self.assertIn("• D.U: 0 increase(s), 1 item(s) cheaper elsewhere → (no manager registered — sent to you)", text)
        self.assertIn("🟢 LIVE", text)
        self.assertIn("🧪 TEST MODE", ba.format_owner_delivery_summary(
            [{"outlet_code": "VISTA", "reason": "delivery_disabled"}], enabled=False))
        self.assertEqual(ba.format_owner_delivery_summary([], True), "")

    def test_formatters_never_raise_on_garbage(self):
        self.assertEqual(ba.format_owner_price_report(None), "")
        self.assertEqual(ba.format_owner_outlet_report({"comparisons": [None]}), "")
        self.assertEqual(ba.format_manager_note(None, None), "")
        self.assertEqual(ba.format_owner_delivery_summary(None, True), "")


class GatherEndToEnd(unittest.TestCase):
    """Through the FakeSupabase and the cleaning pass: raw item_prices with
    an internal transfer, a legal-suffix variant and a future-dated row."""

    def setUp(self):
        self.db = FakeSupabase()
        self.db._store["item_prices"] = [
            _db_row("telur", "HANEE", "BISTRO7", 0.38, 1, days_ago=3),
            _db_row("telur", "SAIDA", "VISTA", 0.41, 2, days_ago=4),
            _db_row("telur", "SAIDA SDN BHD", "VISTA", 0.46, 3, days_ago=0,
                    created_at=(NOW - timedelta(hours=1)).isoformat()),
            # Internal transfer: must not become a shop price.
            _db_row("telur", "RESTORAN KHULAFA", "SEK20", 0.10, 4, days_ago=1),
            # Future-dated OCR garbage.
            _db_row("telur", "HANEE", "SEK20", 0.20, 5, days_ago=-30),
            # A different cut: never compared with gred A.
            _db_row("telur", "SAIDA", "SEK20", 0.90, 6, days_ago=1, raw="TELUR KAMPUNG"),
            # Utility receipt by type.
            _db_row("telur", "TNB", "SEK20", 0.05, 7, days_ago=1),
            # No canonical item: dropped by the bulk loader.
            _db_row(None, "SAIDA", "VISTA", 1.0, 8, days_ago=1),
        ]
        self.db._store["receipts"] = [
            {"id": 7, "receipt_type": "UTILITY", "merchant_canonical_id": None},
            {"id": 3, "receipt_type": "SUPPLIER_PURCHASE", "merchant_canonical_id": None},
        ]

    def test_bulk_loader_cleans_and_tags(self):
        rows, stats = load_all_price_rows_with_stats(self.db, lookback_days=90, today=TODAY)
        self.assertEqual(sorted(r["receipt_id"] for r in rows), [1, 2, 3, 6])
        self.assertEqual(stats["own_outlet"], 1)
        self.assertEqual(stats["bad_date"], 1)
        self.assertEqual(stats["non_supplier_receipt"], 1)
        self.assertEqual(stats["no_item"], 1)
        by_id = {r["receipt_id"]: r for r in rows}
        self.assertEqual(by_id[3]["shop"], "SAIDA")            # suffix stripped
        self.assertEqual(by_id[3]["shop_key"], by_id[2]["shop_key"])
        self.assertEqual(by_id[3]["outlet_code"], "VISTA")
        self.assertEqual(by_id[3]["canonical_item"], "telur")
        self.assertEqual(by_id[6]["variant"], "TELUR KAMPUNG")

    def test_bulk_loader_window(self):
        rows = load_all_price_rows_with_stats(self.db, lookback_days=2, today=TODAY)[0]
        self.assertEqual(sorted(r["receipt_id"] for r in rows), [3, 6])

    def test_gather_finds_the_increase_and_the_outlet_gap(self):
        bundle = ba.gather_bill_analysis(self.db, now=NOW, today=TODAY)
        self.assertEqual(len(bundle["increases"]), 1)
        e = bundle["increases"][0]
        self.assertEqual((e["shop"], e["outlet_code"]), ("SAIDA", "VISTA"))
        self.assertAlmostEqual(e["previous_price"], 0.41)
        self.assertEqual([s["shop"] for s in e["cheaper_shops"]], ["HANEE"])
        self.assertEqual([o["outlet"] for o in e["cheaper_outlets"]], ["Bistro"])
        labels = {c["label"]: c for c in bundle["comparisons"]}
        self.assertIn("Telur Gred A", labels)
        self.assertNotIn("Telur Kampung", labels)  # one outlet only
        self.assertEqual(labels["Telur Gred A"]["cheapest"]["outlet_code"], "BISTRO7")
        self.assertEqual(bundle["outlet_codes"], ["BISTRO7", "VISTA"])
        self.assertEqual(bundle["stats"]["new_bills"], 1)
        self.assertEqual(bundle["load_stats"]["own_outlet"], 1)

    def test_on_demand_report_all_items(self):
        text = ba.build_outlet_price_report(self.db, "", today=TODAY)
        self.assertIn("Telur Gred A:", text)
        self.assertIn("🥇 Bistro RM0.38 (HANEE)", text)

    def test_on_demand_report_one_item(self):
        text = ba.build_outlet_price_report(self.db, "eggs", today=TODAY)
        self.assertIn("Telur Gred A:", text)
        self.assertIn("Vista RM0.46 (SAIDA) +21%", text)

    def test_on_demand_report_unknown_and_single_outlet(self):
        self.assertIn("I don't know an item called", ba.build_outlet_price_report(
            self.db, "zzzz", today=TODAY))
        self.db._store["item_prices"] = [
            _db_row("gula", "SAIDA", "VISTA", 3.0, 1, days_ago=1),
        ]
        self.assertIn("only one outlet has bought it", ba.build_outlet_price_report(
            self.db, "gula", today=TODAY))

    def test_on_demand_report_nothing_comparable(self):
        self.db._store["item_prices"] = []
        self.assertIn("nothing to compare yet", ba.build_outlet_price_report(
            self.db, "", today=TODAY))

    def test_gather_never_raises(self):
        class Boom:
            def table(self, *_a, **_k):
                raise RuntimeError("db down")

        bundle = ba.gather_bill_analysis(Boom(), now=NOW, today=TODAY)
        self.assertEqual(bundle["increases"], [])
        self.assertEqual(bundle["comparisons"], [])
        self.assertEqual(bundle["outlet_codes"], [])
        text = ba.build_outlet_price_report(Boom(), "telur", today=TODAY)
        self.assertIsInstance(text, str)
        self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
