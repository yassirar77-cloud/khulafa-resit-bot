"""Learning loop: picking fast wordings, the token budget, examples in the prompt."""

import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import staff_chat as sc
import staff_learning as slr
from tests.fake_supabase import FakeSupabase

WEEK = date(2026, 9, 14)   # a Monday


def _t(text, minutes, *, lang="bm", slot="order", clear=True, tid=None):
    return {"id": tid, "status": "answered", "language": lang, "slot": slot,
            "question_text": text, "reply_clear": clear,
            "asked_at": "2026-09-15T20:05:00+08:00",
            "answered_at": f"2026-09-15T{20 + (5 + minutes) // 60:02d}:{(5 + minutes) % 60:02d}:00+08:00"}


THREADS = (
    [_t("Order esok: Ayam 40kg. Ok?", 4), _t("Order esok: Ayam 40kg. Ok?", 6)]          # 5 min
    + [_t("Esok nak order apa?", 30), _t("Esok nak order apa?", 20, clear=False)]        # 25 min
    + [_t("Untuk esok: Ayam 40kg. Ok ke?", 10), _t("Untuk esok: Ayam 40kg. Ok ke?", 12)]  # 11 min
    + [_t("Barang utama esok: Ayam 40kg.", 8), _t("Barang utama esok: Ayam 40kg.", 8)]  # 8 min
    + [_t("Sekali je", 1)]                                                              # 1 sample
    + [_t("கடை ரெடியா?", 3, lang="tamil", slot="open"), _t("கடை ரெடியா?", 9, lang="tamil", slot="open")]
    + [dict(_t("x", 2), status="no_reply")]
)


class PickTests(unittest.TestCase):
    def test_top_three_fastest_per_language_and_slot(self):
        rows = slr.pick(THREADS, week=WEEK)
        bm = [r for r in rows if (r["language"], r["slot"]) == ("bm", "order")]
        self.assertEqual([r["text"] for r in bm],
                         ["Order esok: Ayam 40kg. Ok?", "Barang utama esok: Ayam 40kg.",
                          "Untuk esok: Ayam 40kg. Ok ke?"])
        self.assertEqual([r["rank"] for r in bm], [1, 2, 3])
        self.assertEqual((bm[0]["median_minutes"], bm[0]["samples"], bm[0]["clear_rate"]),
                         (5.0, 2, 1.0))
        self.assertNotIn("Sekali je", [r["text"] for r in rows])          # one sample only
        self.assertNotIn("Esok nak order apa?", [r["text"] for r in bm])  # 4th
        tamil = [r for r in rows if r["language"] == "tamil"]
        self.assertEqual((len(tamil), tamil[0]["slot"], tamil[0]["median_minutes"]),
                         (1, "open", 6.0))
        self.assertEqual({r["week_start"] for r in rows}, {"2026-09-14"})

    def test_clear_rate_breaks_ties(self):
        rows = slr.pick([_t("A", 10), _t("A", 10, clear=False), _t("B", 10), _t("B", 10)], week=WEEK)
        self.assertEqual([r["text"] for r in rows], ["B", "A"])
        self.assertEqual(rows[1]["clear_rate"], 0.5)

    def test_week_start_and_empty(self):
        self.assertEqual(slr.week_start(date(2026, 9, 17)), WEEK)
        self.assertEqual(slr.pick([], week=WEEK), [])
        self.assertIn("no wording", slr.format_report([]))
        self.assertIn("bm order: 5.0 min", slr.format_report(slr.pick(THREADS, week=WEEK)))


class BudgetTests(unittest.TestCase):
    def test_budget_is_thirty_percent_of_the_average_prompt(self):
        self.assertEqual(slr.budget(1000), 300)
        self.assertEqual(slr.budget(None), int(slr.DEFAULT_AVG_TOKENS * 0.3))
        self.assertEqual(slr.est_tokens("Order esok: Ayam 40kg. Ok?"), 6)
        self.assertGreater(slr.est_tokens("கடை ரெடியா?"), slr.est_tokens("Kedai ready?"))

    def test_select_stops_at_the_budget(self):
        rows = slr.pick(THREADS, week=WEEK)
        self.assertEqual(len(slr.select(rows, "bm", "order", 300)), 3)
        self.assertEqual(len(slr.select(rows, "bm", "order", 12)), 1)
        self.assertEqual(slr.select(rows, "bm", "order", 0), [])
        self.assertEqual(slr.select(rows, "bengali", "order", 300), [])
        self.assertEqual(slr.select([], "bm", "order", 300), [])


class PromptTests(unittest.TestCase):
    def test_examples_in_prompt_when_available_and_absent_when_empty(self):
        seen = {}

        def capture(system, user):
            seen["system"], seen["user"] = system, json.loads(user)
            return None
        sc.build_message("order", "bm", {"ask": True}, complete=capture,
                         examples=["Order esok: Ayam 40kg. Ok?", "", "Barang utama esok?"])
        self.assertEqual(seen["user"]["examples_of_wording_that_got_fast_replies"],
                         ["Order esok: Ayam 40kg. Ok?", "Barang utama esok?"])
        self.assertIn("tone and shape", seen["system"])
        sc.build_message("order", "bm", {"ask": True}, complete=capture)
        self.assertNotIn("examples_of_wording_that_got_fast_replies", seen["user"])
        res = sc.build_message("open", "bm", {}, complete=lambda s, u: None, examples=["x"])
        self.assertEqual(res["source"], "template")

    def test_example_numbers_are_still_not_allowed_in_the_wording(self):
        facts = {"ask": True}
        res = sc.build_message("order", "bm", facts, vocabulary={"ayam"},
                               examples=["Order esok: Ayam 40kg. Ok?"],
                               complete=lambda s, u: {"data": {"text": "Order esok: Ayam 40kg. Ok?",
                                                               "english": "x"}})
        self.assertEqual(res["source"], "template")
        self.assertIn("number 40 not in data", res["problems"])


class TableTests(unittest.TestCase):
    def test_weekly_rows_populate_the_table(self):
        db = FakeSupabase()
        rows = slr.pick(THREADS, week=WEEK)
        db.table(slr.TABLE).insert(rows).execute()
        stored = db.rows(slr.TABLE)
        self.assertEqual(len(stored), 4)
        self.assertEqual({r["week_start"] for r in stored}, {"2026-09-14"})
        # Re-running the week replaces, never duplicates.
        db.table(slr.TABLE).delete().eq("week_start", "2026-09-14").execute()
        db.table(slr.TABLE).insert(rows).execute()
        self.assertEqual(len(db.rows(slr.TABLE)), 4)
        pairs = slr.by_pair(db.rows(slr.TABLE))
        self.assertEqual(len(pairs[("bm", "order")]), 3)


if __name__ == "__main__":
    unittest.main()
