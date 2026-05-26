"""IMAP fetcher for POS shift-close emails (PR #35).

Polls the master inbox (``khulafa.reports@gmail.com``) over IMAP using a Gmail
App Password — NOT OAuth2 (the App Password is what's available, per the
variance analysis). Finds unread shift-close emails from the POS sender,
pulls the ``.TXT`` attachment, decodes it (UTF-16 → UTF-8 fallback), and
resolves the outlet from the SUBJECT.

The ``Mailbox`` class wraps ``imaplib`` so the ingestion orchestration can drive
it (search → fetch → mark-seen) and so tests can substitute a fake without a
real server. Marking a message ``\\Seen`` is deliberately left to the caller so
it only happens AFTER a message has been successfully ingested.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
from datetime import datetime
from email import policy

from sales_parser import (
    decode_shift_close_bytes,
    extract_outlet_from_subject,
    extract_shift_no_from_subject,
)

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
DEFAULT_SENDER = "myposkhulafa@gmail.com"
DEFAULT_SUBJECT_TOKEN = "SHIFTCLOSE"


def _imap_date(dt: datetime) -> str:
    # IMAP SINCE wants e.g. 26-May-2026.
    return dt.strftime("%d-%b-%Y")


class Mailbox:
    """Thin wrapper over an IMAP connection scoped to one mailbox/folder."""

    def __init__(self, conn, folder: str = "INBOX"):
        self._conn = conn
        self._folder = folder

    @classmethod
    def connect(cls, *, inbox: str | None = None, password: str | None = None,
                host: str = IMAP_HOST, folder: str = "INBOX") -> "Mailbox":
        inbox = inbox or os.environ["GMAIL_INBOX"]
        password = password or os.environ["GMAIL_APP_PASSWORD"]
        conn = imaplib.IMAP4_SSL(host)
        conn.login(inbox, password)
        conn.select(folder)
        return cls(conn, folder)

    def search(self, *, sender: str = DEFAULT_SENDER, subject_token: str = DEFAULT_SUBJECT_TOKEN,
               unseen_only: bool = True, since: datetime | None = None) -> list[bytes]:
        """Return matching message ids. Criteria are ANDed by IMAP: the POS
        sender, a SHIFTCLOSE subject, (optionally) unread only, and an optional
        SINCE date."""
        criteria: list[str] = []
        if unseen_only:
            criteria.append("UNSEEN")
        if sender:
            criteria.append(f'FROM "{sender}"')
        if subject_token:
            criteria.append(f'SUBJECT "{subject_token}"')
        if since is not None:
            criteria.append(f'SINCE "{_imap_date(since)}"')
        typ, data = self._conn.search(None, *criteria)
        if typ != "OK" or not data or data[0] is None:
            return []
        return data[0].split()

    def fetch(self, msg_id):
        """Fetch and parse one message into an ``email.message.Message``."""
        typ, msg_data = self._conn.fetch(msg_id, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"IMAP fetch failed for {msg_id!r}")
        raw = msg_data[0][1]
        return email.message_from_bytes(raw, policy=policy.default)

    def mark_seen(self, msg_id) -> None:
        self._conn.store(msg_id, "+FLAGS", "\\Seen")

    def close(self) -> None:
        for step in (self._conn.close, self._conn.logout):
            try:
                step()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


def _find_txt_attachment(msg):
    """Return (filename, raw_bytes) for the first ``.TXT`` attachment, else
    (None, None)."""
    for part in msg.walk():
        filename = part.get_filename()
        if filename and filename.upper().endswith(".TXT"):
            return filename, part.get_payload(decode=True)
    return None, None


def extract_shift_close(msg) -> dict | None:
    """Turn an email message into a shift-close dict, or ``None`` if it carries
    no ``.TXT`` attachment. The outlet code is taken from the SUBJECT only.

    An empty attachment yields ``content == ""`` (kept, not dropped) so the
    ingestion layer can record it as an error and leave it unread for inspection.
    """
    subject = msg["subject"]
    filename, raw_bytes = _find_txt_attachment(msg)
    if filename is None:
        logger.warning("Shift-close email %r has no .TXT attachment", subject)
        return None
    content = decode_shift_close_bytes(raw_bytes) if raw_bytes else ""
    return {
        "message_id": msg["message-id"],
        "subject": subject,
        "outlet_code": extract_outlet_from_subject(subject),
        "shift_no_from_subject": extract_shift_no_from_subject(subject),
        "filename": filename,
        "content": content,
        "received_at": msg["date"],
    }


def fetch_new_shift_close_emails(since: datetime | None = None, *, mark_seen: bool = True,
                                 mailbox: Mailbox | None = None) -> list[dict]:
    """Convenience one-shot fetch (used outside the ingestion loop / for manual
    pokes). Connects, searches unread shift-close mail, extracts each, and —
    only when ``mark_seen`` — flags them read. The ingestion entry point drives
    the Mailbox directly so it can defer the flag until after a successful store.
    """
    own = mailbox is None
    mailbox = mailbox or Mailbox.connect()
    results: list[dict] = []
    try:
        for msg_id in mailbox.search(since=since):
            try:
                msg = mailbox.fetch(msg_id)
                data = extract_shift_close(msg)
            except Exception:  # noqa: BLE001 - one bad message must not abort the batch
                logger.exception("Failed to read shift-close message %r", msg_id)
                continue
            if data is None:
                continue
            results.append(data)
            if mark_seen:
                mailbox.mark_seen(msg_id)
    finally:
        if own:
            mailbox.close()
    return results
