"""Cashier on shift: shift boundaries, the group-message prefix, the table."""

import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cashier_names as cn
from tests.fake_supabase import FakeSupabase

MY = cn.MALAYSIA_TZ
SEK20_GROUP = -5043287182
SEK6_GROUP = -5279243634
KLANG_GROUP = -5003341957


def _at(hour, minute=0, day=24):
    return datetime(2026, 9, day, hour, minute, tzinfo=MY)


def _seed():
    sb = FakeSupabase()
    sb.table(cn.MANAGERS_TABLE).insert([
        {"outlet_code": "SEK20", "manager_name": "Syed / Ismath", "chat_id": SEK20_GROUP},
        {"outlet_code": "SEK6", "manager_name": "Imdadul", "chat_id": SEK6_GROUP},
        {"outlet_code": "KLANG", "manager_name": None, "chat_id": KLANG_GROUP},
        # A manager registered by DM is a person, not a group: no prefix.
        {"outlet_code": "VISTA", "manager_name": "Buhari", "chat_id": 777},
    ]).execute()
    sb.table(cn.TABLE).insert([
        {"outlet_code": "SEK20", "shift": "morning", "name": "Syed"},
        {"outlet_code": "SEK20", "shift": "night", "name": "Ismath"},
        {"outlet_code": "SEK6", "shift": "morning", "name": "Imdadul"},
        {"outlet_code": "SEK6", "shift": "night", "name": "Mahadir / Pandi"},
    ]).execute()
    return sb


class ShiftTests(unittest.TestCase):
    def test_morning_runs_7am_to_before_7pm(self):
        self.assertEqual(cn.shift_at(_at(7)), (cn.MORNING, date(2026, 9, 24)))
        self.assertEqual(cn.shift_at(_at(18, 59)), (cn.MORNING, date(2026, 9, 24)))

    def test_night_starts_at_7pm(self):
        self.assertEqual(cn.shift_at(_at(19)), (cn.NIGHT, date(2026, 9, 24)))
        self.assertEqual(cn.shift_at(_at(23, 59)), (cn.NIGHT, date(2026, 9, 24)))

    def test_after_midnight_belongs_to_previous_evening(self):
        self.assertEqual(cn.shift_at(_at(1)), (cn.NIGHT, date(2026, 9, 23)))
        self.assertEqual(cn.shift_at(_at(6, 59)), (cn.NIGHT, date(2026, 9, 23)))

    def test_utc_input_is_read_in_malaysia_time(self):
        # 23:30 UTC on the 23rd = 07:30 MY on the 24th -> morning.
        utc = datetime(2026, 9, 23, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(cn.shift_at(utc), (cn.MORNING, date(2026, 9, 24)))

    def test_normalize_shift(self):
        self.assertEqual(cn.normalize_shift("Night"), cn.NIGHT)
        self.assertEqual(cn.normalize_shift("malam"), cn.NIGHT)
        self.assertEqual(cn.normalize_shift("pagi"), cn.MORNING)
        self.assertIsNone(cn.normalize_shift("evening"))


class _CacheCase(unittest.TestCase):
    def tearDown(self):
        cn.reset_cache()


class PrefixTests(_CacheCase):
    def setUp(self):
        self.sb = _seed()
        self.assertTrue(cn.refresh(self.sb))

    def test_group_message_opens_with_cashier_on_shift(self):
        self.assertEqual(
            cn.with_address(SEK20_GROUP, "Stok ayam?", now=_at(10)),
            "Syed,\nStok ayam?",
        )
        self.assertEqual(
            cn.with_address(SEK20_GROUP, "Stok ayam?", now=_at(1)),
            "Ismath,\nStok ayam?",
        )

    def test_sek6_night_addresses_both(self):
        self.assertTrue(
            cn.with_address(SEK6_GROUP, "x", now=_at(22)).startswith("Mahadir / Pandi,\n")
        )

    def test_missing_name_says_cashier(self):
        self.assertEqual(
            cn.with_address(KLANG_GROUP, "x", now=_at(10)), "Cashier,\nx"
        )

    def test_dm_director_and_unknown_chats_unchanged(self):
        for chat in (777, -1001, 12345, None):
            self.assertEqual(cn.with_address(chat, "hello", now=_at(10)), "hello")

    def test_empty_text_unchanged_and_no_double_prefix(self):
        self.assertEqual(cn.with_address(SEK20_GROUP, "", now=_at(10)), "")
        once = cn.with_address(SEK20_GROUP, "x", now=_at(10))
        self.assertEqual(cn.with_address(SEK20_GROUP, once, now=_at(10)), once)

    def test_html_parse_mode_escapes_name(self):
        cn.set_name(self.sb, "SEK20", "morning", "A<b>")
        self.assertEqual(
            cn.with_address(SEK20_GROUP, "x", parse_mode="HTML", now=_at(10)),
            "A&lt;b&gt;,\nx",
        )

    def test_group_cache_only_keeps_negative_chat_ids(self):
        self.assertEqual(
            cn.group_chats(),
            {SEK20_GROUP: "SEK20", SEK6_GROUP: "SEK6", KLANG_GROUP: "KLANG"},
        )
        self.assertFalse(cn.is_outlet_group(777))

    def test_failed_refresh_keeps_last_cache(self):
        class Broken:
            def table(self, _name):
                raise RuntimeError("db down")
        self.assertFalse(cn.refresh(Broken()))
        self.assertTrue(cn.is_outlet_group(SEK20_GROUP))


class SetNameTests(_CacheCase):
    def setUp(self):
        self.sb = _seed()
        cn.refresh(self.sb)

    def test_set_name_upserts_and_updates_cache(self):
        res = cn.set_name(self.sb, "klang", "Malam", "  New   Guy ", updated_by=5)
        self.assertEqual(res, {"ok": True, "outlet_code": "KLANG",
                               "shift": cn.NIGHT, "name": "New Guy"})
        self.assertEqual(cn.name_for("KLANG", "night"), "New Guy")
        cn.set_name(self.sb, "KLANG", "night", "Other")
        rows = [r for r in self.sb.table(cn.TABLE).select("*").execute().data
                if r["outlet_code"] == "KLANG"]
        self.assertEqual([r["name"] for r in rows], ["Other"])
        # Survives a reload from the table.
        cn.refresh(self.sb)
        self.assertEqual(cn.name_for("KLANG", "night"), "Other")

    def test_set_name_rejects_bad_input(self):
        self.assertFalse(cn.set_name(self.sb, "SEK20", "evening", "X")["ok"])
        self.assertFalse(cn.set_name(self.sb, "SEK20", "night", "  ")["ok"])
        self.assertFalse(cn.set_name(self.sb, "", "night", "X")["ok"])


class TextTests(_CacheCase):
    def setUp(self):
        cn.refresh(_seed())

    def test_ping_text_names_the_shift(self):
        self.assertIn("night — started 23 Sep 19:00", cn.ping_text(_at(1)))
        self.assertIn("morning — started 24 Sep 07:00", cn.ping_text(_at(9)))
        self.assertIn("please ignore", cn.ping_text(_at(9)))

    def test_roster_lists_groups_with_fallback(self):
        text = cn.format_roster(_at(9))
        self.assertIn("SEK20: morning Syed [bm_tamil] · night Ismath [bm_tamil]", text)
        self.assertIn("KLANG: morning (Cashier) [bm_tamil] · night (Cashier) [bm_tamil]", text)
        self.assertNotIn("VISTA", text)


if __name__ == "__main__":
    unittest.main()
