#!/usr/bin/env python3
"""Repair OCR-corrupted receipt dates (one-time pass; dry-run by default).

The diagnostic found impossible receipt_date values — far-future (Diamond Ball
2029-05-29, Inbois 2026-08-22 / 2026-12-16, Victory 2026-06-26) and stale-year
(2024 on 2026 uploads). These poison cadence detection (fake "rhythm broken")
and punch holes in the order window.

Correction rule (date_utils.effective_purchase_date): a receipt_date is
implausible when it is in the FUTURE (> today) or more than --max-drift-days
(default 60) from the ingestion day (created_at) in EITHER direction. When so,
fall back to the ingestion day — the reliable signal of when the bill arrived.
We flag, never guess wildly; a row with no created_at to anchor to is reported
but left untouched.

Repairs BOTH tables the order pipeline relies on, each using its OWN created_at
as the ingestion anchor:
  * receipts.receipt_date       (source of truth: digests, food-cost, reconcile)
  * item_prices.receipt_date    (what the order generator reads)

Idempotent: a repaired row's new receipt_date equals its ingestion day, which is
no longer implausible, so a second run changes nothing. Every change is logged.

  Dry run (DEFAULT — writes nothing; lists id, merchant, receipt_date,
  ingested_at, proposed_corrected_date):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/repair_corrupt_dates.py
  Apply (only after you approve the dry-run output):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/repair_corrupt_dates.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import date_utils  # noqa: E402

_TABLES = ("receipts", "item_prices")


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _today_my() -> date:
    return datetime.now(date_utils._MY_TZ).date()


def _fetch_all(client, table, columns, page=1000):
    rows, start = [], 0
    while True:
        data = getattr(
            client.table(table).select(columns).range(start, start + page - 1).execute(),
            "data", None) or []
        rows.extend(data)
        if len(data) < page:
            break
        start += page
    return rows


def _scan(client, table, *, today, max_drift_days):
    """Return (repairs, flagged) for one table.

    repairs:  rows we WOULD change (year-fix or future->ingestion) -> dicts with
              id/merchant/old/new/ingested/reason
    flagged:  implausible rows we DON'T change — ambiguous old dates or rows with
              no ingestion anchor -> reported for manual review, left untouched
    """
    rows = _fetch_all(client, table, "id, merchant, receipt_date, created_at")
    repairs, flagged = [], []
    for r in rows:
        eff, corrected, reason = date_utils.effective_purchase_date(
            r.get("receipt_date"), r.get("created_at"),
            today=today, max_drift_days=max_drift_days)
        if corrected and eff is not None:
            old = date_utils._parse_local_date(r.get("receipt_date"))
            if old == eff:
                continue  # already clean (idempotent re-run)
            repairs.append({
                "id": r.get("id"), "merchant": r.get("merchant"),
                "old": r.get("receipt_date"),
                "ingested": date_utils._parse_upload_date(r.get("created_at")),
                "new": eff.isoformat(),
                "kind": "YEAR_FIX" if "year fix" in (reason or "") else "INGEST_FALLBACK",
                "reason": reason,
            })
        elif reason is not None:
            # Implausible but we won't guess (ambiguous old / no anchor).
            flagged.append({
                "id": r.get("id"), "merchant": r.get("merchant"),
                "old": r.get("receipt_date"), "reason": reason,
            })
    return repairs, flagged


def run(client, *, apply: bool, max_drift_days: int) -> dict:
    today = _today_my()
    print("=" * 78)
    print("Corrupt receipt_date repair — today(MY)=%s  drift>%dd or future  (%s)"
          % (today, max_drift_days, "APPLY — WILL WRITE" if apply else "DRY RUN — no writes"))
    print("=" * 78)

    totals = {"repairs": 0, "year_fix": 0, "ingest_fallback": 0,
              "flagged": 0, "written": 0}
    for table in _TABLES:
        repairs, flagged = _scan(client, table, today=today, max_drift_days=max_drift_days)
        yf = sum(1 for x in repairs if x["kind"] == "YEAR_FIX")
        ifb = len(repairs) - yf
        totals["repairs"] += len(repairs)
        totals["year_fix"] += yf
        totals["ingest_fallback"] += ifb
        totals["flagged"] += len(flagged)
        print("\n--- %s --- %d to repair (%d year-fix keep MM-DD, %d future->ingest), "
              "%d flagged for review (untouched)"
              % (table, len(repairs), yf, ifb, len(flagged)))
        if repairs:
            print("  id | merchant | receipt_date -> proposed | kind | reason")
            for x in sorted(repairs, key=lambda d: str(d["old"]), reverse=True):
                print("  %s | %-26s | %s -> %s | %-15s | %s" % (
                    x["id"], str(x["merchant"])[:26], x["old"], x["new"],
                    x["kind"], x["reason"]))
        for x in flagged:
            print("  ⚠️ id=%s merchant=%r receipt_date=%s — %s (left untouched)"
                  % (x["id"], x["merchant"], x["old"], x["reason"]))

        if apply and repairs:
            for x in repairs:
                client.table(table).update(
                    {"receipt_date": x["new"]}).eq("id", x["id"]).execute()
                totals["written"] += 1
                print("  ✔ %s id=%s: %s -> %s" % (table, x["id"], x["old"], x["new"]))

    print("\n%s — %d row(s) %s (%d year-fix, %d future->ingest); %d flagged for review."
          % ("APPLIED" if apply else "DRY RUN",
             totals["written"] if apply else totals["repairs"],
             "written" if apply else "would be repaired",
             totals["year_fix"], totals["ingest_fallback"], totals["flagged"]))
    if not apply:
        print("Re-run with --apply to write the proposed corrections.")
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair OCR-corrupted receipt dates (dry-run by default).")
    parser.add_argument("--apply", action="store_true",
                        help="actually update (default is a read-only dry run)")
    parser.add_argument("--max-drift-days", type=int,
                        default=date_utils.DEFAULT_MAX_DRIFT_DAYS,
                        help="days from ingestion day that count as corrupt (default 60)")
    args = parser.parse_args()
    run(_build_client(), apply=args.apply, max_drift_days=args.max_drift_days)


if __name__ == "__main__":
    main()
