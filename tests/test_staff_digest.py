"""Nightly director digest: facts, ordering, per-line fact check, fallback."""

import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_digest as sd

OUTLETS = {"SEK7": "Sek 7", "BISTRO7": "Bistro 7", "SEK20": "Sek 20"}
A = "2026-09-24T08:00:00+08:00"


def _t(code, slot, status, tid, **kw):
    row = {"id": tid, "outlet_code": code, "slot": slot, "status": status, "asked_at": A,
           "chat_id": -1, "facts": {}}
    row.update(kw)
    return row


# Sek 20 never replied; Bistro 7 changed its order; Sek 7 answered everything.
THREADS = [
    _t("SEK20", "open", "no_reply", 1),
    _t("SEK20", "lunch", "no_reply", 2),
    _t("BISTRO7", "open", "answered", 3, reply_status="ok"),
    _t("BISTRO7", "order", "answered", 4, reply_status="order",
       reply_en="Wants ayam 12kg instead of 10kg"),
    _t("SEK7", "open", "answered", 5, reply_status="ok"),
    _t("SEK7", "order", "answered", 6, reply_status="ok"),
    _t("SEK7", "night", "queued", 7, asked_at=None),          # never sent
]


def _ai(text):
    return lambda system, user: {"data": {"text": text}, "provider": "deepseek",
                                 "model": "deepseek-flash", "tokens_in": 900, "tokens_out": 120}


class GatherTests(unittest.TestCase):
    def test_three_outlets_sorted_into_sections(self):
        facts = sd.gather(THREADS, OUTLETS, day=date(2026, 9, 24))
        self.assertEqual(facts["silent"], [{"outlet": "Sek 20", "asked": "2", "unanswered": "2",
                                            "never_replied": True}])
        self.assertEqual(facts["orders_changed"],
                         [{"outlet": "Bistro 7", "change": "Wants ayam 12kg instead of 10kg"}])
        self.assertEqual(facts["normal"], [{"outlet": "Sek 7", "answered": "2"}])
        self.assertEqual(facts["bills_open"], [])
        self.assertEqual(facts["outlets_asked"], "3")

    def test_bills_open_unless_uploaded_or_handed_in(self):
        threads = [
            _t("SEK7", "bills", "no_reply", 1, facts={"supplier": "Bestari Farm", "days": "6"}),
            _t("SEK20", "bills", "answered", 2, reply_status="other", reply_en="No bill",
               facts={"supplier": "Saida", "days": "9"}),
            _t("BISTRO7", "bills", "answered", 3, reply_status="handed_in",
               facts={"supplier": "Hanee", "days": "4"}),
        ]
        facts = sd.gather(threads, OUTLETS)
        self.assertEqual([b["supplier"] for b in facts["bills_open"]], ["Bestari Farm", "Saida"])
        self.assertEqual(facts["bills_open"][1]["answer"], "No bill")

    def test_issues_and_partial_silence(self):
        threads = [_t("SEK7", "open", "answered", 1, reply_status="ok"),
                   _t("SEK7", "lunch", "no_reply", 2)]
        facts = sd.gather(threads, OUTLETS, issues=[
            {"outlet": "Sek 7", "type": "equipment", "summary_en": "Gas finished", "urgent": True}])
        self.assertEqual(facts["silent"], [{"outlet": "Sek 7", "asked": "2", "unanswered": "1",
                                            "never_replied": False}])
        self.assertEqual(facts["issues"][0]["type"], "equipment")
        self.assertEqual(facts["normal"], [])


class PlainTests(unittest.TestCase):
    def test_order_by_concern(self):
        facts = sd.gather(THREADS, OUTLETS)
        text = sd.plain(facts)
        lines = text.split("\n")[1:]
        self.assertEqual(lines[0], "❌ Sek 20: no reply to any of 2 check-ins")
        self.assertEqual(lines[1], "✏️ Bistro 7: order changed — Wants ayam 12kg instead of 10kg")
        self.assertEqual(lines[2], "✅ Sek 7: all 2 replies in, no changes")
        self.assertEqual(len(lines), 3)

    def test_empty_day(self):
        self.assertEqual(sd.plain(sd.gather([], OUTLETS)), "")
        res = sd.build(sd.gather([], OUTLETS), complete=_ai("anything"))
        self.assertEqual((res["text"], res["problems"]), ("", ["nothing to report"]))

    def test_capped_at_twelve_lines(self):
        many = [_t(f"O{i}", "open", "no_reply", i) for i in range(20)]
        text = sd.plain(sd.gather(many, {f"O{i}": f"Outlet {i}" for i in range(20)}))
        self.assertEqual(len(text.split("\n")), 1 + sd.MAX_LINES)


class BuildTests(unittest.TestCase):
    facts = sd.gather(THREADS, OUTLETS)
    labels = list(OUTLETS.values())

    def test_ai_digest_kept_when_every_line_matches_the_facts(self):
        res = sd.build(self.facts, complete=_ai(
            "❌ Sek 20 did not answer either of its 2 check-ins today.\n"
            "✏️ Bistro 7 changed the order: wants ayam 12kg instead of 10kg.\n"
            "✅ Sek 7: all 2 replies in, nothing changed."), all_labels=self.labels)
        self.assertEqual(res["source"], "ai")
        self.assertEqual(res["problems"], [])
        self.assertTrue(res["text"].startswith("🌙 Staff digest — today\n❌ Sek 20"))
        self.assertEqual(res["tokens_in"], 900)

    def test_bad_line_dropped_not_the_digest(self):
        res = sd.build(self.facts, complete=_ai(
            "❌ Sek 20 did not answer its 2 check-ins.\n"
            "✏️ Bistro 7 wants ayam 12kg instead of 10kg.\n"
            "💰 Bistro 7 spent RM400 on ayam.\n"                  # money
            "⚠️ Sek 6 was quiet too.\n"                           # outlet not in facts
            "✏️ Sek 7 changed sotong to 15kg.\n"                  # item + number not in facts
            "✅ Sek 7: all 2 replies in."), all_labels=self.labels + ["Sek 6"],
            vocabulary={"ayam", "sotong"})
        self.assertEqual(res["source"], "ai")
        kept = res["text"].split("\n")[1:]
        self.assertEqual(len(kept), 3)
        self.assertNotIn("RM400", res["text"])
        self.assertNotIn("Sek 6", res["text"])
        self.assertNotIn("sotong", res["text"])
        self.assertEqual(len(res["problems"]), 3)

    def test_all_lines_bad_or_ai_down_gives_plain_list(self):
        res = sd.build(self.facts, complete=_ai("Sek 6 spent RM9"), all_labels=self.labels + ["Sek 6"])
        self.assertEqual(res["source"], "template")
        self.assertEqual(res["text"], sd.plain(self.facts))
        self.assertTrue(res["problems"])
        res = sd.build(self.facts, complete=lambda s, u: None)
        self.assertEqual((res["source"], res["problems"]), ("template", ["ai unavailable"]))

        def boom(s, u):
            raise RuntimeError("x")
        self.assertEqual(sd.build(self.facts, complete=boom)["source"], "template")

    def test_prompt_carries_facts_and_reference(self):
        seen = {}

        def capture(system, user):
            seen["system"], seen["user"] = system, json.loads(user)
            return None
        sd.build(self.facts, complete=capture)
        self.assertIn("ONLY the facts", seen["system"])
        self.assertIn("12 lines", seen["system"])
        self.assertEqual(seen["user"]["facts"]["silent"][0]["outlet"], "Sek 20")
        self.assertTrue(seen["user"]["reference_digest"].startswith("🌙"))

    def test_log_row(self):
        res = sd.build(self.facts, complete=_ai("✅ Sek 7: all 2 replies in."))
        row = sd.log_row(self.facts, res)
        self.assertEqual((row["kind"], row["slot"], row["outlet_code"], row["mode"]),
                         ("digest", "digest", "ALL", "natural"))
        self.assertEqual(row["source"], "ai")
        self.assertEqual(row["facts"]["normal"][0]["outlet"], "Sek 7")


if __name__ == "__main__":
    unittest.main()
