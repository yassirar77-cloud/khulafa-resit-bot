"""Field-by-field OCR scoring for the Qwen shadow test (spec §5).

The order generator lives or dies on purchase-data accuracy: a misread qty or
unit teaches the cadence engine the wrong rhythm, a misread item routes to the
wrong supplier, a misread price breaks spike detection. So the Qwen-vs-GLM
shadow comparison must be scored on the four fields that actually drive ordering:

    1. item name  (-> canonicalisation -> correct supplier)
    2. quantity   (-> qty forecast)
    3. unit/pack  (sack vs kg vs carton)
    4. price      (-> spike detection)

This is the missing measurement: ``ocr_shadow_comparison`` only compares the
receipt total/date/confidence. This module aligns the two providers' item lines
and emits one ``ocr_shadow_log`` row per field, so a per-field match rate (and a
clean win/tie/lose verdict) can be read off without burning more Qwen quota.

PURE module — no DB, no network, no live-OCR import. It only transforms already-
extracted ``{name, qty, price}`` item dicts (GLM's live output and Qwen's shadow
output). Safe for the shadow tooling to import; never touches the live path.
"""
from __future__ import annotations

import re

from item_canonicalization_v2 import canonicalize_item

# Ordering-critical fields, in the order they're reported.
FIELDS = ("item", "qty", "unit", "price")

# match-column vocabulary (spec §7 ocr_shadow_log.match):
#   no manual value -> "agree" / "disagree"  (GLM vs Qwen agreement)
#   manual present  -> who matched the human spot-check:
MATCH_AGREE = "agree"
MATCH_DISAGREE = "disagree"
MATCH_BOTH = "both"        # GLM and Qwen both matched manual
MATCH_GLM = "glm"          # only GLM matched manual
MATCH_QWEN = "qwen"        # only Qwen matched manual
MATCH_NEITHER = "neither"  # neither matched manual

_PRICE_TOL = 0.01
_QTY_TOL = 1e-6

# Pack/unit tokens that may be embedded in an item name ("AYAM 4 KG",
# "SANTAN 1 KG", "KICAP 2.4 KG", "PETRONAS 14KG"). Normalised to a canonical
# unit so "kgs"/"kg"/"kilo" all compare equal.
_UNIT_ALIASES = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gram": "g", "grams": "g", "gm": "g", "gms": "g",
    "l": "l", "liter": "l", "litre": "l", "liters": "l", "litres": "l", "ltr": "l",
    "ml": "ml",
    "pcs": "pcs", "pc": "pcs", "biji": "pcs", "unit": "pcs", "units": "pcs",
    "pack": "pack", "packet": "pack", "pkt": "pack", "pek": "pack",
    "tin": "tin", "can": "tin", "cans": "tin",
    "carton": "carton", "ctn": "carton", "kotak": "carton", "box": "carton",
    "btl": "btl", "bottle": "btl", "botol": "btl",
    "sack": "sack", "guni": "sack", "bag": "sack",
    "tong": "tong",
}
_UNIT_RE = re.compile(
    r"(?<![a-z])(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(_UNIT_ALIASES, key=len, reverse=True)) + r")(?![a-z])",
    re.IGNORECASE,
)


def extract_unit(name) -> str | None:
    """Pull a normalised pack/unit token out of an item name, or None.

    Returns the unit only (not the count): "AYAM 4 KG" -> "kg",
    "KICAP 2.4 KG" -> "kg", "SANTAN" -> None.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    m = _UNIT_RE.search(name)
    if not m:
        return None
    return _UNIT_ALIASES.get(m.group(2).lower())


def _canonical(name) -> str | None:
    res = canonicalize_item(name)
    return res.get("canonical")


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def align_items(glm_items, qwen_items) -> list[tuple[dict | None, dict | None]]:
    """Pair GLM and Qwen item lines by canonical item (greedy, one-to-one).

    Lines that canonicalise to the same key are matched; the rest are emitted
    unpaired (one side None) so misses are visible, never hidden. Items that
    don't canonicalise fall back to a normalised-name key so identical raw
    names still pair up.
    """
    def key(it):
        name = (it or {}).get("name")
        return _canonical(name) or (str(name or "").strip().upper() or None)

    qwen_pool = list(qwen_items or [])
    pairs: list[tuple[dict | None, dict | None]] = []
    used = [False] * len(qwen_pool)
    for g in glm_items or []:
        gk = key(g)
        match_idx = None
        if gk is not None:
            for i, q in enumerate(qwen_pool):
                if not used[i] and key(q) == gk:
                    match_idx = i
                    break
        if match_idx is None:
            pairs.append((g, None))
        else:
            used[match_idx] = True
            pairs.append((g, qwen_pool[match_idx]))
    for i, q in enumerate(qwen_pool):
        if not used[i]:
            pairs.append((None, q))
    return pairs


def _values_match(field, glm_val, qwen_val) -> bool:
    if field == "item":
        gk = _canonical(glm_val) or (str(glm_val or "").strip().upper() or None)
        qk = _canonical(qwen_val) or (str(qwen_val or "").strip().upper() or None)
        return gk is not None and gk == qk
    if field == "qty":
        g, q = _num(glm_val), _num(qwen_val)
        if g is None or q is None:
            return g is None and q is None
        return abs(g - q) <= _QTY_TOL
    if field == "price":
        g, q = _num(glm_val), _num(qwen_val)
        if g is None or q is None:
            return g is None and q is None
        return abs(g - q) <= _PRICE_TOL
    if field == "unit":
        return extract_unit(glm_val) == extract_unit(qwen_val)
    return glm_val == qwen_val


def _matches_manual(field, value, manual) -> bool:
    return _values_match(field, value, manual)


def _match_label(field, glm_val, qwen_val, manual_val) -> str:
    """The ocr_shadow_log.match value for one field."""
    if manual_val is None or (isinstance(manual_val, str) and not manual_val.strip()):
        return MATCH_AGREE if _values_match(field, glm_val, qwen_val) else MATCH_DISAGREE
    glm_ok = _matches_manual(field, glm_val, manual_val)
    qwen_ok = _matches_manual(field, qwen_val, manual_val)
    if glm_ok and qwen_ok:
        return MATCH_BOTH
    if glm_ok:
        return MATCH_GLM
    if qwen_ok:
        return MATCH_QWEN
    return MATCH_NEITHER


def _field_values(field, glm_item, qwen_item):
    """Pull the comparable value of ``field`` from an item dict (name for item
    and unit; qty/price for the rest)."""
    src = "name" if field in ("item", "unit") else field
    g = (glm_item or {}).get(src) if glm_item else None
    q = (qwen_item or {}).get(src) if qwen_item else None
    return g, q


def field_rows(receipt_id, glm_items, qwen_items, *, manual_items=None) -> list[dict]:
    """Build ``ocr_shadow_log`` rows for one receipt: one row per (item line ×
    field). ``manual_items`` is an optional aligned list of human spot-check
    dicts (same shape, parallel to GLM order); when absent the rows record
    GLM/Qwen agreement only.

    Each row: ``{receipt_id, field, glm_value, qwen_value, manual_value, match}``.
    """
    pairs = align_items(glm_items, qwen_items)
    manual_items = manual_items or []
    rows: list[dict] = []
    for idx, (g, q) in enumerate(pairs):
        manual = manual_items[idx] if idx < len(manual_items) else None
        for field in FIELDS:
            gv, qv = _field_values(field, g, q)
            mv, _ = _field_values(field, manual, None) if manual else (None, None)
            rows.append({
                "receipt_id": receipt_id,
                "field": field,
                "glm_value": None if gv is None else str(gv),
                "qwen_value": None if qv is None else str(qv),
                "manual_value": None if mv is None else str(mv),
                "match": _match_label(field, gv, qv, mv),
            })
    return rows


def score(rows: list[dict]) -> dict:
    """Aggregate ocr_shadow_log rows into a per-field verdict.

    For each field: agreement rate (GLM vs Qwen) over rows with no manual value,
    and — where a manual spot-check exists — how often GLM vs Qwen matched it.
    The overall verdict applies the spec's decision rule to the manual-checked
    rows: Qwen WINS or TIES -> switch; Qwen LOSES -> stay on GLM.
    """
    per_field: dict[str, dict] = {
        f: {"agree": 0, "disagree": 0, "glm_correct": 0, "qwen_correct": 0,
            "manual_checked": 0} for f in FIELDS
    }
    glm_correct = qwen_correct = manual_total = 0
    for r in rows:
        f = r.get("field")
        if f not in per_field:
            continue
        m = r.get("match")
        pf = per_field[f]
        if m in (MATCH_AGREE, MATCH_DISAGREE):
            pf["agree" if m == MATCH_AGREE else "disagree"] += 1
            continue
        pf["manual_checked"] += 1
        manual_total += 1
        if m in (MATCH_BOTH, MATCH_GLM):
            pf["glm_correct"] += 1
            glm_correct += 1
        if m in (MATCH_BOTH, MATCH_QWEN):
            pf["qwen_correct"] += 1
            qwen_correct += 1

    for f, pf in per_field.items():
        compared = pf["agree"] + pf["disagree"]
        pf["agreement_rate"] = (pf["agree"] / compared) if compared else None
        pf["glm_accuracy"] = (
            pf["glm_correct"] / pf["manual_checked"] if pf["manual_checked"] else None)
        pf["qwen_accuracy"] = (
            pf["qwen_correct"] / pf["manual_checked"] if pf["manual_checked"] else None)

    if manual_total == 0:
        verdict = "NO_MANUAL"  # only agreement measured; can't crown a winner
    elif qwen_correct > glm_correct:
        verdict = "QWEN_WINS"
    elif qwen_correct == glm_correct:
        verdict = "TIE"
    else:
        verdict = "QWEN_LOSES"

    return {
        "rows": len(rows),
        "per_field": per_field,
        "glm_correct": glm_correct,
        "qwen_correct": qwen_correct,
        "manual_checked": manual_total,
        "verdict": verdict,
        "decision": _decision_text(verdict),
    }


def _decision_text(verdict: str) -> str:
    if verdict == "QWEN_WINS":
        return "Qwen wins on ordering fields — switch (consolidates to one vision vendor)."
    if verdict == "TIE":
        return "Qwen ties GLM — switch is safe (saves GLM cost)."
    if verdict == "QWEN_LOSES":
        return "Qwen loses on ordering fields — stay on GLM. Decided before quota expiry."
    return "No manual spot-checks yet — only GLM/Qwen agreement measured."
