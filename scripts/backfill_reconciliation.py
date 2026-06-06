#!/usr/bin/env python3
"""Backfill purchase_reconciliation: re-pull sales_total from sales_daily_summary.

``purchase_reconciliation.sales_total`` is a snapshot frozen at reconcile time.
The nightly digest only re-reconciles today + yesterday, so any business date
whose D-file (sales_daily_summary) landed after its one next-day refresh pass is
stuck with sales_total=NULL — and outlets with no receipts at reconcile time got
no row at all — even though sales_daily_summary now holds the correct sales.

This re-runs reconciliation over a date range so each outlet/date row is rebuilt
from the data that exists NOW: sales_total is re-pulled from sales_daily_summary
and any missing outlet rows are created. It UPSERTs via the same
reconciliation_service path the nightly job uses (on_conflict
outlet_canonical,business_date), so it's idempotent and writes nothing the
nightly run wouldn't.

  Dry run (no writes — prints current → would-be sales_total per outlet/date):
    SUPABASE_URL=... SUPABASE_KEY=... \
      python scripts/backfill_reconciliation.py --dry-run --start 2026-06-02 --end 2026-06-04
  Apply (default range = last 7 days ending today, Malaysia time):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backfill_reconciliation.py
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reconciliation_service  # noqa: E402
from digest_data import FOOD_COST_LOOKBACK_DAYS, MALAYSIA_TZ  # noqa: E402

logger = logging.getLogger("backfill_reconciliation")

RECONCILIATION_TABLE = "purchase_reconciliation"


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _resolve_range(args) -> tuple:
    """(start, end) inclusive. --end defaults to today (Malaysia); --start
    defaults to ``--days`` back from end (last N days inclusive)."""
    end = _parse_date(args.end) if args.end else datetime.now(MALAYSIA_TZ).date()
    if args.start:
        start = _parse_date(args.start)
    else:
        start = end - timedelta(days=args.days - 1)
    if start > end:
        raise SystemExit(f"--start {start} is after --end {end}")
    return start, end


def _dates_in_range(start: date, end: date) -> list:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _current_sales_total(client, start: date, end: date) -> dict:
    """{(business_date_iso, outlet_canonical): sales_total} for the existing rows
    in range — the 'before' side of the preview."""
    resp = (
        client.table(RECONCILIATION_TABLE)
        .select("business_date, outlet_canonical, sales_total")
        .gte("business_date", start.isoformat())
        .lte("business_date", end.isoformat())
        .execute()
    )
    out = {}
    for r in (getattr(resp, "data", None) or []):
        key = (str(r.get("business_date"))[:10], r.get("outlet_canonical"))
        out[key] = r.get("sales_total")
    return out


def _fmt(value) -> str:
    return "NULL" if value is None else f"{float(value):,.2f}"


def _classify(old_present: bool, old_val, new_val) -> str:
    """One of NEW / FILLED / CHANGED / SAME for the per-row tag."""
    if not old_present:
        return "NEW"
    o = None if old_val is None else round(float(old_val), 2)
    n = None if new_val is None else round(float(new_val), 2)
    if o == n:
        return "SAME"
    if o is None:
        return "FILLED"
    return "CHANGED"


def backfill(client, start: date, end: date, *, dry_run: bool) -> dict:
    current = _current_sales_total(client, start, end)
    stats = {"NEW": 0, "FILLED": 0, "CHANGED": 0, "SAME": 0, "rows": 0}
    for d in _dates_in_range(start, end):
        result = reconciliation_service.run_reconciliation(
            client, d.isoformat(), dry_run=dry_run
        )
        for row in sorted(result["rows"], key=lambda r: r.get("outlet_canonical") or ""):
            outlet = row.get("outlet_canonical")
            new_val = row.get("sales_total")
            key = (d.isoformat(), outlet)
            old_present = key in current
            old_val = current.get(key)
            tag = _classify(old_present, old_val, new_val)
            stats[tag] += 1
            stats["rows"] += 1
            before = _fmt(old_val) if old_present else "(no row)"
            logger.info(
                "%s  %-14s  %12s -> %-12s  [%s]  (purch %s)",
                d.isoformat(), outlet or "?", before, _fmt(new_val), tag,
                _fmt(row.get("total_food_purchases")),
            )
    return stats


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Backfill purchase_reconciliation.sales_total from sales_daily_summary."
    )
    parser.add_argument("--start", help="first business date YYYY-MM-DD (default: --days back from --end)")
    parser.add_argument("--end", help="last business date YYYY-MM-DD (default: today, Malaysia time)")
    parser.add_argument("--days", type=int, default=FOOD_COST_LOOKBACK_DAYS,
                        help=f"window length when --start omitted (default {FOOD_COST_LOOKBACK_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print current -> would-be sales_total per outlet/date; write nothing")
    args = parser.parse_args()

    start, end = _resolve_range(args)
    mode = "DRY RUN — no writes" if args.dry_run else "APPLY"
    logger.info("Backfill reconciliation [%s]  %s -> %s", mode, start, end)
    stats = backfill(_build_client(), start, end, dry_run=args.dry_run)
    logger.info(
        "Done (%s): %d rows — NEW=%d FILLED=%d CHANGED=%d SAME=%d",
        mode, stats["rows"], stats["NEW"], stats["FILLED"], stats["CHANGED"], stats["SAME"],
    )


if __name__ == "__main__":
    main()
