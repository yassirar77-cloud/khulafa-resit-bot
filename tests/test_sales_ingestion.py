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
from sales_ingest import _canonical_code, build_sales_record, process_email, run  # noqa: E402
from sales_parser import OUTLET_CANONICAL_BY_CODE, parse_shift_close, read_shift_close_file  # noqa: E402
from tests.sales_fixtures import path_for_code  # noqa: E402
from tests.sales_daily_fixtures import path_for_code as d_path_for_code  # noqa: E402


_INACTIVE = {"S-ST KHU", "S-MB", "S-RAZAK"}
_UNCONFIRMED = {"S-SBESI", "S-ST KHU", "S-MB", "S-RAZAK"}


def _default_outlets():
    """Registry mirroring the 0015/0016 seed: all real outlets active+confirmed,
    S-ST KHU / S-MB / S-RAZAK inactive (placeholders)."""
    return {
        code.upper(): {
            "canonical_name": name,
            "active": code not in _INACTIVE,
            "confirmed": code not in _UNCONFIRMED,
        }
        for code, name in OUTLET_CANONICAL_BY_CODE.items()
    }


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


def _sek20_d_content():
    return read_shift_close_file(d_path_for_code("D-SEK20"))


class FakeStore:
    def __init__(self, outlets=None):
        self.saved = []
        self.daily = []
        self.logs = []
        self._keys = set()
        self._dkeys = set()
        self._outlets = _default_outlets() if outlets is None else outlets
        self.load_calls = 0

    def load_outlets(self):
        self.load_calls += 1
        return dict(self._outlets)

    def exists(self, *key):
        return key in self._keys

    def save(self, record):
        self.saved.append(record)
        self._keys.add(record["key"])
        return len(self.saved)

    def exists_daily(self, outlet_canonical, business_date):
        return (outlet_canonical, business_date) in self._dkeys

    def save_daily(self, record):
        self.daily.append(record)
        self._dkeys.add(record["key"])
        return len(self.daily)

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

    def test_ingestion_skips_inactive_outlets(self):
        # S-ST KHU is active=false — must be skipped (not inserted) and marked read.
        mailbox = FakeMailbox([(b"1", make_email("S-ST KHU  SHIFTCLOSE (860)", _klang_content()))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped_inactive"], 1)
        self.assertEqual(summary["inserted"], 0)
        self.assertEqual(store.saved, [])
        self.assertIn(b"1", mailbox.seen)  # terminal decision -> mark read
        self.assertTrue(any(e["status"] == "skipped_inactive" for e in store.logs))

    def test_ingestion_skips_unknown_outlets(self):
        # An outlet not in outlet_canonical is skipped and left UNREAD for retry.
        mailbox = FakeMailbox([(b"1", make_email("S-FOO  SHIFTCLOSE (1)", _klang_content()))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped_unknown"], 1)
        self.assertEqual(store.saved, [])
        self.assertNotIn(b"1", mailbox.seen)  # unread so it retries once registered
        self.assertTrue(any(e["status"] == "skipped_unknown" for e in store.logs))

    def test_ingestion_proceeds_with_unconfirmed_active_outlets(self):
        # S-SBESI is active=true, confirmed=false (placeholder name OK) -> ingest.
        mailbox = FakeMailbox([(b"1", make_email("S-SBESI  SHIFTCLOSE (2019)", _klang_content()))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0]["parent"]["outlet_canonical"], "SBESI")
        self.assertIn(b"1", mailbox.seen)

    def test_outlet_canonical_loaded_once_per_run(self):
        # The registry is loaded ONCE per run, reused for every email (no N+1).
        emails = [
            (b"1", make_email("S-KLANG  SHIFTCLOSE (1499)", _klang_content(), message_id="<a>")),
            (b"2", make_email("S-SBESI  SHIFTCLOSE (2019)", _klang_content(), message_id="<b>")),
            (b"3", make_email("S-ST KHU  SHIFTCLOSE (860)", _klang_content(), message_id="<c>")),
        ]
        store = FakeStore()
        run(store=store, mailbox=FakeMailbox(emails), now_my=NOW)
        self.assertEqual(store.load_calls, 1)

    def test_build_record_strips_null_bytes_before_insert(self):
        # Defensive backstop: even if raw content carries NUL bytes, no string
        # in the record (parent or children) reaches Postgres with U+0000.
        content = _klang_content()
        email_dict = {
            "subject": "S-KLANG SHIFTCLOSE (1499)",
            "outlet_code": "S-KLANG",
            "content": content + "\x00\x00trailing\x00",
            "message_id": "<n>",
            "filename": "x.TXT",
            "received_at": "Tue, 26 May 2026 19:30:00 +0800",
        }
        record = build_sales_record(email_dict, parse_shift_close(content), "Klang B.Emas", NOW)
        self.assertNotIn("\x00", record["parent"]["raw_content"])
        self.assertNotIn("\x00", repr(record))

    def test_handles_empty_attachment(self):
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)", "", empty=True))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(len(store.saved), 0)
        self.assertNotIn(b"1", mailbox.seen)
        self.assertTrue(any(e["detail"] == "empty_attachment" for e in store.logs))


class DailyRoutingTests(unittest.TestCase):
    """PR #60: D-files route to the daily-summary parser/table; S-files unchanged."""

    def test_d_file_routes_to_d_parser(self):
        mailbox = FakeMailbox([(b"1", make_email("D-SEK20 ON 26/May/2026 00:09:19", _sek20_d_content()))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(len(store.daily), 1)
        self.assertEqual(len(store.saved), 0)  # NOT written to the S-file table
        self.assertAlmostEqual(store.daily[0]["parent"]["day_sales"], 8246.10, places=2)
        self.assertEqual(store.daily[0]["parent"]["outlet_canonical"], "SEK-20")
        self.assertIn(b"1", mailbox.seen)

    def test_s_file_routes_to_s_parser(self):
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)", _klang_content()))])
        store = FakeStore()
        run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(len(store.daily), 0)  # NOT written to the D-file table

    def test_outlet_prefix_stripped_for_canonical_lookup(self):
        self.assertEqual(_canonical_code("D-SEK20"), "S-SEK20")
        self.assertEqual(_canonical_code("S-SEK20"), "S-SEK20")
        mailbox = FakeMailbox([(b"1", make_email("D-SEK20 ON 26/May/2026 00:09:19", _sek20_d_content()))])
        store = FakeStore()
        run(store=store, mailbox=mailbox, now_my=NOW)
        # D-SEK20 resolved through S-SEK20 -> canonical "SEK-20".
        self.assertEqual(store.daily[0]["parent"]["outlet_canonical"], "SEK-20")

    def test_d_file_inactive_outlet_skipped(self):
        # D-ST KHU resolves to S-ST KHU (active=false) -> skipped, not parsed.
        mailbox = FakeMailbox([(b"1", make_email("D-ST KHU ON 26/May/2026 00:09:19", _sek20_d_content()))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped_inactive"], 1)
        self.assertEqual(len(store.daily), 0)
        self.assertIn(b"1", mailbox.seen)


if __name__ == "__main__":
    unittest.main()
