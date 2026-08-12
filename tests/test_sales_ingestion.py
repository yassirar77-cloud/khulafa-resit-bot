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
from sales_ingest import (  # noqa: E402
    DEAD_LETTER_THRESHOLD,
    _canonical_code,
    build_sales_record,
    process_email,
    run,
)
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
    def __init__(self, outlets=None, *, error_counts=None, drop_saves=False):
        self.saved = []
        self.daily = []
        self.logs = []
        self._keys = set()
        self._dkeys = set()
        self._mids = set()        # source_message_id in sales_daily
        self._dmids = set()       # source_message_id in sales_daily_summary
        self._error_counts = dict(error_counts or {})
        # When True, save()/save_daily() silently fail to land the row — models
        # an insert that the destination read-back can't confirm (Part 2).
        self._drop_saves = drop_saves
        self._outlets = _default_outlets() if outlets is None else outlets
        self.load_calls = 0

    def load_outlets(self):
        self.load_calls += 1
        return dict(self._outlets)

    def load_error_counts(self):
        return dict(self._error_counts)

    def exists(self, *key):
        return key in self._keys

    def exists_message_id(self, message_id):
        return message_id in self._mids

    def save(self, record):
        if self._drop_saves:
            return 0
        self.saved.append(record)
        self._keys.add(record["key"])
        mid = record["parent"].get("source_message_id")
        if mid:
            self._mids.add(mid)
        return len(self.saved)

    def exists_daily(self, outlet_canonical, business_date, printed_at=None):
        return (outlet_canonical, business_date, printed_at) in self._dkeys

    def exists_daily_message_id(self, message_id):
        return message_id in self._dmids

    def save_daily(self, record):
        if self._drop_saves:
            return 0
        self.daily.append(record)
        self._dkeys.add(record["key"])
        mid = record["parent"].get("source_message_id")
        if mid:
            self._dmids.add(mid)
        return len(self.daily)

    def log(self, entry):
        self.logs.append(entry)


class FakeConn:
    def __init__(self, result=b"1 2"):
        self.uid_args = None
        self._result = result

    def uid(self, command, *args):
        self.uid_args = (command, args)
        return "OK", [self._result]


class ImapSearchTests(unittest.TestCase):
    def test_imap_search_returns_only_shiftclose_emails(self):
        conn = FakeConn(b"1 2")
        mailbox = Mailbox(conn)
        ids = mailbox.search()
        self.assertEqual(ids, [b"1", b"2"])
        command, criteria = conn.uid_args
        # UID SEARCH, not sequence-number SEARCH: sequence numbers shift when
        # another client expunges from the shared inbox mid-batch.
        self.assertEqual(command, "SEARCH")
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


class SelfHealingTests(unittest.TestCase):
    """PR #65: mark-read-only-after-confirmed-store + dedup against the
    destination + dead-letter cap. Four coupled guarantees:
      1. reading an email never sets \\Seen (BODY.PEEK[]);
      2. \\Seen is set only after the row is confirmed in the destination;
      3. dedup checks the DESTINATION by source_message_id, not the ingest log;
      4. a message that fails 3+ times is dead-lettered (no log spam, unread).
    """

    # --- Part 1: BODY.PEEK[] (reading must not set \Seen) --------------------

    def test_fetch_uses_body_peek_not_rfc822(self):
        class FetchConn:
            def __init__(self, raw):
                self._raw = raw
                self.fetch_args = None

            def uid(self, command, msg_id, spec):
                assert command == "FETCH"
                self.fetch_args = (msg_id, spec)
                return "OK", [(b"1 (BODY[] {N}", self._raw)]

        raw = make_email("S-KLANG SHIFTCLOSE (1499)", _klang_content()).as_bytes()
        conn = FetchConn(raw)
        Mailbox(conn).fetch(b"1")
        # PEEK reads the body without the server flagging it \Seen.
        self.assertIn("BODY.PEEK[]", conn.fetch_args[1])
        self.assertNotIn("RFC822", conn.fetch_args[1])

    # --- Part 2: confirm row in destination, THEN mark read ------------------

    def test_marks_seen_only_after_confirmed_store(self):
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)", _klang_content()))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 1)
        self.assertIn(b"1", mailbox.seen)  # confirmed in destination -> seen

    def test_unconfirmed_store_left_unread(self):
        # save() silently fails to land the row; the destination read-back can't
        # confirm it, so it is recorded as an error and left UNREAD for retry.
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)", _klang_content()))])
        store = FakeStore(drop_saves=True)
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 0)
        self.assertEqual(summary["errors"], 1)
        self.assertNotIn(b"1", mailbox.seen)
        self.assertTrue(any(e["detail"] == "insert_not_confirmed" for e in store.logs))

    # --- Part 3: dedup against the DESTINATION by source_message_id ----------

    def test_dedup_against_destination_message_id(self):
        # The row for <dup> already exists in the destination -> skip, no resave.
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)",
                                                  _klang_content(), message_id="<dup>"))])
        store = FakeStore()
        store._mids.add("<dup>")
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(store.saved, [])
        self.assertIn(b"1", mailbox.seen)

    def test_logged_but_missing_reprocesses(self):
        # <lost> has prior error logs (seen before) but is NOT in the
        # destination -> dedup must NOT trust the log; reprocess and insert.
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)",
                                                  _klang_content(), message_id="<lost>"))])
        store = FakeStore(error_counts={"<lost>": 1})
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(len(store.saved), 1)
        self.assertIn(b"1", mailbox.seen)

    def test_d_file_dedup_against_destination_message_id(self):
        mailbox = FakeMailbox([(b"1", make_email("D-SEK20 ON 26/May/2026 00:09:19",
                                                  _sek20_d_content(), message_id="<d-dup>"))])
        store = FakeStore()
        store._dmids.add("<d-dup>")
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(store.daily, [])

    # --- Part 4: dead-letter after DEAD_LETTER_THRESHOLD failures ------------

    def test_dead_letter_caps_log_spam(self):
        # <stuck> has already failed the threshold number of times: a fresh
        # failure is NOT re-logged (no spam) and is counted as a dead letter,
        # still left UNREAD.
        bad = make_email("S-JAKEL SHIFTCLOSE (1)", "garbage with no sales total",
                         message_id="<stuck>")
        mailbox = FakeMailbox([(b"1", bad)])
        store = FakeStore(error_counts={"<stuck>": DEAD_LETTER_THRESHOLD})
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["dead_letter"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(store.logs, [])  # suppressed — no per-poll spam
        self.assertNotIn(b"1", mailbox.seen)

    def test_dead_letters_surface_their_subjects(self):
        # A bare dead_letter COUNT hides which emails keep failing (production:
        # 40/poll, undiagnosable). The summary must name them (capped).
        bad = make_email("S-JAKEL SHIFTCLOSE (1)", "garbage no sales",
                         message_id="<dl-subject>")
        mailbox = FakeMailbox([(b"1", bad)])
        store = FakeStore(error_counts={"<dl-subject>": DEAD_LETTER_THRESHOLD})
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["dead_letter"], 1)
        self.assertEqual(
            summary["dead_letter_subjects"],
            ["S-JAKEL SHIFTCLOSE (1) [no_total_parsed]"],
        )

    def test_below_threshold_still_logs_error(self):
        bad = make_email("S-JAKEL SHIFTCLOSE (1)", "garbage with no sales total",
                         message_id="<warming>")
        mailbox = FakeMailbox([(b"1", bad)])
        store = FakeStore(error_counts={"<warming>": DEAD_LETTER_THRESHOLD - 1})
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["dead_letter"], 0)
        self.assertTrue(any(e["status"] == "error" for e in store.logs))
        self.assertNotIn(b"1", mailbox.seen)


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


class PartialIngestRepairTests(unittest.TestCase):
    """2026-07 audit: a crash between the parent insert and a child insert
    used to be permanent — the next poll's message-id dedup saw the parent and
    skipped, leaving the summary childless forever. The store now backfills
    empty child tables under the existing parent."""

    def test_repair_daily_children_backfills_missing(self):
        from tests.fake_supabase import FakeSupabase

        from sales_ingest import DAILY_CHILD_TABLES, SupabaseSalesStore

        fake = FakeSupabase()
        fake._store["sales_daily_summary"] = [{"id": 10, "source_message_id": "<d1>"}]
        store = SupabaseSalesStore(fake)
        itemwise = DAILY_CHILD_TABLES[0]
        record = {"children": {itemwise: [{"item_name": "AYAM", "qty": 2}]}}

        repaired = store.repair_daily_children("<d1>", record)
        self.assertEqual(repaired, [itemwise])
        rows = fake.rows(itemwise)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary_id"], 10)
        # Idempotent: children already present -> nothing re-inserted.
        self.assertEqual(store.repair_daily_children("<d1>", record), [])
        self.assertEqual(len(fake.rows(itemwise)), 1)

    def test_repair_children_no_parent_is_noop(self):
        from tests.fake_supabase import FakeSupabase

        from sales_ingest import CHILD_TABLES, SupabaseSalesStore

        store = SupabaseSalesStore(FakeSupabase())
        record = {"children": {CHILD_TABLES[0]: [{"x": 1}]}}
        self.assertEqual(store.repair_children("<missing>", record), [])

    def test_duplicate_with_repair_hook_reports_repaired(self):
        # process-level: duplicate message whose store repairs children is
        # reported as inserted/children_repaired, not silently skipped.
        mailbox = FakeMailbox([(b"1", make_email("S-KLANG SHIFTCLOSE (1499)",
                                                  _klang_content(), message_id="<dup>"))])
        store = FakeStore()
        store._mids.add("<dup>")
        store.repair_children = lambda mid, record: ["sales_items"]
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 1)
        self.assertTrue(any(
            (e.get("detail") or "").startswith("children_repaired") for e in store.logs
        ))
        self.assertIn(b"1", mailbox.seen)


class SkippedUnknownCapTests(unittest.TestCase):
    def test_skipped_unknown_stops_relogging_past_threshold(self):
        # An unregistered outlet's email is refetched every poll (never marked
        # seen); past the threshold it must stop adding log rows.
        from sales_ingest import DEAD_LETTER_THRESHOLD

        email = make_email("S-NEWSHOP SHIFTCLOSE (1)", _klang_content(), message_id="<unk>")
        mailbox = FakeMailbox([(b"1", email)])
        store = FakeStore(error_counts={"<unk>": DEAD_LETTER_THRESHOLD})
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["dead_letter"], 1)
        self.assertEqual(store.logs, [])
        self.assertNotIn(b"1", mailbox.seen)  # still unread -> recoverable


def _mini_d_content(printed, shift_id, sales="3,500.00"):
    """A minimal but parseable D-file for one daily close: header, TOTAL SHIFTS,
    the daily aggregate block, and one SHIFT breakdown block."""
    return "\n".join([
        f"   D-SEK20   ON {printed}",
        "    NASI KANDAR HAJI SHARFUDDIN",
        "    JALAN TEST",
        "    (TOTAL SHIFTS :1)",
        f"DAY SALES        :      {sales}",
        "TAX 6%           :          .00",
        f"NET SALES        :      {sales}",
        f"SHIFT :1 ({shift_id}) ON {printed}",
        f"TODAY SALES      :      {sales}",
    ])


class TwoDailyClosesTests(unittest.TestCase):
    """24h outlets close their POS day TWICE (~19:00 + ~07:00 next morning) and
    email a D-file per close; both fold onto the same business_date and the
    day's sales are their SUM. The second close must land as its own row — it
    was previously skipped as a 'duplicate' (and marked read), silently dropping
    half the day's sales."""

    EVENING = "07/August/2026 19:15:02"       # covers ~7am-7pm of 07 Aug
    MORNING = "08/August/2026 07:00:05"       # covers 7pm 07 Aug - 7am 08 Aug

    def _mailbox(self):
        return FakeMailbox([
            (b"1", make_email(f"D-SEK20 ON {self.EVENING}",
                              _mini_d_content(self.EVENING, 9001, sales="3,500.00"),
                              message_id="<d-eve>")),
            (b"2", make_email(f"D-SEK20 ON {self.MORNING}",
                              _mini_d_content(self.MORNING, 9002, sales="2,100.00"),
                              message_id="<d-morn>")),
        ])

    def test_both_closes_ingest_onto_same_business_date(self):
        store = FakeStore()
        mailbox = self._mailbox()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["inserted"], 2)
        self.assertEqual(len(store.daily), 2)
        dates = {r["parent"]["business_date"] for r in store.daily}
        self.assertEqual(dates, {"2026-08-07"})  # both halves of the SAME day
        self.assertEqual(
            sorted(float(r["parent"]["day_sales"]) for r in store.daily),
            [2100.0, 3500.0],  # sum = the true 24h total
        )
        # Distinct idempotency keys — printed_at separates the two closes.
        keys = {r["key"] for r in store.daily}
        self.assertEqual(len(keys), 2)
        self.assertEqual(mailbox.seen, [b"1", b"2"])

    def test_resend_of_same_close_is_still_a_duplicate(self):
        store = FakeStore()
        run(store=store, mailbox=self._mailbox(), now_my=NOW)
        # The POS re-sends the MORNING close under a NEW message id: same
        # outlet+date+print time -> duplicate, not a third row.
        resend = FakeMailbox([
            (b"9", make_email(f"D-SEK20 ON {self.MORNING}",
                              _mini_d_content(self.MORNING, 9002, sales="2,100.00"),
                              message_id="<d-morn-resend>")),
        ])
        summary = run(store=store, mailbox=resend, now_my=NOW)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(len(store.daily), 2)
        self.assertIn(b"9", resend.seen)

    def test_subject_token_flows_to_the_mailbox_search(self):
        # The targeted recovery sweep (/activate_outlet) narrows the IMAP
        # search to one outlet's subjects.
        class RecordingMailbox(FakeMailbox):
            def __init__(self, messages):
                super().__init__(messages)
                self.search_kwargs = None

            def search(self, **kwargs):
                self.search_kwargs = kwargs
                return super().search(**kwargs)

        mailbox = RecordingMailbox([])
        run(store=FakeStore(), mailbox=mailbox, now_my=NOW,
            unseen_only=False, subject_token="S-44")
        self.assertEqual(mailbox.search_kwargs.get("subject_token"), "S-44")

    def test_seen_sweep_reaches_already_read_mail(self):
        # unseen_only=False must flow to the mailbox search so the recovery
        # sweep can revisit read-but-dropped closes.
        class RecordingMailbox(FakeMailbox):
            def __init__(self, messages):
                super().__init__(messages)
                self.search_kwargs = None

            def search(self, **kwargs):
                self.search_kwargs = kwargs
                return super().search(**kwargs)

        mailbox = RecordingMailbox([])
        run(store=FakeStore(), mailbox=mailbox, now_my=NOW, unseen_only=False)
        self.assertEqual(mailbox.search_kwargs.get("unseen_only"), False)


class Migration0038DetectionTests(unittest.TestCase):
    """Pre-0038 database: the second daily close of a 24h day hits the old
    UNIQUE(outlet, business_date) constraint. That must surface as a NAMED,
    actionable detail (and summary flag) — not loop as a generic error."""

    class UniqueViolatingStore(FakeStore):
        def save_daily(self, record):
            class FakeAPIError(Exception):
                code = "23505"
                message = "duplicate key value violates unique constraint"
            raise FakeAPIError()

    def test_unique_violation_names_migration_0038(self):
        from sales_ingest import MIGRATION_0038_DETAIL

        email = make_email(
            "D-SEK20 ON 08/August/2026 07:00:05",
            _mini_d_content("08/August/2026 07:00:05", 9002),
            message_id="<blocked>",
        )
        mailbox = FakeMailbox([(b"1", email)])
        store = self.UniqueViolatingStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["errors"], 1)
        self.assertTrue(summary["migration_0038_needed"])
        self.assertNotIn(b"1", mailbox.seen)  # unread -> auto-ingests post-fix
        self.assertTrue(any(e.get("detail") == MIGRATION_0038_DETAIL for e in store.logs))

    def test_other_save_errors_still_propagate_as_plain_errors(self):
        class CrashingStore(FakeStore):
            def save_daily(self, record):
                raise RuntimeError("boom")

        email = make_email(
            "D-SEK20 ON 08/August/2026 07:00:05",
            _mini_d_content("08/August/2026 07:00:05", 9002),
            message_id="<boom>",
        )
        mailbox = FakeMailbox([(b"1", email)])
        summary = run(store=CrashingStore(), mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["errors"], 1)
        self.assertFalse(summary["migration_0038_needed"])


class NewOutletPlaceholderTests(unittest.TestCase):
    """A brand-new shop's POS (e.g. S-44) coming online must not spin forever:
    the first unknown email auto-registers an INACTIVE placeholder, the rest of
    its mail is skipped-inactive (marked read), and the summary surfaces the
    new code so the owner is told to /activate_outlet it."""

    class RegisteringStore(FakeStore):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.registered = []

        def register_placeholder_outlet(self, s_code):
            self.registered.append(s_code)
            return True

    def test_unknown_outlet_registers_placeholder_and_batch_goes_quiet(self):
        mailbox = FakeMailbox([
            (b"1", make_email("S-44 SHIFTCLOSE (10)", _klang_content(), message_id="<44a>")),
            (b"2", make_email("S-44 SHIFTCLOSE (11)", _klang_content(), message_id="<44b>")),
        ])
        store = self.RegisteringStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(store.registered, ["S-44"])
        self.assertEqual(summary["new_outlets"], ["S-44"])
        # First email discovered the outlet (left unread); the SECOND already
        # sees the inactive placeholder -> skipped_inactive + marked read, so
        # the refetch/warn loop ends within the same batch.
        self.assertEqual(summary["skipped_unknown"], 1)
        self.assertEqual(summary["skipped_inactive"], 1)
        self.assertEqual(mailbox.seen, [b"2"])

    def test_registration_failure_keeps_old_behaviour(self):
        class FailingStore(FakeStore):
            def register_placeholder_outlet(self, s_code):
                return False

        mailbox = FakeMailbox([
            (b"1", make_email("S-44 SHIFTCLOSE (10)", _klang_content(), message_id="<44a>")),
        ])
        summary = run(store=FailingStore(), mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["new_outlets"], [])
        self.assertEqual(summary["skipped_unknown"], 1)
        self.assertNotIn(b"1", mailbox.seen)  # still unread for retry

    def test_monthly_report_email_is_terminal_skip(self):
        """MONTHLY REPORT emails are a known POS report we don't ingest — they
        must be marked read (terminal) instead of refetching + dead-lettering
        on every poll forever (the production dead_letter:40 loop)."""
        mailbox = FakeMailbox([
            (b"1", make_email("MONTHLY REPORT-SEK15   ON Jun -2026",
                              "monthly report body", message_id="<mr1>")),
        ])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped_report"], 1)
        self.assertEqual(summary["skipped_unknown"], 0)
        self.assertEqual(summary["dead_letter"], 0)
        self.assertEqual(store.saved, [])
        self.assertIn(b"1", mailbox.seen)  # terminal: never refetched again
        self.assertTrue(any(
            e["status"] == "skipped_report"
            and e["detail"] == "monthly_report_not_ingested"
            for e in store.logs
        ))

    def test_monthly_report_past_dead_letter_threshold_still_resolves(self):
        """A monthly email that ALREADY dead-lettered many times must resolve
        on the first poll after the fix — marked seen, not counted dead."""
        mailbox = FakeMailbox([
            (b"1", make_email("MONTHLY REPORT-VISTA   ON Jun -2026",
                              "monthly report body", message_id="<mr2>")),
        ])
        store = FakeStore(error_counts={"<mr2>": DEAD_LETTER_THRESHOLD + 5})
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped_report"], 1)
        self.assertEqual(summary["dead_letter"], 0)
        self.assertIn(b"1", mailbox.seen)

    def test_truly_unrecognised_subject_still_skipped_unknown(self):
        mailbox = FakeMailbox([(b"1", make_email("WEEKLY WHATEVER", "x"))])
        store = FakeStore()
        summary = run(store=store, mailbox=mailbox, now_my=NOW)
        self.assertEqual(summary["skipped_unknown"], 1)
        self.assertEqual(summary.get("skipped_report", 0), 0)
        self.assertNotIn(b"1", mailbox.seen)

    def test_shift_itemwise_child_rows_nasi_kandar_only(self):
        """The S-file's per-dish rows land in sales_shift_itemwise, filtered to
        the nasi kandar proteins (ayam/ikan/kambing/daging dishes only)."""
        email = make_email("S-KLANG  SHIFTCLOSE (1654)", _klang_content())
        from sales_email_fetcher import extract_shift_close

        email_dict = extract_shift_close(email)
        parsed = parse_shift_close(email_dict["content"])
        record = build_sales_record(email_dict, parsed, "Klang B.Emas", NOW)
        rows = record["children"]["sales_shift_itemwise"]
        self.assertTrue(rows)
        names = [r["item_name"].lower() for r in rows]
        self.assertTrue(all(
            any(b in n for b in ("ayam", "ikan", "kambing", "daging"))
            for n in names
        ))
        # untracked dishes (roti/teh/...) are NOT stored
        self.assertFalse(any("roti" in n or "teh " in n for n in names))
        # category comes from the subgroup name
        self.assertTrue(all("category" in r for r in rows))
        self.assertTrue(any(r["category"] for r in rows))

    def test_store_inserts_inactive_unconfirmed_row(self):
        from tests.fake_supabase import FakeSupabase

        from sales_ingest import SupabaseSalesStore

        fake = FakeSupabase()
        store = SupabaseSalesStore(fake)
        self.assertTrue(store.register_placeholder_outlet("S-44"))
        rows = fake.rows("outlet_canonical")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "S-44")
        self.assertEqual(rows[0]["canonical_name"], "44")
        self.assertFalse(rows[0]["active"])
        self.assertFalse(rows[0]["confirmed"])


if __name__ == "__main__":
    unittest.main()
