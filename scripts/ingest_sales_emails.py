#!/usr/bin/env python3
"""PR #35 — POS shift-close email ingestion (CLI entry point).

Thin wrapper over ``sales_ingest.run_ingest_once`` for manual / cron use. The
bot polls the same function in-process every 30 min via APScheduler, so a
separate Render cron job is intentionally NOT created ($0 extra).

  Manual run:
    GMAIL_INBOX=... GMAIL_APP_PASSWORD=... SUPABASE_URL=... SUPABASE_KEY=... \
      python scripts/ingest_sales_emails.py

  Recovery sweep — also re-scan ALREADY-READ mail (e.g. the 24h outlets'
  morning daily-close emails that were wrongly skipped as duplicates and
  marked read before migration 0038; message-id dedup keeps stored ones
  idempotent):
    python scripts/ingest_sales_emails.py --include-seen --since 2026-08-01
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_ingest import run_ingest_once  # noqa: E402

logger = logging.getLogger("ingest_sales_emails")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest POS shift-close/daily emails.")
    parser.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                        help="only consider mail received on/after this date")
    parser.add_argument("--include-seen", action="store_true",
                        help="re-scan already-read mail too (recovery sweep; "
                             "pair with --since to bound the range)")
    args = parser.parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    summary = run_ingest_once(since=since, unseen_only=not args.include_seen)
    logger.info("Done: %s", summary)


if __name__ == "__main__":
    main()
