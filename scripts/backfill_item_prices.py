#!/usr/bin/env python3
"""Backfill item_prices for the 2026-05-23 classification-gap window.

Diamond Ball (roti/capati) and gas suppliers classified as UNKNOWN from ~23 May
(see docs/briefs/classification-gap-investigation.md), so their receipts never
populated item_prices and the order generator went blind on those items. PR #83
whitelists the real suppliers; this script repopulates the missing item_prices
line rows from the receipts that are NOW classified SUPPLIER_PURCHASE.

PREREQUISITE — run FIRST (with PR #83 deployed):
    python backfill_canonical.py --reclassify           # upgrades UNKNOWN headers
This script then reads the CORRECTED receipts.receipt_type and only aggregates
rows whose stored type is SUPPLIER_PURCHASE — it never re-imports a receipt under
its old UNKNOWN/wrong category. If it sees receipts in the window that the
current classifier WOULD upgrade to SUPPLIER but whose stored type is still
UNKNOWN, it warns you to run the reclassify step first.

It mirrors the live path exactly:
    price_aggregation.classify_and_extract_items(receipt['items'])
    outlet_mapping.outlet_from_chat_title(receipt['outlet'])
so the rows it writes are identical to what live ingestion would have stored.

Idempotent: dedups on (receipt_id, canonical_item) against existing item_prices,
so re-running never double-counts.

  Dry run (DEFAULT — writes nothing; prints would-be inserts + Inbois probe):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backfill_item_prices.py
  Apply (only after you approve the dry-run output):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backfill_item_prices.py --apply
  Widen / narrow the window:
    ... python scripts/backfill_item_prices.py --since 2026-05-22
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from price_aggregation import classify_and_extract_items  # noqa: E402
from outlet_mapping import outlet_from_chat_title  # noqa: E402
from receipt_classifier import ReceiptType, classify_receipt  # noqa: E402

_RECEIPTS = "receipts"
_ITEM_PRICES = "item_prices"
_SUPPLIER = ReceiptType.SUPPLIER_PURCHASE.value

# Merchants from the diagnostic whose ingestion gap this recovers. Used only for
# the read-only "Inbois" raw-text probe (requirement 5) and gap reporting.
_GAP_PATTERNS = ["diamond", "petrogas", "inbois", "invois", "gas"]


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_all(client, table, columns, *, build=None, page=1000):
    """Paginate a SELECT past PostgREST's 1000-row default. Read-only."""
    rows, start = [], 0
    while True:
        q = client.table(table).select(columns)
        if build is not None:
            q = build(q)
        data = getattr(q.range(start, start + page - 1).execute(), "data", None) or []
        rows.extend(data)
        if len(data) < page:
            break
        start += page
    return rows


def _window_receipts(client, since):
    cols = "id, merchant, receipt_date, outlet, chat_id, items, receipt_type, raw_text, total"
    return _fetch_all(client, _RECEIPTS, cols,
                      build=lambda q: q.gte("receipt_date", since))


def _existing_canonicals(client, receipt_ids):
    """{receipt_id: {canonical_item, ...}} already in item_prices (for dedup)."""
    out: dict = defaultdict(set)
    ids = [r for r in receipt_ids if r is not None]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        rows = _fetch_all(client, _ITEM_PRICES, "receipt_id, canonical_item",
                          build=lambda q, c=chunk: q.in_("receipt_id", c))
        for r in rows:
            out[r.get("receipt_id")].add(r.get("canonical_item"))
    return out


def _new_rows_for_receipt(receipt, existing: set) -> list[dict]:
    """Mirror the live path: extract line items, drop null-canonical, and dedup
    on (receipt_id, canonical_item) against what is already stored."""
    records = classify_and_extract_items(receipt.get("items") or [])
    rows = []
    for rec in records:
        canonical = rec.get("canonical_item")
        if not canonical or canonical in existing:
            continue  # null canonical (live path drops these) or already present
        rows.append({
            "receipt_id": receipt.get("id"),
            "receipt_date": receipt.get("receipt_date"),
            "outlet_code": outlet_from_chat_title(receipt.get("outlet")),
            "chat_id": receipt.get("chat_id"),
            "merchant": receipt.get("merchant"),
            "canonical_item": canonical,
            "raw_item_name": rec.get("raw_item_name"),
            "qty": rec.get("qty"),
            "unit_price": rec.get("unit_price"),
            "line_total": rec.get("line_total"),
        })
    return rows


def _matches_gap(merchant) -> bool:
    m = (merchant or "").lower()
    return any(p in m for p in _GAP_PATTERNS)


def inbois_probe(client, since) -> None:
    """Requirement 5: confirm from raw text what the 'Inbois' gas merchant
    really is (a real supplier vs OCR of 'invois'/invoice). Read-only."""
    print("\n--- 'Inbois'/gas raw-text probe (requirement 5) ---")
    rows = _window_receipts(client, since)
    hits = [r for r in rows if _matches_gap(r.get("merchant"))]
    if not hits:
        print("  (no merchants matching diamond/petrogas/inbois/invois/gas in window)")
        return
    seen = 0
    for r in sorted(hits, key=lambda r: str(r.get("receipt_date")), reverse=True):
        m = (r.get("merchant") or "").lower()
        if "inbois" not in m and "invois" not in m and "gas" not in m:
            continue
        raw = (r.get("raw_text") or "").replace("\n", " ")[:160]
        print("  %s | merchant=%r | type=%s | raw: %s" % (
            str(r.get("receipt_date"))[:10], r.get("merchant"),
            r.get("receipt_type"), raw))
        seen += 1
        if seen >= 8:
            break
    if not seen:
        print("  (no inbois/invois/gas merchant rows to sample)")


def run(client, *, since: str, apply: bool) -> None:
    receipts = _window_receipts(client, since)
    dates = [str(r.get("receipt_date"))[:10] for r in receipts if r.get("receipt_date")]
    print("=" * 78)
    print("item_prices backfill — window receipt_date >= %s   (%s)"
          % (since, "APPLY — WILL WRITE" if apply else "DRY RUN — no writes"))
    print("=" * 78)
    print("Receipts in window: %d  (%s .. %s)"
          % (len(receipts), min(dates) if dates else "-", max(dates) if dates else "-"))

    eligible = [r for r in receipts if (r.get("receipt_type") or "") == _SUPPLIER]

    # Guard (requirement 1): receipts the current classifier WOULD call SUPPLIER
    # but whose stored header is still not SUPPLIER -> reclassify wasn't run.
    pending = 0
    for r in receipts:
        if (r.get("receipt_type") or "") == _SUPPLIER:
            continue
        res = classify_receipt(ocr_text=r.get("raw_text") or "",
                               parsed_items=r.get("items") or [],
                               total=_to_float(r.get("total")),
                               merchant=r.get("merchant"))
        if res.receipt_type == ReceiptType.SUPPLIER_PURCHASE:
            pending += 1
    print("Eligible (stored type SUPPLIER_PURCHASE): %d" % len(eligible))
    if pending:
        print("⚠️  %d receipt(s) would become SUPPLIER after reclassify but are NOT "
              "yet — run `python backfill_canonical.py --reclassify` FIRST so this "
              "backfill sees corrected headers." % pending)

    existing = _existing_canonicals(client, [r.get("id") for r in eligible])

    per_key: dict = defaultdict(lambda: {"lines": 0, "qty": 0.0})
    all_rows: list[dict] = []
    skipped_dupe = 0
    for r in eligible:
        before = len(classify_and_extract_items(r.get("items") or []))
        rows = _new_rows_for_receipt(r, existing.get(r.get("id"), set()))
        skipped_dupe += max(0, before - len(rows))
        for row in rows:
            k = ((row.get("merchant") or "?"), (row.get("canonical_item") or "?"))
            per_key[k]["lines"] += 1
            per_key[k]["qty"] += _to_float(row.get("qty")) or 0.0
        all_rows.extend(rows)

    print("\nWould insert %d new item_prices row(s); skipped %d already-present/"
          "null-canonical line(s).\n" % (len(all_rows), skipped_dupe))
    if per_key:
        print("  merchant | item | new_lines | sum_qty")
        for (merchant, item), v in sorted(per_key.items(),
                                          key=lambda kv: (-kv[1]["lines"], kv[0])):
            print("  %-26s | %-14s | %4d | %.1f"
                  % (merchant[:26], item[:14], v["lines"], v["qty"]))

    inbois_probe(client, since)

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to insert the above.")
        return
    if not all_rows:
        print("\nNothing to insert.")
        return
    inserted = 0
    for i in range(0, len(all_rows), 500):
        batch = all_rows[i:i + 500]
        client.table(_ITEM_PRICES).insert(batch).execute()
        inserted += len(batch)
    print("\nAPPLIED — inserted %d item_prices row(s)." % inserted)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill item_prices for the classification-gap window (dry-run by default).")
    parser.add_argument("--since", default="2026-05-22",
                        help="earliest receipt_date to re-aggregate (default 2026-05-22)")
    parser.add_argument("--apply", action="store_true",
                        help="actually insert (default is a read-only dry run)")
    args = parser.parse_args()
    run(_build_client(), since=args.since, apply=args.apply)


if __name__ == "__main__":
    main()
