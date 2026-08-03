"""Turn an OCR'd invoice into ``receipt_items`` rows.

One row per printed line, canonicalised with ``item_canonicalization_v2``
(34 categories / 205 variations — the same map the price-history layer uses,
so a wastage report and a price chart always agree on what "ayam" is) and
converted to a base quantity through ``uom_conversion``.

The module's whole contract is: store what was printed, convert only when a
real conversion row exists, and flag everything else. Specifically —

* no matching conversion row  -> ``qty_base = NULL``, ``needs_review = true``
* invoice fails the subtotal check -> ``qty_base`` withheld for EVERY line,
  ``needs_review = true`` (see ``receipt_validation``)
* ``qty × unit_price != line_total`` -> that line flagged
* unknown item (no canonical) -> stored with ``canonical_item = NULL``, not
  dropped: the money is real even when the category isn't recognised yet.

Failures never raise into the receipt pipeline — :func:`save_receipt_items`
returns 0 and logs.
"""
from __future__ import annotations

import logging
from typing import Any

from item_canonicalization_v2 import canonicalize_item
from receipt_validation import check_line_arithmetic, check_subtotal
from uom_conversion import convert_to_base

logger = logging.getLogger(__name__)

RECEIPT_ITEMS_TABLE = "receipt_items"

#: Confidence floor when the receipt itself carried no score.
DEFAULT_CONFIDENCE = 50.0

# Per-line confidence penalties, subtracted from the receipt-level score.
PENALTY_MISSING_QTY = 15
PENALTY_MISSING_UNIT = 15
PENALTY_MISSING_UNIT_PRICE = 10
PENALTY_MISSING_LINE_TOTAL = 10
PENALTY_NO_CANONICAL = 10
PENALTY_NO_CONVERSION = 15
PENALTY_LINE_ARITHMETIC = 25
PENALTY_SUBTOTAL_MISMATCH = 30


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def line_confidence(
    base: Any,
    *,
    has_qty: bool,
    has_unit: bool,
    has_unit_price: bool,
    has_line_total: bool,
    has_canonical: bool,
    has_conversion: bool,
    arithmetic_bad: bool,
    subtotal_bad: bool,
) -> float:
    """Score one line 0-100, starting from the receipt-level confidence.

    Same scale as ``receipts.confidence`` so the two are comparable at a
    glance. Each missing or contradictory field docks a fixed amount — the
    weights are ordered by how much they hurt a wastage number: a line with no
    unit or no conversion cannot contribute a quantity at all, while a missing
    unit_price only costs you the per-kg average.
    """
    score = float(base) if _is_number(base) else DEFAULT_CONFIDENCE
    if not has_qty:
        score -= PENALTY_MISSING_QTY
    if not has_unit:
        score -= PENALTY_MISSING_UNIT
    if not has_unit_price:
        score -= PENALTY_MISSING_UNIT_PRICE
    if not has_line_total:
        score -= PENALTY_MISSING_LINE_TOTAL
    if not has_canonical:
        score -= PENALTY_NO_CANONICAL
    if not has_conversion:
        score -= PENALTY_NO_CONVERSION
    if arithmetic_bad:
        score -= PENALTY_LINE_ARITHMETIC
    if subtotal_bad:
        score -= PENALTY_SUBTOTAL_MISMATCH
    return _clamp(score)


def build_item_rows(
    parsed_invoice: dict,
    *,
    receipt_id: Any,
    outlet: Any,
    conversions: list[dict],
    invoice_date: Any = None,
    supplier: Any = None,
    base_confidence: Any = None,
    subtotal_check: dict | None = None,
) -> list[dict]:
    """Build the ``receipt_items`` rows for one parsed invoice.

    ``parsed_invoice`` is :func:`invoice_ocr.parse_line_items_response` output.
    ``invoice_date`` / ``supplier`` override the invoice's own values — the
    caller has the reconciled receipt record and should win over a second OCR
    pass that may have read the header differently.

    ``subtotal_check`` is :func:`receipt_validation.check_subtotal` output. Pass
    it in when the caller has already run the check; otherwise it is run here.
    When it fails, NO line gets a ``qty_base`` — an invoice that doesn't add up
    has an unknown error in an unknown row, so trusting any single quantity
    from it would be guessing which row is fine.

    Returns ``[]`` for an invoice with no usable lines. Never raises.
    """
    if not isinstance(parsed_invoice, dict):
        return []
    lines = parsed_invoice.get("lines")
    if not isinstance(lines, list) or not lines:
        return []

    if subtotal_check is None:
        subtotal_check = check_subtotal(lines, parsed_invoice.get("subtotal"))
    subtotal_bad = bool(subtotal_check.get("needs_review"))

    resolved_supplier = supplier if supplier is not None else parsed_invoice.get("supplier")
    resolved_date = invoice_date if invoice_date is not None else parsed_invoice.get("invoice_date")

    rows: list[dict] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        raw_item = line.get("item")
        if not isinstance(raw_item, str) or not raw_item.strip():
            continue
        raw_item = raw_item.strip()

        qty = line.get("qty") if _is_number(line.get("qty")) else None
        unit_raw = line.get("unit") if isinstance(line.get("unit"), str) else None
        unit_raw = unit_raw.strip() if unit_raw else None
        unit_price = line.get("unit_price") if _is_number(line.get("unit_price")) else None
        line_total = line.get("line_total") if _is_number(line.get("line_total")) else None

        canonical = canonicalize_item(raw_item).get("canonical")

        qty_base, base_unit = convert_to_base(
            qty,
            unit_raw,
            conversions=conversions,
            supplier=resolved_supplier,
            canonical_item=canonical,
        )
        has_conversion = qty_base is not None

        # An invoice that doesn't reconcile contributes no quantities at all.
        if subtotal_bad:
            qty_base, base_unit = None, None

        arithmetic_bad = check_line_arithmetic(line)
        needs_review = subtotal_bad or arithmetic_bad or qty_base is None

        rows.append({
            "receipt_id": receipt_id,
            "outlet": outlet,
            "invoice_date": resolved_date,
            "supplier": resolved_supplier,
            "line_no": line.get("line_no"),
            "raw_item_text": raw_item,
            "qty": qty,
            "unit_raw": unit_raw,
            "unit_price": unit_price,
            "line_total": line_total,
            "canonical_item": canonical,
            "qty_base": qty_base,
            "base_unit": base_unit,
            "confidence": line_confidence(
                base_confidence,
                has_qty=qty is not None,
                has_unit=bool(unit_raw),
                has_unit_price=unit_price is not None,
                has_line_total=line_total is not None,
                has_canonical=canonical is not None,
                has_conversion=has_conversion,
                arithmetic_bad=arithmetic_bad,
                subtotal_bad=subtotal_bad,
            ),
            "needs_review": needs_review,
        })

    return rows


def unconvertible_units(rows: list[dict]) -> list[str]:
    """Distinct printed units that produced no conversion, for the ops alert.

    These are the units an owner needs to add to ``uom_conversion`` before the
    wastage report can count them.
    """
    seen: list[str] = []
    for row in rows or []:
        if row.get("qty_base") is not None:
            continue
        unit = row.get("unit_raw")
        if not unit:
            continue
        key = unit.strip().upper()
        if key and key not in seen:
            seen.append(key)
    return seen


def save_receipt_items(supabase_client, rows: list[dict]) -> int:
    """Upsert ``rows`` into ``receipt_items``. Returns the row count written.

    Upsert (not insert) on ``(receipt_id, line_no)`` so re-processing a receipt
    corrects its lines instead of duplicating them. Returns 0 and logs on any
    failure — the receipt is already saved and must not be rolled back by a
    line-item problem.
    """
    if not rows:
        return 0
    try:
        result = (
            supabase_client.table(RECEIPT_ITEMS_TABLE)
            .upsert(rows, on_conflict="receipt_id,line_no")
            .execute()
        )
    except Exception:
        logger.exception(
            "save_receipt_items: upsert failed (receipt_id=%s, rows=%d)",
            rows[0].get("receipt_id"),
            len(rows),
        )
        return 0
    written = len(getattr(result, "data", None) or [])
    flagged = sum(1 for r in rows if r.get("needs_review"))
    logger.info(
        "save_receipt_items: wrote %d line(s) for receipt %s (%d need review)",
        written or len(rows), rows[0].get("receipt_id"), flagged,
    )
    return written
