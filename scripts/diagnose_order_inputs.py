#!/usr/bin/env python3
"""READ-ONLY diagnostics for the order generator's purchase inputs.

Two questions the test order output raised, answered straight from the live
``item_prices`` / ``receipts`` / ``pending_review`` tables. This script ONLY
reads (``.select()`` calls) — it writes nothing and changes no order logic.

  A) INGESTION GAP — for Diamond Ball (roti/capati) and the gas suppliers
     (Petronas / Inbois / Victory / Ranau), across ALL outlets:
       A1  last N ingested purchase rows each (date, outlet, item, qty, source)
       A2  header receipts since --since with their item_prices line-row count
           (a receipt present with line_rows=0 ⇒ parse/extraction failure, not a
            missing source)
       A3  pending_review rows for these merchants / since the cutoff (held or
           failed parses never reach item_prices)
     A per-merchant verdict classifies the gap as PARSE FAILURE vs MISSING
     SOURCE vs FEWER/NO PURCHASES.

  B) QTY OUTLIERS — every purchase row whose qty exceeds --factor × the median
     qty for that (outlet, canonical_item). Surfaces the Ais 5062 / 144 type
     OCR-merged quantities that poison the trailing average.

Usage (run where the bot's creds live):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/diagnose_order_inputs.py
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/diagnose_order_inputs.py \
        --cutoff 2026-05-22 --since 2026-05-01 --factor 5 --rows 5
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Merchant groups to investigate. Each label maps to case-insensitive substring
# patterns matched against item_prices.merchant / receipts.merchant.
MERCHANT_GROUPS: dict[str, list[str]] = {
    "Diamond Ball (roti/capati)": ["diamond"],
    "Petronas (gas)": ["petronas"],
    "Inbois (gas)": ["inbois"],
    "Victory (gas)": ["victory"],
    "Ranau (gas)": ["ranau"],
}


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _d(value) -> str:
    """Normalise a date/timestamp value to its YYYY-MM-DD prefix (or '')."""
    return str(value)[:10] if value else ""


def fetch_all(client, table: str, columns: str, *, build=None, page: int = 1000) -> list[dict]:
    """Paginate a SELECT past PostgREST's 1000-row default. ``build(query)``
    applies filters/ordering. Read-only."""
    rows: list[dict] = []
    start = 0
    while True:
        q = client.table(table).select(columns)
        if build is not None:
            q = build(q)
        resp = q.range(start, start + page - 1).execute()
        data = getattr(resp, "data", None) or []
        rows.extend(data)
        if len(data) < page:
            break
        start += page
    return rows


def _ilike_rows(client, table: str, columns: str, column: str, pattern: str,
                *, since: str | None = None, since_col: str | None = None) -> list[dict]:
    def build(q):
        q = q.ilike(column, f"%{pattern}%")
        if since and since_col:
            q = q.gte(since_col, since)
        return q
    return fetch_all(client, table, columns, build=build)


# --- A) ingestion gap --------------------------------------------------------

def task_a(client, *, cutoff: str, since: str, rows_per: int) -> None:
    print("=" * 78)
    print("TASK A — INGESTION GAP  (cutoff suspected ~%s; header scan since %s)" % (cutoff, since))
    print("=" * 78)

    for label, patterns in MERCHANT_GROUPS.items():
        # Gather matching item_prices rows for every pattern, dedup by id.
        ip: dict = {}
        for pat in patterns:
            for r in _ilike_rows(
                client, "item_prices",
                "id, receipt_id, merchant, receipt_date, outlet_code, "
                "canonical_item, raw_item_name, qty",
                "merchant", pat,
            ):
                ip[r["id"]] = r
        ip_rows = sorted(ip.values(), key=lambda r: (_d(r.get("receipt_date")), r.get("id") or 0),
                         reverse=True)

        # Resolve the ingestion timestamp (source) for the shown rows.
        recent = ip_rows[:rows_per]
        rid_set = [r.get("receipt_id") for r in recent if r.get("receipt_id")]
        ingested: dict = {}
        for i in range(0, len(rid_set), 100):
            chunk = rid_set[i:i + 100]
            resp = (client.table("receipts").select("id, created_at, chat_id")
                    .in_("id", chunk).execute())
            for rr in (getattr(resp, "data", None) or []):
                ingested[rr["id"]] = rr

        print("\n--- %s ---" % label)
        if not recent:
            print("  A1: NO item_prices rows ever for this merchant.")
        else:
            print("  A1 last %d purchase rows (date | outlet | item | qty | ingested_at | chat):"
                  % min(rows_per, len(recent)))
            for r in recent:
                src = ingested.get(r.get("receipt_id"), {})
                print("     %s | %-8s | %-14s | %8s | %s | %s" % (
                    _d(r.get("receipt_date")), r.get("outlet_code") or "?",
                    r.get("canonical_item") or (r.get("raw_item_name") or "?")[:14],
                    r.get("qty"), _d(src.get("created_at")) or "?", src.get("chat_id") or "?"))

        # A2: header receipts since the cutoff window + their line-row counts.
        hdr: dict = {}
        for pat in patterns:
            for r in _ilike_rows(
                client, "receipts",
                "id, merchant, receipt_date, outlet, created_at, receipt_type, confidence",
                "merchant", pat, since=since, since_col="receipt_date",
            ):
                hdr[r["id"]] = r
        hdr_rows = sorted(hdr.values(), key=lambda r: _d(r.get("receipt_date")), reverse=True)
        line_counts: dict = defaultdict(int)
        hid = list(hdr.keys())
        for i in range(0, len(hid), 100):
            chunk = hid[i:i + 100]
            resp = client.table("item_prices").select("receipt_id").in_("receipt_id", chunk).execute()
            for rr in (getattr(resp, "data", None) or []):
                line_counts[rr.get("receipt_id")] += 1

        print("  A2 header receipts since %s (date | outlet | conf | type | line_rows):" % since)
        if not hdr_rows:
            print("     (none)")
        for r in hdr_rows:
            print("     %s | %-8s | %3s | %-16s | line_rows=%d" % (
                _d(r.get("receipt_date")), r.get("outlet") or "?", r.get("confidence"),
                r.get("receipt_type") or "?", line_counts.get(r.get("id"), 0)))

        # A3: pending_review (held / failed parses).
        pend: dict = {}
        for pat in patterns:
            for r in _ilike_rows(
                client, "pending_review",
                "id, parsed_merchant, parsed_total, parsed_date, confidence, status, reason, created_at",
                "parsed_merchant", pat,
            ):
                pend[r["id"]] = r
        if pend:
            print("  A3 pending_review (held/failed parses):")
            for r in sorted(pend.values(), key=lambda r: _d(r.get("created_at")), reverse=True):
                print("     %s | %s | conf=%s | %s | %s" % (
                    _d(r.get("created_at")), r.get("parsed_merchant"), r.get("confidence"),
                    r.get("status"), (r.get("reason") or "")[:40]))

        # Verdict heuristic.
        latest_ip = _d(ip_rows[0].get("receipt_date")) if ip_rows else None
        hdr_after = [r for r in hdr_rows if _d(r.get("receipt_date")) >= cutoff]
        hdr_after_no_lines = [r for r in hdr_after if line_counts.get(r.get("id"), 0) == 0]
        hdr_after_lines = [r for r in hdr_after if line_counts.get(r.get("id"), 0) > 0]
        if hdr_after_no_lines and not hdr_after_lines:
            verdict = ("PARSE FAILURE — %d receipt(s) ingested after %s but 0 item lines extracted"
                       % (len(hdr_after_no_lines), cutoff))
        elif hdr_after_lines:
            verdict = ("INGESTING — %d receipt(s) with item lines after %s (latest item row %s)"
                       % (len(hdr_after_lines), cutoff, latest_ip))
        elif not hdr_rows and not pend:
            verdict = ("MISSING SOURCE / NO PURCHASES — no receipts or pending rows since %s (last item row %s)"
                       % (since, latest_ip or "never"))
        else:
            verdict = ("CHECK — receipts exist but none after %s; last item row %s" % (cutoff, latest_ip or "never"))
        print("  ▶ VERDICT: %s" % verdict)


# --- B) qty outliers ---------------------------------------------------------

def task_b(client, *, factor: float) -> None:
    print("\n" + "=" * 78)
    print("TASK B — QTY OUTLIERS  (qty > %.0f× median per outlet+item)" % factor)
    print("=" * 78)

    rows = fetch_all(client, "item_prices",
                     "outlet_code, canonical_item, raw_item_name, merchant, receipt_date, qty")
    groups: dict = defaultdict(list)
    for r in rows:
        q = r.get("qty")
        if not isinstance(q, (int, float)) or isinstance(q, bool) or q <= 0:
            continue
        key = (r.get("outlet_code"), r.get("canonical_item"))
        groups[key].append(r)

    medians = {k: statistics.median([float(r["qty"]) for r in v]) for k, v in groups.items()}
    outliers = []
    for key, members in groups.items():
        med = medians[key]
        if med <= 0:
            continue
        for r in members:
            q = float(r["qty"])
            if q > factor * med:
                outliers.append((q / med, r, med, len(members)))
    outliers.sort(key=lambda t: t[0], reverse=True)

    print("Scanned %d priced rows across %d (outlet,item) groups — %d outlier(s):"
          % (len(rows), len(groups), len(outliers)))
    if not outliers:
        print("  (none)")
        return
    print("  ratio | qty | median | n | outlet | item | merchant | date | raw_name")
    for ratio, r, med, n in outliers:
        print("  %5.1fx | %8s | %7.1f | %3d | %-8s | %-14s | %-16s | %s | %s" % (
            ratio, r.get("qty"), med, n, r.get("outlet_code") or "?",
            (r.get("canonical_item") or "?")[:14], (r.get("merchant") or "?")[:16],
            _d(r.get("receipt_date")), (r.get("raw_item_name") or "")[:30]))


def main() -> None:
    parser = argparse.ArgumentParser(description="READ-ONLY order-input diagnostics (A: ingestion gap, B: qty outliers).")
    parser.add_argument("--cutoff", default="2026-05-22", help="suspected ingestion-stop date YYYY-MM-DD (A verdict)")
    parser.add_argument("--since", default="2026-05-01", help="header-receipt scan lower bound YYYY-MM-DD (A2)")
    parser.add_argument("--factor", type=float, default=5.0, help="qty outlier threshold as a multiple of the median (B)")
    parser.add_argument("--rows", type=int, default=5, help="rows shown per merchant in A1")
    parser.add_argument("--only", choices=["a", "b"], help="run only task A or only task B")
    args = parser.parse_args()

    client = _build_client()
    if args.only != "b":
        task_a(client, cutoff=args.cutoff, since=args.since, rows_per=args.rows)
    if args.only != "a":
        task_b(client, factor=args.factor)


if __name__ == "__main__":
    main()
