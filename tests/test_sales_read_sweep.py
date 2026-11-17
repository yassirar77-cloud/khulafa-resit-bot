"""Read-mail recovery sweep: POS emails a person opened (marked read) before
the poll saw them must still be ingested — the UNSEEN poll alone loses them.

Run with::

    python -m unittest tests.test_sales_read_sweep
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_email_fetcher import Mailbox  # noqa: E402
from sales_ingest import _is_final_log_row, recover_read_mail  # noqa: E402
from tests.test_sales_ingestion import (  # noqa: E402
    FakeStore, _klang_content, make_email,
)

NOW = datetime(2026, 5, 26, 20, 0, 0)
SUBJECT = "S-KLANG  SHIFTCLOSE (1842) ON 26/May/2026 19:00:03"


class SweepMailbox:
    """Read mail only: ``messages`` maps uid -> EmailMessage."""

    def __init__(self, messages):
        self._messages = dict(messages)
        self.search_kwargs = None
        self.fetched = []
        self.seen = []

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        return list(self._messages.keys())

    def fetch_message_ids(self, uids):
        return {u: self._messages[u]["message-id"] for u in uids
                if self._messages[u]["message-id"]}

    def fetch(self, uid):
        self.fetched.append(uid)
        return self._messages[uid]

    def mark_seen(self, uid):
        self.seen.append(uid)


class SweepStore(FakeStore):
    def __init__(self, handled=(), **kwargs):
        super().__init__(**kwargs)
        self._handled = set(handled)

    def load_handled_message_ids(self, since_iso):
        self.handled_since = since_iso
        # Whatever this run logged with a final status is handled next time.
        return self._handled | {
            e["source_message_id"] for e in self.logs
            if _is_final_log_row(e["status"], e["detail"])
        }


class ReadSweepTests(unittest.TestCase):
    def test_read_but_never_ingested_email_is_recovered(self):
        mailbox = SweepMailbox({
            b"7": make_email(SUBJECT, _klang_content(), message_id="<lost>"),
        })
        store = SweepStore()
        summary = recover_read_mail(store=store, mailbox=mailbox, now_my=NOW, days=7)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0]["parent"]["source_message_id"], "<lost>")
        # Searched READ mail from the sender, any subject, bounded by a date.
        self.assertTrue(mailbox.search_kwargs["seen_only"])
        self.assertIsNone(mailbox.search_kwargs["subject_token"])
        self.assertEqual(mailbox.search_kwargs["since"].date().isoformat(), "2026-05-19")

    def test_recovered_insert_is_tagged_in_the_log(self):
        mailbox = SweepMailbox({
            b"7": make_email(SUBJECT, _klang_content(), message_id="<lost>"),
        })
        store = SweepStore()
        summary = recover_read_mail(store=store, mailbox=mailbox, now_my=NOW, days=7)
        self.assertEqual(store.logs[-1]["status"], "inserted")
        self.assertEqual(store.logs[-1]["detail"], "recovered_read_mail")
        self.assertEqual(summary["inserted_subjects"], [SUBJECT])

    def test_already_handled_mail_is_not_downloaded(self):
        mailbox = SweepMailbox({
            b"7": make_email(SUBJECT, _klang_content(), message_id="<done>"),
            b"8": make_email(SUBJECT, _klang_content(), message_id="<lost>"),
        })
        store = SweepStore(handled={"<done>"})
        summary = recover_read_mail(store=store, mailbox=mailbox, now_my=NOW, days=7)
        self.assertEqual(mailbox.fetched, [b"8"])
        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["fetched"], 1)

    def test_second_sweep_does_nothing(self):
        mailbox = SweepMailbox({
            b"7": make_email(SUBJECT, _klang_content(), message_id="<lost>"),
        })
        store = SweepStore()
        recover_read_mail(store=store, mailbox=mailbox, now_my=NOW, days=7)
        mailbox.fetched.clear()
        again = recover_read_mail(store=store, mailbox=mailbox, now_my=NOW, days=7)
        self.assertEqual(mailbox.fetched, [])
        self.assertEqual(again["fetched"], 0)
        self.assertEqual(len(store.saved), 1)

    def test_mail_without_message_id_is_left_alone(self):
        msg = make_email(SUBJECT, _klang_content())
        del msg["Message-ID"]
        mailbox = SweepMailbox({b"7": msg})
        summary = recover_read_mail(store=SweepStore(), mailbox=mailbox, now_my=NOW, days=7)
        self.assertEqual(mailbox.fetched, [])
        self.assertEqual(summary["fetched"], 0)

    def test_zero_days_disables_the_sweep(self):
        mailbox = SweepMailbox({b"7": make_email(SUBJECT, _klang_content())})
        summary = recover_read_mail(store=SweepStore(), mailbox=mailbox, now_my=NOW, days=0)
        self.assertIsNone(mailbox.search_kwargs)
        self.assertEqual(summary["fetched"], 0)

    def test_final_log_rows(self):
        for status in ("inserted", "skipped", "skipped_inactive", "skipped_report"):
            self.assertTrue(_is_final_log_row(status, None))
        self.assertTrue(_is_final_log_row("error", "dead_letter_parked:no_total_parsed"))
        self.assertTrue(_is_final_log_row("error", "no_txt_attachment"))
        # Retryable outcomes must be looked at again.
        self.assertFalse(_is_final_log_row("error", "insert_not_confirmed"))
        self.assertFalse(_is_final_log_row("error", "exception: boom"))
        self.assertFalse(_is_final_log_row("skipped_unknown", "S-99 not in outlet_canonical"))


class _HeaderConn:
    def __init__(self, data):
        self._data = data
        self.calls = []

    def uid(self, command, *args):
        self.calls.append((command, args))
        return "OK", self._data


class MailboxHeaderTests(unittest.TestCase):
    def test_seen_only_search_uses_seen_not_unseen(self):
        conn = _HeaderConn([b"4 5"])
        Mailbox(conn).search(subject_token=None, seen_only=True)
        _command, criteria = conn.calls[0]
        self.assertIn("SEEN", criteria)
        self.assertNotIn("UNSEEN", criteria)

    def test_fetch_message_ids_parses_uid_and_header(self):
        conn = _HeaderConn([
            (b"1 (UID 41 BODY[HEADER.FIELDS (MESSAGE-ID)] {30}",
             b"Message-ID: <a@pos>\r\n\r\n"), b")",
            # UID after the literal, as some servers send it.
            (b"2 (BODY[HEADER.FIELDS (MESSAGE-ID)] {30}",
             b"Message-ID: <b@pos>\r\n\r\n"), b" UID 42)",
            # No Message-ID header at all.
            (b"3 (UID 43 BODY[HEADER.FIELDS (MESSAGE-ID)] {2}", b"\r\n"), b")",
        ])
        out = Mailbox(conn).fetch_message_ids([b"41", b"42", b"43"])
        self.assertEqual(out, {b"41": "<a@pos>", b"42": "<b@pos>"})
        command, args = conn.calls[0]
        self.assertEqual(command, "FETCH")
        self.assertEqual(args[0], "41,42,43")
        self.assertIn("PEEK", args[1])  # must not touch flags


if __name__ == "__main__":
    unittest.main()
