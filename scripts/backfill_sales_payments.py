#!/usr/bin/env python3
"""PR #36 backfill (step 3): re-populate sales_payments from raw_content.

The first ingestion routed payments wrongly (MOBILE CASH QR transactions were
lost, the flat cash labels were never captured). This re-parses each stored
shift's ``raw_content`` and rewrites its sales_payments rows with the corrected
routing:

  * MOBILE CASH        -> one row per QR transaction (method='qr_pay',
                          transaction_id, transaction_at)
  * flat cash labels   -> aggregate rows (qr_pay_total / cash / cash_on_hand /
                          opening_balance / closing_balance)

Idempotent: a shift's existing sales_payments rows are deleted before the fresh
set is inserted. Run AFTER migrations 0018 (schema) and 0019 (cashdrawer
quarantine).

  Dry run (no writes, just counts):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backfill_sales_payments.py --dry-run
  Apply:
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backfill_sales_payments.py
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_parser import parse_shift_close  # noqa: E402

logger = logging.getLogger("backfill_sales_payments")

SALES_DAILY_TABLE = "sales_daily"
SALES_PAYMENTS_TABLE = "sales_payments"


def payment_rows(sales_daily_id, parsed) -> list:
    """Map a parsed report's payments to sales_payments insert rows."""
    rows = []
    for p in parsed.get("payments", []):
        ta = p.get("transaction_at")
        rows.append({
            "sales_daily_id": sales_daily_id,
            "method": p["method"],
            "amount": p["amount"],
            "transaction_id": p.get("transaction_id"),
            "transaction_at": ta.isoformat() if ta is not None else None,
        })
    return rows


def backfill(client, *, dry_run=False) -> dict:
    resp = client.table(SALES_DAILY_TABLE).select("id, raw_content").execute()
    daily = resp.data or []
    stats = {"shifts": 0, "payments": 0, "skipped_no_raw": 0}
    for row in daily:
        sid = row["id"]
        raw = row.get("raw_content")
        if not raw:
            stats["skipped_no_raw"] += 1
            continue
        payload = payment_rows(sid, parse_shift_close(raw))
        stats["shifts"] += 1
        stats["payments"] += len(payload)
        if dry_run:
            continue
        client.table(SALES_PAYMENTS_TABLE).delete().eq("sales_daily_id", sid).execute()
        if payload:
            client.table(SALES_PAYMENTS_TABLE).insert(payload).execute()
    return stats


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="PR #36 sales_payments backfill.")
    parser.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = parser.parse_args()
    stats = backfill(_build_client(), dry_run=args.dry_run)
    logger.info("Backfill done (dry_run=%s): %s", args.dry_run, stats)


if __name__ == "__main__":
    main()
