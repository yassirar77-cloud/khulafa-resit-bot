#!/usr/bin/env python3
"""READ-ONLY check of the kitchen Used-vs-POS mapping against LIVE sales data.

Pulls the real ``sales_daily_summary`` + ``sales_daily_itemwise`` rows for an
outlet/date, runs the EXACT shipped mapping (``kitchen_usage`` —
``normalize_outlet_code``, ``ITEM_POS_KEYWORDS``, ``_pos_dish_matches``,
``_pos_dish_excluded``, ``pos_qty_for_item``) and prints, per kitchen item:
  * every matched POS dish name + its qty, and the rolled-up total
  * the kg-equivalent for Kambing/Daging (portions x locked grams)
then prints:
  * a category cross-check — computed protein count vs the POS category header
    total (AYAM / KAMBING / DAGING), with matched + excluded = header so nothing
    is silently lost
  * the explicit EXCLUDED list (isi ayam / Thai / staff / no-style) with the
    reason each dish was dropped

NOTHING is written. Only ``.select()`` queries are issued — safe to run on the
live database. The mapping logic is imported, never re-implemented, so what this
prints is what the bot computes.

Run on the Render shell (or locally with the prod env vars)::

    SUPABASE_URL=... SUPABASE_KEY=... python scripts/verify_kitchen_pos.py
    # custom outlet / dates:
    python scripts/verify_kitchen_pos.py --outlet KLANG --dates 2026-06-23,2026-06-24
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitchen_usage as ku  # noqa: E402

# Default to the outlet the owner cross-checks (kitchen "KLANG" joins POS
# S-KLANG / D-KLANG) over the two business days from the brief.
DEFAULT_OUTLET = "KLANG"
DEFAULT_DATES = ["2026-06-23", "2026-06-24"]


def _build_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_KEY (read-only).")
    return create_client(url, key)


def _rows(resp):
    return getattr(resp, "data", None) or []


def _summaries_for_date(client, business_date):
    """READ-ONLY: every sales_daily_summary row for one business_date."""
    return _rows(
        client.table(ku.SALES_SUMMARY_TABLE)
        .select("id, outlet_canonical, outlet_code, business_date")
        .eq("business_date", str(business_date))
        .execute()
    )


def _fetch_matched(client, outlet_code, business_date):
    """READ-ONLY: matched summaries + all their itemwise rows for one date.

    Mirrors ``kitchen_usage._fetch_itemwise`` join (outlet_join_keys intersect on
    both sides) and also returns ALL summaries for the date so the report can
    show exactly which POS rows were (or were not) joined."""
    target_keys = ku.outlet_join_keys(outlet_code)
    summaries = _summaries_for_date(client, business_date)
    matched = [
        s for s in summaries
        if (ku.outlet_join_keys(s.get("outlet_code")) & target_keys)
        or (ku.outlet_join_keys(s.get("outlet_canonical")) & target_keys)
    ]
    ids = [s["id"] for s in matched]
    items = []
    if ids:
        items = _rows(
            client.table(ku.SALES_ITEMWISE_TABLE)
            .select("item_name, qty, category, summary_id")
            .in_("summary_id", ids)
            .execute()
        )
    return target_keys, summaries, matched, items


def _scan_outlet_across_dates(client, outlet_code, limit=400):
    """READ-ONLY diagnostic: which business_dates DO have a summary for this
    outlet, so a 'no match' is shown to be a missing-date vs a join bug."""
    target_keys = ku.outlet_join_keys(outlet_code)
    rows = _rows(
        client.table(ku.SALES_SUMMARY_TABLE)
        .select("outlet_code, outlet_canonical, business_date")
        .order("business_date", desc=True)
        .limit(limit)
        .execute()
    )
    hits = [
        r for r in rows
        if (ku.outlet_join_keys(r.get("outlet_code")) & target_keys)
        or (ku.outlet_join_keys(r.get("outlet_canonical")) & target_keys)
    ]
    return hits


def _qty(row):
    try:
        return float(row.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _exclusion_reason(name_l, category, base):
    """Why a dish that CONTAINS a tracked base keyword was not counted, mirroring
    ``_pos_dish_excluded`` plus the no-style / no-phrase fall-through."""
    if "thai" in str(category or "").lower():
        return "THAI FOOD category"
    if ku._POS_STAFF_SUBSTR in name_l:
        return "staff meal"
    if base == "ayam":
        for sub in ku.AYAM_EXCLUDE_SUBSTRINGS:
            if sub in name_l:
                return f"Thai/noodle ayam ('{sub.strip()}')"
    return "isi ayam / no tracked style keyword"


def _classify(name, category):
    """Return (matched_item_codes, exclusion_reason_or_None, has_tracked_base).

    Uses the shipped matchers so classification == what pos_qty_for_item sums."""
    name_l = str(name or "").lower()
    matched = []
    reason = None
    has_base = False
    for code, spec in ku.ITEM_POS_KEYWORDS.items():
        base = spec["base"]
        if base not in name_l:
            continue
        has_base = True
        if ku._pos_dish_excluded(name_l, category, base):
            if reason is None:
                reason = _exclusion_reason(name_l, category, base)
            continue
        if ku._pos_dish_matches(spec, name_l):
            matched.append(code)
    if not matched and has_base and reason is None:
        # base present but no rule matched (plain isi ayam, etc.)
        any_base = next(
            ku.ITEM_POS_KEYWORDS[c]["base"]
            for c in ku.ITEM_POS_KEYWORDS
            if ku.ITEM_POS_KEYWORDS[c]["base"] in name_l
        )
        reason = _exclusion_reason(name_l, category, any_base)
    return matched, reason, has_base


def _fmt(n):
    return f"{n:g}"


def report(client, outlet_code, business_date):
    print("=" * 70)
    print(f"OUTLET {outlet_code!r}   business_date {business_date}")
    target_keys, all_summaries, matched, items = _fetch_matched(
        client, outlet_code, business_date
    )
    print(f"outlet_join_keys({outlet_code!r}) -> {sorted(target_keys)}")
    print(f"  {len(all_summaries)} total summary row(s) exist for {business_date}.")
    if not matched:
        print("  NO matching sales_daily_summary row for this outlet/date.\n")
        print("  --- DIAGNOSTIC: all summary rows present for this date ---")
        if not all_summaries:
            print("    (zero summary rows for this date — not ingested yet?)")
        for s in all_summaries:
            print(
                f"    outlet_code={s.get('outlet_code')!r:14} "
                f"outlet_canonical={s.get('outlet_canonical')!r:18} "
                f"-> keys {sorted(ku.outlet_join_keys(s.get('outlet_code')) | ku.outlet_join_keys(s.get('outlet_canonical')))}"
            )
        print(f"\n  --- DIAGNOSTIC: dates this outlet ({outlet_code!r}) DOES have summaries ---")
        hits = _scan_outlet_across_dates(client, outlet_code)
        if not hits:
            print("    (none found in the recent scan — outlet may use a different code)")
        for h in hits[:25]:
            print(
                f"    {h.get('business_date')}  outlet_code={h.get('outlet_code')!r} "
                f"outlet_canonical={h.get('outlet_canonical')!r}"
            )
        return
    for s in matched:
        print(
            f"  matched summary id={s['id']} "
            f"outlet_code={s.get('outlet_code')!r} "
            f"outlet_canonical={s.get('outlet_canonical')!r}"
        )
    print(f"  {len(items)} itemwise dish rows pulled.\n")

    # --- per kitchen item: matched dishes + total -----------------------------
    print("--- PER KITCHEN ITEM: matched POS dishes ---")
    item_codes = [c for c in ku.ITEM_POS_KEYWORDS]  # ordered as defined
    matched_by_dish = {}  # id(row) -> [codes]
    for code in item_codes:
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
            print(f"    + {r.get('item_name')!r:42} qty {_fmt(_qty(r))}  [{r.get('category')}]")
        if unit == "kg":
            grams = ku.KG_PORTION_GRAMS.get(code, 0.0)
            print(f"    = {_fmt(portions)} portions x {_fmt(grams)} g = {total_qty} kg")
        else:
            print(f"    = TOTAL {total_qty} pcs")

    # --- excluded dishes (carry a tracked base but dropped) -------------------
    print("\n--- EXCLUDED dishes (contain a protein word but NOT counted) ---")
    excluded_any = False
    for row in items:
        if id(row) in matched_by_dish:
            continue
        codes, reason, has_base = _classify(row.get("item_name"), row.get("category"))
        if codes:
            continue  # actually matched (shouldn't reach here)
        if not has_base:
            continue  # not a tracked protein at all (Nasi Putih, Roti, etc.)
        excluded_any = True
        print(
            f"  - {row.get('item_name')!r:42} qty {_fmt(_qty(row))} "
            f"[{row.get('category')}]  -> {reason}"
        )
    if not excluded_any:
        print("  (none)")

    # --- multi-match warning (a dish counted by >1 item) ----------------------
    dbl = {rid: cs for rid, cs in matched_by_dish.items() if len(cs) > 1}
    if dbl:
        print("\n  !! WARNING: dishes counted by MORE THAN ONE item:")
        for row in items:
            if id(row) in dbl:
                print(f"     {row.get('item_name')!r} -> {dbl[id(row)]}")

    # --- category header cross-check -----------------------------------------
    print("\n--- CATEGORY CROSS-CHECK (matched + excluded = POS header) ---")
    for cat_key, label in (("ayam", "AYAM"), ("kambing", "KAMBING"), ("daging", "DAGING")):
        cat_rows = [
            r for r in items
            if cat_key in str(r.get("category") or "").lower()
        ]
        header = sum(_qty(r) for r in cat_rows)
        matched_pcs = 0.0
        excluded_pcs = 0.0
        for r in cat_rows:
            if id(r) in matched_by_dish:
                matched_pcs += _qty(r)
            else:
                excluded_pcs += _qty(r)
        ok = abs((matched_pcs + excluded_pcs) - header) < 1e-6
        line = (
            f"  {label:8} header={_fmt(header)}  "
            f"matched={_fmt(matched_pcs)}  excluded={_fmt(excluded_pcs)}  "
            f"(matched+excluded={'OK' if ok else 'MISMATCH'})"
        )
        if cat_key in ("kambing", "daging"):
            grams = ku.KG_PORTION_GRAMS.get(cat_key, 0.0)
            line += f"  -> {round(matched_pcs * grams / 1000.0, 2)} kg"
        print(line)
    print()


ALL_KITCHEN_OUTLETS = ("BISTRO7", "SEK20", "SEK14", "SEK15", "SEK6",
                       "VISTA", "JAKEL", "D", "KLANG", "KLRAZAK")


def print_outlet_resolution(client=None):
    """Print, for all 10 kitchen outlets, the join keys they resolve to and the
    POS outlet_codes actually present in sales_daily_summary that join to them."""
    print("=" * 70)
    print("ALL 10 OUTLETS — kitchen code -> join keys (and live POS codes)")
    live = []
    if client is not None:
        live = _rows(
            client.table(ku.SALES_SUMMARY_TABLE)
            .select("outlet_code, outlet_canonical")
            .limit(2000)
            .execute()
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
