#!/usr/bin/env python3
"""READ-ONLY: locate POS ingest latency — is a shift late because the POS SENT it
late, or because WE ingested it late?

Two timestamps on a ``sales_daily`` row are easy to confuse:
  * ``received_at`` = the email's **Date header** — when the POS *sent/dated* it.
  * ``created_at``  = DB ``now()`` default — when WE actually inserted the row
                      (i.e. when our poll picked the email up).

So the latency splits cleanly:
  * shift_close_at -> received_at  = POS send delay  (outside our control)
  * received_at    -> created_at   = our INGEST delay (the poll cadence)

If created_at trails received_at by hours, the poll is the bottleneck. If
received_at itself is hours after shift_close_at (e.g. the overnight shift closes
07:00 but the email is dated ~23:00), the POS is batching its sends and faster
polling cannot help — the email simply isn't in the inbox earlier.

Also dumps recent ``sales_ingest_log`` (``ran_at`` = real poll times) so you can
see how often the poll actually ran.

Nothing is written. Needs the bot's DB creds (SUPABASE_URL, SUPABASE_KEY).

Run on the Render shell::

    python scripts/report_ingest_latency.py --outlet KLANG --days 5
    python scripts/report_ingest_latency.py --outlet SEK20 --days 7
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitchen_usage as ku  # noqa: E402


def _build_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_KEY (read-only).")
    return create_client(url, key)


def _safe(label, thunk):
    try:
        resp = thunk()
        return getattr(resp, "data", None) or []
    except Exception as exc:  # pragma: no cover - live-DB diagnostics
        print(f"  !! query FAILED [{label}]: {type(exc).__name__}: {exc}")
        return []


def _f(v) -> str:
    return "—" if v is None else str(v)


def report(client, outlet_code: str, days: int) -> None:
    keys = ku.outlet_join_keys(outlet_code)
    since = (date.today() - timedelta(days=days)).isoformat()

    rows = _safe(
        "sales_daily",
        lambda: client.table(ku.SALES_DAILY_TABLE)
        .select("outlet_code, outlet_canonical, shift_type, shift_no, "
                "shift_close_at, shift_business_date, received_at, created_at")
        .gte("shift_business_date", since)
        .order("shift_business_date", desc=True)
        .execute(),
    )
    mine = [r for r in rows
            if (ku.outlet_join_keys(r.get("outlet_code")) & keys)
            or (ku.outlet_join_keys(r.get("outlet_canonical")) & keys)]

    print(f"\n=== sales_daily ingest latency for {outlet_code} (since {since}) ===")
    print("  bd / type | shift_close_at | received_at(email Date) | created_at(ingest)")
    if not mine:
        print("  (no rows)")
    for r in sorted(mine, key=lambda r: (str(r.get("shift_business_date")),
                                         str(r.get("shift_type")))):
        print(f"  {_f(r.get('shift_business_date'))} {_f(r.get('shift_type')):<9} | "
              f"close={_f(r.get('shift_close_at'))} | "
              f"recv={_f(r.get('received_at'))} | "
              f"ingest={_f(r.get('created_at'))}")

    log = _safe(
        "sales_ingest_log",
        lambda: client.table("sales_ingest_log")
        .select("ran_at, status, detail, source_subject, outlet_canonical")
        .order("ran_at", desc=True)
        .limit(60)
        .execute(),
    )
    print("\n=== sales_ingest_log — recent poll runs (ran_at = real ingest time) ===")
    if not log:
        print("  (no log rows)")
    for r in log[:40]:
        print(f"  {_f(r.get('ran_at'))} [{_f(r.get('status'))}] "
              f"{_f(r.get('outlet_canonical'))} :: {_f(r.get('detail'))}")
    print("\nRead: recv ≈ ingest but both hours after close -> POS sends late "
          "(faster polling won't help). ingest hours after recv -> the poll is "
          "the bottleneck. Distinct ran_at values show how often the poll fired.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outlet", default="KLANG", help="kitchen outlet code")
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()
    report(_build_client(), args.outlet, args.days)


if __name__ == "__main__":
    main()
