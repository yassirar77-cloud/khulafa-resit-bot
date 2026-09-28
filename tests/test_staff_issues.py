"""Issue flagging in staff replies: keyword rule, AI reading, rows, texts."""

import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_issues as si
import staff_live as sl

THREAD = {"id": 12, "outlet_code": "SEK7", "chat_id": -7, "slot": "night", "cashier": "Kalai"}


class ClassifyTests(unittest.TestCase):
    def test_plain_cases_in_staff_languages(self):
        cases = {
            "gas habis": "equipment", "peti ais rosak": "equipment", "aircond tak sejuk": "equipment",
            "POS rosak boss": "equipment", "fridge nosto": "equipment",
            "cashier tak datang": "staff", "staff mc hari ni": "staff", "pekerja sakit": "staff",
            "cashier ashe nai": "staff", "ஆள் இல்ல": "staff",
            "supplier lambat": "supplier", "barang belum sampai": "supplier",
            "Bestari delivery tak sampai": "supplier", "maal ashe nai": "supplier",
            "customer complain nasi basi": "customer", "pelanggan marah": "customer",
            "cash kurang RM50": "cash", "duit tak cukup dalam laci": "cash",
        }
        for text, kind in cases.items():
            got = si.classify(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["type"], kind, text)

    def test_fine_replies_are_not_issues(self):
        for text in ("all ok", "ok boss", "semua ok", "Thik ache", "எல்லாம் சரி", "done", "",
                     "esok ayam 40kg ikan 10kg", "ramai lunch tadi", None):
            self.assertIsNone(si.classify(text), text)

    def test_urgent(self):
        self.assertTrue(si.classify("dapur terbakar!")["urgent"])
        self.assertTrue(si.classify("gas bocor kedai")["urgent"])
        self.assertTrue(si.classify("tiada elektrik sejak 3pm")["urgent"])
        self.assertEqual(si.classify("ada kemalangan depan kedai"),
                         {"type": "other", "urgent": True})
        self.assertFalse(si.classify("gas habis")["urgent"])
        self.assertFalse(si.classify("supplier lambat")["urgent"])


class FromReplyTests(unittest.TestCase):
    def _parsed(self, status="problem", issue=None, summary="x"):
        return {"is_answer": True, "clear": True, "summary_en": summary, "status": status,
                "items": [], "asks_if_bot": False, "issue": issue}

    def test_ai_issue_wins(self):
        issue = si.from_reply(self._parsed(issue={"type": "supplier", "summary_en": "Fish late",
                                                  "urgent": False}), "ikan lambat")
        self.assertEqual(issue, {"type": "supplier", "summary_en": "Fish late", "urgent": False})

    def test_ai_none_and_ok_reply_is_no_issue(self):
        self.assertIsNone(si.from_reply(self._parsed("ok", {"type": "none"}), "semua ok"))
        # A sold-out item in an "ok" reply is not an issue either.
        self.assertIsNone(si.from_reply(self._parsed("order", None), "order gas 2 tong"))

    def test_keyword_fallback_when_ai_gives_none(self):
        issue = si.from_reply(self._parsed("problem", None, "Gas finished"), "gas habis")
        self.assertEqual(issue, {"type": "equipment", "summary_en": "Gas finished", "urgent": False})
        issue = si.from_reply(self._parsed("finished", {"type": "weird"}), "cashier tak datang")
        self.assertEqual(issue["type"], "staff")
        # AI down: the keyword rule alone.
        issue = si.from_reply(None, "supplier lambat hari ni")
        self.assertEqual((issue["type"], issue["summary_en"]), ("supplier", "supplier lambat hari ni"))
        self.assertIsNone(si.from_reply(None, "all ok"))

    def test_force_for_problem_button_details(self):
        self.assertIsNone(si.from_reply(self._parsed("ok", None), "peti rosak"))
        self.assertEqual(si.from_reply(self._parsed("ok", None), "peti rosak", force=True)["type"],
                         "equipment")

    def test_keyword_urgency_upgrades_ai(self):
        issue = si.from_reply(self._parsed(issue={"type": "equipment", "summary_en": "Fire in kitchen",
                                                  "urgent": False}), "dapur terbakar")
        self.assertTrue(issue["urgent"])


class RowAndTextTests(unittest.TestCase):
    def test_row_shape(self):
        now = datetime(2026, 9, 24, 21, 30, tzinfo=timezone(timedelta(hours=8)))
        row = si.row(THREAD, {"type": "cash", "summary_en": "Cash short RM50", "urgent": False},
                     "cash kurang RM50", now)
        self.assertEqual(row["outlet_code"], "SEK7")
        self.assertEqual(row["thread_id"], 12)
        self.assertEqual(row["type"], "cash")
        self.assertFalse(row["urgent"])
        self.assertEqual(row["raw_reply"], "cash kurang RM50")
        self.assertEqual(row["ts"], now.isoformat())

    def test_urgent_text_and_open_list(self):
        rows = [
            {"id": 3, "outlet_code": "SEK7", "type": "equipment", "summary_en": "Gas finished",
             "urgent": False, "ts": "2026-09-24T21:30:00+08:00", "raw_reply": "gas habis"},
            {"id": 5, "outlet_code": "SEK20", "type": "other", "summary_en": "Fire in kitchen",
             "urgent": True, "ts": "2026-09-24T22:00:00+08:00", "raw_reply": "dapur terbakar",
             "cashier": "Syed"},
        ]
        text = si.format_open(rows, lambda c: c.title())
        self.assertLess(text.index("#5"), text.index("#3"))       # urgent first
        self.assertIn("🚨 #5 Sek20 · other · 24/09 22:00", text)
        self.assertIn("Gas finished", text)
        self.assertEqual(si.format_open([]), "✅ No open staff issues.")
        urgent = si.urgent_text(rows[1])
        self.assertTrue(urgent.startswith("🚨 URGENT — SEK20 (other): Fire in kitchen"))
        self.assertIn("/resolve 5", urgent)
        self.assertIn("Syed wrote: dapur terbakar", urgent)


class ParserTests(unittest.TestCase):
    def test_reply_prompt_asks_for_issue_and_parse_keeps_it(self):
        self.assertIn('"issue"', sl.REPLY_PROMPT)
        self.assertIn("equipment", sl.REPLY_PROMPT)
        parsed = sl.parse_reply("q", "q", "gas habis", lambda s, u: {"data": {
            "is_answer": True, "clear": True, "summary_en": "Gas finished", "status": "problem",
            "issue": {"type": "equipment", "summary_en": "Gas finished", "urgent": False}}})
        self.assertEqual(parsed["issue"]["type"], "equipment")
        self.assertEqual(si.from_reply(parsed, "gas habis")["type"], "equipment")
        parsed = sl.parse_reply("q", "q", "ok", lambda s, u: {"data": {
            "is_answer": True, "status": "ok", "issue": "none"}})
        self.assertIsNone(parsed["issue"])


if __name__ == "__main__":
    unittest.main()
