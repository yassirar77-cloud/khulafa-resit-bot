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
        self.assertEqual(sl.plan_tick([_t("open", 20)], NOW), [])

    def test_reminder_after_thirty_minutes_once(self):
        self.assertEqual([a for a, _ in sl.plan_tick([_t("open", 35)], NOW)], ["remind"])
        self.assertEqual(sl.plan_tick([_t("reminded", 50)], NOW), [])

    def test_expires_after_one_hour(self):
        self.assertEqual([a for a, _ in sl.plan_tick([_t("reminded", 61)], NOW)], ["expire"])
        self.assertEqual([a for a, _ in sl.plan_tick([_t("open", 61)], NOW)], ["expire"])

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
        t["asked_at"] = (late - timedelta(minutes=40)).isoformat()
        self.assertEqual(sl.plan_tick([t], late), [])
        t["asked_at"] = (late - timedelta(minutes=70)).isoformat()
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
                                  "summary_en": "Chicken finished", "status": "finished",
                                  "items": [], "asks_if_bot": False})

    def test_parse_reply_keeps_order_items(self):
        parsed = sl.parse_reply("What to order?", "Esok nak order apa?",
                                "esok ayam 40kg ikan 10kg", self._complete({
                                    "is_answer": True, "clear": True, "summary_en": "x",
                                    "status": "order",
                                    "items": [{"item": "ayam", "qty": 40, "unit": "kg"}]}))
        self.assertEqual(parsed["items"], [{"item": "ayam", "qty": 40, "unit": "kg"}])
        self.assertIn("never invent", sl.REPLY_PROMPT.lower())


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
        self.assertIn("• BISTRO7 10:55 cook: Ayam kicap finished by 2pm", text)
        self.assertEqual(sl.format_morning_summary([]), "")

    def test_summary_shows_each_send_time_and_outcome(self):
        threads = [
            dict(_t("answered", tid=1, slot="open"), asked_at="2026-09-24T08:00:10+08:00",
                 answered_at="2026-09-24T08:12:10+08:00"),
            dict(_t("no_reply", tid=2, slot="lunch"), asked_at="2026-09-24T15:00:20+08:00"),
            dict(_t("reminded", tid=3, slot="order"), asked_at="2026-09-24T20:05:20+08:00"),
            dict(_t("queued", tid=4, slot="bills")),            # never sent
            dict(_t("dropped", tid=5, slot="night")),           # never sent
            dict(_t("no_reply", tid=6, slot="order", outlet_code="SEK20", chat=-2),
                 asked_at="2026-09-24T20:05:21+08:00"),
        ]
        text = sl.format_morning_summary(threads)
        self.assertIn("   08:00 open ✅ 12m · 15:00 lunch ✗ · 20:05 order ⏳", text)
        self.assertIn("   20:05 order ✗", text)
        self.assertNotIn("bills", text)
        self.assertIn("By check-in time (all outlets):", text)
        self.assertIn("• 08:00 open: 1/1 answered", text)
        self.assertIn("• 20:05 order: 0/2 answered", text)
        # Schedule order, not alphabetical.
        self.assertLess(text.index("• 08:00 open"), text.index("• 15:00 lunch"))
        self.assertLess(text.index("• 15:00 lunch"), text.index("• 20:05 order"))

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


class HonestyTests(unittest.TestCase):
    def test_detects_are_you_a_bot_in_staff_languages(self):
        for text in ("Ni bot ke?", "are you a bot?", "Is this a real person?",
                     "Awak ni robot ke", "Apni ki bot?", "manusia atau bot?",
                     "இது bot-ஆ?", "நீங்க மனுஷனா?", "நீங்க ஆளா?", "kamu bot"):
            self.assertTrue(sl.asks_if_bot(text), text)

    def test_ignores_normal_replies(self):
        for text in ("ok done", "Ayam habis", "order ayam 40kg", "Esok ikan 10kg ok boss",
                     "bos ke ni?", "ஆள் இல்ல", "சாமான் வந்தாச்சா", "semua ok"):
            self.assertFalse(sl.asks_if_bot(text), text)

    def test_answer_is_honest_in_every_language(self):
        self.assertEqual(sl.honest_reply("english"),
                         "This is the Khulafa office system; the boss reads every reply "
                         "every morning.")
        for lang in ("bm", "tamil", "english", "indonesian", "bengali", "bm_tamil"):
            text = sl.honest_reply(lang)
            self.assertIn("Khulafa", text)
            for claim in ("I am a person", "saya orang", "manusia", "real person"):
                self.assertNotIn(claim, text)



class SlotSettingTests(unittest.TestCase):
    def test_only_listed_check_ins_run(self):
        with mock.patch.dict("os.environ", {"STAFF_CHAT_SLOTS": "order, Bills"}):
            self.assertEqual(sl.enabled_slots(), {"order", "bills"})
            self.assertTrue(sl.slot_enabled("order"))
            for slot in ("open", "stock", "cook", "lunch", "night"):
                self.assertFalse(sl.slot_enabled(slot), slot)

    def test_unset_means_all(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(sl.enabled_slots(),
                             set(sl.staff_chat.SLOTS) | set(sl.staff_ops.OPS_SLOTS))


class ButtonTests(unittest.TestCase):
    def test_button_sets_per_check_in(self):
        self.assertEqual(sl.button_set("order", {"items": [1]}), "order")
        self.assertIsNone(sl.button_set("order", {"ask": True}))   # must be typed
        self.assertEqual(sl.button_set("open", {}), "status")
        self.assertEqual(sl.button_set("night", {}), "status")
        self.assertEqual(sl.button_set("bills", {}), "bills")
        self.assertEqual(sl.button_set("lunch", {}), "lunch")

    def test_keyboard_in_cashier_language(self):
        rows = sl.keyboard(42, "order", "tamil")
        self.assertEqual(rows, [[("✅ சரி", "sc:42:ok"), ("✏️ மாத்தணும்", "sc:42:change")]])
        bills = sl.keyboard(7, "bills", "bengali")
        self.assertEqual([r[0][1] for r in bills], ["sc:7:upload", "sc:7:gave", "sc:7:nobill"])
        self.assertEqual(bills[1][0][0], "📦 Boss ke diyechi")
        self.assertEqual(sl.keyboard(7, "status", "bm")[0][0][0], "✅ Semua OK")
        self.assertEqual(sl.keyboard(7, "lunch", "english")[0][1][0], "⚠️ Something ran out")
        self.assertEqual(sl.keyboard(7, "status", "bm_tamil")[0][0][0], "✅ Semua OK")
        self.assertEqual(sl.keyboard(None, "order", "bm"), [])
        self.assertEqual(sl.keyboard(7, None, "bm"), [])

    def test_every_label_fits_telegram(self):
        for lang, labels in sl._LABELS.items():
            self.assertEqual(set(labels), set(sl.CHOICES), lang)
            for code in sl.CHOICES:
                self.assertLessEqual(len(f"sc:999999999:{code}".encode()), 64)

    def test_parse_callback(self):
        self.assertEqual(sl.parse_callback("sc:42:gave"), (42, "gave"))
        for bad in ("sc:x:ok", "sc:42:hack", "review:1:save", None, "sc:1"):
            self.assertIsNone(sl.parse_callback(bad), bad)

    def test_tap_counts_as_answer(self):
        now = NOW
        self.assertEqual(sl.tap_outcome(_t("open", 5), "ok", now), "answer")
        self.assertEqual(sl.tap_outcome(_t("reminded", 45), "ok", now), "answer")
        self.assertEqual(sl.tap_outcome(_t("no_reply", 120), "ok", now), "answer")   # late tap
        self.assertEqual(sl.tap_outcome(_t("no_reply", 60 * 13), "ok", now), "stale")
        self.assertEqual(sl.tap_outcome(_t("answered", 5), "ok", now), "already")
        fields = sl.tap_fields("gave", "📦 Dah bagi bos", now)
        self.assertEqual(fields["status"], sl.ANSWERED)
        self.assertEqual(fields["reply_status"], sl.HANDED_IN)
        self.assertEqual(fields["answer_source"], "button")
        self.assertFalse(fields["awaiting_detail"])
        self.assertTrue(sl.tap_fields("change", "x", now)["awaiting_detail"])

    def test_details_after_change_tap(self):
        now = NOW
        thread = dict(_t("answered", 5), answered_at=(now - timedelta(minutes=5)).isoformat(),
                      awaiting_detail=True, reply_text="[button] ✏️ Tukar",
                      reply_en="Wants to change it")
        self.assertTrue(sl.awaiting_detail(thread, now))
        self.assertFalse(sl.awaiting_detail(dict(thread, awaiting_detail=False), now))
        self.assertFalse(sl.awaiting_detail(
            dict(thread, answered_at=(now - timedelta(minutes=70)).isoformat()), now))
        fields = sl.detail_fields(thread, "ayam 60kg", "Chicken 60kg instead")
        self.assertEqual(fields["reply_en"], "Wants to change it: Chicken 60kg instead")
        self.assertIn("ayam 60kg", fields["reply_text"])
        self.assertFalse(fields["awaiting_detail"])
        self.assertIn("type", sl.detail_prompt("change", "english").lower())
        self.assertIn("\n", sl.detail_prompt("problem", "bm_tamil"))
        self.assertIsNone(sl.detail_prompt("ok", "bm"))


class HandInTests(unittest.TestCase):
    FACTS = {"supplier": "Bestari Farm", "days": "12", "last": "12/09",
             "supplier_full": "BESTARI FARM (M) SDN BHD", "last_iso": "2026-09-12"}

    def test_handin_row(self):
        thread = dict(_t("answered", 5, slot="bills", cashier="Kalai"), facts=self.FACTS)
        self.assertEqual(sl.handin_row(thread), {
            "outlet_code": "BISTRO7", "supplier": "BESTARI FARM (M) SDN BHD",
            "last_bill": "2026-09-12", "days_missing": 12, "cashier": "Kalai",
            "thread_id": 1})
        self.assertIsNone(sl.handin_row(dict(thread, slot="order")))

    def test_summary_lists_handed_in_bills(self):
        threads = [dict(_t("answered", slot="bills", cashier="Kalai"),
                        asked_at="2026-09-24T22:10:00+08:00",
                        answered_at="2026-09-24T22:18:00+08:00",
                        reply_status=sl.HANDED_IN, facts=self.FACTS)]
        text = sl.format_morning_summary(threads)
        self.assertIn("📦 Bills handed in, not uploaded — please check the paper bills:", text)
        self.assertIn("• BISTRO7: Bestari Farm (last upload 12/09) — Kalai, 22:18", text)
        self.assertIn("handed_in", sl.REPLY_PROMPT)

if __name__ == "__main__":
    unittest.main()
