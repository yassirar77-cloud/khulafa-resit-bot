"""Pinpoint Target v2: shadow mode, known merchants, overbuy, sales-figure
audit, admin gating."""

import os
import re
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import known_merchants as km
import outside_purchase as op
import overbuy_check as ob
from tests.fake_supabase import FakeSupabase
from tests.test_outside_purchase import ALLOWED, ROSTER, SUPPLIERS, _at, _receipt, _utc

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MY = op.MY_TZ
NOW = datetime(2026, 9, 24, 21, 0, tzinfo=MY)

KNOWN = [
    {"id": 1, "outlet": "SEK-20", "canonical_merchant": "PASARAYA BORONG SNS ALI", "aliases": ["PASARAYA BORONG SNS ALT"],
     "bill_count": 31, "source": "history", "active": True},
    {"id": 2, "outlet": "SEK-20", "canonical_merchant": "EVEREST AISVARAM SDN. BHD.", "aliases": [],
     "bill_count": 48, "source": "history", "active": True},
    {"id": 3, "outlet": "Bistro", "canonical_merchant": "EVEREST AISVARAM SDN. BHD.", "aliases": [],
     "bill_count": 81, "source": "history", "active": True},
    {"id": 4, "outlet": "SEK-20", "canonical_merchant": "99 SPEED MART SDN. BHD.", "aliases": [],
     "bill_count": 9, "source": "history", "active": False},      # removed by /buang_merchant
]
CONFIG = {"suppliers": SUPPLIERS, "allowed": ALLOWED, "roster": ROSTER, "known": KNOWN}

SALES_FIGURE = re.compile(r"RM\s?\d|\d+(\.\d+)?\s?%|purata|average|avg|\bRM\b", re.IGNORECASE)


class ModeTests(unittest.TestCase):
    def test_default_is_shadow_and_live_rows_only_count_after_the_switch(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(op.mode(), op.SHADOW)
            self.assertFalse(op.is_live())
        with mock.patch.dict("os.environ", {"OUTSIDE_PURCHASE_MODE": "LIVE"}):
            self.assertTrue(op.is_live())
        with mock.patch.dict("os.environ", {"OUTSIDE_PURCHASE_MODE": "banana"}):
            self.assertEqual(op.mode(), op.SHADOW)
        history = [{"id": 1, "mode": "shadow"}, {"id": 2, "mode": "shadow"}, {"id": 3, "mode": "shadow"},
                   {"id": 4, "mode": "shadow"}, {"id": 5, "mode": "shadow"}, {"id": 6, "mode": "live"}]
        # Five shadow strikes, first live bill: the cashier hears "strike 1", not a scold.
        self.assertEqual(op.message_strike_no(history, 6), 1)
        self.assertEqual(op.tier_for(op.message_strike_no(history, 6)), op.INFO)
        self.assertEqual(op.tier_for(6), op.SCOLD)                 # management still sees 6
        self.assertIsNone(op.message_strike_no(history[:5], 5))    # nothing live yet -> nothing to say
        self.assertIsNone(op.message_strike_no(history, None))

    def test_rows_carry_the_mode_they_were_recorded_in(self):
        with mock.patch.dict("os.environ", {"OUTSIDE_PURCHASE_MODE": "shadow"}):
            result = op.evaluate(_receipt(merchant="KEDAI HARDWARE ALI"), CONFIG, group_code="SEK20", now=NOW)
            self.assertEqual(result["row"]["mode"], "shadow")
            self.assertEqual(result["row"]["source"], "new_merchant")
            result = op.evaluate(_receipt(merchant="LOTUS'S STORES"), CONFIG, group_code="SEK20", now=NOW)
            self.assertEqual(result["row"]["source"], "minimarket")
        with mock.patch.dict("os.environ", {"OUTSIDE_PURCHASE_MODE": "live"}):
            result = op.evaluate(_receipt(merchant="99 SPEED MART SDN. BHD."), CONFIG, group_code="SEK20", now=NOW)
            self.assertEqual((result["row"]["mode"], result["row"]["source"]), ("live", "minimarket"))

    def test_shadow_summary_lists_what_would_have_gone_out(self):
        rows = [
            {"id": 1, "outlet": "SEK-20", "cashier_name": "Syed", "status": op.COUNTED, "strike_no": 2,
             "merchant_raw": "LOTUS", "items": [{"canonical_item": "ayam", "qty": 2.0}],
             "created_at": "2026-09-24T10:00:00+08:00", "business_date": "2026-09-24", "mode": "shadow"},
            {"id": 2, "outlet": "SEK-6", "cashier_name": "Imdadul", "status": op.PENDING, "strike_no": None,
             "merchant_raw": "SAlDA", "items": [], "created_at": "2026-09-24T11:00:00+08:00",
             "business_date": "2026-09-24", "mode": "shadow"},
            {"id": 3, "outlet": "SEK-6", "cashier_name": "Imdadul", "status": op.COUNTED, "strike_no": 5,
             "merchant_raw": "GIANT", "items": [], "created_at": "2026-09-23T11:00:00+08:00",
             "business_date": "2026-09-23", "mode": "shadow"},
        ]
        flags = [{"id": 9, "outlet": "Vista", "cashier": "Buhari", "status": ob.SHADOW, "supplier": "BESTARI FARM",
                  "item": "ayam", "item_label": "Ayam", "qty": 40, "unit": "kg", "baseline_qty": 25,
                  "created_at": "2026-09-24T12:00:00+08:00", "business_date": "2026-09-24"}]
        text = op.shadow_summary(rows, flags, date(2026, 9, 24), threshold=5)
        self.assertIn("Syed · SEK-20 · new merchant #1 — strike 2 → reminder — LOTUS: Ayam x2", text)
        self.assertIn("Imdadul · SEK-6 · new merchant #2 — held for review (no message)", text)
        self.assertNotIn("GIANT", text)                 # yesterday's row is not today's summary
        self.assertIn("Buhari · Vista · overbuy #9 — question would be asked — BESTARI FARM: Ayam 40 kg (usual 25)", text)
        self.assertIn("2 outside-purchase row(s), 1 overbuy flag(s)", text)
        self.assertIn("Nothing would have been sent", op.shadow_summary([], [], date(2026, 9, 25)))


class KnownMerchantTests(unittest.TestCase):
    def test_known_for_the_outlet_is_skipped_new_elsewhere_is_pinned(self):
        # EVEREST is known at SEK-20 (and Bistro): not an outside purchase there.
        res = op.evaluate(_receipt(merchant="EVEREST AISVARAM SDN. BHD."), CONFIG, group_code="SEK20", now=NOW)
        self.assertEqual(res["action"], "skip")
        self.assertTrue(res["match"]["tier"].startswith("known_"))
        # The same shop at Vista has never been seen: pinned.
        res = op.evaluate(_receipt(merchant="EVEREST AISVARAM SDN. BHD.", outlet="Vista"), CONFIG,
                          group_code="VISTA", now=NOW)
        self.assertEqual(res["action"], "count")
        # Approved suppliers are known everywhere.
        self.assertEqual(op.evaluate(_receipt(merchant="BABAS", outlet="Vista"), CONFIG, group_code="VISTA",
                                     now=NOW)["action"], "skip")

    def test_ocr_drift_and_alias_still_match_a_known_merchant(self):
        for text in ("EVEREST AISVARAN SDN BHD", "EVERESTAISVARAM", "PASARAYA BORONG SNS ALT",
                     "PASARAYA BORONG SNS ALI (SEK 20)"):
            res = op.evaluate(_receipt(merchant=text), CONFIG, group_code="SEK20", now=NOW)
            self.assertEqual(res["action"], "skip", text)
        # Bestari stays an exact-merchant rule even through the known list.
        self.assertEqual(op.evaluate(_receipt(merchant="BESTARI MINIMART"), CONFIG, group_code="SEK20",
                                     now=NOW)["action"], "count")

    def test_removed_merchant_is_a_pin_target_again(self):
        res = op.evaluate(_receipt(merchant="99 SPEED MART SDN. BHD."), CONFIG, group_code="SEK20", now=NOW)
        self.assertEqual((res["action"], res["row"]["source"]), ("count", "minimarket"))

    def test_baseline_folds_variants_and_needs_three_bills(self):
        agg = {}
        rows = [
            {"chat_id": -500, "merchant": "SWEETTI FREEZEE ENTERPRISE", "receipt_date": "2026-09-01", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "SWEETTI FREEZE ENTERPRISE", "receipt_date": "2026-09-05", "receipt_type": "SUPPLIER_PURCHASE"},
            {"chat_id": -500, "merchant": "SWEETTI FREEZEE ENTERPRISE", "receipt_date": "2026-09-09", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "LOTUS", "receipt_date": "2026-09-09", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "LOTUS", "receipt_date": "2026-09-10", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "BABAS PRODUCTS SDN BHD", "receipt_date": "2026-09-10", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "BABAS", "receipt_date": "2026-09-11", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "BABAS", "receipt_date": "2026-09-12", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "KHULAFA SEK 6", "receipt_date": "2026-09-12", "receipt_type": "UNKNOWN"},
            {"chat_id": -500, "merchant": "GAJI ALI", "receipt_date": "2026-09-12", "receipt_type": "STAFF_ADVANCE"},
            {"chat_id": -999, "merchant": "LOTUS", "receipt_date": "2026-09-12", "receipt_type": "UNKNOWN", "outlet": None},
        ]
        km.aggregate_receipts(rows[:6], agg, group_codes={-500: "SEK20"})
        km.aggregate_receipts(rows[6:], agg, group_codes={-500: "SEK20"})
        self.assertEqual(agg[("SEK-20", "LOTUS")]["bills"], 2)
        self.assertNotIn(("SEK-20", "KHULAFA SEK 6"), agg)
        self.assertNotIn(("SEK-20", "GAJI ALI"), agg)
        baseline = km.build_baseline(agg, SUPPLIERS)
        names = {r["canonical_merchant"]: r for r in baseline}
        self.assertEqual(list(names), ["SWEETTI FREEZEE ENTERPRISE"])        # LOTUS: 2 bills; BABAS: approved
        self.assertEqual(names["SWEETTI FREEZEE ENTERPRISE"]["aliases"], ["SWEETTI FREEZE ENTERPRISE"])
        self.assertEqual(names["SWEETTI FREEZEE ENTERPRISE"]["bill_count"], 3)
        self.assertEqual(names["SWEETTI FREEZEE ENTERPRISE"]["outlet"], "SEK-20")

    def test_refresh_recounts_but_never_promotes_a_shop_cashiers_keep_using(self):
        db = FakeSupabase()
        db.table(op.SUPPLIERS_TABLE).insert([dict(s) for s in SUPPLIERS]).execute()
        db.table(op.ROSTER_TABLE).insert([dict(r) for r in ROSTER]).execute()
        db.table(km.TABLE).insert([{"outlet": "SEK-20", "canonical_merchant": "EVEREST AISVARAM SDN. BHD.",
                                    "aliases": [], "bill_count": 1, "source": "history", "active": True}]).execute()
        receipts = [{"chat_id": -500, "merchant": "LOTUS", "receipt_date": f"2026-09-{d:02d}", "receipt_type": "UNKNOWN"}
                    for d in range(1, 21)]          # a mini market used 20 times
        receipts += [{"chat_id": -500, "merchant": "EVEREST AISVARAM SDN BHD", "receipt_date": f"2026-09-{d:02d}",
                      "receipt_type": "UNKNOWN"} for d in (2, 9, 16)]
        db.table(km.RECEIPTS_TABLE).insert(receipts).execute()
        summary = km.refresh(db, SUPPLIERS, group_codes={-500: "SEK20"}, today=date(2026, 9, 24))
        rows = db.rows(km.TABLE)
        names = {(r["outlet"], r["canonical_merchant"]): r for r in rows}
        self.assertNotIn(("SEK-20", "LOTUS"), names)                     # never auto-known
        self.assertEqual(names[("SEK-20", "EVEREST AISVARAM SDN. BHD.")]["bill_count"], 3)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["seeded"], 0)
        # Approved suppliers got a row at every roster outlet.
        self.assertIn(("Bistro", "BABAS"), names)
        self.assertEqual(names[("Bistro", "BABAS")]["source"], "approved")
        self.assertEqual(summary["approved_added"], 3 * sum(1 for s in SUPPLIERS if s["active"]))
        # Second run: nothing new.
        again = km.refresh(db, SUPPLIERS, group_codes={-500: "SEK20"}, today=date(2026, 9, 24))
        self.assertEqual((again["updated"], again["approved_added"], again["seeded"]), (0, 0, 0))

    def test_first_run_seeds_the_baseline_once(self):
        db = FakeSupabase()
        db.table(km.RECEIPTS_TABLE).insert([
            {"chat_id": -500, "merchant": "PVS SANTAN MAJU ENTERPRISE", "receipt_date": f"2026-09-{d:02d}",
             "receipt_type": "UNKNOWN"} for d in (1, 5, 9, 13)]).execute()
        summary = km.refresh(db, SUPPLIERS, group_codes={-500: "SEK20"}, today=date(2026, 9, 24))
        self.assertEqual(summary["seeded"], 1)
        self.assertEqual(db.rows(km.TABLE)[0]["canonical_merchant"], "PVS SANTAN MAJU ENTERPRISE")

    def test_remove_and_formatting(self):
        db = FakeSupabase()
        db.table(km.TABLE).insert([dict(r) for r in KNOWN]).execute()
        row = km.remove(db, "SEK20", "pasaraya borong sns ali", removed_by=42)
        self.assertEqual((row["active"], row["removed_by"]), (False, 42))
        self.assertIsNone(km.remove(db, "SEK20", "NOBODY"))
        rows = db.rows(km.TABLE)
        text = km.format_known(rows, "SEK20")
        self.assertIn("EVEREST AISVARAM SDN. BHD. — 48 bil (sejarah)", text)
        self.assertNotIn("PASARAYA", text)
        report = km.format_report(KNOWN)
        self.assertIn("SEK-20 (2):", report)
        self.assertIn("31  PASARAYA BORONG SNS ALI", report)
        self.assertNotIn("99 SPEED", report)                                # inactive
        self.assertIn("⚠️", km.format_report([dict(KNOWN[3], active=True)]))  # mini market flagged


class OverbuyDetectionTests(unittest.TestCase):
    def _days(self, qtys, start=date(2026, 9, 1), gap=3):
        return [(start + timedelta(days=gap * i), q) for i, q in enumerate(qtys)]

    def test_cadence_adjusted_rates(self):
        days = self._days([30, 30, 30, 30, 30], gap=3)      # 10 kg/day
        current = date(2026, 9, 16)                          # 3 days after the last buy
        # 30 kg after 3 days is usual; 45 kg is +50% -> flag at 40%.
        self.assertFalse(ob.evaluate_item(30, days, current, pct=40)["flag"])
        v = ob.evaluate_item(45, days, current, pct=40)
        self.assertTrue(v["flag"])
        self.assertEqual((v["cover_days"], v["baseline_qty"], v["pct_over"]), (3, 30.0, 50.0))
        # A bigger delivery after a longer gap is NOT an overbuy.
        self.assertFalse(ob.evaluate_item(55, days, date(2026, 9, 19), pct=40)["flag"])   # 6 days -> usual 60
        # Same-day second bill adds up.
        days2 = days + [(current, 20)]
        self.assertTrue(ob.evaluate_item(25, days2, current, pct=40)["flag"])

    def test_too_few_purchases_means_no_baseline(self):
        v = ob.evaluate_item(99, self._days([10, 10, 10]), date(2026, 9, 20))
        self.assertEqual((v["flag"], v["reason"]), (False, "few_purchases"))
        self.assertTrue(ob.evaluate_item(99, self._days([10, 10, 10, 10]), date(2026, 9, 20))["flag"])

    def test_sales_summary_and_condition(self):
        y = date(2026, 9, 23)
        day_map = {y - timedelta(days=k): {"total": 1000.0 + 10 * k, "shifts": 2, "ids": [k]} for k in range(1, 15)}
        # Yesterday fully in (2 shifts), lower than the average -> condition holds.
        day_map[y] = {"total": 800.0, "shifts": 2, "ids": [99]}
        s = ob.sales_summary(day_map, y)
        self.assertEqual((s["yesterday"], s["days"], s["complete"]), (800.0, 14, True))
        self.assertEqual(ob.sales_condition(s), (True, None))
        self.assertEqual(ob.pct_drop(s), round((1 - 800 / s["avg"]) * 100, 1))
        # Sales up -> no flag.
        day_map[y] = {"total": 1500.0, "shifts": 2, "ids": [99]}
        self.assertEqual(ob.sales_condition(ob.sales_summary(day_map, y)), (False, "sales_up"))
        # Only one of the two shifts arrived -> POS missing, deferred.
        day_map[y] = {"total": 500.0, "shifts": 1, "ids": [99]}
        s = ob.sales_summary(day_map, y)
        self.assertIsNone(s["yesterday"])
        self.assertEqual(ob.sales_condition(s), (False, "pos_missing"))
        del day_map[y]
        self.assertEqual(ob.sales_condition(ob.sales_summary(day_map, y)), (False, "pos_missing"))
        # Too few POS days for an average.
        thin = {y: {"total": 100.0, "shifts": 1, "ids": [1]}, y - timedelta(days=1): {"total": 300.0, "shifts": 1, "ids": [2]}}
        self.assertEqual(ob.sales_condition(ob.sales_summary(thin, y)), (False, "no_sales_baseline"))

    def test_item_level_pos_mapping(self):
        rows = [{"sales_daily_id": 1, "item_name": "Nasi  Ayam Goreng", "qty": 40, "category": "MAKANAN"},
                {"sales_daily_id": 1, "item_name": "Ayam Goreng Staff", "qty": 5, "category": "MAKANAN"},
                {"sales_daily_id": 1, "item_name": "Mee Goreng Ayam", "qty": 9, "category": "THAI FOOD"},
                {"sales_daily_id": 1, "item_name": "Teh Tarik", "qty": 50, "category": "MINUMAN"},
                {"sales_daily_id": 2, "item_name": "Ayam Kicap", "qty": 12, "category": "MAKANAN"}]
        day_map = {date(2026, 9, 23): {"total": 1.0, "shifts": 2, "ids": [1, 2]}}
        self.assertEqual(ob.item_sales_by_day(rows, day_map, "ayam"), {date(2026, 9, 23): 52.0})
        self.assertEqual(ob.pos_base_qty("ikan", rows), 0.0)

    def _items(self, qty=45):
        return op.normalise_purchase_items([{"name": "AYAM BERSIH", "qty": qty, "price": 10.0},
                                            {"name": "ROTI CANAI", "qty": 50, "price": 0.5},
                                            {"name": "TELUR GRED A", "qty": None, "price": 14.0}])

    def _history(self):
        return {"ayam": self._days([30, 30, 30, 30, 30], gap=3)}

    def _sales(self, yesterday=800.0):
        return {"*": {"yesterday": yesterday, "avg": 1000.0, "days": 14, "complete": True}}

    def test_evaluate_bill_flags_and_skip_reasons(self):
        out = ob.evaluate_bill(self._items(), outlet="SEK20", supplier="BESTARI FARM (M) SDN BHD",
                               receipt_date=date(2026, 9, 16), total=45 * 10 + 25 + 14,
                               history_by_item=self._history(), sales_by_item=self._sales(), pct=40)
        self.assertEqual([f["item"] for f in out["flags"]], ["ayam"])
        flag = out["flags"][0]
        self.assertEqual((flag["qty"], flag["baseline_qty"], flag["unit"], flag["outlet"]), (45.0, 30.0, "kg", "SEK-20"))
        self.assertEqual((flag["yesterday_sales"], flag["avg_sales"], flag["pct_drop"], flag["sales_source"]),
                         (800.0, 1000.0, 20.0, "total"))
        reasons = {s["item"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(reasons, {"roti": "standing_order", "telur": "bad_qty"})
        # Item-level sales source when the mapping has it.
        out = ob.evaluate_bill(self._items(), outlet="SEK20", supplier="X", receipt_date=date(2026, 9, 16), total=None,
                               history_by_item=self._history(),
                               sales_by_item={"ayam": {"yesterday": 40.0, "avg": 50.0, "days": 14, "complete": True}})
        self.assertEqual(out["flags"][0]["sales_source"], "items")

    def test_skip_rules(self):
        base = dict(outlet="SEK20", supplier="S", receipt_date=date(2026, 9, 16), total=None,
                    history_by_item=self._history(), sales_by_item=self._sales(), pct=40)
        # Sales up -> no flag.
        out = ob.evaluate_bill(self._items(), **{**base, "sales_by_item": self._sales(1200.0)})
        self.assertEqual(out["flags"], [])
        self.assertIn({"item": "ayam", "reason": "sales_up"}, out["skipped"])
        # POS not in yet -> deferred.
        out = ob.evaluate_bill(self._items(), **{**base, "sales_by_item": {"*": {"yesterday": None, "avg": 1000.0}}})
        self.assertIn({"item": "ayam", "reason": "pos_missing"}, out["skipped"])
        # Bad OCR: lines sum to RM475, total says RM900 -> never judged.
        out = ob.evaluate_bill(self._items(), **{**base, "total": 900.0})
        self.assertEqual({s["reason"] for s in out["skipped"]}, {"bad_ocr_total"})
        # Too few purchases.
        out = ob.evaluate_bill(self._items(), **{**base, "history_by_item": {"ayam": self._days([30, 30])}})
        self.assertIn({"item": "ayam", "reason": "few_purchases"}, out["skipped"])
        # Holiday (yesterday or the bill day), standing order list, the catering outlet.
        out = ob.evaluate_bill(self._items(), **{**base, "holidays": [date(2026, 9, 15)]})
        self.assertEqual({s["reason"] for s in out["skipped"]}, {"holiday"})
        out = ob.evaluate_bill(self._items(), **{**base, "standing_items": {"ayam"}})
        self.assertIn({"item": "ayam", "reason": "standing_order"}, out["skipped"])
        out = ob.evaluate_bill(self._items(), **{**base, "outlet": "Jakel"})
        self.assertEqual({s["reason"] for s in out["skipped"]}, {"catering_outlet"})
        # Non-numeric qty is never accused.
        bad = op.normalise_purchase_items([{"name": "AYAM", "qty": "banyak", "price": 10}])
        out = ob.evaluate_bill(bad, **base)
        self.assertIn({"item": "ayam", "reason": "bad_qty"}, out["skipped"])


class OverbuyMessageTests(unittest.TestCase):
    FLAG = {"id": 7, "outlet": "SEK-20", "cashier": "Syed", "cashier_shift": "morning",
            "supplier": "BESTARI FARM (M) SDN BHD", "item": "ayam", "item_label": "Ayam", "qty": 45.0, "unit": "kg",
            "baseline_qty": 30.0, "cover_days": 3, "pct_over": 50.0, "yesterday_sales": 812.4, "avg_sales": 1033.0,
            "pct_drop": 21.4, "sales_source": "total", "sales_date": "2026-09-15", "business_date": "2026-09-16",
            "status": "pending", "unit_price": 10.0}

    def test_cashier_question_has_no_sales_figure_but_management_alert_does(self):
        q = ob.cashier_question(self.FLAG, "bm")
        self.assertEqual(q, "📦 Jualan semalam lebih rendah dari biasa.\n"
                            "Tapi bil Bestari Farm hari ini: Ayam 45 kg (biasa 30 kg).\n"
                            "Kenapa beli lebih?")
        both = ob.cashier_question(self.FLAG)
        self.assertIn("நேத்து sales வழக்கத்தை விட குறைவு", both)
        for lang in ("bm", "tamil", "english", "bm_tamil"):
            text = ob.cashier_question(self.FLAG, lang)
            self.assertIsNone(SALES_FIGURE.search(text), (lang, text))
            self.assertNotIn("812", text)
            self.assertNotIn("1033", text)
        alert = ob.management_alert(self.FLAG)
        self.assertIn("RM812", alert)
        self.assertIn("RM1,033", alert)
        self.assertIn("-21% vs avg", alert)
        self.assertIn("Cashier: Syed", alert)
        self.assertIn("Terima = reason accepted", alert)
        shadow = ob.management_alert(self.FLAG, shadow=True)
        self.assertIn("[SHADOW]", shadow)
        self.assertNotIn("Terima =", shadow)

    def test_buttons_and_prompts(self):
        buttons = ob.reason_buttons(7)
        self.assertEqual([d for _, d in buttons], ["ov:7:stock", "ov:7:order", "ov:7:supplier", "ov:7:other"])
        self.assertEqual(buttons[0][0], "Stok habis / Stock தீர்ந்துடுச்சு")
        self.assertEqual([d for _, d in ob.decision_buttons(7)], ["ovm:7:accept", "ovm:7:reject"])
        self.assertIn("taip sebabnya", ob.other_prompt("bm"))
        self.assertIn("Stok habis", ob.thanks_text("Stok habis", "bm"))
        for _label, data in buttons:
            self.assertLessEqual(len(data.encode()), 64)

    def test_strike_tiers_without_sales_figures(self):
        history = [dict(self.FLAG, id=i, business_date=f"2026-09-{10 + i:02d}", status=ob.NO_REPLY) for i in range(1, 6)]
        for n, marker in ((1, "ℹ️"), (2, "kali ke-2"), (4, "amaran terakhir"), (5, "🛑"), (6, "🛑")):
            text = ob.strike_message(dict(self.FLAG, status=ob.REJECTED), n, history[:n], "bm", threshold=5, window_days=30)
            self.assertIn(marker, text, n)
            self.assertIsNone(SALES_FIGURE.search(text), (n, text))
        scold = ob.strike_message(self.FLAG, 5, history, "bm", threshold=5)
        self.assertEqual(scold.count("• "), 5)
        self.assertIn("Pengurusan telah dimaklumkan", scold)
        self.assertIn("Ayam 45 kg (biasa 30 kg)", scold)
        for word in ("bodoh", "bangang", "sial", "stupid", "idiot", "india", "bangla", "melayu", "agama"):
            self.assertNotIn(word, ob.strike_message(self.FLAG, 7, history, "bm_tamil").lower())
        report = ob.management_strike_report(dict(self.FLAG, status=ob.NO_REPLY), 5, history)
        self.assertIn("strike 5 (threshold 5", report)
        self.assertIn("no reply in time", report)
        self.assertIn("RM", ob.no_reply_alert(self.FLAG) + ob.management_alert(self.FLAG))

    def test_summary_and_monthly_section(self):
        rows = [dict(self.FLAG, id=1, status=ob.REJECTED, reason_code="stock"),
                dict(self.FLAG, id=2, status=ob.ACCEPTED, reason_code="order"),
                dict(self.FLAG, id=3, status=ob.NO_REPLY),
                dict(self.FLAG, id=4, status=ob.SHADOW)]
        text = ob.format_summary(rows, "SEK20", 30)
        self.assertIn("• Syed (SEK-20) · Ayam: 3x · +45 kg · RM450.00 ·", text)
        self.assertIn("Stock ran out ✖", text)
        self.assertIn("no_reply 1", text)
        self.assertIn("Tiada flag", ob.format_summary([], None, 7))
        month = ob.monthly_section(rows, 2026, 9)
        self.assertIn("• SEK-20: 3 flag · 2 strike · 1 diterima · lebih ≈RM450 · Syed 3", month)
        self.assertIn("tiada bulan ini", ob.monthly_section(rows, 2026, 8))


class OverbuyFlowTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeSupabase()
        self.db.table(op.ROSTER_TABLE).insert([dict(r) for r in ROSTER]).execute()
        hist = []
        for i, d in enumerate(("2026-09-01", "2026-09-04", "2026-09-07", "2026-09-10", "2026-09-13")):
            hist.append({"receipt_id": 10 + i, "receipt_date": d, "merchant": "BESTARI FARM (M) SDN BHD",
                         "canonical_item": "ayam", "qty": 30, "outlet_code": "SEK20", "chat_id": -500})
        hist.append({"receipt_id": 50, "receipt_date": "2026-09-13", "merchant": "LOTUS",
                     "canonical_item": "ayam", "qty": 200, "outlet_code": "SEK20", "chat_id": -500})   # other shop
        self.db.table(ob.ITEM_PRICES_TABLE).insert(hist).execute()
        sales = []
        for k in range(0, 15):
            d = (date(2026, 9, 15) - timedelta(days=k)).isoformat()
            total = 400.0 if k == 0 else 500.0 + 5 * k
            for shift in ("day", "overnight"):
                sales.append({"outlet_canonical": "SEK-20", "outlet_code": "S-SEK20", "shift_business_date": d,
                              "shift_type": shift, "total_sales": total})
        self.db.table(ob.SALES_DAILY_TABLE).insert(sales).execute()

    def _bill(self, qty=45, **over):
        fields = dict(id=100, merchant="BESTARI FARM (M) SDN BHD", receipt_date="2026-09-16", total=qty * 10.0,
                      items=[{"name": "AYAM BERSIH", "qty": qty, "price": 10.0}],
                      created_at=_utc(datetime(2026, 9, 16, 10, 0, tzinfo=MY)))
        fields.update(over)
        return _receipt(**fields)

    def test_live_flag_pending_shadow_flag_recorded_only(self):
        with mock.patch.dict("os.environ", {"OUTSIDE_PURCHASE_MODE": "shadow"}):
            res = ob.process_bill(self.db, self._bill(), group_code="SEK20", roster=ROSTER)
        self.assertEqual(len(res["flags"]), 1)
        flag = res["flags"][0]
        self.assertEqual((flag["status"], flag["mode"], flag["cashier"]), (ob.SHADOW, "shadow", "Syed"))
        self.assertIsNone(flag["asked_at"])
        self.assertEqual(flag["yesterday_sales"], 800.0)
        self.assertEqual(flag["sales_source"], "total")
        # The same bill is not flagged twice; a shadow flag never expires into a strike.
        self.assertEqual(ob.process_bill(self.db, self._bill(), group_code="SEK20", roster=ROSTER)["flags"], [])
        self.assertEqual(ob.expire_no_reply(self.db, now=datetime(2026, 9, 18, tzinfo=MY)), [])
        with mock.patch.dict("os.environ", {"OUTSIDE_PURCHASE_MODE": "live"}):
            res = ob.process_bill(self.db, self._bill(id=101, created_at=_utc(datetime(2026, 9, 16, 11, tzinfo=MY))),
                                  group_code="SEK20", roster=ROSTER)
        live = res["flags"][0]
        self.assertEqual((live["status"], live["mode"]), (ob.PENDING, "live"))
        self.assertIsNotNone(live["asked_at"])

    def test_usual_quantity_and_sales_up_are_quiet(self):
        res = ob.process_bill(self.db, self._bill(qty=31), group_code="SEK20", roster=ROSTER)
        self.assertEqual(res["flags"], [])
        self.assertIn({"item": "ayam", "reason": "within_usual"}, res["skipped"])
        # Yesterday above average -> no question even for a big bill.
        self.db.table(ob.SALES_DAILY_TABLE).update({"total_sales": 900.0}).eq("shift_business_date", "2026-09-15").execute()
        res = ob.process_bill(self.db, self._bill(), group_code="SEK20", roster=ROSTER)
        self.assertIn({"item": "ayam", "reason": "sales_up"}, res["skipped"])

    def test_pos_missing_is_deferred_not_flagged(self):
        self.db.table(ob.SALES_DAILY_TABLE).delete().eq("shift_business_date", "2026-09-15").execute()
        res = ob.process_bill(self.db, self._bill(), group_code="SEK20", roster=ROSTER)
        self.assertEqual(res["flags"], [])
        self.assertTrue(res["sales_missing"])

    def test_answer_decide_no_reply_and_separate_counter(self):
        with mock.patch.dict("os.environ", {"OUTSIDE_PURCHASE_MODE": "live"}):
            flag = ob.process_bill(self.db, self._bill(), group_code="SEK20", roster=ROSTER)["flags"][0]
            # Button answer.
            row = ob.answer(self.db, flag["id"], "stock")
            self.assertEqual((row["status"], row["reason_code"]), (ob.ANSWERED, "stock"))
            # Typed reason after Lain-lain.
            ob.set_fields(self.db, flag["id"], prompt_message_id=555)
            found = ob.flag_by_prompt(self.db, -500, 555)
            self.assertEqual(found["id"], flag["id"])
            row = ob.answer(self.db, flag["id"], "other", "  ada  tempahan 200 pax ")
            self.assertEqual((row["reason_code"], row["reason"]), ("other", "ada tempahan 200 pax"))
            # Management accepts: no strike.
            out = ob.decide(self.db, flag["id"], True, decided_by=42)
            self.assertEqual((out["row"]["status"], out["strike_no"]), (ob.ACCEPTED, None))
            self.assertIsNone(ob.decide(self.db, flag["id"], False))            # already decided
            # Second flag rejected: overbuy strike 1.
            flag2 = ob.process_bill(self.db, self._bill(id=101, qty=50, created_at=_utc(datetime(2026, 9, 16, 12, tzinfo=MY))),
                                    group_code="SEK20", roster=ROSTER)["flags"][0]
            out = ob.decide(self.db, flag2["id"], False, decided_by=42)
            self.assertEqual((out["row"]["status"], out["strike_no"]), (ob.REJECTED, 1))
            # Third flag unanswered for 12h -> no_reply: overbuy strike 2.
            flag3 = ob.process_bill(self.db, self._bill(id=102, qty=60, created_at=_utc(datetime(2026, 9, 16, 13, tzinfo=MY))),
                                    group_code="SEK20", roster=ROSTER)["flags"][0]
            asked = datetime.fromisoformat(flag3["asked_at"])
            self.assertEqual(ob.expire_no_reply(self.db, now=asked + timedelta(hours=11)), [])
            expired = ob.expire_no_reply(self.db, now=asked + timedelta(hours=12, minutes=1))
            self.assertEqual([(e["row"]["status"], e["strike_no"]) for e in expired], [(ob.NO_REPLY, 2)])
            self.assertEqual([h["id"] for h in expired[0]["history"]], [flag2["id"], flag3["id"]])
        # Overbuy strikes never touch the outside-purchase counter and vice versa.
        self.assertEqual(op.counted_in_window(self.db.rows(op.TABLE), "SEK-20", "Syed", date(2026, 9, 16), 30), [])
        self.db.table(op.TABLE).insert({"outlet": "SEK-20", "cashier_name": "Syed", "status": op.COUNTED,
                                        "business_date": "2026-09-16", "mode": "live"}).execute()
        self.assertEqual(len(op.counted_in_window(self.db.rows(op.TABLE), "SEK-20", "Syed", date(2026, 9, 16), 30)), 1)
        self.assertEqual(len(ob.counted_in_window(self.db.rows(ob.TABLE), "SEK-20", "Syed", date(2026, 9, 16), 30)), 2)


class StaffPaymentTests(unittest.TestCase):
    PAYROLL = ("LEAVE PAY", "LEEVE PAY.", "LENE PAY", "L TAVE PAY", "O.T PAY", "TONYAM GAJI", "SALARY TANGGARI",
               "SALARV VOUCHER OVERTIME", "USTAD", "SURAU TAMAN PERANGSANG PERMAI", "RIZAL PINJAM", "ADVANCE",
               "PAYOUT", "CUTI CASH", "SALARY ADVANCE REQUIREMENT FORM")
    SHOPS = ("DAILY PAY", "PAY TO GRAB", "BESTARI FARM (M) SDN BHD", "PASAR MINI A M", "EVEREST AISVARAM SDN. BHD.",
             "LOTUS'S STORES", "KEDAI HARDWARE ALI")

    def test_detection(self):
        for name in self.PAYROLL:
            self.assertTrue(op.is_staff_payment(name), name)
        for name in self.SHOPS:
            self.assertFalse(op.is_staff_payment(name), name)
        # A payroll line inside an otherwise blank receipt counts too.
        self.assertTrue(op.is_staff_payment(None, [{"name": "Gaji Ali September", "qty": 1, "price": 1800}]))

    def test_payroll_is_outside_the_whole_flow(self):
        for name in ("LEAVE PAY", "USTAD", "TONYAM GAJI"):
            res = op.evaluate(_receipt(merchant=name), CONFIG, group_code="SEK20", now=NOW)
            self.assertEqual((res["action"], res["match"]["tier"]), ("skip", "payroll"), name)
        db = FakeSupabase()
        self.assertIsNone(op.process_receipt(db, _receipt(merchant="LEAVE PAY"), group_code="SEK20", now=NOW))
        self.assertEqual(db.rows(op.TABLE), [])
        res = ob.process_bill(db, _receipt(merchant="LEAVE PAY", items=[{"name": "AYAM", "qty": 99, "price": 1}]),
                              group_code="SEK20", roster=ROSTER)
        self.assertEqual(res, {"flags": [], "skipped": [], "sales_missing": False})
        # Never seeded as a known merchant either.
        agg = km.aggregate_receipts([{"chat_id": -500, "merchant": "LEAVE PAY", "receipt_date": f"2026-09-{d:02d}",
                                      "receipt_type": "UNKNOWN"} for d in (1, 2, 3, 4)], group_codes={-500: "SEK20"})
        self.assertEqual(agg, {})


class OutletGroupSalesDataTests(unittest.TestCase):
    """No outlet-group message carries units sold per dish or a food-cost %."""

    def test_food_cost_never_leaves_the_director_chat(self):
        import group_reports as gr

        for env in ({}, {"GROUP_MONEY_REPORTS": "all"}):
            with mock.patch.dict("os.environ", env, clear=True):
                self.assertTrue(gr.blocked(gr.FOOD_COST, -500, -100))     # outlet group
                self.assertTrue(gr.blocked(gr.FOOD_COST, 777, -100))      # manager DM
                self.assertFalse(gr.blocked(gr.FOOD_COST, -100, -100))    # director chat

    def test_kitchen_recap_shows_gaps_not_units_sold(self):
        import kitchen_usage as ku

        evals = [
            ku.evaluate_usage("ayam_goreng", 100, 0, [{"item_name": "Ayam Goreng", "qty": 80}]),
            ku.evaluate_usage("kambing", 5.0, 4.5, [{"item_name": "Kambing", "qty": 3}]),
        ]
        for lang in ("bm", "tamil", "english"):
            text = ku.render_mini_summary("SEK-6", "2026-06-22", evals, lang)
            for figure in ("100", "80", "0.5", "0.54"):
                self.assertNotIn(figure, text, (lang, text))
            self.assertIn("20 pcs", text)
        pandari = ku.render_pandari_wastage("SEK-6", "2026-06-22", ku.leak_items(evals))
        manager = ku.render_manager_wastage("SEK-6", "2026-06-22", ku.leak_items(evals))
        for text in (pandari, manager):
            self.assertNotIn("100", text)
            self.assertNotIn("80", text)
            self.assertNotIn("POS jual", text)
        pos_only = ku.render_pos_only_summary("SEK-6", "2026-06-22", [
            {"code": "ayam_goreng", "label": "Ayam Goreng", "unit": "pcs", "used": None, "pos": 96.0,
             "flag": None, "source": "pos"}])
        self.assertNotIn("96", pos_only)
        # Management keeps the numbers.
        self.assertIn("guna 100 vs POS 80 pcs", ku.render_mini_summary_full("SEK-6", "2026-06-22", evals))


class AdminGateTests(unittest.TestCase):
    def test_admin_allowed(self):
        reviewers = {42}
        is_rev = lambda uid: uid in reviewers  # noqa: E731
        self.assertTrue(op.admin_allowed(-100, 7, -100, is_rev))        # inside the director chat
        self.assertTrue(op.admin_allowed(-500, 42, -100, is_rev))       # reviewer in an outlet group
        self.assertTrue(op.admin_allowed(42, 42, -100, is_rev))         # reviewer DM
        self.assertFalse(op.admin_allowed(-500, 7, -100, is_rev))       # cashier in an outlet group
        self.assertFalse(op.admin_allowed(7, 7, -100, is_rev))          # stranger DM
        self.assertFalse(op.admin_allowed(None, None, -100, is_rev))
        self.assertFalse(op.admin_allowed(-500, 7, None, lambda uid: (_ for _ in ()).throw(ValueError())))


class CashierTextAuditTests(unittest.TestCase):
    """No cashier-facing text in the bot carries a sales RM, average or %."""

    def test_pinpoint_texts(self):
        import staff_ops

        sig = {"direction": "low", "count": 613, "usual": 900}
        for lang in ("bm", "tamil", "english", "bengali", "indonesian"):
            for full in (True, False):
                self.assertIsNone(SALES_FIGURE.search(staff_ops.sales_text(sig, 1, lang, full_day=full)))
        for tier_text in op._TEXTS.values():
            for text in tier_text.values():
                self.assertNotIn("{sales", text)
        from item_sales_watch import format_manager_slow_items
        text = format_manager_slow_items({"display": "Bistro", "business_date": "2026-09-15",
                                          "flags": [{"label": "NAAN", "qty": 50.0, "median": 120.0, "drop_pct": 58, "streak": 1}]})
        self.assertIsNone(SALES_FIGURE.search(text))
        self.assertNotIn("120", text)
        from overbuy_watch import format_manager_overbuy
        text = format_manager_overbuy({"outlet": "SEK-20", "week_sales": 10200.0, "base_weekly_sales": 13100.0,
                                       "week_purchases": 4900.0, "base_weekly_purchases": 5100.0,
                                       "sales_drop_pct": 22.1, "purchase_drop_pct": 3.9, "items": []})
        self.assertIsNone(SALES_FIGURE.search(text))

    def test_bot_wiring_drops_the_sales_metric_for_cashier_anomaly_questions(self):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            src = f.read()
        start = src.index("def _anomaly_metrics(")
        body = src[start:src.index("\ndef ", start + 1)]
        self.assertNotIn('"metric": "sales"', body)
        ops = src[src.index("def _ops_message("):]
        ops = ops[:ops.index("\ndef ", 1)]
        self.assertIn('if k not in ("count", "usual")', ops)


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()
        with open(os.path.join(REPO_ROOT, "migrations", "0059_pinpoint_v2.sql")) as f:
            cls.sql = f.read()

    def _block(self, marker):
        start = self.src.index(marker)
        end = self.src.find("\nasync def ", start + 1)
        return self.src[start:end if end != -1 else len(self.src)]

    def test_one_bill_one_message(self):
        photo = self._block("async def handle_photo(")
        self.assertIn("pinpoint_sent, outcome = await outside_purchase_on_upload(context, stored, message)", photo)
        self.assertIn('if outcome == "skip" and not pinpoint_sent:', photo)
        self.assertIn("pinpoint_sent = await overbuy_on_upload(context, stored, message)", photo)
        self.assertIn('if staff_ops.is_minimarket(stored.get("merchant")) and not pinpoint_sent:', photo)
        self.assertIn('if not staff_ops.is_minimarket(stored.get("merchant")) and not pinpoint_sent:', photo)
        # The pinpoint hooks run BEFORE the mini-market question.
        self.assertLess(photo.index("await outside_purchase_on_upload("), photo.index("supplier=False)"))

    def test_shadow_mode_sends_nothing_to_cashiers(self):
        send = self._block("async def _outside_send_strike(")
        self.assertIn("if not outside_purchase.is_live():", send)
        self.assertLess(send.index("if not outside_purchase.is_live():"), send.index("bot.send_message(\n                chat_id=chat_id"))
        over = self._block("async def overbuy_on_upload(")
        self.assertIn('live = flag.get("status") == overbuy_check.PENDING', over)
        self.assertIn("if live:", over)
        self.assertIn("_overbuy_alert_management(context.bot, flag, shadow=not live)", over)
        strike = self._block("async def _overbuy_send_strike(")
        self.assertIn('if outside_purchase.is_live() and row.get("chat_id"):', strike)
        self.assertIn("outside_purchase.live_rows(history)", strike)

    def test_overbuy_buttons_typed_reason_and_jobs(self):
        self.assertIn(r'pattern=r"^ov:\d+:(stock|order|supplier|other)$"', self.src)
        self.assertIn(r'CallbackQueryHandler(handle_overbuy_decision, pattern=r"^ovm:\d+:(accept|reject)$")', self.src)
        self.assertIn("handle_overbuy_reason_text", self.src[self.src.index("async def run_bot("):])
        self.assertIn("overbuy_check.flag_by_prompt", self._block("async def handle_overbuy_reason_text("))
        for job in ('id="overbuy_no_reply_tick"', 'id="known_merchants_refresh"', 'id="pinpoint_shadow_summary"'):
            self.assertIn(job, self.src)
        decision = self._block("async def handle_overbuy_decision(")
        self.assertIn("outside_purchase.admin_allowed(", decision)

    def test_admin_commands_gated_and_registered(self):
        for name, fn in (("lebih_beli", "lebih_beli_command"), ("merchant_known", "merchant_known_command"),
                         ("buang_merchant", "buang_merchant_command"), ("pinpoint_shadow", "pinpoint_shadow_command"),
                         ("beli_luar", "beli_luar_command"), ("beli_luar_cashier", "beli_luar_cashier_command"),
                         ("izin", "izin_command"), ("bukan_beli_luar", "bukan_beli_luar_command"),
                         ("tambah_supplier", "tambah_supplier_command")):
            self.assertIn(f'CommandHandler("{name}", {fn})', self.src)
            self.assertIn("_outside_admin(update)", self._block(f"async def {fn}("), fn)
        gate = self.src[self.src.index("def _outside_admin("):]
        self.assertIn("outside_purchase.admin_allowed(", gate[:600])

    def test_monthly_close_has_both_sections(self):
        block = self._block("async def _with_outside_section(")
        self.assertIn("outside_purchase.monthly_section(rows, year, month)", block)
        self.assertIn("overbuy_check.monthly_section(flags, year, month)", block)

    def test_migration_rls_and_seeds(self):
        for table in ("outlet_known_merchants", "overbuy_flags", "holiday_calendar"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS public.{table}", self.sql)
            self.assertIn(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;", self.sql)
            self.assertIn(f"CREATE POLICY {table}_service ON public.{table}", self.sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'shadow'", self.sql)
        self.assertIn("HAVING count(*) >= 3", self.sql)
        for holiday in ("Hari Raya Aidilfitri", "Deepavali", "Chinese New Year", "Merdeka Day"):
            self.assertIn(holiday, self.sql)
        self.assertIn("'2027-", self.sql)

    def test_config_documented(self):
        with open(os.path.join(REPO_ROOT, ".env.example")) as f:
            env = f.read()
        for key in ("OUTSIDE_PURCHASE_MODE=shadow", "OVERBUY_PCT=40"):
            self.assertIn(key, env)


if __name__ == "__main__":
    unittest.main()
