#!/usr/bin/env python3
"""Audit: every distinct POS item name sold per outlet, and which kitchen lauk
bucket it counts toward (or UNMAPPED).

Reads sales_daily_itemwise (D-files) for the last N days, normalises each name
(pos_names.normalize_pos_name), flags typos / spacing variants, and writes a
Markdown report:

    python scripts/audit_pos_item_names.py --days 30 > docs/audits/pos_items.md
    python scripts/audit_pos_item_names.py --from-json dump.json   # offline

``--from-json`` takes a JSON list of {outlet, item_name, category, qty} rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitchen_usage as ku  # noqa: E402
from pos_names import name_issues, normalize_pos_name  # noqa: E402


def classify(name: str, category) -> str:
    """The kitchen item a POS name counts toward, an "(no button: X)" bucket,
    "(excluded: …)" or UNMAPPED."""
    n = normalize_pos_name(name)
    hits = [code for code in ku.ITEM_POS_KEYWORDS if ku._row_counts_for(code, n, category)]
    if hits:
        return "+".join(hits)
    bucket = ku.unmapped_ayam_counts([{"item_name": name, "category": category, "qty": 1}])
    if bucket:
        return f"(no button: {next(iter(bucket))})"
    if "thai" in str(category or "").lower():
        return "(excluded: THAI FOOD)"
    if ku._POS_STAFF_SUBSTR in n:
        return "(excluded: staff)"
    if "ayam" in n and any(x in n for x in ku.AYAM_EXCLUDE_SUBSTRINGS):
        return "(excluded: isi-ayam noodle/rice)"
    return "UNMAPPED"


def fetch_rows(days: int) -> list:
    from supabase import create_client

    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    since = (date.today() - timedelta(days=days)).isoformat()
    summaries = client.table("sales_daily_summary").select("id, outlet_code") \
        .gte("business_date", since).execute().data or []
    outlet_by_id = {s["id"]: s["outlet_code"] for s in summaries}
    rows, ids = [], list(outlet_by_id)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        start = 0
        while True:
            page = client.table("sales_daily_itemwise") \
                .select("summary_id, item_name, category, qty") \
                .in_("summary_id", chunk).range(start, start + 999).execute().data or []
            for r in page:
                rows.append({"outlet": outlet_by_id[r["summary_id"]], **r})
            if len(page) < 1000:
                break
            start += 1000
    return rows


def report(rows: list, days: int) -> str:
    by_name = defaultdict(lambda: {"outlets": set(), "qty": 0.0, "category": None})
    for r in rows:
        name = r["item_name"]
        e = by_name[name]
        e["outlets"].add(str(r["outlet"]).replace("D-", ""))
        e["qty"] += float(r.get("qty") or 0)
        e["category"] = e["category"] or r.get("category")
    variants = defaultdict(set)
    for name in by_name:
        variants[normalize_pos_name(name)].add(name)

    lines = [f"# POS item names → kitchen lauk bucket (last {days} days)", ""]
    # Summary first: protein dishes that count toward nothing, and fixed typos.
    protein = ("ayam", "chicken", "ikan", "fish", "kambing", "mutton", "lamb",
               "daging", "beef", "telur ikan", "65")
    loose = sorted(
        (n for n in by_name
         if classify(n, by_name[n]["category"]) == "UNMAPPED"
         and any(w in normalize_pos_name(n) for w in protein)),
        key=lambda n: -by_name[n]["qty"])
    lines += ["## Protein dishes that count toward NO kitchen item", "",
              "| POS name | Outlets | Qty |", "|---|---|---:|"]
    lines += [f"| `{n}` | {','.join(sorted(by_name[n]['outlets']))} | {by_name[n]['qty']:g} |"
              for n in loose]
    fixed = [n for n in sorted(by_name) if any(i.startswith(("typo", "'_'", "double", "leading"))
                                               for i in name_issues(n))]
    lines += ["", "## Names fixed by normalisation (typos, '_', double/leading spaces)", ""]
    lines += [f"- `{n}` → `{normalize_pos_name(n)}` ({'; '.join(name_issues(n))})" for n in fixed]
    lines += ["", "## Every name", "",
              "| POS name (raw) | Category | Outlets | Qty | Bucket | Name issues |",
              "|---|---|---|---:|---|---|"]
    for name in sorted(by_name, key=lambda x: (classify(x, by_name[x]["category"]), x.lower())):
        e = by_name[name]
        issues = name_issues(name)
        others = variants[normalize_pos_name(name)] - {name}
        if others:
            issues.append("variant of " + ", ".join(f"`{o}`" for o in sorted(others)))
        lines.append(
            f"| `{name}` | {e['category'] or ''} | {','.join(sorted(e['outlets']))} "
            f"| {e['qty']:g} | {classify(name, e['category'])} | {'; '.join(issues)} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--from-json")
    args = ap.parse_args()
    if args.from_json:
        with open(args.from_json) as f:
            rows = json.load(f)
    else:
        rows = fetch_rows(args.days)
    sys.stdout.write(report(rows, args.days))


if __name__ == "__main__":
    main()
