#!/usr/bin/env python3
"""READ-ONLY diagnostic: why do some S-file POS emails fail `no_total_parsed`?

A shift-close email that parses with no TODAY SALES total is logged to
``sales_ingest_log`` as ``error / no_total_parsed`` and is NOT saved to
``sales_daily`` — so there is no ``raw_content`` in the DB for it. The email is
left UNREAD in the inbox for automatic retry. This script re-reads those emails
straight from the inbox (using ``BODY.PEEK`` — it NEVER marks them seen, so the
retry queue is untouched), decodes each the exact way ingestion does, runs the
shipped ``parse_shift_close``, and shows what the total extractor saw.

For every matching email it prints: the subject, the decoded attachment size,
the parsed ``total_sales`` / ``net_sales``, and the candidate summary lines
(anything that looks like ``LABEL : value`` or contains SALES/TOTAL/CASH/TAX) so
a format variant — a renamed label, a missing colon, a wrapped amount, an
aborted/zero-sales report — is visible at a glance. When the total is missing it
also dumps the first ~50 decoded lines verbatim.

Nothing is written and nothing is marked read. Needs the same inbox creds the
bot uses (GMAIL_INBOX, GMAIL_APP_PASSWORD).

Run on the Render shell::

    python scripts/diagnose_sales_parse.py                       # SEK15, last 30 days
    python scripts/diagnose_sales_parse.py --outlet SEK15 --since 2026-05-28
    python scripts/diagnose_sales_parse.py --outlet SEK15 --only-failures
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sales_parser as sp  # noqa: E402
from sales_email_fetcher import Mailbox, detect_email_type, extract_shift_close  # noqa: E402

_SUMMARY_HINT = ("SALES", "TOTAL", "CASH", "TAX", "NET", "RECEIV")


def _summary_lines(content: str) -> list[str]:
    out = []
    for ln in content.split("\n"):
        u = ln.upper()
        if ":" in ln and any(h in u for h in _SUMMARY_HINT):
            out.append(ln.rstrip())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outlet", default="SEK15",
                    help="match this token in the subject (e.g. SEK15)")
    ap.add_argument("--since", default=None,
                    help="IMAP SINCE date YYYY-MM-DD (default: 30 days ago)")
    ap.add_argument("--limit", type=int, default=25, help="max emails to inspect")
    ap.add_argument("--only-failures", action="store_true",
                    help="only show emails whose total_sales parses to None")
    args = ap.parse_args()

    since = (
        datetime.fromisoformat(args.since)
        if args.since
        else datetime.now(timezone.utc) - timedelta(days=30)
    )

    mbox = Mailbox.connect()
    try:
        # ALL SHIFTCLOSE from the POS sender since the date (seen + unseen) so we
        # can compare a failing email against neighbouring good ones.
        ids = mbox.search(unseen_only=False, since=since)
        token = args.outlet.upper().replace(" ", "")
        shown = 0
        for msg_id in ids:
            if shown >= args.limit:
                break
            msg = mbox.fetch(msg_id)  # BODY.PEEK — does NOT mark \Seen
            subject = str(msg["subject"] or "")
            det = detect_email_type(subject)
            if not det or det[0] != "S":
                continue
            if token not in subject.upper().replace(" ", ""):
                continue
            info = extract_shift_close(msg)
            content = (info or {}).get("content") or ""
            parsed = sp.parse_shift_close(content) if content else {}
            total = parsed.get("total_sales")
            if args.only_failures and total is not None:
                continue
            shown += 1
            print("=" * 72)
            print(f"SUBJECT : {subject}")
            print(f"FILE    : {(info or {}).get('filename')}  "
                  f"({len(content)} chars decoded)")
            print(f"PARSED  : total_sales={total}  net_sales={parsed.get('net_sales')}  "
                  f"close={parsed.get('close_time')}  shift_no={parsed.get('shift_no')}")
            sl = _summary_lines(content)
            print(f"SUMMARY-LIKE LINES ({len(sl)}):")
            for ln in sl[:15]:
                print(f"   {ln!r}")
            if total is None:
                print("   !! total_sales is None — first 50 decoded lines:")
                for ln in content.split("\n")[:50]:
                    print(f"      {ln!r}")
        print("=" * 72)
        print(f"Inspected {shown} '{args.outlet}' S-file(s). "
              "Nothing was marked read; the inbox retry queue is untouched.")
    finally:
        mbox.close()


if __name__ == "__main__":
    main()
