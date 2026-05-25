#!/usr/bin/env python3
"""PR #31 — Backfill canonical merchants on historical receipts.

Resolves each receipt's stored ``merchant`` text through the PR #30
``resolve_merchant`` matcher and tags ``receipts.merchant_canonical_id`` for
confident (>= 80) matches. Records one ``backfill_audit`` row per receipt so
resolution quality (exact vs fuzzy) and the still-unmatched merchants are
auditable.

Safe to re-run: candidates are only receipts whose ``merchant_canonical_id`` is
still NULL, and ``backfill_audit`` has UNIQUE(receipt_id).

  # sanity check, writes nothing:
  SUPABASE_URL=... SUPABASE_KEY=... \
    python scripts/backfill_canonical_merchants.py --dry-run --limit 50
  # audit only (no receipt mutation):
  python scripts/backfill_canonical_merchants.py
  # tag receipts for real:
  python scripts/backfill_canonical_merchants.py --apply
  # also upgrade receipt_type where the canonical implies a better type:
  python scripts/backfill_canonical_merchants.py --apply --reclassify
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backfill_canonical import format_run_report, run_backfill  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_canonical_merchants")


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PR #31 canonical-merchant backfill. Default: audit only "
        "(writes backfill_audit, does NOT mutate receipts).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="evaluate and print the report without writing anything",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="process only the first N candidate receipts",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="tag receipts.merchant_canonical_id for matches >= 80 confidence",
    )
    parser.add_argument(
        "--reclassify", action="store_true",
        help="with --apply, also upgrade receipts.receipt_type via "
        "classify_receipt fed the canonical merchant (never downgrades)",
    )
    args = parser.parse_args()

    if args.reclassify and not args.apply:
        logger.warning("--reclassify has no effect without --apply; ignoring it.")

    client = _build_client()
    stats, tier_counts, top_unmatched = run_backfill(
        client,
        dry_run=args.dry_run,
        limit=args.limit,
        apply=args.apply,
        reclassify=args.reclassify,
    )
    print(format_run_report(
        stats, tier_counts, top_unmatched,
        dry_run=args.dry_run, apply=args.apply, reclassify=args.reclassify,
    ))


if __name__ == "__main__":
    main()
