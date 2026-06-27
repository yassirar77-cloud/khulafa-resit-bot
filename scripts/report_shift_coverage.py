#!/usr/bin/env python3
"""READ-ONLY report: POS shift coverage per business_date for a kitchen outlet.

Answers the timing question behind the Used-vs-POS comparison: a 24h outlet
reports its POS in TWO shift-close emails that both fold to the SAME
``business_date`` — the ~7PM "day" shift and the ~7AM-next-day "overnight" shift.
The comparison must wait until BOTH are in (otherwise it's against a half-day of
sales). This prints, per business_date, the ``sales_daily`` shift rows
(shift_no, shift_type, open, close, received_at) AND whether the D-file daily
summary (``sales_daily_summary``, which carries the itemwise quantities) exists,
so you can SEE whether both shifts land under the same business_date and WHEN
each arrives — i.e. the correct comparison time and whether the fold is right.

Nothing is written — only ``.select()`` queries. The outlet bridging
(``outlet_join_keys``) and the completeness gate (``pos_shift_coverage``) are
imported from the shipped ``kitchen_usage`` module, so the output matches exactly
what the bot's STAGE 2 comparison uses to decide.

Run on the Render shell (or locally with the prod env vars)::

    SUPABASE_URL=... SUPABASE_KEY=... python scripts/report_shift_coverage.py
    python scripts/report_shift_coverage.py --outlet SEK6 --days 5
    python scripts/report_shift_coverage.py --outlet KLANG --dates 2026-06-23,2026-06-24
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitchen_usage as ku  # noqa: E402

DEFAULT_OUTLET = "SEK6"


def _build_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_KEY (read-only).")
    return create_client(url, key)


def _recent_dates(days: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=d)).isoformat() for d in range(1, days + 1)]


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def _safe_rows(label, thunk):
    try:
        resp = thunk()
        return getattr(resp, "data", None) or []
    except Exception as exc:  # pragma: no cover - live-DB diagnostics
        print(f"  !! query FAILED [{label}]: {type(exc).__name__}: {exc}")
        return []


def report_raw_shifts(client, outlet_code: str, dates: list[str]) -> None:
    """Dump EVERY sales_daily row whose shift_business_date is in the window —
    including 'unknown'-typed shifts (a day shift closing outside 17–22h is typed
    'unknown' and keeps its own date, so it would NOT show as a 'day' shift and
    looks 'missing'). This surfaces misclassification, not just absence."""
    keys = ku.outlet_join_keys(outlet_code)
    span = sorted(dates)
    rows = _safe_rows(
        "sales_daily",
        lambda: client.table(ku.SALES_DAILY_TABLE)
        .select("outlet_code, outlet_canonical, shift_no, shift_type, "
                "shift_open_at, shift_close_at, shift_business_date, received_at, "
                "source_subject")
        .gte("shift_business_date", span[0])
        .lte("shift_business_date", span[-1])
        .execute(),
    )
    mine = [r for r in rows
            if (ku.outlet_join_keys(r.get("outlet_code")) & keys)
            or (ku.outlet_join_keys(r.get("outlet_canonical")) & keys)]
    print("\n=== RAW sales_daily rows (all shift_types incl. 'unknown') ===")
    if not mine:
        print("  (none in window)")
    for r in sorted(mine, key=lambda r: (str(r.get("shift_business_date")),
                                         str(r.get("shift_close_at") or ""))):
        print(f"  bd={_fmt(r.get('shift_business_date'))} "
              f"type={_fmt(r.get('shift_type')):<9} "
              f"shift_no={_fmt(r.get('shift_no')):>6} "
              f"close={_fmt(r.get('shift_close_at'))} "
              f"recv={_fmt(r.get('received_at'))}")


def report_ingest_log(client, outlet_code: str, dates: list[str]) -> None:
    """Dump recent sales_ingest_log rows for this outlet — shows whether a shift
    email was 'inserted' / 'skipped' (duplicate/dedup) / 'error' (parse) /
    'skipped_unknown', so a missing shift can be traced to email-absent vs
    parse-fail vs dedup collision."""
    keys = ku.outlet_join_keys(outlet_code)
    rows = _safe_rows(
        "sales_ingest_log",
        lambda: client.table("sales_ingest_log")
        .select("ran_at, outlet_code, outlet_canonical, status, detail, source_subject")
        .order("ran_at", desc=True)
        .limit(500)
        .execute(),
    )
    mine = [r for r in rows
            if (ku.outlet_join_keys(r.get("outlet_code")) & keys)
            or (ku.outlet_join_keys(r.get("outlet_canonical")) & keys)]
    print("\n=== sales_ingest_log (recent, this outlet) ===")
    if not mine:
        print("  (no log rows matched — email may never have arrived, or outlet "
              "not resolved)")
    for r in mine[:40]:
        print(f"  {_fmt(r.get('ran_at'))} [{_fmt(r.get('status'))}] "
              f"{_fmt(r.get('detail'))} :: {_fmt(r.get('source_subject'))}")


def report(client, outlet_code: str, dates: list[str]) -> None:
    print(f"\n=== POS shift coverage for kitchen outlet {outlet_code} ===")
    print(f"join keys: {sorted(ku.outlet_join_keys(outlet_code))}\n")
    for d in dates:
        shifts = ku._matching_shift_rows(client, outlet_code, d)
        cov = ku.pos_shift_coverage(client, outlet_code, d)
        gate = "✅ COMPLETE" if cov["complete"] else "⏳ INCOMPLETE"
        print(f"business_date {d}  ->  {gate}")
        print(f"  shifts={cov['shift_count']} types={sorted(cov['shift_types'])} "
              f"day={cov['has_day']} overnight={cov['has_overnight']} "
              f"| D-file summary present={cov['summary_present']} "
              f"total_shifts={_fmt(cov['total_shifts'])}")
        if shifts:
            print("  sales_daily rows (per shift):")
            for r in sorted(shifts, key=lambda r: str(r.get("shift_close_at") or "")):
                print(f"    - shift_no={_fmt(r.get('shift_no')):>6} "
                      f"type={_fmt(r.get('shift_type')):<9} "
                      f"close={_fmt(r.get('shift_close_at'))} "
                      f"received={_fmt(r.get('received_at'))} "
                      f"code={_fmt(r.get('outlet_code'))}")
        else:
            print("  sales_daily: (no shift rows for this business_date)")
        print()
    print("Read: if both a 'day' and an 'overnight' shift sit under the SAME "
          "business_date, the POS fold matches the kitchen fold. The 'received' "
          "timestamps show when each email arrived — the overnight one is the "
          "late (~7AM) arrival the comparison must wait for.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outlet", default=DEFAULT_OUTLET,
                    help="kitchen outlet code (e.g. SEK6, KLANG)")
    ap.add_argument("--days", type=int, default=5,
                    help="how many recent business_dates to show (default 5)")
    ap.add_argument("--dates", default=None,
                    help="explicit comma-separated YYYY-MM-DD list (overrides --days)")
    ap.add_argument("--diagnose", action="store_true",
                    help="also dump RAW sales_daily rows (incl. 'unknown' shifts) "
                         "and sales_ingest_log — to trace a missing/half-day shift")
    args = ap.parse_args()

    dates = (
        [s.strip() for s in args.dates.split(",") if s.strip()]
        if args.dates else _recent_dates(args.days)
    )
    client = _build_client()
    report(client, args.outlet, dates)
    if args.diagnose:
        report_raw_shifts(client, args.outlet, dates)
        report_ingest_log(client, args.outlet, dates)


if __name__ == "__main__":
    main()
