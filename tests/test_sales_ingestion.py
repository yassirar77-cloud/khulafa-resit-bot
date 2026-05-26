"""Ingestion-orchestration tests for PR #35.

Covers IMAP search criteria, idempotency, graceful per-message error handling,
and the mark-as-read contract (only after a successful/duplicate ingest).

Run with::

    python -m unittest tests.test_sales_ingestion
"""

import os
import sys
import unittest
from datetime import datetime
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_email_fetcher import Mailbox  # noqa: E402
from sales_ingest import run  # noqa: E402
from sales_parser import read_shift_close_file  # noqa: E402
from tests.sales_fixtures import path_for_code  # noqa: E402


NOW = datetime(2026, 5, 26, 20, 0, 0)


def _klang_content():
    return read_shift_close_file(path_for_code("S-KLANG"))


def make_email(subject, content_str, *, filename="SHIFTCLOSE.TXT", message_id="<m1>", empty=False):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "myposkhulafa@gmail.com"
    msg["Message-ID"] = message_id
    msg["Date"] = "Tue, 26 May 2026 19:30:00 +0800"
    msg.set_content("Shift close report attached.")
    data = b"" if empty else content_str.encode("utf-16")
    msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    return msg


class FakeMailbox:
    def __init__(self, messages):
        self._messages = dict(messages)
        self.seen = []

    def search(self, **kwargs):
        return list(self._messages.keys())

    def fetch(self, msg_id):
        return self._messages[msg_id]

    def mark_seen(self, msg_id):
        self.seen.append(msg_id)


class FakeStore:
    def __init__(self):
        self.saved = []
        self.logs = []
        self._keys = set()

    def exists(self, *key):
        return key in self._keys

    def save(self, record):
        self.saved.append(record)
        self._keys.add(record["key"])
        return len(self.saved)

    def log(self, entry):
        self.logs.append(entry)


class FakeConn:
    def __init__(self, result=b"1 2"):
        self.search_args = None
        self._result = result

    def search(self, charset, *criteria):
        self.search_args = (charset, criteria)
        return "OK", [self._result]


class ImapSearchTests(unittest.TestCase):
    def test_imap_search_returns_only_shiftclose_emails(self):
        conn = FakeConn(b"1 2")
        mailbox = Mailbox(conn)
        ids = mailbox.search()
        self.assertEqual(ids, [b"1", b"2"])
        _, criteria = conn.search_args
        self.assertIn("UNSEEN", criteria)
        self.assertTrue(any(c.startswith("FROM ") for c in criteria))
        self.assertIn('SUBJECT "SHIFTCLOSE"', criteria)


class IngestionRunTests(unittest.TestCase):
    def test_ingestion_idempotent_same_email_twice(self):
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)", _klang_content()))])
        store = FakeStore()
        run(store=store, mailbox=mailbox, now_my=NOW)
        second = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(second["skipped"], 1)

    def test_ingestion_handles_parse_error_gracefully(self):
        good = make_email("S-KLANG SHIFTCLOSE (1499)", _klang_content(), message_id="<good>")
        bad = make_email("S-JAKEL SHIFTCLOSE (1)", "garbage with no sales total", message_id="<bad>")
        mailbox = FakeMailbox([(b"1", good), (b"2", bad)])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(len(store.saved), 1)
        self.assertIn(b"1", mailbox.seen)
        self.assertNotIn(b"2", mailbox.seen)

    def test_marks_email_as_read_after_successful_ingestion(self):
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)", _klang_content()))])
        store = FakeStore()
        run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertIn(b"1", mailbox.seen)

    def test_does_not_mark_read_if_parse_fails(self):
        bad = make_email("S-JAKEL SHIFTCLOSE (1)", "garbage with no sales total")
        mailbox = FakeMailbox([(b"1", bad)])
        store = FakeStore()
        run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertNotIn(b"1", mailbox.seen)

    def test_handles_empty_attachment(self):
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)", "", empty=True))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(len(store.saved), 0)
        self.assertNotIn(b"1", mailbox.seen)
        self.assertTrue(any(e["detail"] == "empty_attachment" for e in store.logs))


if __name__ == "__main__":
    unittest.main()
