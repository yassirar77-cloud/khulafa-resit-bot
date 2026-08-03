"""Printed unit -> base unit conversion, table-driven and never guessed.

Malaysian supplier invoices print whatever unit the supplier feels like:
``KG``, ``EKOR`` (per animal), ``GUNI`` (sack), ``PAPAN`` (tray/board),
``CTN``, ``BTL``, ``PKT``. Only a handful of those have a factor that is a
definition rather than an estimate — a GUNI of beras is 25kg at one supplier
and 10kg at another, and a PAPAN of telur is 30 eggs until the day it isn't.

So the factor comes from the ``uom_conversion`` table and NOWHERE else. When
no row matches, the caller gets ``None`` and must store ``qty_base = NULL``
with ``needs_review = true``. There is deliberately no default, no fuzzy
match, and no "close enough" fallback: a wrong factor silently corrupts every
wastage number computed from it, while a missing one is visible and fixable.

Lookup order, most specific first:

    1. supplier + canonical_item + unit_raw
    2. canonical_item + unit_raw            (row's supplier IS NULL)
    3. unit_raw                             (row's supplier AND item IS NULL)
    4. no match -> None

Pure Python apart from :func:`load_conversions`, so the ordering rules are
unit-testable without a database.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

UOM_CONVERSION_TABLE = "uom_conversion"

#: The only base units ``receipt_items.base_unit`` accepts (mirrors the CHECK
#: constraint in migrations/0038_receipt_items_uom.sql).
BASE_UNITS = ("g", "ml", "pcs")

_WS_RE = re.compile(r"\s+")


def normalize_unit(unit: Any) -> str | None:
    """Fold a printed unit into its lookup key: upper-cased, trimmed, no dots.

    ``" kg. "``, ``"Kg"`` and ``"KG"`` are the same unit; ``"KG / EKOR"`` is
    not something we try to be clever about — it is normalised as-is and will
    simply fail to match, which is the honest outcome. Returns ``None`` for
    non-strings and for anything that is empty once cleaned.
    """
    if not isinstance(unit, str):
        return None
    cleaned = _WS_RE.sub(" ", unit.strip().upper()).strip(" .")
    return cleaned or None


def _normalize_scope(value: Any) -> str | None:
    """Fold a supplier / canonical_item scope value for comparison."""
    if not isinstance(value, str):
        return None
    cleaned = _WS_RE.sub(" ", value.strip().upper())
    return cleaned or None


def load_conversions(supabase_client) -> list[dict]:
    """Read every ``uom_conversion`` row. Returns ``[]`` on any failure.

    The table is tiny (tens of rows) and read once per receipt, so there is no
    pagination and no cache — a stale cache would keep converting with a factor
    an owner has just corrected, which is exactly the failure this module
    exists to prevent.
    """
    try:
        result = supabase_client.table(UOM_CONVERSION_TABLE).select("*").execute()
    except Exception:
        logger.exception("load_conversions: read failed; treating as no conversions")
        return []
    return list(getattr(result, "data", None) or [])


def _row_matches_unit(row: dict, unit_key: str) -> bool:
    return normalize_unit(row.get("unit_raw")) == unit_key


def _valid_factor(row: dict) -> float | None:
    """Return the row's factor as a positive float, or ``None`` if unusable."""
    factor = row.get("factor")
    if isinstance(factor, bool) or not isinstance(factor, (int, float, str)):
        return None
    try:
        value = float(factor)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def resolve_conversion(
    conversions: list[dict],
    supplier: Any,
    canonical_item: Any,
    unit_raw: Any,
) -> dict | None:
    """Find the conversion row for ``(supplier, canonical_item, unit_raw)``.

    Returns the matching row dict (with a validated ``factor``/``base_unit``)
    or ``None`` when nothing matches at any tier. Rows with a non-positive /
    unparseable factor or a base_unit outside :data:`BASE_UNITS` are skipped as
    if absent — a malformed row must not shadow a valid, less specific one.
    """
    unit_key = normalize_unit(unit_raw)
    if not unit_key or not isinstance(conversions, list):
        return None

    supplier_key = _normalize_scope(supplier)
    item_key = _normalize_scope(canonical_item)

    usable: list[dict] = []
    for row in conversions:
        if not isinstance(row, dict) or not _row_matches_unit(row, unit_key):
            continue
        if _valid_factor(row) is None:
            logger.warning(
                "uom_conversion: ignoring row id=%s unit=%r with unusable factor=%r",
                row.get("id"), row.get("unit_raw"), row.get("factor"),
            )
            continue
        if row.get("base_unit") not in BASE_UNITS:
            logger.warning(
                "uom_conversion: ignoring row id=%s unit=%r with bad base_unit=%r",
                row.get("id"), row.get("unit_raw"), row.get("base_unit"),
            )
            continue
        usable.append(row)

    if not usable:
        return None

    tiers = (
        # 1. supplier + item + unit
        lambda r: (
            supplier_key is not None
            and item_key is not None
            and _normalize_scope(r.get("supplier")) == supplier_key
            and _normalize_scope(r.get("canonical_item")) == item_key
        ),
        # 2. item + unit (generic across suppliers)
        lambda r: (
            item_key is not None
            and r.get("supplier") is None
            and _normalize_scope(r.get("canonical_item")) == item_key
        ),
        # 3. unit only (generic across suppliers and items)
        lambda r: r.get("supplier") is None and r.get("canonical_item") is None,
    )

    for matches in tiers:
        for row in usable:
            if matches(row):
                return row
    return None


def convert_to_base(
    qty: Any,
    unit_raw: Any,
    *,
    conversions: list[dict],
    supplier: Any = None,
    canonical_item: Any = None,
) -> tuple[float | None, str | None]:
    """Convert ``qty`` of ``unit_raw`` into ``(qty_base, base_unit)``.

    Returns ``(None, None)`` — meaning "store NULL and flag for review" — when
    the quantity is missing/non-numeric, the unit is missing, or no conversion
    row matches. Never falls back to a default factor.
    """
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        return (None, None)
    row = resolve_conversion(conversions, supplier, canonical_item, unit_raw)
    if row is None:
        return (None, None)
    factor = _valid_factor(row)
    if factor is None:  # pragma: no cover - resolve_conversion already filtered
        return (None, None)
    return (float(qty) * factor, row["base_unit"])
