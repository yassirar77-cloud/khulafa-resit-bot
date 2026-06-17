"""Standing orders — fixed daily staples that bypass OCR/forecast entirely.

roti / capati / gas come on handwritten-quantity receipts OCR can't read (the
Case-B verdict). They are fixed daily staples, so instead of forecasting we emit
a configured ``default_qty`` straight from the ``standing_orders`` table: no
cadence detection, no ``forecast_qty``, no NEEDS_REVIEW — just a clean
"Roti — 6 pack" line the manager can still edit before sending.

Split like the rest of the repo: the DB fetch takes the ``supabase`` client and
is best-effort (never crashes a draft run); the line builder is pure.
"""
from __future__ import annotations

import logging

import order_items

logger = logging.getLogger(__name__)

_TABLE = "standing_orders"


def fetch_standing_orders(supabase) -> list[dict]:
    """All ACTIVE standing-order rows. Returns ``[]`` on any failure — a draft
    run must never crash because the config table is missing or unreachable."""
    try:
        resp = (
            supabase.table(_TABLE)
            .select("outlet, supplier, item, default_qty, unit, cadence, active")
            .execute()
        )
        rows = resp.data or []
    except Exception:
        logger.exception("standing_orders: fetch failed")
        return []
    # Treat a missing/null ``active`` as active (column defaults true); only an
    # explicit False pauses the row.
    return [r for r in rows if r.get("active", True) is not False]


def group_by_outlet(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by outlet code. Rows missing an outlet, item, or a usable
    positive default_qty are dropped (a standing order with no quantity is not a
    standing order)."""
    out: dict[str, list[dict]] = {}
    for r in rows or []:
        outlet = (r.get("outlet") or "").strip()
        item = (r.get("item") or "").strip().lower()
        qty = r.get("default_qty")
        if not outlet or not item:
            continue
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            continue
        out.setdefault(outlet, []).append({
            "item": item,
            "supplier": r.get("supplier"),
            "default_qty": qty,
            "unit": (r.get("unit") or "").strip() or None,
            "cadence": (r.get("cadence") or "DAILY").strip() or "DAILY",
        })
    return out


def build_standing_line(row: dict) -> dict:
    """A draft line for one standing order, shaped for ``order_draft`` formatting
    and ``order_generator.persist_drafts``. Marked ``standing`` so the formatter
    renders it clean — no cadence tag, no flags, no forecast reasoning."""
    item = (row.get("item") or "").strip().lower()
    unit = row.get("unit") or order_items.unit_noun(item)
    return {
        "canonical_item": item,
        "qty": row.get("default_qty"),
        "pack": unit,
        "pack_known": True,            # config-provided, so never "confirm pack size"
        "standing": True,
        "supplier": row.get("supplier"),
        # Minimal cadence_info so persist_drafts records the cadence label and
        # never trips needs_review for a standing order.
        "cadence_info": {"cadence": row.get("cadence") or "DAILY",
                         "needs_review": False},
        "due_info": {"due": True, "reason": "standing order"},
        "alternate": None,
        "spike": None,
        "history_expired": False,
        "qty_anomaly": False,
        "date_corrected_count": 0,
    }
