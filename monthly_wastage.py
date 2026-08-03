"""Monthly wastage: theoretical usage from POS sales vs actual purchases.

Theoretical usage comes from ``monthly_itemwise`` through the owner-locked v12
rule set in ``wastage_rules_v12`` — portions, the Thai keyword list, the chicken
cut rules, the egg rules, the drinks rules and the Thai/Mamak protein split.
Actual purchases come from ``receipt_items`` for the same outlet and period.

    variance % = (purchased − theoretical) / theoretical
    > +15%        HIGH WASTAGE
    −5% … +15%    healthy
    < −5%         OVER-USED

Rows are keyed ``(canonical_item, unit)``, not by item alone. That is what lets
ayam be compared honestly: whole cuts are bought by the EKOR and consumed in
pieces, fillet is bought by the KG and consumed in grams, and liver is neither.
Collapsing them would force a MIXED verdict on the largest ingredient in the
report — the one the whole exercise exists to measure.

A row without a percentage always carries the reason, and the reasons are kept
distinct because they call for different actions:

* ``NO PORTION RULE``      — udang, sotong, hati ayam. The rules identify the
  demand but no portion is locked, so it is reported in DISHES and never
  converted to a weight.
* ``NO PURCHASE CATEGORY`` — telur, beras, minyak masak, susu, tulang. The
  rules model them, but canonicalization v2 has no category, so no purchase
  line can ever be matched. The fix is a category, not a portion.
* ``UNRELIABLE``           — purchases exist but some could not be converted, or
  arrived in a unit the theoretical figure is not in.
* ``NOT MODELLED``         — purchases exist for something no rule mentions.

Nothing is estimated to fill any of those gaps.

Pure functions apart from :func:`fetch_purchases`.
"""
from __future__ import annotations

import logging
from typing import Any

from wastage_rules_v12 import (
    CANONICAL_BY_INGREDIENT,
    UNPORTIONED,
    usage_for_dish,
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
#: The rules DO model this ingredient, but canonicalization v2 has no category
#: for it, so no purchase line can ever be matched against the theoretical
#: figure. Distinct from NOT MODELLED, which means the reverse.
VERDICT_NO_PURCHASE_CATEGORY = "NO PURCHASE CATEGORY"
#: Identified by the rules but with no owner-locked portion (udang, sotong,
#: hati ayam). Demand is reported in dishes; it is never converted to a weight.
VERDICT_NO_PORTION_RULE = "NO PORTION RULE"

def _bucket_for(ingredient: str) -> str:
    """The key an ingredient's THEORETICAL usage is reported under.

    The canonical purchase category when one exists, so several rule-level
    ingredients that come out of the same stock are compared as one — MYSOOR and
    MD HANI beef are both bought as `daging` and are only distinguishable on the
    kitchen line, not on an invoice. Otherwise the ingredient's own key, so that
    beras biasa, beras basmati and tulang (all `None`, all grams) never merge
    into one meaningless total.
    """
    return CANONICAL_BY_INGREDIENT.get(ingredient) or ingredient


def _has_no_purchase_category(ingredients: dict) -> bool:
    """True when EVERY rule-level ingredient in a bucket lacks a canonical item.

    Checked across all of them, not just the first: a bucket can hold several
    (daging_mysoor + daging_md_hani), and a bucket is only uncomparable when
    none of its ingredients has anywhere for purchases to land.
    """
    keys = list(ingredients or {})
    if not keys:
        return False
    return all(CANONICAL_BY_INGREDIENT.get(k) is None for k in keys)


def theoretical_usage(itemwise_rows: list[dict]) -> dict[tuple[str, str], dict]:
    """Theoretical ingredient usage implied by a month of POS sales.

    Keyed ``(bucket, unit)``. Ayam appears under three keys — fillet in grams,
    whole cuts in pieces, liver in dishes — because they are bought, measured
    and wasted differently and summing them would be arithmetic on
    incompatible things.

    Each value carries ``ingredients`` (the rule-level breakdown) and
    ``contributing`` (the dish names that fed it), so a surprising figure can be
    traced back to the buttons that produced it.
    """
    out: dict[tuple[str, str], dict] = {}
    for row in itemwise_rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("item_name")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue

        for ingredient, usage in usage_for_dish(raw, qty).items():
            key = (_bucket_for(ingredient), usage["unit"])
            entry = out.setdefault(key, {
                "qty": 0.0, "unit": usage["unit"], "dishes": 0.0,
                "portioned": usage["portioned"], "ingredients": {},
                "contributing": [],
            })
            entry["qty"] += usage["amount"]
            entry["dishes"] += usage["servings"]
            entry["ingredients"][ingredient] = round(
                entry["ingredients"].get(ingredient, 0.0) + usage["amount"], 3
            )
            if raw not in entry["contributing"]:
                entry["contributing"].append(raw)

    for entry in out.values():
        entry["qty"] = round(entry["qty"], 3)
        entry["dishes"] = round(entry["dishes"], 3)
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


def summarise_purchases(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """Group purchase rows by ``(canonical_item, base_unit)``.

    Grouping by unit as well as item is what lets ayam bought by the EKOR be
    compared with whole-cut demand while ayam bought by the KG is compared with
    fillet demand — collapsing them to one row would force a MIXED verdict on
    the single largest ingredient in the report.

    Flagged and unconvertible rows are tracked per item (not per unit — they
    have no unit) so an ingredient can be marked UNRELIABLE whichever unit its
    good rows arrived in.
    """
    from wastage_export import line_cost

    out: dict[tuple[str, str], dict] = {}
    flagged_by_item: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        canonical = row.get("canonical_item")
        canonical = (canonical.strip() if isinstance(canonical, str) and canonical.strip()
                     else "UNCATEGORISED")
        cost = line_cost(row) or 0.0
        qty_base = _to_float(row.get("qty_base"))
        base_unit = row.get("base_unit")

        if row.get("needs_review") or qty_base is None or not base_unit:
            entry = flagged_by_item.setdefault(canonical, {"rows": 0, "cost": 0.0})
            entry["rows"] += 1
            entry["cost"] += cost
            continue

        bucket = out.setdefault((canonical, base_unit), {
            "qty_base": 0.0, "unit": base_unit, "cost": 0.0,
        })
        bucket["qty_base"] += qty_base
        bucket["cost"] += cost

    for bucket in out.values():
        bucket["qty_base"] = round(bucket["qty_base"], 3)
        bucket["cost"] = round(bucket["cost"], 2)
    for entry in flagged_by_item.values():
        entry["cost"] = round(entry["cost"], 2)
    return {"buckets": out, "flagged": flagged_by_item}


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

    Every row carries a verdict, and a row without a percentage always carries
    the REASON it has none. There are four such reasons, and they are kept
    distinct because they call for different actions:

    * ``NO PORTION RULE``      — udang / sotong / hati ayam: demand shown in
      dishes; the owner must supply a portion before a weight can exist.
    * ``NO PURCHASE CATEGORY`` — the rules model it (telur, beras, minyak,
      susu, tulang) but canonicalization v2 has no category, so purchases can
      never be matched. Add the category, not a portion.
    * ``UNRELIABLE``           — purchases exist but some could not be converted,
      or arrived in a unit the theoretical figure is not in.
    * ``NOT MODELLED``         — purchases exist for something the rules say
      nothing about.
    """
    theoretical = theoretical_usage(itemwise_rows)
    summary = summarise_purchases(purchase_rows)
    buckets = summary["buckets"]
    flagged = summary["flagged"]

    rows: list[dict] = []
    seen_purchase_keys: set[tuple[str, str]] = set()

    for (bucket, unit), theory in sorted(theoretical.items()):
        purchase = buckets.get((bucket, unit))
        if purchase is not None:
            seen_purchase_keys.add((bucket, unit))
        flag = flagged.get(bucket, {})
        flagged_rows = flag.get("rows", 0)
        flagged_cost = flag.get("cost", 0.0)

        entry = {
            "canonical_item": bucket,
            "theoretical_qty": theory["qty"],
            "theoretical_unit": unit,
            "dishes_sold": theory["dishes"],
            "ingredients": theory["ingredients"],
            "purchased_qty": purchase["qty_base"] if purchase else None,
            "purchased_unit": purchase["unit"] if purchase else None,
            "purchase_cost": purchase["cost"] if purchase else 0.0,
            "flagged_rows": flagged_rows,
            "flagged_cost": flagged_cost,
            "variance_pct": None,
            "verdict": None,
            "reason": None,
        }

        if not theory["portioned"]:
            entry["verdict"] = VERDICT_NO_PORTION_RULE
            entry["reason"] = (
                f"{theory['dishes']:g} dish(es) called for this, but no portion "
                "size is locked for it — demand shown in dishes, never converted "
                "to a weight"
            )
        elif _has_no_purchase_category(theory["ingredients"]):
            entry["verdict"] = VERDICT_NO_PURCHASE_CATEGORY
            entry["reason"] = (
                "canonicalization v2 has no category for this ingredient, so no "
                "purchase line can be matched against it — add the category to "
                "data/canonical_items_v2.json to make the variance computable"
            )
        elif flagged_rows:
            entry["verdict"] = VERDICT_UNRELIABLE
            entry["reason"] = (
                f"{flagged_rows} purchase line(s) worth RM{flagged_cost:.2f} could "
                "not be converted to a base quantity — variance withheld"
            )
        elif purchase is None:
            other_units = sorted({u for (b, u) in buckets if b == bucket})
            if other_units:
                entry["verdict"] = VERDICT_UNRELIABLE
                entry["reason"] = (
                    f"theoretical usage is in {unit} but purchases arrived only in "
                    f"{', '.join(other_units)}, and no locked rule converts between them"
                )
            else:
                entry["verdict"] = VERDICT_UNRELIABLE
                entry["reason"] = "no convertible purchases recorded for this ingredient"
        elif theory["qty"] <= 0:
            entry["verdict"] = VERDICT_UNRELIABLE
            entry["reason"] = "theoretical usage is zero — variance undefined"
        else:
            pct = (purchase["qty_base"] - theory["qty"]) / theory["qty"] * 100.0
            entry["variance_pct"] = round(pct, 1)
            entry["verdict"] = classify_variance(pct)

        rows.append(entry)

    # Purchases the rules say nothing about.
    for (bucket, unit), purchase in sorted(buckets.items()):
        if (bucket, unit) in seen_purchase_keys:
            continue
        flag = flagged.get(bucket, {})
        rows.append({
            "canonical_item": bucket,
            "theoretical_qty": None, "theoretical_unit": None, "dishes_sold": None,
            "ingredients": {},
            "purchased_qty": purchase["qty_base"], "purchased_unit": purchase["unit"],
            "purchase_cost": purchase["cost"],
            "flagged_rows": flag.get("rows", 0), "flagged_cost": flag.get("cost", 0.0),
            "variance_pct": None, "verdict": VERDICT_NOT_MODELLED,
            "reason": ("no v12 rule produces theoretical usage for this ingredient — "
                       "purchases shown, nothing to compare against"),
        })

    # Flagged-only items: money spent, no usable quantity, no theoretical match.
    reported = {r["canonical_item"] for r in rows}
    for item, flag in sorted(flagged.items()):
        if item in reported:
            continue
        rows.append({
            "canonical_item": item,
            "theoretical_qty": None, "theoretical_unit": None, "dishes_sold": None,
            "ingredients": {},
            "purchased_qty": None, "purchased_unit": None, "purchase_cost": flag["cost"],
            "flagged_rows": flag["rows"], "flagged_cost": flag["cost"],
            "variance_pct": None, "verdict": VERDICT_UNRELIABLE,
            "reason": (f"{flag['rows']} purchase line(s) worth RM{flag['cost']:.2f} "
                       "could not be converted to a base quantity"),
        })

    rows.sort(key=lambda r: (
        r["variance_pct"] is None, -(r["variance_pct"] or 0), r["canonical_item"]
    ))
    return {
        "rows": rows,
        "unreliable_count": sum(1 for r in rows if r["verdict"] == VERDICT_UNRELIABLE),
        "not_modelled": [r["canonical_item"] for r in rows
                         if r["verdict"] == VERDICT_NOT_MODELLED],
        "no_purchase_category": [r["canonical_item"] for r in rows
                                 if r["verdict"] == VERDICT_NO_PURCHASE_CATEGORY],
        "no_portion_rule": [r["canonical_item"] for r in rows
                            if r["verdict"] == VERDICT_NO_PORTION_RULE],
        "flagged_cost": round(sum(f["cost"] for f in flagged.values()), 2),
        "flagged_rows": sum(f["rows"] for f in flagged.values()),
    }


def top_variances(wastage: dict, limit: int = 3) -> list[dict]:
    """The worst real variances, for the Telegram digest.

    Only rows with a printed percentage qualify — an UNRELIABLE row has no
    magnitude to rank by, and putting it in a "top 3 worst" list would imply one.
    """
    ranked = [r for r in wastage.get("rows", []) if r.get("variance_pct") is not None]
    ranked.sort(key=lambda r: -abs(r["variance_pct"]))
    return ranked[:limit]
