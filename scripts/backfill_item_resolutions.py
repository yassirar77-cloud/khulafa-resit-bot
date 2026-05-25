#!/usr/bin/env python3
"""PR #32b — Item resolution backfill.

Walks every receipt's ``items`` jsonb array, resolves each item name through
the PR #32 ``resolve_item`` matcher, and records one ``item_resolutions`` row
per (receipt, item index). High-confidence matches (>= 80) get a canonical id;
weaker / no matches are still recorded (canonical NULL, match_tier
'low_confidence'/'none') so coverage is auditable.

Safe to re-run: UNIQUE(receipt_id, item_index) means a second run only picks up
items it hasn't recorded yet. Defaults to --dry-run for safety.

  # sanity check, writes nothing:
  SUPABASE_URL=... SUPABASE_KEY=... \
    python scripts/backfill_item_resolutions.py --dry-run --limit 100
  # full dry-run counts:
  python scripts/backfill_item_resolutions.py --dry-run
  # write for real:
  python scripts/backfill_item_resolutions.py --apply
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backfill_items import format_run_report, run_item_backfill  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_item_resolutions")


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PR #32b item resolution backfill. Default: dry-run "
        "(counts only, writes nothing). Pass --apply to write item_resolutions.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="evaluate and print counts without writing (default behaviour)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write item_resolutions rows",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="process only the first N receipts",
    )
    args = parser.parse_args()

    # Safety: only write when --apply is explicitly given.
    dry_run = not args.apply

    client = _build_client()
    stats, tier_counts, top_unmatched = run_item_backfill(
        client, dry_run=dry_run, limit=args.limit,
    )
    print(format_run_report(stats, tier_counts, top_unmatched, dry_run=dry_run))


if __name__ == "__main__":
    main()
