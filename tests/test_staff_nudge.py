"""Follow-up nudges: timing, window, wording, fact check, logging."""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_live as sl
import staff_nudge as sn

MY = sl.cashier_names.MALAYSIA_TZ
NOW = datetime(2026, 9, 24, 11, 0, tzinfo=MY)
ASKED = NOW - timedelta(minutes=45)


def _thread(**kw):
    row = {"id": 3, "chat_id": -5207657926, "outlet_code": "BISTRO7", "slot": "order",
           "status": "open", "asked_at": ASKED.isoformat(), "language": "bm",
           "cashier": "Kalai", "message_id": 77, "nudge_count": 0}
    row.update(kw)
    return row


def _ai(text, english="EN"):
    return lambda system, user: {"data": {"text": text, "english": english},
                                 "provider": "deepseek", "model": "deepseek-flash",
                                 "tokens_in": 300, "tokens_out": 40}


class TimingTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_default_interval_and_env_override(self):
        self.assertEqual(sn.after(), timedelta(minutes=40))
        with mock.patch.dict("os.environ", {"NUDGE_AFTER_MIN": "15"}):
            self.assertEqual(sn.after(), timedelta(minutes=15))
        with mock.patch.dict("os.environ", {"NUDGE_AFTER_MIN": "abc"}):
            self.assertEqual(sn.after(), timedelta(minutes=40))

    def test_exactly_two_nudges_at_the_right_times(self):
        t = _thread()
        nudges = []
        for minute in range(0, 130, 5):
            now = ASKED + timedelta(minutes=minute)
            n = sn.due(t, now)
            if n:
                nudges.append((minute, n))
                t["nudge_count"] = n
        self.assertEqual(nudges, [(40, 1), (80, 2)])
        self.assertIsNone(sn.due(t, ASKED + timedelta(minutes=500)))
        self.assertEqual(sn.expire_after("order"), timedelta(minutes=120))
        self.assertEqual(sn.expire_after("wastage"), timedelta(hours=1))

    def test_window(self):
        self.assertTrue(sn.in_window(datetime(2026, 9, 24, 7, 0, tzinfo=MY)))
        self.assertTrue(sn.in_window(datetime(2026, 9, 24, 23, 30, tzinfo=MY)))
        self.assertFalse(sn.in_window(datetime(2026, 9, 24, 23, 31, tzinfo=MY)))
        self.assertFalse(sn.in_window(datetime(2026, 9, 24, 6, 59, tzinfo=MY)))

    def test_simulated_no_reply_gives_two_nudges_then_no_reply(self):
        """The whole tick plan for one silent outlet, every 10 minutes."""
        t = dict(_thread(), asked_at=None)
        asked = datetime(2026, 9, 24, 20, 5, tzinfo=MY)
        t["asked_at"] = asked.isoformat()
        seen = []
        for k in range(0, 14):
            now = asked + timedelta(minutes=10 * k)
            for action, _ in sl.plan_tick([t], now):
                seen.append((10 * k, action))
                if action == "nudge":
                    t["nudge_count"] += 1
                    t["status"] = sl.REMINDED
                elif action == "expire":
                    t["status"] = sl.NO_REPLY
        self.assertEqual(seen, [(40, "nudge"), (80, "nudge"), (120, "expire")])


class WordingTests(unittest.TestCase):
    def test_facts_are_only_outlet_checkin_minutes(self):
        facts = sn.facts_for(_thread(), "Bistro 7", NOW, 1)
        self.assertEqual(facts, {"outlet": "Bistro 7", "checkin": "tomorrow's order",
                                 "slot": "order", "minutes": "45", "nudge_no": 1})

    def test_template_in_every_language_and_firmer_second_time(self):
        facts = sn.facts_for(_thread(), "Bistro 7", NOW, 1)
        for lang in ("bm", "tamil", "english", "indonesian", "bengali"):
            first = sn.template(facts, lang)
            second = sn.template(dict(facts, nudge_no=2), lang)
            self.assertIn("45", first, lang)
            self.assertIn("45", second, lang)
            self.assertNotEqual(first, second, lang)
            self.assertIn("Bistro 7", second, lang)
        self.assertEqual(sn.template(facts, "bm_tamil").count("\n"), 1)
        self.assertEqual(sn.template(facts, "unknown"), sn.template(facts, "bm"))

    def test_ai_wording_used_in_the_cashiers_language(self):
        res = sn.build(_thread(language="english"), "Bistro 7", NOW, 1,
                       complete=_ai("Still waiting on the order question from 45 minutes ago 🙏"))
        self.assertEqual(res["source"], "ai")
        self.assertIn("45", res["text"])
        self.assertEqual(res["tokens_in"], 300)
        self.assertEqual(res["facts"]["nudge_no"], 1)

    def test_prompt_carries_language_and_facts(self):
        seen = {}

        def capture(system, user):
            seen["system"], seen["user"] = system, json.loads(user)
            return None
        sn.build(_thread(language="bengali"), "Bistro 7", NOW, 2, complete=capture)
        self.assertIn("ONLY the facts", seen["system"])
        self.assertIn("Bengali", seen["user"]["language"])
        self.assertEqual(seen["user"]["facts"]["minutes"], "45")
        self.assertEqual(seen["user"]["facts"]["nudge_no"], 2)

    def test_invented_number_item_or_name_falls_back(self):
        for bad in ("Order question from 50 minutes ago?", "Ayam order still waiting, 45 min",
                    "Syed, the order from 45 minutes ago?", "45 min — RM200 order waiting"):
            res = sn.build(_thread(language="english"), "Bistro 7", NOW, 1,
                           complete=_ai(bad), other_names=["Syed"])
            self.assertEqual(res["source"], "template", bad)
            self.assertEqual(res["text"], res["template"])
            self.assertTrue(res["problems"], bad)

    def test_minutes_must_be_written(self):
        res = sn.build(_thread(language="english"), "Bistro 7", NOW, 1,
                       complete=_ai("Still waiting on the order question 🙏"))
        self.assertEqual(res["source"], "template")
        self.assertIn("minutes not written as in the data", res["problems"])

    def test_ai_down_uses_template(self):
        res = sn.build(_thread(), "Bistro 7", NOW, 1, complete=lambda s, u: None)
        self.assertEqual((res["source"], res["problems"]), ("template", ["ai unavailable"]))

        def boom(s, u):
            raise RuntimeError("x")
        self.assertEqual(sn.build(_thread(), "Bistro 7", NOW, 2, complete=boom)["source"],
                         "template")

    def test_log_row_kind_and_number(self):
        res = sn.build(_thread(), "Bistro 7", NOW, 2, complete=_ai("Bistro 7, order 45 minit. Jawab ya."))
        row = sn.log_row(_thread(), res, "natural")
        self.assertEqual((row["kind"], row["nudge_no"], row["slot"], row["mode"]),
                         ("nudge", 2, "order", "natural"))
        self.assertEqual(row["source"], "ai")
        self.assertEqual(row["facts"]["minutes"], "45")
        self.assertEqual(row["provider"], "deepseek")


if __name__ == "__main__":
    unittest.main()
