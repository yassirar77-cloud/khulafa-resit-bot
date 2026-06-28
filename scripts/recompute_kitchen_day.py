#!/usr/bin/env python3
"""Repair days whose kitchen Used-vs-POS was frozen with a false pos_qty=0 / LEAK.

Some days were reconciled during the ~16h POS overnight-send delay: the
comparison ran before the POS itemwise was ingested, wrote pos_qty=0 + LEAK for
every item, and marked the day reconciled — so the pos_qty-not-null idempotency
guard then blocked it from ever picking up the real POS.

This bypasses that guard via ``kitchen_usage.recompute_outlet_day``:
  * POS now COMPLETE (D-file summary + both day & overnight shifts ingested)
    -> recompute and OVERWRITE pos_qty/mismatch_flag with the real values.
  * POS still NOT complete -> CLEAR pos_qty/mismatch_flag back to NULL (the day
    stops counting as reconciled and will reconcile once POS lands), instead of
    leaving a false 0/LEAK.

This WRITES to kitchen_daily_usage (only pos_qty + mismatch_flag). It does NOT
touch cooked/left or any sales table.

Run on the Render shell::

    python scripts/recompute_kitchen_day.py --outlet KLANG --dates 2026-06-24,2026-06-25
    python scripts/recompute_kitchen_day.py --outlet KLANG --dates 2026-06-24 --clear-only
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitchen_usage as ku  # noqa: E402


def _build_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_KEY.")
    return create_client(url, key)


def _snapshot(client, outlet, date):
    rows = ku._rows(
        client.table(ku.USAGE_TABLE)
        .select("item_code, cooked_qty, left_qty, used_qty, pos_qty, mismatch_flag")
        .eq("outlet_code", outlet)
        .eq("business_date", date)
        .execute()
    )
    for r in sorted(rows, key=lambda r: str(r.get("item_code"))):
        print(f"    {str(r.get('item_code')):<14} cooked={r.get('cooked_qty')} "
              f"left={r.get('left_qty')} used={r.get('used_qty')} "
              f"pos={r.get('pos_qty')} flag={r.get('mismatch_flag')}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outlet", default="KLANG", help="kitchen outlet code")
    ap.add_argument("--dates", required=True,
                    help="comma-separated YYYY-MM-DD business_dates to repair")
    ap.add_argument("--clear-only", action="store_true",
                    help="only NULL pos_qty/flag (don't recompute, even if POS complete)")
    args = ap.parse_args()

    client = _build_client()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    for date in dates:
        print(f"\n=== {args.outlet} {date} ===")
        print("  BEFORE:")
        _snapshot(client, args.outlet, date)

        complete = ku.pos_complete_for_outlet(client, args.outlet, date)
        coverage = ku.pos_shift_coverage(client, args.outlet, date)
        print(f"  POS coverage: complete={complete} "
              f"day={coverage.get('has_day')} overnight={coverage.get('has_overnight')} "
              f"summary={coverage.get('summary_present')}")

        if args.clear_only:
            n = ku._clear_pos_reconciliation(client, args.outlet, date)
            print(f"  ACTION: cleared {n} row(s) to NULL (clear-only)")
        else:
            res = ku.recompute_outlet_day(client, args.outlet, date)
            print(f"  ACTION: {res['action']} — {res['detail']}")

        print("  AFTER:")
        _snapshot(client, args.outlet, date)

    print("\nDone. If a day showed 'cleared' (POS not complete), it will reconcile "
          "automatically once both shifts + the D-file are ingested.")


if __name__ == "__main__":
    main()
