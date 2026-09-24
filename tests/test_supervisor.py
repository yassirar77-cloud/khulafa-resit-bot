"""Unit tests for ``supervisor`` — the human-supervisor question layer.

Run with::

    python -m unittest tests.test_supervisor
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervisor import (  # noqa: E402
    REMINDER_TEXT,
    REMINDER_TYPE,
    REPLY_FOOTER,
    close_question,
    format_owner_pending,
    format_owner_reply_note,
    format_reply_ack,
    linked_original_id,
    log_question,
    log_reminder,
    open_questions_between,
    open_questions_since,
    with_reply_footer,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


class WithReplyFooter(unittest.TestCase):

    def test_appends_the_mechanical_tamil_instruction(self):
        out = with_reply_footer("விலை ஏறிடுச்சு!")
        self.assertTrue(out.endswith(REPLY_FOOTER))
        # The gesture is spelled out, not just named.
        self.assertIn("அழுத்தி பிடிங்க", out)
        self.assertIn("'Reply' தட்டுங்க", out)

    def test_idempotent_and_safe_on_empty(self):
        once = with_reply_footer("hello")
        self.assertEqual(with_reply_footer(once), once)
        self.assertEqual(with_reply_footer(""), "")
        self.assertEqual(with_reply_footer(None), "")
        self.assertEqual(with_reply_footer("   "), "")


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, store):
        self.store = store
        self._filters = []

    def insert(self, payload):
        self._payload = payload
        self._op = "insert"
        return self

    def update(self, payload):
        self._payload = payload
        self._op = "update"
        return self

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def limit(self, n):
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def lte(self, col, val):
        self._filters.append(("lte", col, val))
        return self

    def execute(self):
        if getattr(self, "_op", "") == "insert":
            self.store.append(dict(self._payload))
            return FakeResult([self._payload])
        rows = list(self.store)
        for op, col, val in self._filters:
            if op == "is" and val == "null":
                rows = [r for r in rows if r.get(col) is None]
            elif op == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif op == "neq":
                rows = [r for r in rows if r.get(col) != val]
            elif op == "gte":
                rows = [r for r in rows if str(r.get(col) or "") >= val]
            elif op == "lte":
                rows = [r for r in rows if str(r.get(col) or "") <= val]
        if getattr(self, "_op", "") == "update":
            for r in rows:
                r.update(self._payload)
            return FakeResult(rows)
        return FakeResult(rows)


class FakeSupabase:
    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == "audit_responses"
        return FakeQuery(self.rows)


def _question(chat=-100, asked_hours_ago=24, replied=False, qtype="price_spike"):
    from datetime import timedelta
    return {
        "id": 1,
        "chat_id": chat,
        "question_type": qtype,
        "question_text": "விலை ஏறிடுச்சு!\nDetail line",
        "question_message_id": 555,
        "asked_at": (NOW - timedelta(hours=asked_hours_ago)).isoformat(),
        "replied_at": NOW.isoformat() if replied else None,
    }


class LogQuestion(unittest.TestCase):

    def test_logs_the_ledger_row(self):
        fake = FakeSupabase()
        ok = log_question(fake, -100, 555, "price_spike", "விலை ஏறிடுச்சு!", 42)
        self.assertTrue(ok)
        row = fake.rows[0]
        self.assertEqual(row["chat_id"], -100)
        self.assertEqual(row["question_message_id"], 555)
        self.assertEqual(row["question_type"], "price_spike")
        self.assertEqual(row["receipt_id"], 42)

    def test_never_raises(self):
        class Boom:
            def table(self, name):
                raise RuntimeError("boom")

        try:
            self.assertFalse(log_question(Boom(), -100, 555, "x", "y"))
            self.assertFalse(log_question(FakeSupabase(), None, 555, "x", "y"))
            self.assertFalse(log_question(FakeSupabase(), -100, None, "x", "y"))
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"log_question raised: {e}")


class OpenQuestionWindows(unittest.TestCase):

    def test_reminder_window_takes_only_18_to_42h_unanswered(self):
        fake = FakeSupabase()
        fake.rows = [
            _question(asked_hours_ago=10),               # too fresh
            _question(asked_hours_ago=24),               # due a nudge
            _question(asked_hours_ago=50),               # nudge window passed
            _question(asked_hours_ago=24, replied=True), # answered
            _question(asked_hours_ago=24, qtype=REMINDER_TYPE),  # a nudge row
        ]
        due = open_questions_between(fake, now=NOW)
        self.assertEqual(len(due), 1)

    def test_nightly_2100_question_gets_exactly_one_nudge_at_1700(self):
        # Missing-bill questions go out 21:00, the reminder runs 17:00 —
        # age 20 h on day one (inside [18, 42]), 44 h on day two (outside).
        fake = FakeSupabase()
        fake.rows = [_question(asked_hours_ago=20)]
        self.assertEqual(len(open_questions_between(fake, now=NOW)), 1)
        fake.rows = [_question(asked_hours_ago=44)]
        self.assertEqual(len(open_questions_between(fake, now=NOW)), 0)

    def test_pending_overview_takes_all_recent_unanswered(self):
        fake = FakeSupabase()
        fake.rows = [
            _question(asked_hours_ago=10),
            _question(asked_hours_ago=50),
            _question(asked_hours_ago=100, qtype="overbuy"),  # > 3 days old
            _question(asked_hours_ago=24, replied=True),
        ]
        fake.rows.append(_question(asked_hours_ago=30, qtype=REMINDER_TYPE))
        pending = open_questions_since(fake, now=NOW)
        self.assertEqual(len(pending), 2)  # nudge rows are not questions

    def test_read_failure_returns_empty(self):
        class Boom:
            def table(self, name):
                raise RuntimeError("boom")

        self.assertEqual(open_questions_between(Boom(), now=NOW), [])
        self.assertEqual(open_questions_since(Boom(), now=NOW), [])


class ReminderLinking(unittest.TestCase):

    def test_nudge_row_links_back_and_reply_closes_the_original(self):
        fake = FakeSupabase()
        fake.rows = [dict(_question(), id=7)]
        ok = log_reminder(fake, -100, 999, fake.rows[0])
        self.assertTrue(ok)
        nudge = fake.rows[1]
        self.assertEqual(nudge["question_type"], REMINDER_TYPE)
        self.assertEqual(nudge["question_message_id"], 999)
        self.assertEqual(linked_original_id(nudge), 7)
        # Closing the original by that id returns the real question row.
        original = close_question(fake, 7, "supplier problem", NOW.isoformat())
        self.assertEqual(original["id"], 7)
        self.assertEqual(fake.rows[0]["manager_reply"], "supplier problem")
        self.assertIsNotNone(fake.rows[0]["replied_at"])

    def test_ordinary_questions_do_not_link(self):
        self.assertIsNone(linked_original_id(_question()))
        self.assertIsNone(linked_original_id(None))
        self.assertIsNone(linked_original_id(
            {"question_type": REMINDER_TYPE, "question_text": "no prefix"}
        ))

    def test_log_reminder_never_raises(self):
        class Boom:
            def table(self, name):
                raise RuntimeError("boom")

        try:
            self.assertFalse(log_reminder(Boom(), -100, 999, _question()))
            self.assertFalse(log_reminder(FakeSupabase(), None, 999, _question()))
            self.assertFalse(log_reminder(FakeSupabase(), -100, 999, {}))
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"log_reminder raised: {e}")


class OverviewCap(unittest.TestCase):

    def test_long_backlog_is_capped_under_telegram_limit(self):
        rows = [dict(_question(chat=-(100 + i)), id=i) for i in range(60)]
        out = format_owner_pending(rows)
        self.assertLess(len(out), 4096)
        self.assertIn("…and", out)
        self.assertIn("/questions_now", out)


class HumanTouches(unittest.TestCase):

    def test_ack_is_tamil_first_then_malay(self):
        ack = format_reply_ack()
        self.assertIn("பதில் note பண்ணிட்டேன்", ack)
        self.assertIn("Terima kasih", ack)

    def test_reminder_is_simple_and_replyable_itself(self):
        self.assertIn("இன்னும் பதில் வரல", REMINDER_TEXT)
        # It invites a reply to ITSELF (the natural tap target).
        self.assertIn("இந்த message-அ அழுத்தி பிடிங்க", REMINDER_TEXT)

    def test_owner_reply_note_reports_q_and_a(self):
        note = format_owner_reply_note(_question(), "Supplier said flood in Kedah")
        self.assertIn("💬 Reply received (price_spike, chat -100)", note)
        self.assertIn("Q: விலை ஏறிடுச்சு!", note)
        self.assertIn("A: Supplier said flood in Kedah", note)
        self.assertNotIn("Detail line", note)  # first line only

    def test_owner_pending_lists_and_empty_is_silent(self):
        out = format_owner_pending([_question(), _question(chat=-200, qtype="overbuy")])
        self.assertIn("chat -100 — price_spike", out)
        self.assertIn("chat -200 — overbuy", out)
        self.assertEqual(format_owner_pending([]), "")

    def test_formatters_never_raise(self):
        try:
            self.assertEqual(format_owner_reply_note(None, "x"), "")
            self.assertEqual(format_owner_pending(["junk"]), "")
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"formatters raised: {e}")


if __name__ == "__main__":
    unittest.main()
