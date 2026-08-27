#!/usr/bin/env python3
"""Retro-clean item_prices: move already-poisoned rows into quarantine.

Issue #79's ingestion gate (price_sanity + price_aggregation) stops NEW
garbage from entering ``item_prices``, but rows like receipt 2254's
qty=40250 / RM4,025,000 line are already in the corpus and keep polluting
forecasting, spike detection and averages. This script applies the same
sanity rules to the EXISTING table and moves offenders to
``item_price_quarantine`` (source='retro_clean', original id preserved in
``item_price_id``), then deletes them from ``item_prices``.

Checks applied (see price_sanity.py):
- absolute ceilings on qty / unit_price / line_total;
- future-dated receipt_date;
- non-positive qty/price;
- orders-of-magnitude deviation from the item's corpus-wide median
  (medians computed from in-ceiling rows only, so garbage can't stretch
  its own bound).

The line-total-vs-receipt-total check is ingestion-only: it needs the
parsed receipt total, which this scan does not join.

Idempotent: the unique index on ``item_price_quarantine.item_price_id``
plus upsert-ignore means a re-run re-quarantines nothing, and rows are
deleted from ``item_prices`` only after their quarantine copy is safely
stored.

  Dry run (default — prints every offending row and reasons, writes nothing):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/clean_item_prices.py
  Apply:
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/clean_item_prices.py --apply
"""

import argparse
import logging
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import price_sanity  # noqa: E402
from db_pagination import fetch_all_pages  # noqa: E402

logger = logging.getLogger("clean_item_prices")

ITEM_PRICES_TABLE = "item_prices"
QUARANTINE_TABLE = "item_price_quarantine"
DELETE_CHUNK = 100

_COLUMNS = (
    "id, receipt_id, receipt_date, outlet_code, chat_id, merchant, "
    "canonical_item, raw_item_name, qty, unit_price, line_total"
)


def build_client():
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY must be set")
    return create_client(url, key)


def fetch_corpus(client) -> list:
    return fetch_all_pages(
        lambda: client.table(ITEM_PRICES_TABLE)
        .select(_COLUMNS)
        .order("id", desc=False)
    )


def corpus_history_stats(rows: list) -> dict:
    """Per-canonical-item median stats over the whole corpus, using only
    in-ceiling rows (mirrors price_sanity.fetch_history_stats, but from
    the rows we already hold instead of one query per item)."""
    by_item = defaultdict(list)
    for row in rows:
        item = row.get("canonical_item")
        if isinstance(item, str) and item.strip():
            by_item[item].append(row)
    return {item: price_sanity.median_stats(group) for item, group in by_item.items()}


def find_offenders(rows: list, today=None) -> list:
    """[(row, reasons)] for every corpus row failing the sanity rules."""
    stats = corpus_history_stats(rows)
    offenders = []
    for row in rows:
        reasons = price_sanity.evaluate_record(
            row,
            receipt_date=row.get("receipt_date"),
            receipt_total=None,  # not joined in the retro scan
            history=stats.get(row.get("canonical_item")),
            today=today,
        )
        if reasons:
            offenders.append((row, reasons))
    return offenders


def quarantine_payload(offenders: list) -> list:
    return [
        {
            "receipt_id": row.get("receipt_id"),
            "item_price_id": row.get("id"),
            "receipt_date": row.get("receipt_date"),
            "outlet_code": row.get("outlet_code"),
            "chat_id": row.get("chat_id"),
            "merchant": row.get("merchant"),
            "canonical_item": row.get("canonical_item"),
            "raw_item_name": row.get("raw_item_name"),
            "qty": row.get("qty"),
            "unit_price": row.get("unit_price"),
            "line_total": row.get("line_total"),
            "reasons": ",".join(reasons),
            "source": "retro_clean",
        }
        for row, reasons in offenders
    ]


def apply(client, offenders: list) -> int:
    """Quarantine first, delete only what was safely stored. Returns the
    number of rows removed from item_prices."""
    payload = quarantine_payload(offenders)
    for i in range(0, len(payload), DELETE_CHUNK):
        chunk = payload[i : i + DELETE_CHUNK]
        # ignore_duplicates: a re-run after a partial apply skips rows
        # already quarantined instead of failing the whole batch.
        client.table(QUARANTINE_TABLE).upsert(
            chunk, on_conflict="item_price_id", ignore_duplicates=True
        ).execute()

    ids = [row.get("id") for row, _ in offenders if row.get("id") is not None]
    deleted = 0
    for i in range(0, len(ids), DELETE_CHUNK):
        chunk = ids[i : i + DELETE_CHUNK]
        client.table(ITEM_PRICES_TABLE).delete().in_("id", chunk).execute()
        deleted += len(chunk)
    return deleted


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move offending rows to quarantine and delete them (default: dry run)",
    )
    args = parser.parse_args()

    client = build_client()
    rows = fetch_corpus(client)
    logger.info("item_prices corpus: %d rows", len(rows))

    offenders = find_offenders(rows)
    if not offenders:
        logger.info("No offending rows — corpus is clean.")
        return

    for row, reasons in offenders:
        logger.info(
            "OFFENDER id=%s receipt=%s date=%s outlet=%s item=%r "
            "qty=%s unit_price=%s line_total=%s reasons=%s",
            row.get("id"),
            row.get("receipt_id"),
            row.get("receipt_date"),
            row.get("outlet_code"),
            row.get("raw_item_name"),
            row.get("qty"),
            row.get("unit_price"),
            row.get("line_total"),
            ",".join(reasons),
        )
    logger.info("%d offending row(s) of %d", len(offenders), len(rows))

    if not args.apply:
        logger.info("Dry run — nothing written. Re-run with --apply to quarantine.")
        return

    deleted = apply(client, offenders)
    logger.info(
        "Quarantined and removed %d row(s) from item_prices. "
        "Review with /price_quarantine in the bot.",
        deleted,
    )


if __name__ == "__main__":
    main()
