"""Is an outlet's order draft trustworthy enough to show the cashier?

A draft is only as good as the buying history behind it. Klang's first draft
after the #125 re-tag said "Ayam 2kg" — the only "ayam" rows were Knorr
chicken stock and Maggi Mee Ayam, bought on two days. Damansara (no bills
since June) got 2 lines and Sungai Besi 4. Showing those as "tomorrow's
order, OK?" invites a cashier to confirm nonsense.

The rule, per outlet (all must hold, else no draft — the cashier is simply
asked "Esok nak order apa?"):

  * bills uploaded on at least ``MIN_DAYS_60`` separate days in the last
    60 days, and ``MIN_DAYS_28`` in the last 28 (history is recent);
  * at least ``MIN_ITEMS_60`` different items bought in the last 60 days;
  * at least ``MIN_LINES`` draft lines survive the item rule below.

And per item, for a line to be mentioned at all: bought on at least
``MIN_ITEM_DAYS`` separate days in the last 60, and not flagged
NEEDS_REVIEW by the cadence model.
"""
from __future__ import annotations

from datetime import date, timedelta

MIN_DAYS_60 = 20
MIN_DAYS_28 = 8
MIN_ITEMS_60 = 8
MIN_LINES = 5
MIN_ITEM_DAYS = 4


def history_from_rows(rows, today: date) -> dict:
    """Buying-history summary from item_prices rows (receipt_date,
    canonical_item). Rows dated in the future (OCR misreads) are ignored."""
    days_60, days_28 = set(), set()
    item_days: dict[str, set] = {}
    start_60, start_28 = today - timedelta(days=60), today - timedelta(days=28)
    for r in rows or []:
        raw = str(r.get("receipt_date") or "")[:10]
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        if d > today or d < start_60:
            continue
        days_60.add(d)
        if d >= start_28:
            days_28.add(d)
        item = r.get("canonical_item")
        if item:
            item_days.setdefault(item, set()).add(d)
    return {
        "days_60": len(days_60),
        "days_28": len(days_28),
        "items_60": len(item_days),
        "item_days": {k: len(v) for k, v in item_days.items()},
    }


def fetch_history(supabase, codes, today: date) -> dict:
    rows = (
        supabase.table("item_prices")
        .select("receipt_date, canonical_item")
        .in_("outlet_code", list(codes))
        .gte("receipt_date", (today - timedelta(days=60)).isoformat())
        .lte("receipt_date", today.isoformat())
        .limit(10000)
        .execute().data or []
    )
    return history_from_rows(rows, today)


def _needs_review(line) -> bool:
    if line.get("needs_review"):
        return True
    return "NEEDS_REVIEW" in str(line.get("flags") or "").upper()


def assess(history: dict, lines) -> dict:
    """``{"ok", "reason", "lines"}`` — ``lines`` are the draft lines fit to
    mention (enough history, not NEEDS_REVIEW)."""
    item_days = history.get("item_days") or {}
    kept = [
        ln for ln in lines or []
        if ln.get("item") and ln.get("qty")
        and item_days.get(ln["item"], 0) >= MIN_ITEM_DAYS
        and not _needs_review(ln)
    ]
    if history.get("days_60", 0) < MIN_DAYS_60:
        reason = f"bills on {history.get('days_60', 0)} days in 60 (need {MIN_DAYS_60})"
    elif history.get("days_28", 0) < MIN_DAYS_28:
        reason = f"bills on {history.get('days_28', 0)} days in 28 (need {MIN_DAYS_28})"
    elif history.get("items_60", 0) < MIN_ITEMS_60:
        reason = f"{history.get('items_60', 0)} items bought in 60 days (need {MIN_ITEMS_60})"
    elif len(kept) < MIN_LINES:
        reason = f"{len(kept)} reliable draft lines (need {MIN_LINES})"
    else:
        return {"ok": True, "reason": "", "lines": kept}
    return {"ok": False, "reason": reason, "lines": kept}
