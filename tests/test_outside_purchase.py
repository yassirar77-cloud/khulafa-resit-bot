"""Pinpoint Target: outside purchases, cashier attribution, strikes, messages."""

import os
import sys
import unittest
from datetime import date, datetime, time, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import outside_purchase as op
from tests.fake_supabase import FakeSupabase

MY = op.MY_TZ

SUPPLIERS = [
    {"id": 1, "canonical_name": "BABAS", "aliases": ["BABAS PRODUCTS SDN BHD"], "active": True},
    {"id": 2, "canonical_name": "SAIDA", "aliases": [], "active": True},
    {"id": 3, "canonical_name": "BESTARI FARM (M) SDN BHD", "aliases": ["BESTARI FARM"], "active": True},
    {"id": 4, "canonical_name": "BESTARI WHOLESALE SDN BHD", "aliases": [], "active": True},
    {"id": 5, "canonical_name": "M/S BESTARI KHIDMAT", "aliases": [], "active": True},
    {"id": 6, "canonical_name": "FOOK LEONG", "aliases": [], "active": True},
    {"id": 7, "canonical_name": "SAYUR", "aliases": [], "active": True},
    {"id": 8, "canonical_name": "HANEE", "aliases": ["MD HANEE"], "active": True},
    {"id": 9, "canonical_name": "OLD SHOP", "aliases": [], "active": False},
]

ALLOWED = [
    {"id": 1, "canonical_item": "ais_batu", "outlet": None, "reason": "ais habis"},
    {"id": 2, "canonical_item": "gas", "outlet": "SEK-20", "reason": "gas emergency"},
]

ROSTER = [
    {"id": 1, "outlet": "SEK-20", "shift": "morning", "cashier_name": "Syed", "telegram_user_id": None,
     "language": "bm", "active": True},
    {"id": 2, "outlet": "SEK-20", "shift": "night", "cashier_name": "Ismath", "telegram_user_id": 555,
     "language": "tamil", "active": True},
    {"id": 3, "outlet": "SEK-6", "shift": "morning", "cashier_name": "Imdadul", "telegram_user_id": None,
     "language": "bm", "active": True},
    {"id": 4, "outlet": "SEK-6", "shift": "night", "cashier_name": "Mahadir", "telegram_user_id": None,
     "language": "bm", "active": True},
    {"id": 5, "outlet": "SEK-6", "shift": "night", "cashier_name": "Pandi", "telegram_user_id": None,
     "language": "tamil", "active": True},
    {"id": 6, "outlet": "Bistro", "shift": "morning", "cashier_name": "Rahim", "telegram_user_id": None,
     "language": "bm", "active": True},
]

CONFIG = {"suppliers": SUPPLIERS, "allowed": ALLOWED, "roster": ROSTER}

NOW = datetime(2026, 9, 24, 21, 0, tzinfo=MY)


def _at(hour, minute=0, day=24):
    return datetime(2026, 9, day, hour, minute, tzinfo=MY)


def _utc(local: datetime) -> str:
    return local.astimezone(timezone.utc).isoformat()


def _receipt(**over):
    base = {
        "id": 100, "merchant": "LOTUS'S STORES", "outlet": "SEK 20", "chat_id": -500, "message_id": 9,
        "telegram_user_id": 777, "receipt_date": "2026-09-24", "total": 85.0, "confidence": 95,
        "raw_text": "LOTUS'S STORES\nTIME 14:12\nAYAM 2 x 12.50\nTELUR GRED A 1 x 15.00",
        "items": [{"name": "AYAM", "qty": 2, "price": 12.50},
                  {"name": "TELUR GRED A", "qty": 1, "price": 15.00}],
        "created_at": _utc(_at(14, 30)),
    }
    base.update(over)
    return base


class MerchantMatchingTests(unittest.TestCase):
    def test_exact_alias_and_phrase_are_approved(self):
        for text in ("BABAS", "babas", "BABAS PRODUCTS SDN BHD", "SAIDA ENTERPRISE",
                     "FOOK LEONG SEA PRODUCTS SDN BHD", "MD HANEE FROZEN AND SEAFOODS"):
            self.assertEqual(op.match_supplier(text, SUPPLIERS)["decision"], op.APPROVED, text)

    def test_outside_shops_are_outside(self):
        for text in ("LOTUS'S", "99 SPEEDMART SDN BHD", "KEDAI RUNCIT ALI", "GIANT", "SHELL", "PASAR BORONG"):
            res = op.match_supplier(text, SUPPLIERS)
            self.assertEqual(res["decision"], op.OUTSIDE, text)
            self.assertIsNone(res["supplier"])

    def test_bestari_matches_exact_merchants_only(self):
        for text in ("BESTARI FARM (M) SDN BHD", "BESTARI FARM", "BESTARI WHOLESALE SDN BHD",
                     "M/S BESTARI KHIDMAT", "MS BESTARI KHIDMAT"):
            self.assertEqual(op.match_supplier(text, SUPPLIERS)["decision"], op.APPROVED, text)
        # A random shop with "bestari" in its name is NOT one of ours.
        self.assertEqual(op.match_supplier("BESTARI MINIMART", SUPPLIERS)["decision"], op.OUTSIDE)
        self.assertEqual(op.match_supplier("RESTORAN BESTARI", SUPPLIERS)["decision"], op.OUTSIDE)

    def test_ocr_drift_is_approved_when_clear_and_held_when_grey(self):
        clear = op.match_supplier("BESTARI FARN (M) SDN BHD", SUPPLIERS)
        self.assertEqual((clear["decision"], clear["supplier"]), (op.APPROVED, "BESTARI FARM (M) SDN BHD"))
        self.assertEqual(op.match_supplier("FOOK LEONC SEA PRODUCTS", SUPPLIERS)["decision"], op.APPROVED)
        # One edit on a five-letter name is not enough evidence either way.
        grey = op.match_supplier("SAlDA", SUPPLIERS)
        self.assertEqual((grey["decision"], grey["supplier"]), (op.UNSURE, "SAIDA"))
        self.assertEqual(op.match_supplier("HANEF", SUPPLIERS)["decision"], op.UNSURE)

    def test_single_word_among_real_words_is_only_unsure(self):
        res = op.match_supplier("PASAR SAYUR SEGAR", SUPPLIERS)
        self.assertEqual((res["decision"], res["tier"]), (op.UNSURE, "partial_word"))
        self.assertEqual(op.match_supplier("SAYUR", SUPPLIERS)["decision"], op.APPROVED)

    def test_inactive_supplier_empty_and_internal(self):
        self.assertEqual(op.match_supplier("OLD SHOP", SUPPLIERS)["decision"], op.OUTSIDE)
        self.assertEqual(op.match_supplier("", SUPPLIERS)["tier"], "no_merchant")
        self.assertEqual(op.match_supplier(None, SUPPLIERS)["decision"], op.UNSURE)
        self.assertEqual(op.match_supplier("RESTORAN KHULAFA SEK 6", SUPPLIERS)["decision"], op.INTERNAL)

    def test_low_confidence_guard_never_strikes_on_a_doubtful_read(self):
        self.assertEqual(op.classify_merchant("LOTUS", SUPPLIERS, 95)["decision"], op.OUTSIDE)
        held = op.classify_merchant("LOTUS", SUPPLIERS, 60)
        self.assertEqual((held["decision"], held["tier"]), (op.UNSURE, "low_confidence"))
        self.assertEqual(op.classify_merchant("LOTUS", SUPPLIERS, None)["decision"], op.UNSURE)
        # An approved supplier stays approved whatever the confidence.
        self.assertEqual(op.classify_merchant("BABAS", SUPPLIERS, 10)["decision"], op.APPROVED)
        with mock.patch.dict("os.environ", {"OUTSIDE_MIN_CONFIDENCE": "50"}):
            self.assertEqual(op.classify_merchant("LOTUS", SUPPLIERS, 60)["decision"], op.OUTSIDE)


class ItemsTests(unittest.TestCase):
    def test_inconsistent_jsonb_keys(self):
        items = [
            {"item": "AYAM", "quantity": 2, "unit_price": 12.5},      # item / quantity / unit_price
            {"name": "TELUR GRED A", "qty": None, "price": "RM15.00"},  # null qty, RM string
            {"description": "GULA", "qty": "3", "price": 2.9, "total": 8.7},
            "Tube Ice",                                                # bare string
            {"name": "Milo x2 RM7.50", "qty": None, "price": None},    # embedded shape
            {"name": "DEPOSIT", "qty": 1, "price": 10},                # noise line
            {"qty": 1, "price": 5},                                    # nameless
            {"name": "   ", "qty": 1},
        ]
        out = op.normalise_purchase_items(items)
        by_raw = {i["raw_name"]: i for i in out}
        self.assertEqual(by_raw["AYAM"], {"canonical_item": "ayam", "raw_name": "AYAM", "qty": 2.0,
                                          "unit_price": 12.5, "line_total": 25.0})
        self.assertEqual(by_raw["TELUR GRED A"]["qty"], None)
        self.assertEqual(by_raw["TELUR GRED A"]["unit_price"], 15.0)
        self.assertIsNone(by_raw["TELUR GRED A"]["line_total"])
        self.assertEqual((by_raw["GULA"]["qty"], by_raw["GULA"]["line_total"]), (3.0, 8.7))
        self.assertEqual(by_raw["Tube Ice"]["canonical_item"], "ais_batu")
        self.assertEqual((by_raw["Milo"]["qty"], by_raw["Milo"]["unit_price"]), (2.0, 7.5))
        self.assertNotIn("DEPOSIT", by_raw)
        self.assertEqual(len(out), 5)
        self.assertEqual(op.normalise_purchase_items(None), [])
        self.assertEqual(op.normalise_purchase_items("garbage"), [])

    def test_allowed_items_are_skipped_per_outlet(self):
        items = op.normalise_purchase_items([{"name": "TUBE ICE", "qty": 3, "price": 4},
                                             {"name": "GAS PETRONAS 14KG", "qty": 1, "price": 30},
                                             {"name": "AYAM", "qty": 2, "price": 12}])
        kept, removed = op.strip_allowed(items, ALLOWED, "SEK20")
        self.assertEqual([i["canonical_item"] for i in kept], ["ayam"])
        self.assertEqual({i["canonical_item"] for i in removed}, {"ais_batu", "gas"})
        # Gas is only allowed at SEK-20; ice everywhere.
        kept, removed = op.strip_allowed(items, ALLOWED, "Vista")
        self.assertEqual({i["canonical_item"] for i in kept}, {"gas", "ayam"})
        self.assertEqual([i["canonical_item"] for i in removed], ["ais_batu"])


class WhenAndWhoTests(unittest.TestCase):
    def test_receipt_time_is_read_from_raw_text_but_prices_are_not(self):
        self.assertEqual(op.parse_receipt_time("TOTAL RM12.50\nTIME 19:45"), time(19, 45))
        self.assertEqual(op.parse_receipt_time("08:05 PM  cashier 3"), time(20, 5))
        self.assertEqual(op.parse_receipt_time("12:10 am"), time(0, 10))
        self.assertIsNone(op.parse_receipt_time("AYAM 2 x RM12.50 = RM25.00"))
        self.assertIsNone(op.parse_receipt_time(None))
        # The time next to the keyword wins over an earlier receipt number.
        self.assertEqual(op.parse_receipt_time("REF 10:22 ... Masa: 21:30"), time(21, 30))

    def test_purchase_moment_prefers_receipt_date_time_else_upload(self):
        moment, source = op.purchase_moment("2026-09-23", "TIME 23:40", _utc(_at(9, 0)))
        self.assertEqual((moment, source), (datetime(2026, 9, 23, 23, 40, tzinfo=MY), "receipt"))
        moment, source = op.purchase_moment("2026-09-24", "no clock here", _utc(_at(9, 0)))
        self.assertEqual((moment, source), (_at(9, 0), "upload"))
        moment, source = op.purchase_moment(None, "TIME 10:00", _utc(_at(9, 0)))
        self.assertEqual(source, "upload")
        # A receipt time far after the upload is OCR garbage: the upload wins.
        moment, source = op.purchase_moment("2026-09-30", "TIME 10:00", _utc(_at(9, 0)))
        self.assertEqual(source, "upload")

    def test_night_shift_across_midnight(self):
        # 01:30 on the 25th belongs to the night shift that started 19:00 on the 24th.
        who = op.attribute_cashier(ROSTER, "SEK 20", _at(1, 30, day=25))
        self.assertEqual((who["cashier_name"], who["shift"], who["shift_date"]),
                         ("Ismath", "night", date(2026, 9, 24)))
        self.assertEqual(who["telegram_user_id"], 555)
        self.assertEqual(who["language"], "tamil")
        # 06:59 is still the night; 07:00 is the morning cashier.
        self.assertEqual(op.attribute_cashier(ROSTER, "SEK20", _at(6, 59))["cashier_name"], "Ismath")
        self.assertEqual(op.attribute_cashier(ROSTER, "SEK20", _at(7, 0))["cashier_name"], "Syed")
        self.assertEqual(op.attribute_cashier(ROSTER, "SEK20", _at(18, 59))["cashier_name"], "Syed")
        self.assertEqual(op.attribute_cashier(ROSTER, "SEK20", _at(19, 0))["cashier_name"], "Ismath")
        # UTC input is read in Malaysia time: 17:30 UTC = 01:30 MY -> night.
        utc = datetime(2026, 9, 24, 17, 30, tzinfo=timezone.utc)
        self.assertEqual(op.attribute_cashier(ROSTER, "SEK20", utc)["shift"], "night")

    def test_two_night_cashiers_and_nobody_on_roster(self):
        who = op.attribute_cashier(ROSTER, "Sek 6", _at(22))
        self.assertEqual(who["cashier_name"], "Mahadir / Pandi")
        self.assertIsNone(who["telegram_user_id"])
        who = op.attribute_cashier(ROSTER, "Vista", _at(22))
        self.assertIsNone(who["cashier_name"])
        self.assertIsNone(who["matched_by"])

    def test_linked_telegram_account_pins_the_cashier(self):
        # Ismath (night) uploads during the morning: it is still Ismath's bill.
        who = op.attribute_cashier(ROSTER, "SEK-20", _at(10), uploader_telegram_id=555)
        self.assertEqual((who["cashier_name"], who["matched_by"]), ("Ismath", "telegram"))
        # An unknown uploader falls back to the roster.
        who = op.attribute_cashier(ROSTER, "SEK-20", _at(10), uploader_telegram_id=999)
        self.assertEqual((who["cashier_name"], who["matched_by"]), ("Syed", "roster"))


class ExtraCostTests(unittest.TestCase):
    def test_latest_approved_price_ignores_outside_shops(self):
        rows = [
            {"id": 1, "canonical_item": "ayam", "merchant": "BESTARI FARM (M) SDN BHD", "unit_price": 9.5,
             "receipt_date": "2026-09-01"},
            {"id": 2, "canonical_item": "ayam", "merchant": "BESTARI FARM", "unit_price": 9.8,
             "receipt_date": "2026-09-20"},
            {"id": 3, "canonical_item": "ayam", "merchant": "LOTUS", "unit_price": 12.5,
             "receipt_date": "2026-09-23"},
            {"id": 4, "canonical_item": "telur", "merchant": "BABAS", "unit_price": 0, "receipt_date": "2026-09-23"},
            {"id": 5, "canonical_item": "telur", "merchant": "BABAS", "unit_price": "13.0", "receipt_date": "2026-09-10"},
        ]
        prices = op.latest_approved_prices(rows, SUPPLIERS)
        self.assertEqual(prices["ayam"]["unit_price"], 9.8)
        self.assertEqual(prices["telur"]["unit_price"], 13.0)

    def test_known_merchant_prices_never_become_the_approved_reference(self):
        # Shadow-week check: extra_cost_vs_approved compares against approved
        # suppliers only. A known merchant (outside shop the outlet buys from
        # regularly) is matched with the approved list only, so its rows are
        # ignored even when they are the latest, and nothing is compared when
        # no approved supplier ever sold the item.
        rows = [
            {"id": 1, "canonical_item": "ayam", "merchant": "BESTARI FARM", "unit_price": 9.8, "receipt_date": "2026-09-01"},
            {"id": 2, "canonical_item": "ayam", "merchant": "PASAR MINI A M", "unit_price": 7.0, "receipt_date": "2026-09-25"},
            {"id": 3, "canonical_item": "roti", "merchant": "DIAMOND BALL", "unit_price": 28.0, "receipt_date": "2026-09-25"},
        ]
        prices = op.latest_approved_prices(rows, SUPPLIERS)
        self.assertEqual(prices["ayam"]["merchant"], "BESTARI FARM")
        self.assertNotIn("roti", prices)
        total, detail = op.extra_cost(
            [{"canonical_item": "roti", "unit_price": 30.0, "qty": 2}], prices)
        self.assertEqual((total, detail), (None, []))

    def test_extra_cost_with_missing_price_data(self):
        items = op.normalise_purchase_items([
            {"name": "AYAM", "qty": 2, "price": 12.5},        # 2 x (12.5 - 9.8) = 5.40
            {"name": "TELUR GRED A", "qty": None, "price": 15},  # qty null -> 1 x (15 - 13) = 2.00
            {"name": "GULA", "qty": 3, "price": 2.9},         # no approved price -> skipped
            {"name": "XYZ UNKNOWN", "qty": 1, "price": 4},    # no canonical -> skipped
            {"name": "BAWANG", "qty": 2, "price": None},      # no price -> skipped
        ])
        prices = {"ayam": {"unit_price": 9.8, "merchant": "BESTARI FARM"},
                  "telur": {"unit_price": 13.0, "merchant": "BABAS"},
                  "bawang": {"unit_price": 3.0, "merchant": "SAYUR"}}
        total, detail = op.extra_cost(items, prices)
        self.assertEqual(total, 7.4)
        self.assertEqual([d["canonical_item"] for d in detail], ["ayam", "telur"])
        self.assertEqual(detail[1]["qty"], 1.0)
        # Nothing comparable -> None, not 0.
        self.assertEqual(op.extra_cost(items, {}), (None, []))
        # Cheaper outside is a negative extra (shown as RM0.00, never hidden).
        total, _ = op.extra_cost(items[:1], {"ayam": {"unit_price": 20.0}})
        self.assertEqual(total, -15.0)


class StrikeCountingTests(unittest.TestCase):
    def _rows(self):
        def row(i, d, status=op.COUNTED, name="Syed", outlet="SEK-20"):
            return {"id": i, "business_date": d, "status": status, "cashier_name": name, "outlet": outlet,
                    "merchant_raw": "LOTUS", "items": [], "total_amount": 10.0}
        return [
            row(1, "2026-08-20"),                       # outside the 30-day window
            row(2, "2026-08-26"),                       # inside (window starts 08-25)
            row(3, "2026-09-01", status=op.EXCUSED),    # excused: not a strike
            row(4, "2026-09-05", status=op.FALSE_POSITIVE),
            row(5, "2026-09-10", status=op.PENDING),    # pending: not a strike
            row(6, "2026-09-15"),
            row(7, "2026-09-16", name="Ismath"),        # another cashier
            row(8, "2026-09-17", outlet="SEK-6"),       # same name, other outlet
            row(9, "2026-09-24"),
        ]

    def test_rolling_window_counts_only_counted_rows(self):
        counted = op.counted_in_window(self._rows(), "SEK 20", "Syed", date(2026, 9, 24), 30)
        self.assertEqual([r["id"] for r in counted], [2, 6, 9])
        with mock.patch.dict("os.environ", {"STRIKE_WINDOW_DAYS": "7"}):
            counted = op.counted_in_window(self._rows(), "SEK-20", "Syed", date(2026, 9, 24))
            self.assertEqual([r["id"] for r in counted], [9])
        self.assertEqual(op.counted_in_window(self._rows(), "SEK-20", None, date(2026, 9, 24)), [])

    def test_tiers_for_strike_numbers(self):
        self.assertEqual([op.tier_for(n, 5) for n in (None, 1, 2, 3, 4, 5, 6, 12)],
                         [op.INFO, op.INFO, op.REMINDER, op.REMINDER, op.FINAL, op.SCOLD, op.SCOLD, op.SCOLD])
        with mock.patch.dict("os.environ", {"SCOLD_THRESHOLD": "3"}):
            self.assertEqual([op.tier_for(n) for n in (1, 2, 3)], [op.INFO, op.FINAL, op.SCOLD])
        with mock.patch.dict("os.environ", {"SCOLD_THRESHOLD": "abc", "STRIKE_WINDOW_DAYS": "-4"}):
            self.assertEqual((op.scold_threshold(), op.strike_window_days()), (5, 30))
        with mock.patch.dict("os.environ", {"SCOLD_CHANNEL": "DM"}):
            self.assertEqual(op.scold_channel(), "dm")
        with mock.patch.dict("os.environ", {"SCOLD_CHANNEL": "sms"}):
            self.assertEqual(op.scold_channel(), "group")


class MessageTests(unittest.TestCase):
    def _purchase(self, i=9, extra=12.5):
        return {"id": i, "cashier_name": "Syed", "cashier_shift": "morning", "outlet": "SEK-20",
                "merchant_raw": "LOTUS'S STORES", "business_date": "2026-09-24",
                "purchase_datetime": "2026-09-24T14:12:00+08:00", "total_amount": 85.0,
                "extra_cost_vs_approved": extra,
                "items": [{"canonical_item": "ayam", "raw_name": "AYAM", "qty": 2.0, "unit_price": 12.5},
                          {"canonical_item": "telur", "raw_name": "TELUR GRED A", "qty": 1.0, "unit_price": 15.0}]}

    def _history(self, n):
        return [dict(self._purchase(i, 5.0), business_date=f"2026-09-{10 + i:02d}") for i in range(1, n + 1)]

    def test_strike_1_is_info_only(self):
        text = op.group_message(self._purchase(), 1, self._history(1), "bm")
        self.assertEqual(text, "ℹ️ Bil ini dari kedai luar (LOTUS'S STORES). Item: Ayam x2, Telur x1.\n"
                               "Sila order dari supplier rasmi.")
        self.assertNotIn("kali ke-", text)
        self.assertNotIn("RM", text)

    def test_strike_2_reminder_shows_count_and_extra_cost(self):
        text = op.group_message(self._purchase(), 2, self._history(2), "bm", window_days=30)
        self.assertIn("Syed, ini kali ke-2 beli di kedai luar dalam 30 hari (LOTUS'S STORES)", text)
        self.assertIn("Kos lebih berbanding supplier rasmi: RM12.50.", text)
        self.assertNotIn("amaran terakhir", text)
        # No approved price -> the extra-cost line is left out, not "RM—".
        text = op.group_message(self._purchase(extra=None), 3, self._history(3), "bm")
        self.assertIn("kali ke-3", text)
        self.assertNotIn("Kos lebih", text)

    def test_strike_4_is_the_final_warning(self):
        text = op.group_message(self._purchase(), 4, self._history(4), "bm")
        self.assertIn("kali ke-4", text)
        self.assertIn("Ini amaran terakhir. Kali seterusnya akan dilaporkan kepada pengurusan.", text)

    def test_strike_5_and_6_scold_with_full_list_and_boss_informed(self):
        for n in (5, 6):
            history = self._history(n)
            text = op.group_message(self._purchase(), n, history, "bm")
            self.assertIn(f"🛑 Syed, ini kali ke-{n} dalam 30 hari", text, n)
            self.assertEqual(text.count("• "), n, n)         # every purchase listed
            self.assertIn("Pengurusan telah dimaklumkan", text)
            self.assertIn(f"Jumlah: RM{85.0 * n:.2f}.", text)
            self.assertIn(f"Kos lebih berbanding supplier rasmi: RM{5.0 * n:.2f}.", text)

    def test_bm_plus_tamil_by_default_and_tamil_alone(self):
        both = op.group_message(self._purchase(), 5, self._history(5))
        self.assertIn("🛑 Syed, ini kali ke-5", both)
        self.assertIn("Management-க்கு தெரிவிச்சாச்சு", both)
        tamil = op.group_message(self._purchase(), 2, self._history(2), "tamil")
        self.assertIn("30 நாளில் இது 2-வது முறை", tamil)
        self.assertNotIn("kali ke-", tamil)
        english = op.group_message(self._purchase(), 4, self._history(4), "english")
        self.assertIn("final warning", english)

    def test_scold_is_firm_but_clean(self):
        banned = ("bodoh", "bangang", "sial", "babi", "stupid", "idiot", "india", "bangla",
                  "melayu", "cina", "nepal", "agama", "tamil ", "malas", "pencuri", "curi")
        for lang in ("bm", "tamil", "english"):
            text = op.group_message(self._purchase(), 7, self._history(7), lang).lower()
            for word in banned:
                self.assertNotIn(word, text, (lang, word))

    def test_unknown_cashier_gets_the_info_text_only(self):
        purchase = dict(self._purchase(), cashier_name=None)
        text = op.group_message(purchase, None, [], "bm")
        self.assertTrue(text.startswith("ℹ️ Bil ini dari kedai luar"))

    def test_management_report_and_pending_alert(self):
        report = op.management_report(self._purchase(), 5, self._history(5), threshold=5, window_days=30)
        self.assertIn("strike 5 (threshold 5, 30-day window)", report)
        self.assertIn("Cashier: Syed (morning shift) · Outlet: SEK-20", report)
        self.assertIn("When: 2026-09-24 14:12 · Shop: LOTUS'S STORES · Total RM85.00", report)
        self.assertIn("All 5 outside purchases in the last 30 days:", report)
        self.assertEqual(report.count("• "), 5)
        self.assertIn("/izin 9 <reason>", report)
        alert = op.pending_alert(dict(self._purchase(), cashier_name="Syed"),
                                 {"tier": "fuzzy_grey", "supplier": "SAIDA", "score": 0.8})
        self.assertIn("looks a bit like SAIDA (80% similar)", alert)
        self.assertIn("no strike until you confirm", alert)
        self.assertIn("low-confidence", op.pending_alert(self._purchase(), {"tier": "low_confidence"}))


class EvaluateTests(unittest.TestCase):
    def test_clear_outside_purchase_is_counted_and_pinpointed(self):
        result = op.evaluate(_receipt(), CONFIG, group_code="SEK20", now=NOW)
        self.assertEqual(result["action"], "count")
        row = result["row"]
        self.assertEqual((row["outlet"], row["cashier_name"], row["cashier_shift"]), ("SEK-20", "Syed", "morning"))
        self.assertEqual(row["purchase_datetime"], "2026-09-24T14:12:00+08:00")
        self.assertEqual(row["business_date"], "2026-09-24")
        self.assertEqual(row["status"], op.COUNTED)
        self.assertEqual(row["uploader_telegram_id"], 777)
        self.assertEqual([i["canonical_item"] for i in row["items"]], ["ayam", "telur"])
        self.assertEqual(row["total_amount"], 85.0)
        self.assertIn("time from receipt", row["match_note"])

    def test_approved_supplier_and_internal_transfer_are_skipped(self):
        self.assertEqual(op.evaluate(_receipt(merchant="BABAS PRODUCTS SDN BHD"), CONFIG, now=NOW)["action"], "skip")
        self.assertEqual(op.evaluate(_receipt(merchant="KHULAFA SEK 6"), CONFIG, now=NOW)["action"], "skip")
        self.assertEqual(op.evaluate(_receipt(outlet=None), CONFIG, group_code=None, now=NOW)["action"], "skip")

    def test_grey_zone_goes_to_pending_never_a_strike(self):
        for over in ({"merchant": "SAlDA"}, {"merchant": "PASAR SAYUR SEGAR"}, {"merchant": None},
                     {"merchant": "LOTUS", "confidence": 55}):
            result = op.evaluate(_receipt(**over), CONFIG, group_code="SEK20", now=NOW)
            self.assertEqual(result["action"], "pending", over)
            self.assertEqual(result["row"]["status"], op.PENDING, over)
            self.assertIsNone(result["row"]["strike_no"])

    def test_only_allowed_items_means_no_strike(self):
        receipt = _receipt(items=[{"name": "TUBE ICE", "qty": 3, "price": 4},
                                  {"name": "GAS PETRONAS 14KG", "qty": 1, "price": 30}])
        result = op.evaluate(receipt, CONFIG, group_code="SEK20", now=NOW)
        self.assertEqual(result["action"], "allowed_only")
        self.assertEqual(result["row"]["status"], op.EXCUSED)
        self.assertIn("allowed_outside_items", result["row"]["excused_reason"])
        self.assertEqual(result["row"]["items"], [])
        # Mixed: only the ice is dropped, the rest counts.
        receipt = _receipt(items=[{"name": "TUBE ICE", "qty": 3, "price": 4}, {"name": "AYAM", "qty": 2, "price": 12}])
        result = op.evaluate(receipt, CONFIG, group_code="SEK20", now=NOW)
        self.assertEqual(result["action"], "count")
        self.assertEqual([i["canonical_item"] for i in result["row"]["items"]], ["ayam"])
        self.assertEqual([i["canonical_item"] for i in result["removed"]], ["ais_batu"])

    def test_upload_after_midnight_attributes_the_night_cashier(self):
        receipt = _receipt(receipt_date="2026-09-24", raw_text="LOTUS no time printed",
                           created_at=_utc(_at(0, 40, day=25)))
        result = op.evaluate(receipt, CONFIG, group_code="SEK20", now=NOW)
        self.assertEqual((result["row"]["cashier_name"], result["row"]["cashier_shift"]), ("Ismath", "night"))
        self.assertIn("time from upload", result["row"]["match_note"])

    def test_outlet_falls_back_to_the_receipt_outlet_text(self):
        result = op.evaluate(_receipt(outlet="HJ SHARFUDDIN SEK 6"), CONFIG, group_code=None, now=NOW)
        self.assertEqual(result["row"]["outlet"], "SEK-6")
        self.assertEqual(result["row"]["cashier_name"], "Imdadul")


class DatabaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeSupabase()
        self.db.table(op.SUPPLIERS_TABLE).insert([dict(s) for s in SUPPLIERS]).execute()
        self.db.table(op.ALLOWED_TABLE).insert([dict(a) for a in ALLOWED]).execute()
        self.db.table(op.ROSTER_TABLE).insert([dict(r) for r in ROSTER]).execute()
        self.db.table(op.ITEM_PRICES_TABLE).insert([
            {"canonical_item": "ayam", "merchant": "BESTARI FARM (M) SDN BHD", "unit_price": 9.8,
             "receipt_date": "2026-09-20"},
            {"canonical_item": "ayam", "merchant": "LOTUS", "unit_price": 12.5, "receipt_date": "2026-09-23"},
            {"canonical_item": "telur", "merchant": "BABAS", "unit_price": 13.0, "receipt_date": "2026-06-01"},  # too old
        ]).execute()

    def _upload(self, i, day, hour=14, **over):
        fields = {"id": i, "receipt_date": f"2026-09-{day:02d}", "raw_text": "no time",
                  "created_at": _utc(_at(hour, 0, day=day))}
        fields.update(over)
        receipt = _receipt(**fields)
        return op.process_receipt(self.db, receipt, group_code="SEK20", now=NOW)

    def test_strikes_accumulate_and_the_fifth_is_a_scold(self):
        results = [self._upload(i, day) for i, day in enumerate((3, 6, 9, 12, 15), start=1)]
        self.assertEqual([r["strike_no"] for r in results], [1, 2, 3, 4, 5])
        self.assertEqual([r["action"] for r in results], ["count"] * 5)
        fifth = results[-1]
        self.assertEqual(len(fifth["history"]), 5)
        self.assertEqual(op.tier_for(fifth["strike_no"]), op.SCOLD)
        # Extra cost uses the approved price only (ayam 2 x (12.5 - 9.8)); telur has no recent approved price.
        self.assertEqual(fifth["row"]["extra_cost_vs_approved"], 5.4)
        stored = self.db.rows(op.TABLE)
        self.assertEqual([r["strike_no"] for r in stored], [1, 2, 3, 4, 5])
        self.assertTrue(all(r["status"] == op.COUNTED for r in stored))
        # The same receipt is never recorded twice.
        self.assertIsNone(self._upload(5, 15))
        self.assertEqual(len(self.db.rows(op.TABLE)), 5)

    def test_excused_and_false_positive_rows_do_not_count(self):
        first = self._upload(1, 3)
        second = self._upload(2, 6)
        self.assertEqual(second["strike_no"], 2)
        excused = op.excuse(self.db, first["row"]["id"], "gas habis, saya benarkan", reviewer_id=42)
        self.assertEqual((excused["status"], excused["excused_by"], excused["strike_no"]), (op.EXCUSED, 42, None))
        self.assertEqual(excused["excused_reason"], "gas habis, saya benarkan")
        fp = op.mark_false_positive(self.db, second["row"]["id"], reviewer_id=42)
        self.assertEqual(fp["status"], op.FALSE_POSITIVE)
        third = self._upload(3, 9)
        self.assertEqual(third["strike_no"], 1)
        self.assertIsNone(op.excuse(self.db, first["row"]["id"], "again"))
        self.assertIsNone(op.excuse(self.db, 9999, "nope"))

    def test_rolling_window_forgets_old_strikes(self):
        self._upload(1, 1, receipt_date="2026-08-01", created_at=_utc(datetime(2026, 8, 1, 14, tzinfo=MY)))
        self._upload(2, 2, receipt_date="2026-08-20", created_at=_utc(datetime(2026, 8, 20, 14, tzinfo=MY)))
        self._upload(3, 3, receipt_date="2026-08-26", created_at=_utc(datetime(2026, 8, 26, 14, tzinfo=MY)))
        # Window ending 24 Sep starts 25 Aug: 1 Aug and 20 Aug are forgotten, 26 Aug still counts.
        fourth = self._upload(4, 24)
        self.assertEqual(fourth["strike_no"], 2)
        self.assertEqual([r["business_date"] for r in fourth["history"]], ["2026-08-26", "2026-09-24"])
        fifth = self._upload(5, 24, hour=15)
        self.assertEqual(fifth["strike_no"], 3)

    def test_pending_then_confirmed_counts_a_strike(self):
        held = self._upload(1, 3, merchant="SAlDA")
        self.assertEqual(held["action"], "pending")
        self.assertIsNone(held["strike_no"])
        self.assertEqual(self.db.rows(op.TABLE)[0]["status"], op.PENDING)
        confirmed = op.confirm_outside(self.db, held["row"]["id"], reviewer_id=42)
        self.assertEqual(confirmed["strike_no"], 1)
        self.assertEqual(confirmed["row"]["status"], op.COUNTED)
        self.assertEqual(len(confirmed["history"]), 1)
        self.assertIsNone(op.confirm_outside(self.db, held["row"]["id"]))     # already handled
        nxt = self._upload(2, 6)
        self.assertEqual(nxt["strike_no"], 2)
        # [Supplier Kita ❌] on a held row never adds a strike.
        held2 = self._upload(3, 9, merchant="HANEF")
        fp = op.mark_false_positive(self.db, held2["row"]["id"], 42)
        self.assertEqual(fp["status"], op.FALSE_POSITIVE)
        self.assertEqual(self._upload(4, 12)["strike_no"], 3)

    def test_allowed_only_is_recorded_without_a_strike(self):
        result = self._upload(1, 3, items=[{"name": "TUBE ICE", "qty": 2, "price": 5}])
        self.assertEqual(result["action"], "allowed_only")
        self.assertIsNone(result["strike_no"])
        self.assertEqual(self.db.rows(op.TABLE)[0]["status"], op.EXCUSED)
        self.assertEqual(self._upload(2, 6)["strike_no"], 1)

    def test_unknown_cashier_is_recorded_but_never_struck(self):
        receipt = _receipt(id=1, outlet="Vista", raw_text="no time")
        result = op.process_receipt(self.db, receipt, group_code="VISTA", now=NOW)
        self.assertEqual(result["action"], "count")
        self.assertIsNone(result["row"]["cashier_name"])
        self.assertIsNone(result["strike_no"])
        self.assertEqual(result["history"], [])

    def test_approved_supplier_leaves_no_trace(self):
        self.assertIsNone(self._upload(1, 3, merchant="FOOK LEONG SEA PRODUCTS"))
        self.assertEqual(self.db.rows(op.TABLE), [])

    def test_empty_config_holds_everything_for_review(self):
        db = FakeSupabase()
        result = op.process_receipt(db, _receipt(), group_code="SEK20", now=NOW)
        self.assertEqual(result["action"], "pending")
        self.assertEqual(result["match"]["tier"], "no_suppliers")
        self.assertIsNone(result["row"]["cashier_name"])
        self.assertIn("approved_suppliers table is empty", op.pending_alert(result["row"], result["match"]))

    def test_add_supplier_new_alias_and_explicit(self):
        new = op.add_supplier(self.db, "pasaraya borong sns ali", added_by=42)
        self.assertEqual((new["ok"], new["kind"], new["supplier"]), (True, "new", "PASARAYA BORONG SNS ALI"))
        alias = op.add_supplier(self.db, "BABAS PRODUCTS (M) SDN BHD")
        self.assertEqual((alias["kind"], alias["supplier"]), ("alias", "BABAS"))
        babas = next(s for s in self.db.rows(op.SUPPLIERS_TABLE) if s["canonical_name"] == "BABAS")
        self.assertIn("BABAS PRODUCTS (M) SDN BHD", babas["aliases"])
        explicit = op.add_supplier(self.db, "BESTARI FARM SDN BHD", alias_of="BESTARI FARM (M) SDN BHD")
        self.assertEqual(explicit["kind"], "alias")
        self.assertEqual(op.add_supplier(self.db, "X", alias_of="NOBODY")["ok"], False)
        self.assertEqual(op.add_supplier(self.db, "BABAS")["kind"], "exists")
        self.assertEqual(op.add_supplier(self.db, "   ")["ok"], False)
        suppliers = self.db.rows(op.SUPPLIERS_TABLE)
        self.assertEqual(op.match_supplier("PASARAYA BORONG SNS ALI", suppliers)["decision"], op.APPROVED)

    def test_link_cashier_moves_the_account_to_one_row(self):
        row = op.link_cashier(self.db, 1, 555)     # Syed takes the id Ismath had
        self.assertEqual((row["cashier_name"], row["telegram_user_id"]), ("Syed", 555))
        roster = {r["id"]: r for r in self.db.rows(op.ROSTER_TABLE)}
        self.assertEqual(roster[1]["telegram_user_id"], 555)
        self.assertIsNone(roster[2]["telegram_user_id"])
        self.assertIsNone(op.link_cashier(self.db, 999, 1))
        contact = op.cashier_contact(self.db.rows(op.ROSTER_TABLE), "SEK20", "Syed")
        self.assertEqual(contact, {"telegram_user_id": 555, "language": "bm"})
        self.assertIsNone(op.cashier_contact(ROSTER, "SEK-6", "Mahadir / Pandi")["telegram_user_id"])

    def test_fetch_filters(self):
        self._upload(1, 3)
        self._upload(2, 6, merchant="SAlDA")
        rows = op.fetch_purchases(self.db, outlet="SEK20", since=date(2026, 9, 1), statuses=[op.COUNTED])
        self.assertEqual([r["receipt_id"] for r in rows], [1])
        rows = op.fetch_purchases(self.db, cashier_name="Syed")
        self.assertEqual(len(rows), 2)
        self.assertEqual(op.fetch_purchases(self.db, outlet="Vista"), [])


class ReportTests(unittest.TestCase):
    ROWS = [
        {"id": 1, "business_date": "2026-09-03", "status": op.COUNTED, "cashier_name": "Syed", "outlet": "SEK-20",
         "merchant_raw": "LOTUS", "total_amount": 85.0, "extra_cost_vs_approved": 5.4,
         "items": [{"canonical_item": "ayam"}, {"canonical_item": "telur"}]},
        {"id": 2, "business_date": "2026-09-10", "status": op.COUNTED, "cashier_name": "Syed", "outlet": "SEK-20",
         "merchant_raw": "99 SPEEDMART", "total_amount": 20.0, "extra_cost_vs_approved": None,
         "items": [{"canonical_item": "ayam"}]},
        {"id": 3, "business_date": "2026-09-12", "status": op.EXCUSED, "cashier_name": "Syed", "outlet": "SEK-20",
         "merchant_raw": "KEDAI ALI", "total_amount": 30.0, "excused_reason": "gas habis", "items": []},
        {"id": 4, "business_date": "2026-09-14", "status": op.PENDING, "cashier_name": "Imdadul", "outlet": "SEK-6",
         "merchant_raw": "SAlDA", "total_amount": 40.0, "items": []},
        {"id": 5, "business_date": "2026-08-30", "status": op.COUNTED, "cashier_name": "Rahim", "outlet": "Bistro",
         "merchant_raw": "GIANT", "total_amount": 12.0, "extra_cost_vs_approved": 1.0, "items": []},
    ]

    def test_summary_per_cashier(self):
        text = op.format_summary(self.ROWS, None, 30)
        self.assertIn("📋 Beli Luar — semua outlet — 30 hari", text)
        self.assertIn("• Syed (SEK-20): 2x · RM105.00 · lebih RM5.40 · Ayam, Telur", text)
        self.assertIn("• Rahim (Bistro): 1x · RM12.00 · lebih RM1.00", text)
        self.assertIn("⏳ 1 menunggu semakan", text)
        self.assertNotIn("KEDAI ALI", text)
        self.assertIn("Tiada belian luar", op.format_summary([], "SEK20", 7))

    def test_cashier_history(self):
        text = op.format_cashier_history(self.ROWS, "syed")
        self.assertIn("🧾 Beli luar — syed: 2 dikira, 3 rekod", text)
        self.assertIn("🆗 #3 12 Sep · SEK-20 · KEDAI ALI · — · RM30.00 (gas habis)", text)
        self.assertIn("✔ #2 10 Sep", text)
        self.assertTrue(text.index("#3") < text.index("#2") < text.index("#1"))   # newest first
        self.assertEqual(op.format_cashier_history(self.ROWS, "Nobody"), "Tiada rekod beli luar untuk Nobody.")

    def test_monthly_section_per_outlet(self):
        text = op.monthly_section(self.ROWS, 2026, 9)
        self.assertIn("🛒 Beli Luar (kedai luar):", text)
        self.assertIn("• SEK-20: 2 bil · RM105.00 · lebih RM5.40 · Syed 2", text)
        self.assertNotIn("Bistro", text)            # August
        self.assertNotIn("SEK-6", text)             # pending
        self.assertIn("Jumlah: 2 bil · RM105.00 · kos lebih RM5.40", text)
        self.assertEqual(op.monthly_section(self.ROWS, 2026, 7), "🛒 Beli Luar (kedai luar): tiada bulan ini.")


class RegisterFlowTests(unittest.TestCase):
    def test_buttons_and_callback_parsing(self):
        outlets = op.roster_outlets(ROSTER)
        self.assertEqual(outlets, ["Bistro", "SEK-20", "SEK-6"])
        self.assertEqual(op.register_outlet_buttons(ROSTER, 77),
                         [("Bistro", "dc:77:o:0"), ("SEK-20", "dc:77:o:1"), ("SEK-6", "dc:77:o:2")])
        shifts = op.register_shift_buttons(77, 2)
        self.assertEqual([d for _, d in shifts], ["dc:77:s:2:morning", "dc:77:s:2:night"])
        names = op.register_name_buttons(ROSTER, 77, 2, "night")
        self.assertEqual(names, [("Mahadir", "dc:77:n:4"), ("Pandi", "dc:77:n:5")])
        self.assertEqual(op.register_name_buttons(ROSTER, 77, 9, "night"), [])
        self.assertEqual(op.parse_register_callback("dc:77:o:1"), {"user_id": 77, "step": "outlet", "outlet_idx": 1})
        self.assertEqual(op.parse_register_callback("dc:77:s:2:malam"),
                         {"user_id": 77, "step": "shift", "outlet_idx": 2, "shift": "night"})
        self.assertEqual(op.parse_register_callback("dc:77:n:4"), {"user_id": 77, "step": "name", "roster_id": 4})
        for bad in ("dc:x:o:1", "sc:1:yes", "dc:77:s:2:noon", "dc:77:q:1", ""):
            self.assertIsNone(op.parse_register_callback(bad), bad)
        for _label, data in names + shifts:
            self.assertLessEqual(len(data.encode()), 64)


if __name__ == "__main__":
    unittest.main()
