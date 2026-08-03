"""Monthly wastage: theoretical usage from POS sales vs actual purchases.

Theoretical usage comes from ``monthly_itemwise`` through the locked v12
framework in ``kitchen_usage`` — the same Thai-category exclusion, staff-meal
exclusion, ayam cut rules and portion sizes the daily kitchen comparison uses.
Reusing it matters: if the monthly report counted a dish the daily comparison
excludes, the two would disagree about the same kitchen and nobody could tell
which was right.

Actual purchases come from ``receipt_items`` where ``qty_base IS NOT NULL`` for
the same outlet and period.

    variance % = (purchased − theoretical) / theoretical
    > +15%        HIGH WASTAGE
    −5% … +15%    healthy
    < −5%         OVER-USED

Two situations produce no percentage at all, by design:

* **Unconvertible purchases.** If any of an ingredient's purchase rows is
  flagged or has no ``qty_base``, the variance is UNRELIABLE. The flagged RM is
  shown so the gap is visible, but no % is printed — a percentage computed over
  a partial numerator reads as precision that isn't there.
* **No locked portion size.** v12 locks grams-per-portion for kambing (180 g)
  and daging (60 g) only. Ayam and ikan are sold and logged as whole pieces;
  eggs and drinks have no portion rule in the repo at all. Where the units of
  sale and purchase cannot be reconciled from a LOCKED rule, the ingredient is
  reported NOT MODELLED rather than converted on an invented factor.

Pure functions apart from :func:`fetch_purchases`.
"""
from __future__ import annotations

import logging
from typing import Any

from kitchen_usage import (
    AYAM_EXCLUDE_SUBSTRINGS,
    ITEM_BY_CODE,
    ITEM_POS_KEYWORDS,
    KG_PORTION_GRAMS,
)

logger = logging.getLogger(__name__)

RECEIPT_ITEMS_TABLE = "receipt_items"

HIGH_WASTAGE_PCT = 15.0
OVER_USED_PCT = -5.0

VERDICT_HIGH = "HIGH WASTAGE"
VERDICT_HEALTHY = "healthy"
VERDICT_OVER_USED = "OVER-USED"
VERDICT_UNRELIABLE = "UNRELIABLE"
VERDICT_NOT_MODELLED = "NOT MODELLED"

#: v12 kitchen item code -> the ``receipt_items.canonical_item`` it is bought as.
#: Several POS cuts map to one purchased ingredient (all ayam cuts come from the
#: same bird), which is why theoretical usage is summed per canonical, not per cut.
ITEM_TO_CANONICAL: dict[str, str] = {
    "ayam_goreng": "ayam",
    "ayam_bawang": "ayam",
    "ayam_rempah": "ayam",
    "ayam_kicap": "ayam",
    "ayam_madu": "ayam",
    "ayam_tandoori": "ayam",
    "ikan_goreng": "ikan",
    "ikan_kari": "ikan",
    "kambing": "kambing",
    "daging": "daging",
}

#: The base unit each canonical's THEORETICAL usage is expressed in, derived
#: from the v12 rules: 'g' where a portion size is locked, 'pcs' where the item
#: is sold and logged by the piece. Anything absent here is NOT MODELLED.
def _theoretical_unit(item_code: str) -> str | None:
    unit = ITEM_BY_CODE.get(item_code, {}).get("unit")
    if unit == "kg":
        return "g" if KG_PORTION_GRAMS.get(item_code) else None
    if unit == "pcs":
        return "pcs"
    return None


def _dish_excluded(name: str, base: str) -> bool:
    """v12 exclusions, applied without a category column.

    ``monthly_itemwise`` has no category field, so the THAI FOOD category
    exclusion is applied by keyword on the dish name instead. The keyword list
    is the v12 one; staff meals and the ayam isi/rendang rules are unchanged.
    """
    if "staff" in name:
        return True
    if "thai" in name:
        return True
    if base == "ayam" and any(s in name for s in AYAM_EXCLUDE_SUBSTRINGS):
        return True
    return False


def _dish_matches(spec: dict, name: str) -> bool:
    if any(n in name for n in spec.get("not", ())):
        return False
    phrases = spec.get("phrases")
    if phrases is not None:
        return any(p in name for p in phrases)
    styles = spec.get("styles")
    if styles:
        return any(s in name for s in styles)
    return True


def theoretical_usage(itemwise_rows: list[dict]) -> dict[str, dict]:
    """Theoretical ingredient usage implied by a month of POS sales.

    Returns ``{canonical_item: {"qty", "unit", "dishes", "contributing"}}``.
    ``contributing`` lists the dish names that fed the number, so a surprising
    figure can be traced back to the buttons that produced it.

    A dish counted for two different cuts would double-count the bird, so each
    itemwise row contributes to at most one item code — the first match in v12
    order wins, and that choice is recorded.
    """
    out: dict[str, dict] = {}
    for row in itemwise_rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("item_name")
        if not isinstance(raw, str) or not raw.strip():
            continue
        name = raw.strip().lower()
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue

        for item_code, spec in ITEM_POS_KEYWORDS.items():
            base = spec["base"]
            if base not in name:
                continue
            if _dish_excluded(name, base):
                continue
            if not _dish_matches(spec, name):
                continue

            canonical = ITEM_TO_CANONICAL.get(item_code)
            unit = _theoretical_unit(item_code)
            if canonical is None or unit is None:
                break
            grams = KG_PORTION_GRAMS.get(item_code)
            contribution = qty * grams if (unit == "g" and grams) else qty

            entry = out.setdefault(
                canonical, {"qty": 0.0, "unit": unit, "dishes": 0, "contributing": []}
            )
            if entry["unit"] != unit:
                # A canonical whose cuts disagree on unit cannot be summed.
                entry["unit"] = "MIXED"
            entry["qty"] += contribution
            entry["dishes"] += qty
            if raw not in entry["contributing"]:
                entry["contributing"].append(raw)
            break

    for entry in out.values():
        entry["qty"] = round(entry["qty"], 2)
        entry["dishes"] = round(entry["dishes"], 2)
    return out


def fetch_purchases(supabase_client, outlet: str, start: str, end: str) -> list[dict]:
    """Read ``receipt_items`` for one outlet-period, including flagged rows.

    Flagged/unconvertible rows are deliberately INCLUDED: the wastage report has
    to know they exist to mark an ingredient UNRELIABLE. Filtering them out here
    would produce a confident-looking variance computed over whatever happened to
    convert.
    """
    from db_pagination import fetch_all_pages

    def make_query():
        return (
            supabase_client.table(RECEIPT_ITEMS_TABLE)
            .select("canonical_item,qty_base,base_unit,line_total,qty,unit_price,"
                    "unit_raw,needs_review,invoice_date,outlet,raw_item_text")
            .eq("outlet", outlet)
            .gte("invoice_date", start)
            .lte("invoice_date", end)
            .order("id")
        )

    return fetch_all_pages(make_query)


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def summarise_purchases(rows: list[dict]) -> dict[str, dict]:
    """Group purchase rows by canonical item, tracking what could not convert.

    Returns ``{canonical: {"qty_base", "unit", "cost", "flagged_rows",
    "flagged_cost", "units_seen"}}``.
    """
    from wastage_export import line_cost

    out: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        canonical = row.get("canonical_item")
        canonical = canonical.strip() if isinstance(canonical, str) and canonical.strip() else "UNCATEGORISED"
        entry = out.setdefault(canonical, {
            "qty_base": 0.0, "unit": None, "cost": 0.0,
            "flagged_rows": 0, "flagged_cost": 0.0, "units_seen": set(),
        })
        cost = line_cost(row) or 0.0
        entry["cost"] += cost
        qty_base = _to_float(row.get("qty_base"))
        base_unit = row.get("base_unit")
        if row.get("needs_review") or qty_base is None or not base_unit:
            entry["flagged_rows"] += 1
            entry["flagged_cost"] += cost
            continue
        entry["qty_base"] += qty_base
        entry["units_seen"].add(base_unit)
        entry["unit"] = base_unit if entry["unit"] in (None, base_unit) else "MIXED"

    for entry in out.values():
        entry["qty_base"] = round(entry["qty_base"], 2)
        entry["cost"] = round(entry["cost"], 2)
        entry["flagged_cost"] = round(entry["flagged_cost"], 2)
        entry["units_seen"] = sorted(entry["units_seen"])
    return out


def classify_variance(pct: float | None) -> str:
    if pct is None:
        return VERDICT_UNRELIABLE
    if pct > HIGH_WASTAGE_PCT:
        return VERDICT_HIGH
    if pct < OVER_USED_PCT:
        return VERDICT_OVER_USED
    return VERDICT_HEALTHY


def build_wastage(itemwise_rows: list[dict], purchase_rows: list[dict]) -> dict:
    """Compare theoretical usage against purchases, ingredient by ingredient.

    Returns ``{"rows": [...], "unreliable_count", "flagged_cost",
    "not_modelled": [...]}``. Each row carries theoretical qty, purchased qty,
    variance %, verdict and the reason when there is no %.
    """
    theoretical = theoretical_usage(itemwise_rows)
    purchased = summarise_purchases(purchase_rows)

    rows: list[dict] = []
    for canonical in sorted(set(theoretical) | set(purchased)):
        theory = theoretical.get(canonical)
        actual = purchased.get(canonical, {})
        flagged_rows = actual.get("flagged_rows", 0)
        flagged_cost = actual.get("flagged_cost", 0.0)

        entry = {
            "canonical_item": canonical,
            "theoretical_qty": theory["qty"] if theory else None,
            "theoretical_unit": theory["unit"] if theory else None,
            "dishes_sold": theory["dishes"] if theory else None,
            "purchased_qty": actual.get("qty_base"),
            "purchased_unit": actual.get("unit"),
            "purchase_cost": actual.get("cost", 0.0),
            "flagged_rows": flagged_rows,
            "flagged_cost": flagged_cost,
            "variance_pct": None,
            "verdict": None,
            "reason": None,
        }

        if theory is None:
            entry["verdict"] = VERDICT_NOT_MODELLED
            entry["reason"] = (
                "no locked v12 portion rule for this ingredient — purchases "
                "shown, no theoretical usage to compare against"
            )
        elif flagged_rows:
            entry["verdict"] = VERDICT_UNRELIABLE
            entry["reason"] = (
                f"{flagged_rows} purchase line(s) worth RM{flagged_cost:.2f} could "
                "not be converted to a base quantity — variance withheld"
            )
        elif not actual or actual.get("qty_base") is None:
            entry["verdict"] = VERDICT_UNRELIABLE
            entry["reason"] = "no convertible purchases recorded for this ingredient"
        elif actual.get("unit") == "MIXED" or theory["unit"] == "MIXED":
            entry["verdict"] = VERDICT_UNRELIABLE
            entry["reason"] = (
                f"purchases arrived in mixed units {actual.get('units_seen')} — "
                "cannot be summed against a single theoretical figure"
            )
        elif actual.get("unit") != theory["unit"]:
            entry["verdict"] = VERDICT_UNRELIABLE
            entry["reason"] = (
                f"theoretical usage is in {theory['unit']} but purchases are in "
                f"{actual.get('unit')}, and no locked rule converts between them"
            )
        elif theory["qty"] <= 0:
            entry["verdict"] = VERDICT_UNRELIABLE
            entry["reason"] = "theoretical usage is zero — variance undefined"
        else:
            pct = (actual["qty_base"] - theory["qty"]) / theory["qty"] * 100.0
            entry["variance_pct"] = round(pct, 1)
            entry["verdict"] = classify_variance(pct)

        rows.append(entry)

    rows.sort(key=lambda r: (
        r["variance_pct"] is None, -(r["variance_pct"] or 0), r["canonical_item"]
    ))
    return {
        "rows": rows,
        "unreliable_count": sum(1 for r in rows if r["verdict"] == VERDICT_UNRELIABLE),
        "not_modelled": [r["canonical_item"] for r in rows if r["verdict"] == VERDICT_NOT_MODELLED],
        "flagged_cost": round(sum(r["flagged_cost"] for r in rows), 2),
        "flagged_rows": sum(r["flagged_rows"] for r in rows),
    }


def top_variances(wastage: dict, limit: int = 3) -> list[dict]:
    """The worst real variances, for the Telegram digest.

    Only rows with a printed percentage qualify — an UNRELIABLE row has no
    magnitude to rank by, and putting it in a "top 3 worst" list would imply one.
    """
    ranked = [r for r in wastage.get("rows", []) if r.get("variance_pct") is not None]
    ranked.sort(key=lambda r: -abs(r["variance_pct"]))
    return ranked[:limit]
