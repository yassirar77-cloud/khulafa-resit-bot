"""Staff questions v2 (staff_ops): detection rules, texts, buttons, limits."""
import unittest
from datetime import date, datetime, timedelta, timezone

import staff_chat
import staff_live
import staff_ops

MYT = timezone(timedelta(hours=8))
LANGS = ("bm", "tamil", "bengali", "english", "indonesian")


class MiniMarketTests(unittest.TestCase):
    def test_mini_markets(self):
        for name in ("99 SPEED MART SDN. BHD.", "999 SPEED MART SDN. BHD.", "7-ELEVEN MALAYSIA",
                     "FAMILYMART", "TESCO", "RAYYAN MINI MARKET", "PASARAYA EASA",
                     "PASARAYA LONGBER", "KEDAI RUNCIT AH HOCK"):
            self.assertTrue(staff_ops.is_minimarket(name), name)

    def test_wholesaler_and_suppliers_excluded(self):
        for name in ("PASARAYA BORONG SNS ALI", "BESTARI POULTRY", "FRIZZ STATION", None, ""):
            self.assertFalse(staff_ops.is_minimarket(name), name)

    def test_item_list_drops_codes(self):
        text = staff_ops.minimarket_items(
            ["5594 SONGKHLA AIR LIMAU 1L", "GULA", "TELUR", "SUSU"])
        self.assertTrue(text.startswith("Songkhla Air Limau 1L, Gula, Telur"))
        self.assertTrue(text.endswith("(+1)"))


def _hist(merchant, item, qtys, start, step=7):
    return [{"merchant": merchant, "canonical_item": item, "qty": q,
             "receipt_date": (start - timedelta(days=step * (i + 1))).isoformat(),
             "receipt_id": 1000 + i} for i, q in enumerate(qtys)]


class InvoiceTests(unittest.TestCase):
    D = date(2026, 9, 24)

    def test_high(self):
        hist = _hist("BESTARI", "ayam", [55, 50, 60, 55], self.D)
        flag = staff_ops.invoice_flag([{"canonical_item": "ayam", "qty": 80}], hist,
                                      receipt_date=self.D, merchant="BESTARI", outlet_days=30)
        self.assertEqual(flag["kind"], "high")
        self.assertEqual(flag["usual"], 55)

    def test_normal(self):
        hist = _hist("BESTARI", "ayam", [55, 50, 60, 55], self.D)
        self.assertIsNone(staff_ops.invoice_flag(
            [{"canonical_item": "ayam", "qty": 62}], hist,
            receipt_date=self.D, merchant="BESTARI", outlet_days=30))

    def test_other_supplier_history_not_used(self):
        hist = _hist("OTHER", "ayam", [10, 10, 10, 10], self.D)
        self.assertIsNone(staff_ops.invoice_flag(
            [{"canonical_item": "ayam", "qty": 80}], hist,
            receipt_date=self.D, merchant="BESTARI", outlet_days=30))

    def test_rare(self):
        hist = _hist("X", "ayam", [5] * 30, self.D, step=1)
        flag = staff_ops.invoice_flag([{"canonical_item": "tepung dhall", "qty": 2}], hist,
                                      receipt_date=self.D, merchant="EASA", outlet_days=30)
        self.assertEqual(flag["kind"], "rare")

    def test_rare_needs_history(self):
        self.assertIsNone(staff_ops.invoice_flag(
            [{"canonical_item": "tepung dhall", "qty": 2}], [],
            receipt_date=self.D, merchant="EASA", outlet_days=5))


class SalesTests(unittest.TestCase):
    def test_low_high_none(self):
        self.assertEqual(staff_ops.sales_signal(613, [900, 880, 920])["direction"], "low")
        self.assertEqual(staff_ops.sales_signal(613, [900, 880, 920])["usual"], 900)
        self.assertEqual(staff_ops.sales_signal(1100, [900, 880, 920])["direction"], "high")
        self.assertIsNone(staff_ops.sales_signal(850, [900, 880, 920]))
        self.assertIsNone(staff_ops.sales_signal(500, [900, 880]))   # too little history

    def test_texts_have_no_money(self):
        sig = {"direction": "low", "count": 613, "usual": 900}
        for full in (True, False):
            for lang in LANGS:
                text = staff_ops.sales_text(sig, 2, lang, full_day=full)
                self.assertIn("613", text)
                self.assertNotIn("RM", text)
        self.assertIn("shift", staff_ops.sales_text(sig, 2, "english", full_day=False))
        self.assertNotIn("shift", staff_ops.sales_text(sig, 2, "english", full_day=True))


class ItemDropTests(unittest.TestCase):
    def _daily(self, recent_vadai, shop_other=100):
        daily = {}
        start = date(2026, 8, 20)
        for i in range(21):
            d = (start + timedelta(days=i)).isoformat()
            daily[d] = {"VADAI": 40 if i < 18 else recent_vadai, "ROTI CANAI": shop_other}
        return daily

    def test_drop(self):
        drop = staff_ops.item_drop(self._daily(20))
        self.assertEqual(drop["item"], "VADAI")
        self.assertEqual(drop["label"], "Vadai")

    def test_no_drop_and_repeat_guard(self):
        self.assertIsNone(staff_ops.item_drop(self._daily(38)))
        self.assertIsNone(staff_ops.item_drop(self._daily(20), asked_recently=["vadai"]))


class LimitTests(unittest.TestCase):
    def test_may_send(self):
        self.assertTrue(staff_ops.may_send("leftover", 0))
        self.assertTrue(staff_ops.may_send("wastage", 3))
        self.assertFalse(staff_ops.may_send("afternoon", 4))
        self.assertTrue(staff_ops.may_send("bills", 4))          # bills always go
        self.assertFalse(staff_ops.may_send("invoice", 2, events_today=2))
        self.assertTrue(staff_ops.may_send("minimarket", 2, events_today=1))

    def test_worst_case_is_five(self):
        sent = events = 0
        for slot in ("leftover", "invoice", "sales", "minimarket", "wastage",
                     "invoice", "afternoon", "bills"):
            if staff_ops.may_send(slot, sent, events):
                sent += 1
                events += slot in staff_ops.EVENT_SLOTS
        self.assertEqual(sent, 5)

    def test_count_today(self):
        now = datetime(2026, 9, 25, 12, tzinfo=MYT)
        rows = [
            {"outlet_code": "SEK20", "slot": "leftover", "status": "answered",
             "asked_at": "2026-09-24T19:00:00+00:00"},              # 03:00 MYT on the 25th
            {"outlet_code": "SEK20", "slot": "invoice", "status": "open",
             "asked_at": now.isoformat()},
            {"outlet_code": "SEK20", "slot": "sales", "status": "info",
             "asked_at": now.isoformat()},
            {"outlet_code": "SEK20", "slot": "minimarket", "status": "dropped",
             "asked_at": now.isoformat()},
            {"outlet_code": "SEK20", "slot": "bills", "status": "answered",
             "asked_at": "2026-09-24T13:05:00+00:00"},             # yesterday
            {"outlet_code": "JAKEL", "slot": "sales", "status": "info",
             "asked_at": now.isoformat()},
        ]
        self.assertEqual(staff_ops.count_today(rows, "SEK20", now.date(), MYT), (3, 1))


class ButtonTests(unittest.TestCase):
    def test_labels_short_and_complete(self):
        for lang, labels in staff_ops.LABELS.items():
            for set_key, codes in staff_ops.BUTTON_SETS.items():
                for code in codes:
                    self.assertIn(code, labels, (lang, code))
                    self.assertLessEqual(len(labels[code].split()), 3, (lang, labels[code]))

    def test_registered_in_staff_live(self):
        self.assertEqual(staff_live.button_set("invoice", {"kind": "high"}), "invoice")
        self.assertEqual(staff_live.button_set("invoice", {"kind": "rare"}), "rare")
        self.assertEqual(staff_live.button_set("minimarket", {}), "minimarket")
        self.assertEqual(staff_live.button_set("afternoon", {"item": "VADAI"}), "taste")
        self.assertIsNone(staff_live.button_set("afternoon", {"tip": 3}))
        self.assertEqual(staff_live.button_set("leftover", {}), "leftover")
        self.assertEqual(staff_live.button_set("bills", {}), "bills")   # unchanged
        self.assertEqual(staff_live.parse_callback("sc:12:suddenout"), (12, "suddenout"))

    def test_keyboard_layout(self):
        rows = staff_live.keyboard(5, "minimarket", "tamil")
        self.assertEqual([len(r) for r in rows], [2, 2])
        self.assertEqual(rows[0][0][1], "sc:5:nodeliv")
        self.assertEqual([len(r) for r in staff_live.keyboard(5, "leftover", "bm")], [1, 1, 1])
        self.assertEqual(staff_live.keyboard(5, "leftover", "tamil")[2][0][0], "கொட்டணும்")

    def test_detail_after_other(self):
        now = datetime(2026, 9, 25, 12, tzinfo=MYT)
        fields = staff_live.tap_fields("other", "Lain", now)
        self.assertTrue(fields["awaiting_detail"])
        self.assertFalse(staff_live.tap_fields("forgot", "Lupa order", now)["awaiting_detail"])
        self.assertIn("taip", staff_live.detail_prompt("other", "bm"))
        self.assertIn("\n", staff_live.detail_prompt("kept", staff_chat.BM_TAMIL))

    def test_slots_default_include_ops(self):
        import os
        old = os.environ.pop("STAFF_CHAT_SLOTS", None)
        try:
            self.assertTrue(staff_live.slot_enabled("leftover"))
            os.environ["STAFF_CHAT_SLOTS"] = "bills,leftover"
            self.assertTrue(staff_live.slot_enabled("leftover"))
            self.assertFalse(staff_live.slot_enabled("wastage"))
        finally:
            os.environ.pop("STAFF_CHAT_SLOTS", None)
            if old is not None:
                os.environ["STAFF_CHAT_SLOTS"] = old


class TextTests(unittest.TestCase):
    def test_tamil_words(self):
        mm = staff_ops.minimarket_text("99 Speed Mart", "Gula", "tamil")
        self.assertIn("mini market", mm)
        self.assertNotIn("வெளிக்கடை", mm)
        self.assertIn("விக்குது", staff_ops.itemdrop_text("Vadai", "tamil"))
        self.assertIn("கொட்டணும்", staff_ops.leftover_text(date(2026, 9, 25), "tamil"))

    def test_all_texts_render(self):
        d = date(2026, 9, 25)
        flag = {"kind": "high", "item": "ayam", "qty": 80, "usual": 55}
        for lang in LANGS + (staff_chat.BM_TAMIL,):
            for text in (staff_ops.invoice_text(flag, "Bestari", lang),
                         staff_ops.invoice_text({"kind": "rare", "item": "ayam", "qty": 2}, "Easa", lang),
                         staff_ops.minimarket_text("KK Mart", "Gula", lang),
                         staff_ops.itemdrop_text("Vadai", lang),
                         staff_ops.leftover_text(d, lang),
                         staff_ops.wastage_text(lang),
                         staff_ops.tip_text(d, lang),
                         staff_ops.praise_text({"minimarket": "Jakel"}, lang)):
                self.assertTrue(text and "{" not in text, (lang, text))
                self.assertNotIn("RM", text)
        both = staff_ops.wastage_text(staff_chat.BM_TAMIL)
        self.assertEqual(len(both.split("\n")), 2)

    def test_thirty_tips_rotate(self):
        self.assertEqual(len(staff_ops.TIPS), 30)
        days = {staff_ops.tip_index(date(2026, 9, 1) + timedelta(days=i)) for i in range(30)}
        self.assertEqual(len(days), 30)


def _t(code, slot, status="answered", **kw):
    return {"outlet_code": code, "slot": slot, "status": status, **kw}


class WeeklyAndSummaryTests(unittest.TestCase):
    def test_winners(self):
        threads = ([_t("A", "minimarket")] * 3 + [_t("B", "minimarket")]
                   + [_t("A", "wastage", reply_status="ok")] * 3
                   + [_t("B", "wastage", reply_status="finished")] * 3
                   + [_t("C", "leftover", "no_reply")])
        w = staff_ops.weekly_winners(threads, ["A", "B", "C"])
        self.assertEqual(w["minimarket"], "C")
        self.assertEqual(w["wastage"], "A")         # C never answered: no prize
        self.assertIn("replies", w)

    def test_shared_prize_dropped(self):
        w = staff_ops.weekly_winners([], ["A", "B", "C", "D", "E"])
        self.assertNotIn("minimarket", w)

    def test_morning_summary_sections(self):
        now = "2026-09-25T02:00:00+00:00"
        threads = [
            _t("SEK14", "invoice", reply_en="Event / booking", asked_at=now, answered_at=now,
               facts={"kind": "high", "supplier": "Bestari", "item_label": "Ayam",
                      "qty_text": "80 ekor", "usual_text": "55 ekor"}),
            _t("SBESI", "minimarket", "dropped",
               facts={"shop": "99 Speed Mart", "items": "Gula", "not_asked": True}),
            _t("SEK15", "afternoon", reply_en="Taste not right: too salty", asked_at=now,
               answered_at=now, facts={"item": "VADAI", "label": "Vadai", "drop_pct": 37,
                                       "shop_pct": 12}),
            _t("JAKEL", "leftover", reply_status="other", reply_en="Some kept for tomorrow: kari",
               asked_at=now, answered_at=now),
        ]
        text = staff_live.format_morning_summary(threads)
        self.assertIn("🧾 Unusual invoices", text)
        self.assertIn("Ayam 80 ekor (usual 55 ekor) — Event / booking", text)
        self.assertIn("not asked (daily limit)", text)
        self.assertIn("Vadai −37% (shop +12%) — Taste not right", text)
        self.assertIn("Some kept for tomorrow: kari", text)
        self.assertIn("• 03:00 leftover: 1/1 answered", text)
        self.assertIn("• upload invoice: 1/1 answered", text)

    def test_weekly_minimarket(self):
        lines = staff_ops.weekly_minimarket(
            [_t("A", "minimarket", reply_en="Forgot to order")] * 2
            + [_t("B", "minimarket", "no_reply")])
        self.assertIn("• A: 2", lines)
        self.assertIn("Forgot to order (2)", lines[-1])


if __name__ == "__main__":
    unittest.main()
