#!/usr/bin/env python3
"""Backfill sales_shift_itemwise from stored raw_content.

Shifts ingested BEFORE the S-file itemwise parser existed have no
sales_shift_itemwise rows, so the Guna-vs-POS fallback (used when an outlet's
D-file daily summary never arrives) cannot see them. This re-parses each stored
shift's ``raw_content`` and inserts the per-dish nasi kandar rows (ayam / ikan /
kambing / daging dishes only) for every shift that has none yet.

Idempotent: a shift that already has ANY sales_shift_itemwise rows is skipped.
Run AFTER migration 0040 (schema).

  Dry run (no writes, just counts):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backfill_shift_itemwise.py --dry-run
  Apply:
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backfill_shift_itemwise.py
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_parser import is_nasi_kandar_item, parse_shift_close  # noqa: E402

logger = logging.getLogger("backfill_shift_itemwise")

SALES_DAILY_TABLE = "sales_daily"
SHIFT_ITEMWISE_TABLE = "sales_shift_itemwise"
PAGE = 200


def itemwise_rows(sales_daily_id, parsed) -> list:
    return [
        {
            "sales_daily_id": sales_daily_id,
            "category": r.get("category"),
            "item_name": r["name"],
            "qty": r["qty"],
            "amount": r["amount"],
        }
        for r in parsed.get("itemwise", [])
        if is_nasi_kandar_item(r.get("name"))
    ]


def _ids_with_rows(client, ids) -> set:
    if not ids:
        return set()
    resp = (
        client.table(SHIFT_ITEMWISE_TABLE)
        .select("sales_daily_id")
        .in_("sales_daily_id", ids)
        .execute()
    )
    return {r["sales_daily_id"] for r in (resp.data or [])}


def backfill(client, *, dry_run=False) -> dict:
    stats = {"shifts": 0, "inserted_rows": 0, "already_had_rows": 0,
             "skipped_no_raw": 0, "no_itemwise_parsed": 0}
    start = 0
    while True:
        resp = (
            client.table(SALES_DAILY_TABLE)
            .select("id, raw_content")
            .order("id", desc=False)
            .range(start, start + PAGE - 1)
            .execute()
        )
        daily = resp.data or []
        if not daily:
            break
        have = _ids_with_rows(client, [r["id"] for r in daily])
        for row in daily:
            sid = row["id"]
            stats["shifts"] += 1
            if sid in have:
                stats["already_had_rows"] += 1
                continue
            raw = row.get("raw_content")
            if not raw or not str(raw).strip():
                stats["skipped_no_raw"] += 1
                continue
            rows = itemwise_rows(sid, parse_shift_close(raw))
            if not rows:
                stats["no_itemwise_parsed"] += 1
                continue
            if not dry_run:
                client.table(SHIFT_ITEMWISE_TABLE).insert(rows).execute()
            stats["inserted_rows"] += len(rows)
        if len(daily) < PAGE:
            break
        start += PAGE
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="parse + count only, no writes")
    args = ap.parse_args()

    from supabase import create_client

    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    stats = backfill(client, dry_run=args.dry_run)
    logger.info("Backfill %s: %s", "(dry run)" if args.dry_run else "done", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
