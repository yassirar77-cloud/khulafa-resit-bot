"""Extract per-item price records from a parsed receipt and persist them.

This is a passive data-collection layer: every receipt with usable
``(qty, price)`` line items contributes rows to the ``item_prices`` table
so PR #24 (price-spike detection) has a corpus to compare against.

Pipeline:
    normalize_items(...)              # in items_utils
        -> classify_and_extract_items # here: filter + canonicalize + line_total
        -> save_item_prices           # here: batch insert into Supabase

Issue #79: ``save_item_prices`` now runs every row through the
``price_sanity`` gate before insert — implausible qty/price/date rows
(OCR column merges, future dates, orders-of-magnitude history outliers)
are diverted to the ``item_price_quarantine`` table instead of poisoning
the corpus. Failures here MUST NOT crash the receipt pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

from item_canonicalization_v2 import canonicalize_item
import price_sanity

logger = logging.getLogger(__name__)

_ITEM_PRICES_TABLE = "item_prices"
_QUARANTINE_TABLE = "item_price_quarantine"


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


def quarantine_rows(supabase_client, rows: list[dict]) -> int:
    """Insert reject rows into ``item_price_quarantine``.

    Each row should already carry the receipt context plus ``reasons``
    (comma-separated codes) and ``source``. Returns the count inserted;
    never raises — a quarantine failure must not block the pipeline
    (the reject is still fully logged by the caller).
    """
    if not rows:
        return 0
    try:
        result = supabase_client.table(_QUARANTINE_TABLE).insert(rows).execute()
    except Exception:
        logger.exception(
            "quarantine_rows: insert failed (rows=%d) — rejects are in the log only",
            len(rows),
        )
        return 0
    return len(result.data) if getattr(result, "data", None) else 0


def save_item_prices(
    supabase_client,
    receipt_id,
    receipt_date,
    outlet_code,
    chat_id,
    merchant,
    price_records: list[dict],
    receipt_total=None,
    check_history: bool = True,
) -> int:
    """Batch-insert ``price_records`` into the ``item_prices`` table.

    Issue #79: every record passes through the ``price_sanity`` gate
    first. Implausible rows (impossible qty/price, future receipt_date,
    orders-of-magnitude history outliers) go to ``item_price_quarantine``
    with their reject reasons instead of entering the corpus.
    ``receipt_total`` (when the caller has it) powers the
    line-total-vs-receipt-total check; ``check_history=False`` skips the
    per-item history queries (used by callers that already hold history).

    Returns the count of rows inserted into ``item_prices`` (0 on any
    failure or empty input). Never raises — logs the traceback on insert
    failure so the caller (the receipt pipeline) can proceed without
    interruption.
    """
    if not price_records:
        logger.warning(
            "save_item_prices: no valid items to save (receipt_id=%s)",
            receipt_id,
        )
        return 0

    # Hotfix: drop records with no canonical_item — they can't be compared
    # against historical averages and previously crashed the insert when
    # the item_prices table had canonical_item NOT NULL. Log the dropped
    # raw names so we can tune item_canonicalization_v2 over time.
    usable: list[dict] = []
    skipped: list[str] = []
    for rec in price_records:
        if rec.get("canonical_item") is None:
            skipped.append(str(rec.get("raw_item_name") or "?"))
            continue
        usable.append(rec)

    if skipped:
        logger.warning(
            "save_item_prices: skipping %d row(s) with null canonical_item "
            "(receipt_id=%s, raw_names=%s)",
            len(skipped),
            receipt_id,
            skipped[:10],
        )

    if not usable:
        return 0

    # === Issue #79 sanity gate ===
    history_by_item: dict = {}
    if check_history:
        history_by_item = price_sanity.fetch_history_stats(
            supabase_client,
            [rec.get("canonical_item") for rec in usable],
        )
    usable, rejected = price_sanity.partition_records(
        usable,
        receipt_date=receipt_date,
        receipt_total=receipt_total,
        history_by_item=history_by_item,
    )
    if rejected:
        for rec, reasons in rejected:
            logger.warning(
                "save_item_prices: QUARANTINED row (receipt_id=%s, item=%r, "
                "qty=%s, unit_price=%s, line_total=%s, reasons=%s)",
                receipt_id,
                rec.get("raw_item_name"),
                rec.get("qty"),
                rec.get("unit_price"),
                rec.get("line_total"),
                ",".join(reasons),
            )
        quarantine_rows(
            supabase_client,
            [
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
                    "reasons": ",".join(reasons),
                    "source": "ingest",
                }
                for rec, reasons in rejected
            ],
        )

    if not usable:
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
        for rec in usable
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
