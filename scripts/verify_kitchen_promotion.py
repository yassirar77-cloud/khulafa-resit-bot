#!/usr/bin/env python3
"""Verify kitchen_daily_usage promotion against the LIVE Supabase table.

The Telegram /kitchen_post_now tap-through is the end-user check, but this script
exercises the EXACT promotion code path (kitchen_usage._upsert_usage_row, the
same call the Hantar handler makes) directly against the real table — so it
confirms the table is writable and surfaces the precise error if not, without
needing the Telegram round-trip.

It writes one harmless probe row (a fake outlet/date that can't collide with real
data), reads it back to confirm cooked_qty landed and used_qty/left_qty are NULL,
then deletes it. Read-only to your real outlets.

Run on Render (or locally with the prod env vars):
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/verify_kitchen_promotion.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kitchen_usage as ku  # noqa: E402

# A probe key that cannot collide with a real outlet/day.
PROBE_OUTLET = "VERIFY_PROBE"
PROBE_DATE = "2000-01-01"
PROBE_ITEM = "verify_probe_item"


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _cleanup(client):
    try:
        client.table(ku.USAGE_TABLE).delete().eq("outlet_code", PROBE_OUTLET).eq(
            "business_date", PROBE_DATE
        ).eq("item_code", PROBE_ITEM).execute()
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        print(f"  (cleanup warning: {exc})")


def main() -> int:
    client = _build_client()

    print(f"Probing {ku.USAGE_TABLE} promotion via kitchen_usage._upsert_usage_row ...")
    row = {
        "outlet_code": PROBE_OUTLET,
        "business_date": PROBE_DATE,
        "item_code": PROBE_ITEM,
        "item_label": "Verify Probe",
        "unit": "pcs",
        "cooked_qty": 7,
        "cooked_by": "verify_script",
        "cooked_at": "2000-01-01T18:00:00+08:00",
    }

    # Start clean in case a previous run left a probe row.
    _cleanup(client)

    try:
        ku._upsert_usage_row(client, row)
    except Exception as exc:
        print("\n❌ PROMOTION FAILED — the Hantar handler would hit this same error:")
        print(f"   {type(exc).__name__}: {exc}")
        if ku._is_missing_table_error(exc):
            print("\n   -> kitchen_daily_usage is not in the PostgREST schema cache.")
            print("      Apply migration 0032 and run:  NOTIFY pgrst, 'reload schema';")
        else:
            print("\n   -> The native ON CONFLICT upsert AND the manual fallback both failed.")
            print("      Check the table's columns/types against migration 0032 and that")
            print("      the kitchen_daily_usage row is insertable (column names/types).")
        return 1

    # Read it back.
    res = (
        client.table(ku.USAGE_TABLE)
        .select("outlet_code, business_date, item_code, cooked_qty, left_qty, used_qty")
        .eq("outlet_code", PROBE_OUTLET)
        .eq("business_date", PROBE_DATE)
        .eq("item_code", PROBE_ITEM)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        print("\n❌ Wrote without error but the row is not readable back — investigate RLS/policies.")
        _cleanup(client)
        return 1

    r = rows[0]
    print("\n✅ PROMOTION OK — row written and read back:")
    print(f"   cooked_qty = {r.get('cooked_qty')}  (expected 7)")
    print(f"   left_qty   = {r.get('left_qty')}  (expected None/NULL for a COOKED write)")
    print(f"   used_qty   = {r.get('used_qty')}  (expected None/NULL until LEFT is entered)")

    ok = r.get("cooked_qty") in (7, 7.0) and r.get("left_qty") is None and r.get("used_qty") is None
    _cleanup(client)
    print("   (probe row deleted)")

    if ok:
        print("\nThe live table accepts kitchen promotion. /kitchen_post_now will write rows.")
        return 0
    print("\n⚠️ Row landed but cooked_qty/left_qty/used_qty are not as expected — check the")
    print("   used_qty GENERATED expression and that cooked_qty/left_qty have NO DEFAULT.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
