"""Director Q&A: the SQL guard, generation, answer check, table, logging."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import director_sql as ds


def _ai(sql=None, note="", answer=None):
    def complete(system, user):
        if system is ds.ANSWER_PROMPT:
            return {"data": {"answer": answer}} if answer is not None else None
        return {"data": {"sql": sql, "note": note}}
    return complete


class SanitizeTests(unittest.TestCase):
    def test_select_gets_limit_and_comments_stripped(self):
        sql, why = ds.sanitize("SELECT outlet_code, sum(qty) AS kg\n"
                               "FROM staff_order_items -- chicken\n"
                               "/* this week */ WHERE canonical_item = 'ayam'\n"
                               "GROUP BY 1 ORDER BY 2 DESC;")
        self.assertEqual(why, "")
        self.assertTrue(sql.startswith("SELECT outlet_code"))
        self.assertNotIn("--", sql)
        self.assertNotIn("/*", sql)
        self.assertTrue(sql.endswith("ORDER BY 2 DESC LIMIT 200"))

    def test_limit_is_capped_not_raised(self):
        self.assertTrue(ds.sanitize("select * from receipts limit 5")[0].endswith("limit 5"))
        self.assertTrue(ds.sanitize("select * from receipts limit 5000")[0].endswith("LIMIT 200"))
        self.assertTrue(ds.sanitize("select * from receipts limit all")[0].endswith("LIMIT 200"))
        sql, _ = ds.sanitize("select * from receipts offset 10")
        self.assertIn("LIMIT 200 offset 10", sql)

    def test_everything_that_is_not_one_select_is_rejected(self):
        bad = {
            "delete from staff_order_items": "not a SELECT",
            "DELETE orders": "not a SELECT",
            "update receipts set total = 0": "not a SELECT",
            "with x as (select 1) select * from x": "not a SELECT",
            "select 1; drop table receipts": "more than one statement",
            "select * into t from receipts": "forbidden word: into",
            "select * from receipts for update": "forbidden word: update",
            "select * from receipts for share": "row locks not allowed",
            "select pg_sleep(10)": "forbidden word: pg_sleep",
            "select set_config('a','b',false)": "forbidden word: set_config",
            "select * from information_schema.tables": "forbidden word: information_schema",
            "select * from auth.users": "forbidden word: auth",
            "": "empty",
            "-- just a comment": "empty",
            "select 1 /* ; drop */ ": "",           # comment content is stripped, fine
        }
        for sql, reason in bad.items():
            safe, why = ds.sanitize(sql)
            if reason:
                self.assertIsNone(safe, sql)
                self.assertEqual(why, reason, sql)
            else:
                self.assertIsNotNone(safe, sql)
        self.assertIsNone(ds.sanitize("select " + "x" * 3000)[0])
        # A comment cannot hide a second statement.
        self.assertIsNone(ds.sanitize("select 1 -- x\n; delete from receipts")[0])


class GenerateTests(unittest.TestCase):
    def test_chicken_orders_sek7_this_week(self):
        ai = _ai("SELECT order_for, qty, unit FROM staff_order_items "
                 "WHERE outlet_code = 'SEK7' AND canonical_item = 'ayam' "
                 "AND order_for >= date_trunc('week', current_date) ORDER BY order_for")
        gen = ds.generate("chicken orders sek 7 this week", ai)
        self.assertEqual(gen["error"], "")
        self.assertIn("staff_order_items", gen["sql"])
        self.assertTrue(gen["sql"].endswith("LIMIT 200"))

    def test_delete_orders_is_rejected(self):
        gen = ds.generate("delete orders", _ai("DELETE FROM staff_order_items"))
        self.assertIsNone(gen["sql"])
        self.assertEqual(gen["error"], "rejected: not a SELECT")
        gen = ds.generate("x", _ai(None, note="not in these tables"))
        self.assertEqual(gen["error"], "not in these tables")
        self.assertEqual(ds.generate("x", lambda s, u: None)["error"], "ai unavailable")

    def test_prompt_carries_schema_and_rules(self):
        seen = {}

        def capture(system, user):
            seen["system"], seen["user"] = system, json.loads(user)
            return None
        ds.generate("how many bills today", capture)
        self.assertIn("staff_order_items", seen["system"])
        self.assertIn("Exactly one SELECT", seen["system"])
        self.assertEqual(seen["user"]["question"], "how many bills today")

    def test_question_detection_for_the_group(self):
        for t in ("chicken orders sek 7 this week?", "How many bills today", "berapa bil semalam",
                  "show me no-replies", "which outlet ordered most ayam"):
            self.assertTrue(ds.looks_like_question(t), t)
        for t in ("ok noted", "sudah hantar", "thanks", ""):
            self.assertFalse(ds.looks_like_question(t), t)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(ds.enabled())
        with mock.patch.dict("os.environ", {"DIRECTOR_QA": "on"}):
            self.assertTrue(ds.enabled())


ROWS = [{"order_for": "2026-09-22", "qty": 40.0, "unit": "kg"},
        {"order_for": "2026-09-24", "qty": 12.5, "unit": "kg"}]


class AnswerTests(unittest.TestCase):
    def test_answer_numbers_must_come_from_the_rows(self):
        self.assertEqual(ds.check_answer("Sek 7 ordered 40kg on 22 Sep and 12.5kg on 24 Sep.", ROWS,
                                         "chicken orders sek 7"), [])
        self.assertEqual(ds.check_answer("Sek 7 ordered 40kg.", ROWS), ["7"])
        self.assertEqual(ds.check_answer("2 orders this week.", ROWS), [])     # row count
        self.assertEqual(ds.check_answer("52.5kg of chicken in total.", ROWS), ["52.5"])
        text, problems = ds.answer_line("q", ROWS, _ai(answer="Two orders: 40kg and 12.5kg."))
        self.assertEqual((text, problems), ("Two orders: 40kg and 12.5kg.", []))
        text, problems = ds.answer_line("q", ROWS, _ai(answer="52.5kg in total."))
        self.assertEqual(text, "2 rows matched.")
        self.assertEqual(problems, ["number 52.5 not in rows"])
        self.assertEqual(ds.answer_line("q", [], lambda s, u: None)[0], "Nothing matched.")

    def test_table(self):
        table = ds.format_rows(ROWS)
        lines = table.split("\n")
        self.assertEqual(lines[0].split(" | ")[0].strip(), "order_for")
        self.assertIn("40", lines[1])
        self.assertIn("12.50", lines[2])
        self.assertEqual(len(lines), 3)
        many = [{"n": i} for i in range(40)]
        self.assertIn("… 25 more row(s)", ds.format_rows(many))
        self.assertEqual(ds.format_rows([]), "")


class RunTests(unittest.TestCase):
    def test_end_to_end_with_fake_db(self):
        ran = []

        def run_sql(sql):
            ran.append(sql)
            return ROWS
        ai = _ai("SELECT order_for, qty, unit FROM staff_order_items WHERE outlet_code = 'SEK7' "
                 "AND canonical_item = 'ayam'", answer="Two chicken orders: 40kg and 12.5kg.")
        res = ds.run("chicken orders sek 7 this week", complete=ai, run_sql=run_sql)
        self.assertTrue(res["ok"])
        self.assertEqual(res["row_count"], 2)
        self.assertTrue(ran[0].endswith("LIMIT 200"))
        self.assertTrue(res["text"].startswith("Two chicken orders: 40kg and 12.5kg."))
        self.assertIn("SQL: SELECT order_for", res["text"])
        row = ds.log_row(res, chat_id=-1, user_id=5)
        self.assertEqual((row["question"], row["row_count"], row["ok"]),
                         ("chicken orders sek 7 this week", 2, True))
        self.assertTrue(row["sql"].startswith("SELECT"))

    def test_rejected_and_failing_queries_never_reach_the_db(self):
        ran = []
        res = ds.run("delete orders", complete=_ai("DELETE FROM staff_order_items"),
                     run_sql=lambda sql: ran.append(sql))
        self.assertFalse(res["ok"])
        self.assertEqual(ran, [])
        self.assertIn("rejected: not a SELECT", res["text"])
        self.assertEqual(ds.log_row(res, chat_id=-1, user_id=5)["error"], "rejected: not a SELECT")

        def boom(sql):
            raise RuntimeError("canceling statement due to statement timeout")
        res = ds.run("x", complete=_ai("select * from receipts"), run_sql=boom)
        self.assertFalse(res["ok"])
        self.assertIn("timeout", res["error"])
        self.assertIn("query failed", res["text"].lower())


if __name__ == "__main__":
    unittest.main()
