"""Extract per-item price records from a parsed receipt and persist them.

This is a passive data-collection layer: every receipt with usable
``(qty, price)`` line items contributes rows to the ``item_prices`` table
so PR #24 (price-spike detection) has a corpus to compare against.

Pipeline:
    normalize_items(...)              # in items_utils
        -> classify_and_extract_items # here: filter + canonicalize + line_total
        -> save_item_prices           # here: batch insert into Supabase

There is no sanity threshold (RM0.001 and RM10000 both store). Filtering
of outliers is deferred to the consumer in PR #24, where it has access to
historical context. Failures here MUST NOT crash the receipt pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

from item_canonicalization_v2 import canonicalize_item

logger = logging.getLogger(__name__)

_ITEM_PRICES_TABLE = "item_prices"


def _is_numeric(value: Any) -> bool:
    # Reject bool first: ``True``/``False`` are ``int`` subclasses and we
    # don't want a stray ``qty=True`` to be treated as a real quantity.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def classify_and_extract_items(
    items: list[dict], receipt_total: float | None = None
) -> list[dict]:
    """Build per-item price records from a normalized items list.

    Each output dict has keys: ``raw_item_name``, ``canonical_item``,
    ``qty``, ``unit_price``, ``line_total``. ``unit_price`` is the
    ``price`` field as parsed (treated as per-unit, matching PR #23's
    embedded format ``"<name> xN RMX.XX"`` where the RM value is the
    unit price). ``line_total`` is ``qty * unit_price``.

    Items missing a numeric qty or price are silently dropped — they
    carry no usable signal for the price-history layer. Items missing
    or with blank ``name`` are also dropped. Returns ``[]`` for any
    non-list input. Never raises.

    ``receipt_total`` is accepted for forward-compatibility (PR #24 may
    use it for reconciliation) but is not consulted today.
    """
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        qty = item.get("qty")
        price = item.get("price")
        if not _is_numeric(qty) or not _is_numeric(price):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        qty_f = float(qty)
        price_f = float(price)
        canon = canonicalize_item(name).get("canonical")
        out.append({
            "raw_item_name": name,
            "canonical_item": canon,
            "qty": qty_f,
            "unit_price": price_f,
            "line_total": qty_f * price_f,
        })
    return out


def save_item_prices(
    supabase_client,
    receipt_id,
    receipt_date,
    outlet_code,
    chat_id,
    merchant,
    price_records: list[dict],
) -> int:
    """Batch-insert ``price_records`` into the ``item_prices`` table.

    Returns the count of rows inserted (0 on any failure or empty input).
    Never raises — logs the traceback on insert failure so the caller
    (the receipt pipeline) can proceed without interruption.
    """
    if not price_records:
        logger.warning(
            "save_item_prices: no valid items to save (receipt_id=%s)",
            receipt_id,
        )
        return 0

    rows = [
        {
            "receipt_id": receipt_id,
            "receipt_date": receipt_date,
            "outlet_code": outlet_code,
            "chat_id": chat_id,
            "merchant": merchant,
            "canonical_item": rec.get("canonical_item"),
            "raw_item_name": rec.get("raw_item_name"),
            "qty": rec.get("qty"),
            "unit_price": rec.get("unit_price"),
            "line_total": rec.get("line_total"),
        }
        for rec in price_records
    ]

    try:
        result = supabase_client.table(_ITEM_PRICES_TABLE).insert(rows).execute()
    except Exception:
        logger.exception(
            "save_item_prices: insert failed (receipt_id=%s, rows=%d)",
            receipt_id,
            len(rows),
        )
        return 0

    inserted = len(result.data) if getattr(result, "data", None) else 0
    return inserted
