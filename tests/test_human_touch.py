"""Unit tests for ``human_touch`` — honest human-feel supervision.

Run with::

    python -m unittest tests.test_human_touch
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from human_touch import (  # noqa: E402
    ack,
    engagement_by_chat,
    format_owner_scoreboard,
    greeting,
    personalise,
    praise_message,
)

D1 = date(2026, 8, 7)
D2 = date(2026, 8, 8)


class Greetings(unittest.TestCase):

    def test_greets_by_first_name_and_varies_by_day(self):
        g1 = greeting("Ravi Kumar", -100, D1)
        g2 = greeting("Ravi Kumar", -100, D2)
        self.assertIn("Ravi boss", g1)
        self.assertIn("Ravi boss", g2)
        self.assertNotEqual(g1, g2)          # a new line the next day
        self.assertEqual(g1, greeting("Ravi Kumar", -100, D1))  # deterministic

    def test_no_registered_name_falls_back_to_boss(self):
        self.assertIn("boss", greeting(None, -100, D1))
        self.assertIn("boss", greeting("   ", -100, D1))

    def test_personalise_prepends_and_keeps_empty_empty(self):
        out = personalise("விலை ஏறிடுச்சு!", "Ravi", -100, D1)
        self.assertTrue(out.endswith("விலை ஏறிடுச்சு!"))
        self.assertIn("Ravi boss", out.split("\n")[0])
        self.assertEqual(personalise("", "Ravi", -100, D1), "")
        self.assertEqual(personalise(None, "Ravi", -100, D1), "")


class Acks(unittest.TestCase):

    def test_ack_is_named_varied_and_always_mentions_boss(self):
        a1 = ack("Ravi", -100, D1)
        a2 = ack("Ravi", -100, D2)
        self.assertIn("Ravi boss", a1)
        self.assertNotEqual(a1, a2)
        # Every variant truthfully reports the upward step.
        for d in (D1, D2, date(2026, 8, 9)):
            self.assertIn("Boss", ack("Ravi", -100, d))

    def test_ack_never_raises(self):
        try:
            self.assertTrue(ack(None, None, None))
            self.assertTrue(ack(object(), "bad", D1))
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"ack raised: {e}")


def _row(chat=-100, replied=True, asked="2026-08-07T10:00:00+00:00",
         replied_at="2026-08-07T10:30:00+00:00"):
    return {
        "chat_id": chat,
        "question_type": "price_spike",
        "asked_at": asked,
        "replied_at": replied_at if replied else None,
    }


class Engagement(unittest.TestCase):

    def test_stats_count_answers_and_average_minutes(self):
        rows = [
            _row(),                             # answered in 30 min
            _row(replied_at="2026-08-07T11:30:00+00:00"),  # 90 min
            _row(replied=False),
            _row(chat=-200, replied=False),
        ]
        stats = engagement_by_chat(rows)
        by_chat = {s["chat_id"]: s for s in stats}
        self.assertEqual(by_chat[-100]["asked"], 3)
        self.assertEqual(by_chat[-100]["answered"], 2)
        self.assertAlmostEqual(by_chat[-100]["avg_response_minutes"], 60.0)
        self.assertEqual(by_chat[-200]["answered"], 0)
        self.assertIsNone(by_chat[-200]["avg_response_minutes"])

    def test_garbage_never_raises(self):
        try:
            self.assertEqual(engagement_by_chat(None), [])
            self.assertEqual(engagement_by_chat(["junk", {}]), [])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"engagement raised: {e}")


class PraiseAndScoreboard(unittest.TestCase):

    def test_full_responder_is_praised_partial_is_not(self):
        full = {"chat_id": -100, "asked": 4, "answered": 4,
                "avg_response_minutes": 32.0}
        partial = {"chat_id": -200, "asked": 3, "answered": 1,
                   "avg_response_minutes": 400.0}
        none_asked = {"chat_id": -300, "asked": 0, "answered": 0,
                      "avg_response_minutes": None}
        out = praise_message(full)
        self.assertIn("🌟 இந்த வாரம் சூப்பர்!", out)
        self.assertIn("4 கேள்விக்கும் பதில்", out)
        self.assertIn("Boss-க்கும் தெரியும்", out)
        self.assertEqual(praise_message(partial), "")
        self.assertEqual(praise_message(none_asked), "")

    def test_scoreboard_shows_rates_speeds_and_stars(self):
        stats = [
            {"chat_id": -100, "asked": 4, "answered": 4,
             "avg_response_minutes": 32.0},
            {"chat_id": -200, "asked": 3, "answered": 1,
             "avg_response_minutes": 400.0},
        ]
        out = format_owner_scoreboard(stats, lambda c: f"SEK{abs(c)}")
        self.assertIn("SEK100: 4/4 answered, avg 32 min 🌟", out)
        self.assertIn("SEK200: 1/3 answered, avg 6.7 h", out)
        self.assertIn("1 chat(s) answered everything", out)

    def test_empty_scoreboard_is_silent(self):
        self.assertEqual(format_owner_scoreboard([]), "")
        self.assertEqual(
            format_owner_scoreboard([{"chat_id": -1, "asked": 0, "answered": 0}]),
            "",
        )


if __name__ == "__main__":
    unittest.main()
