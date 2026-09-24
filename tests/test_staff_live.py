"""Live staff chat: one question at a time, reminders, replies, summary."""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_live as sl

MY = sl.cashier_names.MALAYSIA_TZ
NOW = datetime(2026, 9, 24, 11, 0, tzinfo=MY)      # morning shift
CHAT = -5207657926


def _t(status, minutes_ago=None, *, chat=CHAT, slot="stock", tid=1, created=None,
       shift="morning", shift_date="2026-09-24", **kw):
    row = {"id": tid, "chat_id": chat, "status": status, "slot": slot,
           "outlet_code": "BISTRO7", "shift": shift, "shift_date": shift_date,
           "created_at": created or f"2026-09-24T0{tid}:00:00+08:00"}
    if minutes_ago is not None:
        row["asked_at"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    row.update(kw)
    return row


class SettingsTests(unittest.TestCase):
    def test_live_outlets_from_env(self):
        with mock.patch.dict("os.environ", {"STAFF_CHAT_LIVE_OUTLETS": " bistro7, SEK20 "}):
            self.assertEqual(sl.live_outlets(), {"BISTRO7", "SEK20"})
            self.assertTrue(sl.is_live("Bistro7"))
            self.assertFalse(sl.is_live("VISTA"))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(sl.live_outlets(), set())


class PlanTests(unittest.TestCase):
    def test_fresh_question_waits(self):
        self.assertEqual(sl.plan_tick([_t("open", 30)], NOW), [])

    def test_reminder_after_one_hour_once(self):
        self.assertEqual([a for a, _ in sl.plan_tick([_t("open", 65)], NOW)], ["remind"])
        self.assertEqual(sl.plan_tick([_t("reminded", 90)], NOW), [])

    def test_no_reply_after_two_hours_then_next_question_released(self):
        threads = [_t("reminded", 125, tid=1), _t("queued", tid=2, slot="cook")]
        self.assertEqual([(a, t["id"]) for a, t in sl.plan_tick(threads, NOW)],
                         [("expire", 1), ("release", 2)])

    def test_one_question_at_a_time(self):
        threads = [_t("open", 20, tid=1), _t("queued", tid=2), _t("queued", tid=3)]
        self.assertEqual(sl.plan_tick(threads, NOW), [])
        # Group free: only the OLDEST queued question goes.
        threads = [_t("queued", tid=3, created="2026-09-24T10:30:00+08:00"),
                   _t("queued", tid=2, created="2026-09-24T10:05:00+08:00")]
        self.assertEqual([(a, t["id"]) for a, t in sl.plan_tick(threads, NOW)],
                         [("release", 2)])

    def test_queued_from_ended_shift_dropped(self):
        threads = [_t("queued", tid=4, shift="night", shift_date="2026-09-23")]
        self.assertEqual([a for a, _ in sl.plan_tick(threads, NOW)], ["drop"])

    def test_no_reminders_after_midnight(self):
        late = datetime(2026, 9, 25, 0, 10, tzinfo=MY)
        t = _t("open", shift="night", shift_date="2026-09-24")
        t["asked_at"] = (late - timedelta(minutes=70)).isoformat()
        self.assertEqual(sl.plan_tick([t], late), [])
        t["asked_at"] = (late - timedelta(minutes=130)).isoformat()
        self.assertEqual([a for a, _ in sl.plan_tick([t], late)], ["expire"])

    def test_groups_are_independent(self):
        threads = [_t("open", 20, tid=1, chat=-1), _t("queued", tid=2, chat=-2)]
        self.assertEqual([(a, t["id"]) for a, t in sl.plan_tick(threads, NOW)],
                         [("release", 2)])


class ReplyTests(unittest.TestCase):
    def _complete(self, data):
        return lambda system, user: {"data": data}

    def test_parse_reply(self):
        parsed = sl.parse_reply("Is chicken enough?", "ayam cukup?", "ayam habis boss",
                                self._complete({"is_answer": True, "clear": True,
                                                "summary_en": "Chicken finished",
                                                "status": "finished"}))
        self.assertEqual(parsed, {"is_answer": True, "clear": True,
                                  "summary_en": "Chicken finished", "status": "finished"})

    def test_parse_reply_bad_or_missing(self):
        self.assertIsNone(sl.parse_reply("q", "q", "r", lambda s, u: None))
        self.assertIsNone(sl.parse_reply("q", "q", "r", self._complete({"x": 1})))
        parsed = sl.parse_reply("q", "q", "r", self._complete(
            {"is_answer": True, "status": "weird"}))
        self.assertEqual(parsed["status"], "other")

    def test_reply_prompt_sees_question_and_message(self):
        seen = {}

        def complete(system, user):
            seen["user"] = json.loads(user)
            return None
        sl.parse_reply("Is chicken enough?", "ayam cukup?", "habis", complete)
        self.assertEqual(seen["user"]["staff_message"], "habis")
        self.assertEqual(seen["user"]["question_in_english"], "Is chicken enough?")

    def test_decide(self):
        clear = {"is_answer": True, "clear": True, "summary_en": "", "status": "ok"}
        unclear = dict(clear, clear=False)
        chatter = dict(clear, is_answer=False)
        self.assertEqual(sl.decide_reply({}, clear, is_reply_to_question=False), "answer")
        self.assertEqual(sl.decide_reply({}, unclear, is_reply_to_question=False), "clarify")
        # Only ONE clarification; then whatever they said is saved.
        self.assertEqual(sl.decide_reply({"clarify_sent_at": "x"}, unclear,
                                         is_reply_to_question=False), "answer")
        self.assertEqual(sl.decide_reply({}, chatter, is_reply_to_question=False), "ignore")
        # A direct reply to the question counts even if the AI is unsure.
        self.assertEqual(sl.decide_reply({}, chatter, is_reply_to_question=True), "answer")
        # AI down: direct replies only.
        self.assertEqual(sl.decide_reply({}, None, is_reply_to_question=True), "answer")
        self.assertEqual(sl.decide_reply({}, None, is_reply_to_question=False), "ignore")

    def test_texts_in_language(self):
        self.assertIn("🙏", sl.reminder_text("tamil"))
        self.assertIn("\n", sl.reminder_text("bm_tamil"))
        self.assertTrue(sl.clarify_text("bengali"))
        self.assertEqual(sl.clarify_text("unknown"), sl.clarify_text("bm"))


class SummaryTests(unittest.TestCase):
    def test_morning_summary(self):
        asked = "2026-09-24T10:35:00+08:00"
        threads = [
            dict(_t("answered", tid=1), asked_at=asked,
                 answered_at="2026-09-24T10:45:00+08:00", reply_status="ok"),
            dict(_t("answered", tid=2, slot="cook"), asked_at=asked,
                 answered_at="2026-09-24T10:55:00+08:00", reply_status="finished",
                 reply_en="Ayam kicap finished by 2pm"),
            dict(_t("no_reply", tid=3, slot="lunch", outlet_code="SEK20", chat=-2),
                 asked_at="2026-09-24T15:00:00+08:00", cashier="Syed"),
            dict(_t("dropped", tid=4, slot="night")),
        ]
        text = sl.format_morning_summary(threads)
        self.assertIn("BISTRO7: ✅ answered — 2/2 answered · avg 15 min", text)
        self.assertIn("SEK20: ❌ no reply — 0/1 answered", text)
        self.assertIn("• SEK20 15:00 lunch (Syed)", text)
        self.assertIn("• BISTRO7 10:55 cook: Ayam kicap finished by 2pm", text)
        self.assertEqual(sl.format_morning_summary([]), "")

    def test_slow_verdict(self):
        self.assertEqual(sl.verdict({"asked": 4, "answered": 4, "no_reply": 0, "avg_minutes": 50}),
                         "🐢 slow")
        self.assertEqual(sl.verdict({"asked": 4, "answered": 3, "no_reply": 1, "avg_minutes": 5}),
                         "🐢 slow")


class ThreadRowTests(unittest.TestCase):
    def test_row_shape(self):
        row = sl.thread_row(outlet_code="BISTRO7", chat_id=CHAT, slot="stock", text="t",
                            question_en="q", facts={"item": "Ayam"}, language="bengali",
                            cashier="Rahim", now=NOW, status=sl.OPEN, message_id=9)
        self.assertEqual((row["shift"], row["shift_date"]), ("morning", "2026-09-24"))
        self.assertEqual(row["asked_at"], NOW.isoformat())
        queued = sl.thread_row(outlet_code="BISTRO7", chat_id=CHAT, slot="stock", text="t",
                               question_en="q", facts={}, language="bm", cashier="R",
                               now=NOW, status=sl.QUEUED)
        self.assertIsNone(queued["asked_at"])


if __name__ == "__main__":
    unittest.main()
