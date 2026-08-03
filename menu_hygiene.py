"""Menu hygiene: duplicate SKUs, dead SKUs, and open-price/FOC leakage surface.

Cheap to compute from ``monthly_itemwise`` alone, and each finding is a real
operational problem:

* **Duplicate SKUs** — two buttons for one dish, differing only by a leading
  space, an underscore, or a double space. The May Damansara fixture really does
  contain ``" Nasi Goreng Daging"``, ``" Tambah_nasi"`` and ``"Barli  Panas"``.
  Every duplicate splits a dish's sales across two lines, so both the top-seller
  ranking and the wastage theoretical usage are understated.
* **Dead SKUs** — buttons sold 5 times or fewer in a whole month. They lengthen
  the till screen and are where mis-taps go.
* **Open-price / FOC buttons** — an amount typed by hand or a giveaway. Not
  wrong in themselves, but they are the leakage surface: the one place a
  cashier can choose the number.

Duplicate detection deliberately normalises ONLY whitespace, underscores and
case. It does not stem, fuzzy-match or edit-distance: "Teh Ais" and "Teh Ais
Special" are different products, and a hygiene report that merges them would be
telling the owner to delete a live SKU.

Pure functions; no I/O.
"""
from __future__ import annotations

import re
from typing import Any

#: Sold this many times or fewer in the period -> dead.
DEAD_SKU_MAX_QTY = 5.0

#: Buttons where the cashier picks the price or gives product away.
LEAKAGE_RE = re.compile(r"open|foc|jumbo|discount", re.IGNORECASE)

_UNDERSCORE_RE = re.compile(r"_+")
_WS_RE = re.compile(r"\s+")


def normalize_sku(name: Any) -> str:
    """Fold a SKU to its comparison key.

    Underscores become spaces, whitespace runs collapse, the result is trimmed
    and upper-cased. Two names with the same key differ only in the ways the POS
    lets a typo hide: ``" Tambah_nasi"``, ``"Tambah nasi"`` and
    ``"Tambah  Nasi "`` all fold to ``"TAMBAH NASI"``.
    """
    if not isinstance(name, str):
        return ""
    folded = _UNDERSCORE_RE.sub(" ", name)
    return _WS_RE.sub(" ", folded).strip().upper()


def _display(name: Any) -> str:
    """Render a name so invisible differences are visible in the sheet."""
    text = "" if name is None else str(name)
    if text != text.strip() or "  " in text or "_" in text:
        return f"«{text}»"
    return text


def find_duplicate_skus(rows: list[dict]) -> list[dict]:
    """Group itemwise rows whose names differ only by whitespace/underscore/case.

    Returns one entry per COLLIDING group (2+ distinct raw spellings), each::

        {"key", "variants": [{"item_name", "display", "qty", "amount"}…],
         "total_qty", "total_amount", "variant_count"}

    Sorted by combined amount descending — the duplicate splitting the most
    money is the one worth merging first.
    """
    groups: dict[str, dict[str, dict]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("item_name")
        if not isinstance(raw, str) or not raw.strip():
            continue
        key = normalize_sku(raw)
        if not key:
            continue
        bucket = groups.setdefault(key, {})
        variant = bucket.setdefault(raw, {"item_name": raw, "display": _display(raw),
                                          "qty": 0.0, "amount": 0.0})
        variant["qty"] += float(row.get("qty") or 0)
        variant["amount"] += float(row.get("amount") or 0)

    out = []
    for key, variants in groups.items():
        if len(variants) < 2:
            continue
        listed = sorted(variants.values(), key=lambda v: -v["amount"])
        for v in listed:
            v["qty"] = round(v["qty"], 2)
            v["amount"] = round(v["amount"], 2)
        out.append({
            "key": key,
            "variants": listed,
            "variant_count": len(listed),
            "total_qty": round(sum(v["qty"] for v in listed), 2),
            "total_amount": round(sum(v["amount"] for v in listed), 2),
        })
    out.sort(key=lambda g: (-g["total_amount"], g["key"]))
    return out


def find_dead_skus(rows: list[dict], *, max_qty: float = DEAD_SKU_MAX_QTY) -> list[dict]:
    """SKUs whose whole-period quantity is ``<= max_qty``.

    Quantities are summed per raw name first, so a SKU sold 3 times on two lines
    counts as 6 and is not reported dead.
    """
    totals: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("item_name")
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = totals.setdefault(raw, {"item_name": raw, "display": _display(raw),
                                        "qty": 0.0, "amount": 0.0})
        entry["qty"] += float(row.get("qty") or 0)
        entry["amount"] += float(row.get("amount") or 0)
    dead = [
        {**e, "qty": round(e["qty"], 2), "amount": round(e["amount"], 2)}
        for e in totals.values() if e["qty"] <= max_qty
    ]
    dead.sort(key=lambda e: (e["qty"], e["item_name"]))
    return dead


def find_leakage_skus(rows: list[dict]) -> list[dict]:
    """Open-price / FOC / jumbo / discount buttons, with txn count and RM."""
    totals: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("item_name")
        if not isinstance(raw, str) or not LEAKAGE_RE.search(raw):
            continue
        entry = totals.setdefault(raw, {"item_name": raw, "display": _display(raw),
                                        "qty": 0.0, "amount": 0.0})
        entry["qty"] += float(row.get("qty") or 0)
        entry["amount"] += float(row.get("amount") or 0)
    out = [
        {**e, "qty": round(e["qty"], 2), "amount": round(e["amount"], 2)}
        for e in totals.values()
    ]
    out.sort(key=lambda e: -e["amount"])
    return out


def build_menu_hygiene(rows: list[dict], *, max_dead_qty: float = DEAD_SKU_MAX_QTY) -> dict:
    """Full hygiene report for one outlet-period.

    Returns duplicates / dead / leakage lists plus headline counts and the RM
    at stake in each category.
    """
    duplicates = find_duplicate_skus(rows)
    dead = find_dead_skus(rows, max_qty=max_dead_qty)
    leakage = find_leakage_skus(rows)
    distinct = len({r.get("item_name") for r in (rows or [])
                    if isinstance(r, dict) and isinstance(r.get("item_name"), str)})
    return {
        "duplicates": duplicates,
        "dead": dead,
        "leakage": leakage,
        "duplicate_count": len(duplicates),
        "dead_count": len(dead),
        "leakage_count": len(leakage),
        "distinct_skus": distinct,
        "duplicate_amount": round(sum(g["total_amount"] for g in duplicates), 2),
        "dead_amount": round(sum(e["amount"] for e in dead), 2),
        "leakage_amount": round(sum(e["amount"] for e in leakage), 2),
        "leakage_txns": round(sum(e["qty"] for e in leakage), 2),
    }
