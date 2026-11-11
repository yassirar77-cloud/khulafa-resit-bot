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
        # A total the CODE computed is allowed; one the model made up is not.
        text, problems = ds.answer_line("q", ROWS, _ai(answer="52.5kg in total."))
        self.assertEqual((text, problems), ("52.5kg in total.", []))
        text, problems = ds.answer_line("q", ROWS, _ai(answer="55kg in total."))
        self.assertEqual(text, "2 rows matched.")
        self.assertEqual(problems, ["number 55 not in rows"])
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
        self.assertNotIn("SQL:", res["text"])          # hidden unless asked for
        with mock.patch.dict("os.environ", {"DIRECTOR_QA_SHOW_SQL": "on"}):
            shown = ds.run("chicken orders sek 7 this week", complete=ai, run_sql=run_sql)
        self.assertIn("SQL: SELECT order_for", shown["text"])
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


def _scripted(*replies):
    """A provider that answers the SQL prompt with ``replies`` in order and
    records every payload it was sent."""
    queue, seen = list(replies), []

    def complete(system, user):
        payload = json.loads(user)
        if system is ds.ANSWER_PROMPT:
            seen.append(("answer", payload))
            return {"data": {"answer": "ok"}}
        seen.append(("sql", payload))
        return {"data": queue.pop(0)} if queue else None
    complete.seen = seen
    return complete


class StatsTests(unittest.TestCase):
    def test_totals_are_computed_in_code(self):
        stats = ds.column_stats(ROWS)
        self.assertEqual(stats, {"qty": {"sum": 52.5, "avg": 26.25, "min": 12.5, "max": 40.0}})
        self.assertEqual(ds.check_answer("52.5kg, 26.25 on average.", ROWS, "", stats), [])
        # A thousands separator does not split one figure into two.
        big = [{"net": 5200.5}, {"net": 4100.0}, {"net": 2950.25}]
        self.assertEqual(ds.check_answer("RM 12,250.75 in total.", big, "",
                                         ds.column_stats(big)), [])
        self.assertEqual(ds.check_answer("RM 12,251.75 in total.", big, "",
                                         ds.column_stats(big)), ["12251.75"])

    def test_no_stats_for_one_row_ids_or_a_cut_off_result(self):
        self.assertEqual(ds.column_stats([{"qty": 5}]), {})
        self.assertEqual(ds.column_stats([{"id": 1, "receipt_id": 7, "ok": True},
                                          {"id": 2, "receipt_id": 9, "ok": False}]), {})
        self.assertEqual(ds.column_stats([{"n": i} for i in range(ds.MAX_ROWS)]), {})
        # A column with a non-number in it is not summed.
        self.assertEqual(ds.column_stats([{"v": 1}, {"v": "2"}, {"v": 3}]), {})

    def test_stats_reach_the_answer_prompt(self):
        ai = _scripted()
        ds.answer_line("total?", ROWS, ai)
        self.assertEqual(ai.seen[0][1]["stats"]["qty"]["sum"], 52.5)


class ConversationTests(unittest.TestCase):
    def setUp(self):
        ds.forget()

    def tearDown(self):
        ds.forget()

    def test_follow_up_sees_the_previous_question_and_sql(self):
        ai = _scripted({"sql": "select sum(net_sales) as net from sales_daily_summary"},
                       {"sql": "select sum(net_sales) as net from sales_daily_summary where 1=1"})
        ds.run("sales this month", complete=ai, run_sql=lambda q: [{"net": 10}], chat_id=-1,
               user_id=5, now=1000)
        self.assertNotIn("conversation", ai.seen[0][1])
        ds.run("and last month?", complete=ai, run_sql=lambda q: [{"net": 9}], chat_id=-1,
               user_id=5, now=1060)
        convo = [p for kind, p in ai.seen if kind == "sql"][1]["conversation"]
        self.assertEqual(convo[0]["question"], "sales this month")
        self.assertIn("sales_daily_summary", convo[0]["sql"])
        # Another chat starts clean, and the memory expires.
        self.assertEqual(ds.recall(-2, now=1060), [])
        self.assertEqual(ds.recall(-1, now=1060 + ds.MEMORY_TTL_S + 1), [])

    def test_memory_keeps_only_the_last_turns(self):
        for i in range(ds.MEMORY_TURNS + 3):
            ds.remember(-1, {"kind": ds.KIND_ANSWER, "question": f"q{i}", "sql": "select 1"},
                        now=1000 + i)
        turns = ds.recall(-1, now=1010)
        self.assertEqual(len(turns), ds.MEMORY_TURNS)
        self.assertEqual(turns[-1]["question"], f"q{ds.MEMORY_TURNS + 2}")
        # Failures and pleasantries are not remembered.
        ds.forget()
        ds.remember(-1, {"kind": ds.KIND_FAILED, "question": "x"}, now=1)
        ds.remember(-1, {"kind": ds.KIND_CHAT, "question": "hi"}, now=1)
        self.assertEqual(ds.recall(-1, now=2), [])

    def test_a_question_back_and_its_reply(self):
        ai = _scripted({"sql": None, "clarify": "Which outlet do you mean?"},
                       {"sql": "select count(*) as n from receipts where outlet = 'SEK7'"})
        res = ds.run("how is it doing", complete=ai, run_sql=lambda q: [], chat_id=-1,
                     user_id=5, now=1000)
        self.assertEqual((res["kind"], res["text"], res["ok"]),
                         (ds.KIND_CLARIFY, "Which outlet do you mean?", False))
        self.assertEqual(ds.log_row(res, chat_id=-1, user_id=5)["error"], "clarify")
        # The reply is a follow-up for the person who was asked, nobody else.
        self.assertTrue(ds.expects_followup(-1, 5, "sek 7", now=1030))
        self.assertFalse(ds.expects_followup(-1, 6, "sek 7", now=1030))
        self.assertFalse(ds.expects_followup(-1, 5, "sek 7", now=1000 + ds.FOLLOWUP_S + 1))
        res = ds.run("sek 7", complete=ai, run_sql=lambda q: [{"n": 3}], chat_id=-1, user_id=5,
                     now=1030)
        self.assertTrue(res["ok"])
        convo = [p for kind, p in ai.seen if kind == "sql"][1]["conversation"]
        self.assertEqual(convo[-1], {"question": "how is it doing",
                                     "you_asked": "Which outlet do you mean?"})

    def test_follow_up_detection(self):
        ds.remember(-1, {"kind": ds.KIND_ANSWER, "question": "sales today", "sql": "select 1"},
                    user_id=5, now=1000)
        for t in ("and last month?", "sek 14 pula", "ikut outlet", "what about klang", "kenapa?"):
            self.assertTrue(ds.expects_followup(-1, 5, t, now=1010), t)
        for t in ("ok noted", "sudah hantar", "thanks", "lorry sampai pukul 3"):
            self.assertFalse(ds.expects_followup(-1, 5, t, now=1010), t)
        self.assertFalse(ds.expects_followup(-9, 5, "and last month?", now=1010))   # no conversation

    def test_show_sql_on_request(self):
        ai = _scripted({"sql": "select count(*) as n from receipts"})
        ds.run("how many bills", complete=ai, run_sql=lambda q: [{"n": 3}], chat_id=-1, now=1000)
        res = ds.run("sql?", complete=ai, run_sql=lambda q: [], chat_id=-1, now=1010)
        self.assertEqual(res["text"], "SQL: select count(*) as n from receipts LIMIT 200")
        self.assertEqual(len([1 for kind, _ in ai.seen if kind == "sql"]), 1)   # no provider call
        self.assertIn("no query", ds.run("sql", complete=ai, run_sql=lambda q: [],
                                         chat_id=-7)["text"])


class RepairTests(unittest.TestCase):
    def setUp(self):
        ds.forget()

    def test_a_failing_query_is_corrected_once(self):
        ran = []

        def run_sql(sql):
            ran.append(sql)
            if "outlet_name" in sql:
                raise RuntimeError('column "outlet_name" does not exist')
            return [{"outlet": "SEK7", "n": 4}]
        ai = _scripted({"sql": "select outlet_name, count(*) as n from receipts group by 1"},
                       {"sql": "select outlet, count(*) as n from receipts group by 1"})
        res = ds.run("bills per outlet", complete=ai, run_sql=run_sql)
        self.assertTrue(res["ok"])
        self.assertTrue(res["retried"])
        self.assertEqual(len(ran), 2)
        failed = [p for kind, p in ai.seen if kind == "sql"][1]["failed_attempt"]
        self.assertIn("outlet_name", failed["sql"])
        self.assertIn("does not exist", failed["error"])
        self.assertIn("select outlet,", res["sql"])

    def test_a_rejected_statement_is_corrected_once_and_never_run(self):
        ran = []
        ai = _scripted({"sql": "with x as (select 1) select * from x"},
                       {"sql": "select 1 as one"})
        res = ds.run("q", complete=ai, run_sql=lambda sql: ran.append(sql) or [{"one": 1}])
        self.assertTrue(res["ok"])
        self.assertEqual(ran, ["select 1 as one LIMIT 200"])
        self.assertEqual([p for kind, p in ai.seen if kind == "sql"][1]["failed_attempt"]["error"],
                         "rejected: not a SELECT")

    def test_the_same_failing_statement_is_not_run_twice(self):
        ran = []

        def boom(sql):
            ran.append(sql)
            raise RuntimeError("statement timeout")
        same = {"sql": "select * from receipts"}
        res = ds.run("q", complete=_scripted(same, dict(same)), run_sql=boom)
        self.assertFalse(res["ok"])
        self.assertEqual(len(ran), 1)
        self.assertIn("narrow it", res["text"])


class AsideTests(unittest.TestCase):
    def setUp(self):
        ds.forget()

    def test_pleasantries_get_one_line_without_figures(self):
        res = ds.run("thanks", complete=_scripted({"sql": None, "chat": "Sama-sama!"}),
                     run_sql=lambda q: [])
        self.assertEqual((res["kind"], res["text"]), (ds.KIND_CHAT, "Sama-sama!"))
        # A pleasantry carrying a figure is replaced by the fixed line.
        res = ds.run("hi", complete=_scripted({"sql": None, "chat": "Hi! Sales are RM 5000 today."}),
                     run_sql=lambda q: [])
        self.assertEqual(res["text"], ds.CAPABILITIES)

    def test_a_question_back_cannot_carry_invented_numbers(self):
        gen = ds.generate("ayam", _scripted({"sql": None, "clarify": "Sek 7 or Sek 14?"}))
        self.assertEqual(gen["clarify"], ds.CLARIFY_FALLBACK)
        gen = ds.generate("ayam sek 7", _scripted({"sql": None, "clarify": "Sek 7 — harga atau order?"}))
        self.assertEqual(gen["clarify"], "Sek 7 — harga atau order?")

    def test_empty_result_says_what_was_looked_for(self):
        ai = _scripted({"sql": "select * from receipts where merchant = 'X'",
                        "note": "bills from X"})
        res = ds.run("bills from X", complete=lambda s, u: (
            None if s is ds.ANSWER_PROMPT else ai(s, u)), run_sql=lambda q: [])
        self.assertEqual(res["text"], "Nothing matched.\n\n(I looked for: bills from X)")

    def test_one_row_answer_has_no_table(self):
        ai = _scripted({"sql": "select count(*) as n from receipts"})
        res = ds.run("how many bills", complete=ai, run_sql=lambda q: [{"n": 3}])
        self.assertEqual(res["text"], "ok")

    def test_malay_question_words_anywhere(self):
        self.assertTrue(ds.looks_like_question("ayam sek 7 minggu ni berapa"))
        self.assertFalse(ds.looks_like_question("lorry dah sampai"))


if __name__ == "__main__":
    unittest.main()
