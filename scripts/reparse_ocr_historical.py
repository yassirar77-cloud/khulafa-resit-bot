#!/usr/bin/env python3
"""PR #29c — Historical OCR re-parse (opt-in batch job).

Selects historical receipts likely to be wrong (low confidence, implausible
total, or out-of-range date), re-runs the PR #29 ``ocr_quality`` heuristics on
their STORED ``raw_text``/``total``/``items`` (NO photo re-OCR), and records
proposed corrections in ``reparse_audit``. It NEVER edits ``receipts`` — that
only happens when the owner runs ``/reparse_apply`` in Telegram.

Idempotent: receipts that already have an applied OR pending audit row are
skipped (also enforced by the table's partial unique index).

Run locally against production:
  SUPABASE_URL=... SUPABASE_KEY=... python scripts/reparse_ocr_historical.py

Optional: set TELEGRAM_BOT_TOKEN and YASSIR_CHAT_ID to also DM the report.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reparse import (  # noqa: E402
    REPARSE_AUDIT_TABLE,
    audit_insert_payload,
    format_report,
    propose_corrections,
    should_reprocess,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reparse_ocr_historical")

RECEIPTS_TABLE = "receipts"
SELECT_COLUMNS = "id, merchant, total, receipt_date, raw_text, confidence, items"
FUTURE_GRACE_DAYS = 7
MIN_PLAUSIBLE_DATE = "2023-01-01"


def _build_client():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def fetch_candidates(client) -> list:
    future_iso = (datetime.now(timezone.utc) + timedelta(days=FUTURE_GRACE_DAYS)).date().isoformat()
    or_filter = ",".join([
        "confidence.lt.80",
        "total.gt.5000",
        f"receipt_date.gt.{future_iso}",
        f"receipt_date.lt.{MIN_PLAUSIBLE_DATE}",
    ])
    resp = (
        client.table(RECEIPTS_TABLE)
        .select(SELECT_COLUMNS)
        .or_(or_filter)
        .order("total", desc=True)
        .order("id", desc=False)
        .execute()
    )
    return resp.data or []


def _audit_receipt_ids(client, applied: bool) -> set:
    resp = (
        client.table(REPARSE_AUDIT_TABLE)
        .select("receipt_id")
        .eq("applied", applied)
        .execute()
    )
    return {r["receipt_id"] for r in (resp.data or []) if r.get("receipt_id") is not None}


def insert_audit_row(client, proposal: dict) -> None:
    client.table(REPARSE_AUDIT_TABLE).insert(audit_insert_payload(proposal)).execute()


def notify_owner(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("YASSIR_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        urllib.request.urlopen(url, data=data, timeout=10)  # noqa: S310
    except Exception:
        logger.warning("Could not DM reparse report to owner", exc_info=True)


def run(client, dry_run: bool = False, limit=None, date_only: bool = False) -> dict:
    candidates = fetch_candidates(client)
    if limit is not None:
        candidates = candidates[:limit]
    applied_ids = _audit_receipt_ids(client, applied=True)
    pending_ids = _audit_receipt_ids(client, applied=False)

    stats = {
        "evaluated": 0, "created": 0, "skipped_empty": 0,
        "already": 0, "no_change": 0, "skipped_total": 0,
        "total_only": 0, "date_only": 0, "both": 0,
    }
    created_rows = []

    for row in candidates:
        stats["evaluated"] += 1
        receipt_id = row.get("id")
        if not should_reprocess(receipt_id, applied_ids, pending_ids):
            stats["already"] += 1
            continue
        proposal = propose_corrections(row)
        if proposal is None:
            stats["skipped_empty"] += 1
            continue
        if not proposal["has_change"]:
            stats["no_change"] += 1
            continue
        # --date-only: historical total corrections are too risky (a stray
        # qty parsed into the item name makes a correct total look 100x off),
        # so only write rows whose sole change is the date.
        if date_only and proposal["correction_type"] in ("total", "total+date"):
            stats["skipped_total"] += 1
            continue
        if not dry_run:
            try:
                insert_audit_row(client, proposal)
            except Exception:
                # Most likely the partial unique index rejecting a concurrent dup.
                logger.warning("Skipping audit insert for receipt %s", receipt_id, exc_info=True)
                stats["already"] += 1
                continue
        pending_ids.add(receipt_id)
        stats["created"] += 1
        created_rows.append(proposal)
        total_c = proposal["old_total"] != proposal["new_total"]
        date_c = bool(proposal["new_date"]) and proposal["new_date"] != proposal["old_date"]
        if total_c and date_c:
            stats["both"] += 1
        elif total_c:
            stats["total_only"] += 1
        elif date_c:
            stats["date_only"] += 1

    top_n = 20 if dry_run else 5
    top_rows = sorted(
        created_rows,
        key=lambda r: abs((r.get("old_total") or 0) - (r.get("new_total") or 0)),
        reverse=True,
    )[:top_n]
    report = format_report(stats, top_rows, dry_run=dry_run, date_only=date_only)
    print(report)
    # Dry runs stay local: no audit writes, no owner DM.
    if not dry_run:
        notify_owner(report)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PR #29c historical OCR re-parse. Default: process all "
        "candidates and queue audit rows (never edits receipts).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="evaluate and print the full report (top 20 deltas) without "
        "inserting any audit rows",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="process only the first N candidates from the query",
    )
    parser.add_argument(
        "--date-only", action="store_true",
        help="only queue date corrections; skip total and total+date "
        "corrections entirely (historical total fixes are deferred to PR #29d)",
    )
    args = parser.parse_args()
    client = _build_client()
    run(client, dry_run=args.dry_run, limit=args.limit, date_only=args.date_only)


if __name__ == "__main__":
    main()
