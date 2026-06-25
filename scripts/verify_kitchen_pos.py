#!/usr/bin/env python3
"""READ-ONLY check of the kitchen Used-vs-POS mapping against LIVE sales data.

Pulls the real ``sales_daily_summary`` + ``sales_daily_itemwise`` rows for an
outlet/date, runs the EXACT shipped mapping (``kitchen_usage`` —
``outlet_join_keys``, ``ITEM_POS_KEYWORDS``, ``_pos_dish_matches``,
``_pos_dish_excluded``, ``pos_qty_for_item``) and prints, per kitchen item:
  * every matched POS dish name + its qty, and the rolled-up total
  * the kg-equivalent for Kambing/Daging (portions x locked grams)
then prints the EXCLUDED dishes (isi ayam / Thai / staff / no-style) with the
reason each was dropped, and CLASSIFICATION NOTES for dishes whose treatment is
an owner decision (Ayam Rendang, plain Briyani Ayam).

It is heavily INSTRUMENTED: each retrieval stage prints its raw counts (summary
rows for the date, resolved summary_id(s), itemwise rows pulled, categories
present) so a "0" is traceable to the exact failing stage. Retrieval is robust:
the server-side date filter is tried first, then a client-side date filter
fallback; the itemwise fetch is paginated past the PostgREST 1000-row cap.

NOTHING is written — only ``.select()`` queries are issued. The mapping logic is
imported from kitchen_usage (never re-implemented), so its output is exactly what
the bot computes.

Run on the Render shell (or locally with the prod env vars)::

    SUPABASE_URL=... SUPABASE_KEY=... python scripts/verify_kitchen_pos.py
    python scripts/verify_kitchen_pos.py --outlet KLANG --dates 2026-06-23
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitchen_usage as ku  # noqa: E402

DEFAULT_OUTLET = "KLANG"
DEFAULT_DATES = ["2026-06-23", "2026-06-24"]

ALL_KITCHEN_OUTLETS = ("BISTRO7", "SEK20", "SEK14", "SEK15", "SEK6",
                       "VISTA", "JAKEL", "D", "KLANG", "KLRAZAK")

# Style/phrase markers that make an ayam dish a TRACKED item (used only to flag
# "plain" briyani/nasi ayam that carry none of them — an owner decision).
_AYAM_STYLE_MARKERS = ("bawang", "kicap", "madu", "tandoori", "tandori",
                       "rempah", "ayam goreng")


def _build_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_KEY (read-only).")
    return create_client(url, key)


def _safe(label, thunk):
    """Run a query thunk; print and swallow any error, returning ``.data`` or []."""
    try:
        resp = thunk()
        return getattr(resp, "data", None) or []
    except Exception as exc:  # pragma: no cover - live-DB diagnostics
        print(f"  !! query FAILED [{label}]: {type(exc).__name__}: {exc}")
        return []


def _paged(label, query_fn, page=1000, cap=50000):
    """Fetch a select in <=``page``-row pages (PostgREST caps each request)."""
    out, start = [], 0
    while start < cap:
        chunk = _safe(f"{label}[{start}:{start + page}]",
                      lambda s=start: query_fn().range(s, s + page - 1).execute())
        out.extend(chunk)
        if len(chunk) < page:
            break
        start += page
    return out


def _qty(row):
    try:
        return float(row.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt(n):
    return f"{n:g}"


# --- retrieval (robust + instrumented) --------------------------------------

def _resolve_summaries(client, outlet_code, business_date):
    """(matched_summaries, all_for_date, note). Server-side date filter first;
    if it returns nothing, scan recent summaries and filter the date client-side
    (catches a date-type/format quirk in the server-side eq)."""
    date_str = str(business_date)[:10]
    note = "server-side eq(business_date)"
    all_for_date = _safe(
        "summaries.eq(business_date)",
        lambda: client.table(ku.SALES_SUMMARY_TABLE)
        .select("id, outlet_canonical, outlet_code, business_date")
        .eq("business_date", date_str)
        .execute(),
    )
    if not all_for_date:
        note = "client-side date filter (server eq returned 0)"
        scanned = _paged(
            "summaries.scan",
            lambda: client.table(ku.SALES_SUMMARY_TABLE)
            .select("id, outlet_canonical, outlet_code, business_date")
            .order("business_date", desc=True),
        )
        all_for_date = [s for s in scanned if str(s.get("business_date"))[:10] == date_str]
    tkeys = ku.outlet_join_keys(outlet_code)
    matched = [
        s for s in all_for_date
        if (ku.outlet_join_keys(s.get("outlet_code")) & tkeys)
        or (ku.outlet_join_keys(s.get("outlet_canonical")) & tkeys)
    ]
    return matched, all_for_date, note


def _fetch_itemwise_rows(client, summary_ids):
    if not summary_ids:
        return []
    return _paged(
        "itemwise.in(summary_id)",
        lambda: client.table(ku.SALES_ITEMWISE_TABLE)
        .select("item_name, qty, category, summary_id")
        .in_("summary_id", summary_ids),
    )


def _scan_outlet_across_dates(client, outlet_code, limit=600):
    tkeys = ku.outlet_join_keys(outlet_code)
    rows = _safe(
        "summaries.recent",
        lambda: client.table(ku.SALES_SUMMARY_TABLE)
        .select("outlet_code, outlet_canonical, business_date")
        .order("business_date", desc=True)
        .limit(limit)
        .execute(),
    )
    return [
        r for r in rows
        if (ku.outlet_join_keys(r.get("outlet_code")) & tkeys)
        or (ku.outlet_join_keys(r.get("outlet_canonical")) & tkeys)
    ]


# --- classification (mirrors shipped matchers) ------------------------------

def _exclusion_reason(name_l, category, base):
    if "thai" in str(category or "").lower():
        return "THAI FOOD category"
    if ku._POS_STAFF_SUBSTR in name_l:
        return "staff meal"
    if base == "ayam":
        for sub in ku.AYAM_EXCLUDE_SUBSTRINGS:
            if sub in name_l:
                return f"Thai/noodle ayam ('{sub.strip()}')"
    return "isi ayam / no tracked style keyword"


def _print_classification_notes(items):
    """Confirm the two owner-decided exclusions are holding (deferred to monthly
    v12). Both are EXCLUDED from the daily comparison by owner decision:
      * Ayam Rendang (low volume; the 3:1 conversion isn't worth daily complexity)
      * plain Briyani Ayam with no style word (shredded/isi type, Thai chef)."""
    print("\n--- CONFIRMED EXCLUSIONS (owner decision; reconcile in monthly v12) ---")
    flagged = False

    rendang = [r for r in items
               if "ayam" in str(r.get("item_name") or "").lower()
               and "rendang" in str(r.get("item_name") or "").lower()]
    if rendang:
        flagged = True
        tot = sum(_qty(r) for r in rendang)
        print(f"  * Ayam Rendang — EXCLUDED (decided): {_fmt(tot)} dish(es), deferred to v12:")
        for r in rendang:
            print(f"      {r.get('item_name')!r} qty {_fmt(_qty(r))}")

    plain_briyani = []
    for r in items:
        n = str(r.get("item_name") or "").lower()
        if "ayam" not in n:
            continue
        if "briyani" not in n and "biriyani" not in n and "briani" not in n:
            continue
        if any(m in n for m in _AYAM_STYLE_MARKERS):
            continue  # has a tracked style (e.g. Briyani Ayam Bawang) -> counted
        plain_briyani.append(r)
    if plain_briyani:
        flagged = True
        print("  * Plain Briyani Ayam (no style word) — EXCLUDED (decided): isi-ayam type:")
        for r in plain_briyani:
            print(f"      {r.get('item_name')!r} qty {_fmt(_qty(r))}")

    if not flagged:
        print("  (neither decided-exclusion pattern present today)")


# --- report ------------------------------------------------------------------

def report(client, outlet_code, business_date):
    print("=" * 70)
    print(f"OUTLET {outlet_code!r}   business_date {business_date}")
    tkeys = ku.outlet_join_keys(outlet_code)
    print(f"outlet_join_keys({outlet_code!r}) -> {sorted(tkeys)}")

    matched, all_for_date, note = _resolve_summaries(client, outlet_code, business_date)
    print(f"  [stage 1] summary rows for date: {len(all_for_date)}  ({note})")

    if not matched:
        print("  NO matching sales_daily_summary row for this outlet/date.\n")
        print("  --- DIAGNOSTIC: every summary row present for this date ---")
        if not all_for_date:
            print("    (zero summary rows for this date — not ingested, or date mismatch)")
        for s in all_for_date:
            keys = ku.outlet_join_keys(s.get("outlet_code")) | ku.outlet_join_keys(s.get("outlet_canonical"))
            print(f"    id={s.get('id')} outlet_code={s.get('outlet_code')!r:14} "
                  f"outlet_canonical={s.get('outlet_canonical')!r:18} keys={sorted(keys)}")
        print(f"\n  --- DIAGNOSTIC: dates {outlet_code!r} DOES have summaries (recent) ---")
        hits = _scan_outlet_across_dates(client, outlet_code)
        if not hits:
            print("    (none found — outlet may use a different code, or RLS/key hides the table)")
        for h in hits[:25]:
            print(f"    {h.get('business_date')}  outlet_code={h.get('outlet_code')!r} "
                  f"outlet_canonical={h.get('outlet_canonical')!r}")
        return

    ids = [s["id"] for s in matched]
    print(f"  [stage 2] RESOLVED summary_id(s) for {outlet_code!r}: {ids}")
    for s in matched:
        print(f"            id={s['id']} outlet_code={s.get('outlet_code')!r} "
              f"outlet_canonical={s.get('outlet_canonical')!r} "
              f"business_date={s.get('business_date')!r}")

    items = _fetch_itemwise_rows(client, ids)
    print(f"  [stage 3] itemwise rows pulled: {len(items)}")
    if not items:
        print("  !! summary FOUND but ZERO itemwise rows — itemwise retrieval issue "
              "(check summary_id type / RLS on sales_daily_itemwise).")
        return
    cats = sorted({str(r.get("category")) for r in items})
    print(f"  [stage 4] categories present (menu sections, NOT proteins): {cats}\n")

    # --- per kitchen item: matched dishes + total -----------------------------
    print("--- PER KITCHEN ITEM: matched POS dishes ---")
    matched_by_dish = {}  # id(row) -> [codes]
    for code in ku.ITEM_POS_KEYWORDS:
        spec = ku.ITEM_POS_KEYWORDS[code]
        base = spec["base"]
        unit = ku.ITEM_BY_CODE.get(code, {}).get("unit", "pcs")
        hits = []
        for row in items:
            name_l = str(row.get("item_name") or "").lower()
            if base not in name_l:
                continue
            if ku._pos_dish_excluded(name_l, row.get("category"), base):
                continue
            if ku._pos_dish_matches(spec, name_l):
                hits.append(row)
                matched_by_dish.setdefault(id(row), []).append(code)
        total_qty = ku.pos_qty_for_item(code, items)
        portions = sum(_qty(r) for r in hits)
        print(f"\n  {code}  ({unit}):")
        if not hits:
            print("    (no matching POS dish)")
        for r in hits:
            print(f"    + {str(r.get('item_name')):42} qty {_fmt(_qty(r))}  [{r.get('category')}]")
        if unit == "kg":
            grams = ku.KG_PORTION_GRAMS.get(code, 0.0)
            print(f"    = {_fmt(portions)} portions x {_fmt(grams)} g = {total_qty} kg")
        else:
            print(f"    = TOTAL {total_qty} pcs")

    # --- excluded dishes (carry a protein word but dropped) -------------------
    print("\n--- EXCLUDED dishes (contain a protein word but NOT counted) ---")
    proteins = ("ayam", "ikan", "kambing", "daging")
    excluded_any = False
    for row in items:
        if id(row) in matched_by_dish:
            continue
        name_l = str(row.get("item_name") or "").lower()
        base = next((p for p in proteins if p in name_l), None)
        if base is None:
            continue  # not a tracked protein at all (Nasi Putih, Roti, drinks...)
        excluded_any = True
        reason = _exclusion_reason(name_l, row.get("category"), base)
        print(f"  - {str(row.get('item_name')):42} qty {_fmt(_qty(row))} "
              f"[{row.get('category')}]  -> {reason}")
    if not excluded_any:
        print("  (none)")

    # --- multi-match warning (a dish counted by >1 item) ----------------------
    dbl = {rid: cs for rid, cs in matched_by_dish.items() if len(cs) > 1}
    if dbl:
        print("\n  !! WARNING: dishes counted by MORE THAN ONE item:")
        for row in items:
            if id(row) in dbl:
                print(f"     {row.get('item_name')!r} -> {dbl[id(row)]}")

    # --- integrity (item_name based, NOT the menu category) -------------------
    prot_rows = [r for r in items
                 if any(p in str(r.get("item_name") or "").lower() for p in proteins)]
    matched_n = sum(1 for r in prot_rows if id(r) in matched_by_dish)
    print(f"\n--- INTEGRITY: {len(prot_rows)} protein-bearing dishes -> "
          f"{matched_n} matched, {len(prot_rows) - matched_n} excluded "
          f"(eyeball the EXCLUDED list above; nothing should be wrongly dropped) ---")

    _print_classification_notes(items)
    print()


def print_outlet_resolution(client=None):
    """For all 10 kitchen outlets: join keys + the live POS outlet_code(s) seen."""
    print("=" * 70)
    print("ALL 10 OUTLETS — kitchen code -> join keys (and live POS codes)")
    live = []
    if client is not None:
        live = _paged(
            "summaries.codes",
            lambda: client.table(ku.SALES_SUMMARY_TABLE)
            .select("outlet_code, outlet_canonical"),
        )
    for code in ALL_KITCHEN_OUTLETS:
        keys = ku.outlet_join_keys(code)
        pos_codes = sorted({
            r.get("outlet_code") for r in live
            if (ku.outlet_join_keys(r.get("outlet_code")) & keys)
            or (ku.outlet_join_keys(r.get("outlet_canonical")) & keys)
        }) if live else []
        suffix = f"   live POS outlet_code(s): {pos_codes}" if client is not None else ""
        print(f"  {code:9} -> keys {sorted(keys)}{suffix}")
    print()


def main():
    ap = argparse.ArgumentParser(description="READ-ONLY kitchen Used-vs-POS mapping check.")
    ap.add_argument("--outlet", default=DEFAULT_OUTLET,
                    help="kitchen outlet_code (default KLANG; joins POS S-/D- prefix)")
    ap.add_argument("--dates", default=",".join(DEFAULT_DATES),
                    help="comma-separated business dates (default 2026-06-23,2026-06-24)")
    args = ap.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]

    client = _build_client()
    print_outlet_resolution(client)
    for d in dates:
        report(client, args.outlet, d)
    print("READ-ONLY: no rows were written.")


if __name__ == "__main__":
    main()
