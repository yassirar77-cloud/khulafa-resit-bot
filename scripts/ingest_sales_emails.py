#!/usr/bin/env python3
"""PR #35 — POS shift-close email ingestion (CLI entry point).

Thin wrapper over ``sales_ingest.run_ingest_once`` for manual / cron use. The
bot polls the same function in-process every 30 min via APScheduler, so a
separate Render cron job is intentionally NOT created ($0 extra).

  Manual run:
    GMAIL_INBOX=... GMAIL_APP_PASSWORD=... SUPABASE_URL=... SUPABASE_KEY=... \
      python scripts/ingest_sales_emails.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_ingest import run_ingest_once  # noqa: E402

logger = logging.getLogger("ingest_sales_emails")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = run_ingest_once()
    logger.info("Done: %s", summary)


if __name__ == "__main__":
    main()
