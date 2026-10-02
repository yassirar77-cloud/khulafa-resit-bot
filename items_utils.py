"""Defensive normalization of OCR ``items`` lists.

The receipt OCR prompt asks for items in dict form
(``[{"name": ..., "qty": ..., "price": ...}, ...]``), but glm-4.6v-flash
occasionally returns plain strings for terse receipts. A real production
crash came from an EVEREST AISVARAM ice receipt where the model returned
``["Tube Ice", "Crush Ice", "Block Ice"]``; every downstream consumer
calls ``.get(...)`` on each entry and blew up with ``AttributeError:
'str' object has no attribute 'get'``.

``normalize_items`` is the safety net that runs once after OCR (and on
verifier corrections) so the rest of the pipeline only ever sees a list
of dicts.

It also rescues the embedded-quantity pattern (PR #23a): when the model
emits ``{"name": "Ayam x30 RM19.80", "qty": null, "price": null}``
instead of the clean three-field shape, ``parse_embedded_format`` peels
qty and price out of the name string so the price-history layer
downstream sees real numbers instead of nulls.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Matches "<name> xN RMX.XX" with the qty/price pair anchored at the end of
# the string, so on inputs with multiple "xN" tokens the rightmost one wins
# (e.g. "Box x2 Burger x3 RM10" -> name="Box x2 Burger", qty=3, price=10).
#
#   .*?\S      name: lazy, must end in a non-whitespace char (trims trailing
#              spaces, requires at least one visible character).
#   \s+x       mandatory whitespace before the qty marker so embedded codes
#              like "5x10" or "P8x" are NOT mistaken for a quantity.
#   re.IGNORECASE handles "RM"/"rm"/"Rm" and "x"/"X" in OCR output.
_EMBEDDED_RE = re.compile(
    r"^(?P<name>.*?\S)\s+x\s*(?P<qty>\d+(?:\.\d+)?)\s+RM\s*(?P<price>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


# Strips a single trailing parenthetical, e.g. " (amount should be RM297.00)"
# at the very end of the string. Zhipu's OCR occasionally appends commentary
# after the price; without this strip the embedded-qty regex refuses to match.
# Only matches at end-of-string, so mid-name parens like "DISH WSH (HIJAU)"
# are preserved when there is more content after them.
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def parse_embedded_format(name_string: Any) -> dict | None:
    """Parse ``"<name> xN RMX.XX"`` patterns trapped in the name field.

    Returns ``{"clean_name": str, "qty": float, "price": float}`` on a
    successful match, otherwise ``None``. Handles decimal qty (``x7.2``)
    and decimal price (``RM3.0``, ``RM19.80``). On multiple ``x...RM...``
    matches, rightmost wins (regex anchored to end). A single trailing
    parenthetical is stripped before matching so OCR commentary like
    ``"... RM9.90 (amount should be RM297.00)"`` still parses. Returns
    ``None`` for ``None``, non-strings, or empty/whitespace-only input.
    """
    if not isinstance(name_string, str):
        return None
    stripped = name_string.strip()
    if not stripped:
        return None
    cleaned = _TRAILING_PAREN_RE.sub("", stripped)
    match = _EMBEDDED_RE.match(cleaned)
    if match is None:
        return None
    return {
        "clean_name": match.group("name").strip(),
        "qty": float(match.group("qty")),
        "price": float(match.group("price")),
    }


def _is_numeric(value: Any) -> bool:
    # Reject bool first: ``True``/``False`` are ``int`` subclasses in Python
    # and we don't want a stray ``qty=True`` to be treated as a real number.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_items(raw_items: Any) -> list[dict[str, Any]]:
    """Coerce ``raw_items`` into a list of ``{"name": str, ...}`` dicts.

    Behaviour:
      * ``None`` or non-list input -> ``[]``
      * String entry -> ``{"name": str, "qty": None, "price": None}``
        (empty / whitespace-only strings are dropped)
      * Dict entry with numeric ``qty`` AND ``price`` -> kept as-is
      * Dict entry whose name matches the embedded ``xN RMX.XX`` pattern
        -> name/qty/price replaced with the parsed values (this overrides
        partial OCR data — a full embedded parse is more reliable than
        a half-filled qty/price pair); all other keys preserved
      * Dict entry that doesn't match any rescue rule -> kept as-is
      * Anything else (int, list, ``None``, ...) -> dropped with a warning
    """
    if not isinstance(raw_items, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw_items:
        if isinstance(entry, dict):
            out.append(_maybe_rescue_embedded(entry))
        elif isinstance(entry, str):
            name = entry.strip()
            if name:
                out.append({"name": name, "qty": None, "price": None})
        else:
            logger.warning(
                "normalize_items: skipping non-string non-dict entry: %r", entry
            )
    return out


# Alternative keys some OCR responses use instead of name / qty / price
# (e.g. ``{"description": ..., "quantity": "12.3kg", "unit_price": 11.7,
# "total": 143.91}`` on AYAM BERLIAN invoices).
_NAME_KEYS = ("description", "item", "product", "particulars", "butiran")
_QTY_KEYS = ("quantity", "kuantiti", "kty")
_PRICE_KEYS = ("unit_price", "harga", "u_price", "rate")
_LINE_TOTAL_KEYS = ("line_total", "total", "amount", "jumlah")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_KG_UNIT_RE = re.compile(r"\d\s*KG\b", re.IGNORECASE)


def _to_number(value: Any) -> float | None:
    """``12``, ``"12.3"``, ``"12.3kg"``, ``"1,234.50"`` -> float; else None."""
    if _is_numeric(value):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER_RE.search(value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _alias_keys(entry: dict[str, Any]) -> dict[str, Any]:
    """Fill ``name`` / ``qty`` / ``price`` / ``line_total`` from the
    alternative keys when the canonical ones are missing. Original keys are
    kept. A ``quantity`` string with a kg unit ("12.3kg") marks the line as
    weighed (``qty_unit = "kg"``)."""
    out = dict(entry)
    if not isinstance(out.get("name"), str) or not out.get("name", "").strip():
        for key in _NAME_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                out["name"] = value.strip()
                break
    if out.get("qty") is None:
        for key in _QTY_KEYS:
            if entry.get(key) is not None:
                number = _to_number(entry.get(key))
                if number is not None:
                    out["qty"] = number
                    if isinstance(entry.get(key), str) and _KG_UNIT_RE.search(entry[key]):
                        out["qty_unit"] = "kg"
                break
    elif isinstance(out.get("qty"), str):
        number = _to_number(out["qty"])
        if number is not None:
            if _KG_UNIT_RE.search(out["qty"]):
                out["qty_unit"] = "kg"
            out["qty"] = number
    if out.get("price") is None:
        for key in _PRICE_KEYS:
            number = _to_number(entry.get(key))
            if number is not None:
                out["price"] = number
                break
    elif isinstance(out.get("price"), str):
        number = _to_number(out["price"])
        if number is not None:
            out["price"] = number
    if out.get("line_total") is None:
        for key in _LINE_TOTAL_KEYS[1:]:
            number = _to_number(entry.get(key))
            if number is not None:
                out["line_total"] = number
                break
    elif not _is_numeric(out.get("line_total")):
        out["line_total"] = _to_number(out.get("line_total"))
    return out


def _maybe_rescue_embedded(entry: dict[str, Any]) -> dict[str, Any]:
    entry = _alias_keys(entry)
    qty = entry.get("qty")
    price = entry.get("price")
    if _is_numeric(qty) and _is_numeric(price):
        return entry
    # Either both fields are missing, or only one is set (partial OCR).
    # In both cases a successful embedded-pattern match is more reliable
    # than the partial data, so the parsed values win when available.
    parsed = parse_embedded_format(entry.get("name"))
    if parsed is None:
        return entry
    rescued = dict(entry)
    rescued["name"] = parsed["clean_name"]
    rescued["qty"] = parsed["qty"]
    rescued["price"] = parsed["price"]
    return rescued


# --- weighed (per-kg) lines ---------------------------------------------------
#
# Chicken / meat / fish invoices (AYAM BERLIAN and the like) print
# "count  name  weight-kg  price-per-kg  line-total". OCR often puts the bird
# count in ``qty`` and the per-kg price in ``price``, so qty x price never
# matches the line or the bill. ``resolve_weighed_lines`` turns those lines
# into qty = weight, using (strongest first):
#   1. an explicit line total on the item (``line_total`` key or "Total RM.."
#      / "= RM.." in the name): qty = line_total / price;
#   2. an explicit sold weight in the name: "(47.2 KG)", "x50.20 KG",
#      "Weight 54.70 KG", "31.72 KG x RM11.50", "48.80kg RM11.80";
#   3. the raw OCR text: the "price  line-total" column pair for the item's
#      price (the weight just before it is used when it agrees).
# Step 3 only applies when the bill total is known, the items as read do NOT
# add up to it (5%), and the corrected lines DO — so ordinary receipts are
# never touched. Pack sizes in product names ("Santan 1 kg", "MINYAK 5KG")
# are not sold weights and never match step 2.

WEIGHT_TOLERANCE = 0.05            # bill-level: line sum within 5% of total
EXACT_TOLERANCE = 0.001            # bill-level: lines that already match (0.1%)
_PAIR_TOLERANCE = 0.003            # weight x price vs printed line total

_W = r"(\d+(?:\.\d+)?)"
_SOLD_WEIGHT_RES = (
    re.compile(r"\(\s*" + _W + r"\s*KG\s*\)", re.IGNORECASE),
    re.compile(r"WEIGHT\s*:?\s*" + _W + r"\s*KG", re.IGNORECASE),
    re.compile(r"\bx\s*" + _W + r"\s*KG\b", re.IGNORECASE),
    re.compile(_W + r"\s*KG\s*(?:[x\u00d7*@]|RM)", re.IGNORECASE),
)
_NAME_TOTAL_RE = re.compile(r"(?:TOTAL\s*:?|=)\s*RM\s*" + _W, re.IGNORECASE)
_NAME_PRICE_RE = re.compile(r"(?<![=:])\s(?:[x\u00d7*@]\s*)?RM\s*" + _W + r"(?!\s*\))", re.IGNORECASE)
_RAW_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?\.?(?:\s*kg)?", re.IGNORECASE)


def sold_weight_from_name(name: Any) -> float | None:
    """The weight sold, when the name states one explicitly (see above)."""
    if not isinstance(name, str):
        return None
    for pattern in _SOLD_WEIGHT_RES:
        match = pattern.search(name)
        if match:
            value = float(match.group(1))
            if value > 0:
                return value
    return None


def line_total_from_name(name: Any) -> float | None:
    if not isinstance(name, str):
        return None
    match = _NAME_TOTAL_RE.search(name)
    return float(match.group(1)) if match else None


def _price_from_name(name: Any) -> float | None:
    """Per-unit price written in the name ("... RM11.50", "x RM11.50/KG"),
    ignoring an "= RM.." / "Total RM.." line total."""
    if not isinstance(name, str):
        return None
    for match in _NAME_PRICE_RE.finditer(" " + name):
        start = match.start()
        before = (" " + name)[max(0, start - 8):start].upper()
        if "TOTAL" in before:
            continue
        return float(match.group(1))
    return None


def _num(value: Any) -> float | None:
    return float(value) if _is_numeric(value) else None


def _line_sum(items: list[dict]) -> float | None:
    total = 0.0
    seen = False
    for it in items:
        q, p = _num(it.get("qty")), _num(it.get("price"))
        if q is None or p is None:
            continue
        total += q * p
        seen = True
    return total if seen else None


def _within(value: float | None, target: float | None, tol: float = WEIGHT_TOLERANCE) -> bool:
    return value is not None and target is not None and target > 0 and abs(value - target) <= tol * target


def _raw_price_pairs(raw_text: Any) -> list[tuple[float | None, float, float]]:
    """``(weight-or-None, price, line_total)`` for each item line found in the
    OCR text, in order. Two printed layouts:

      * "count  name  weight  price  total" (delivery orders):
        ``... 47 11.50 540.50`` — price then total, weight just before;
      * "qty  price  weight  [disc]  total" (Vista invoices):
        ``30 11.70 50.00 585.00`` — accepted only when weight x price matches
        the printed total, so a price followed by any decimal is not misread.
    """
    if not isinstance(raw_text, str):
        return []
    tokens = []
    for match in _RAW_TOKEN_RE.finditer(raw_text):
        text = match.group(0).lower().replace("kg", "").strip().rstrip(".").replace(",", "")
        try:
            tokens.append((float(text), match.group(0)))
        except ValueError:
            continue
    # One candidate per number position (read as the unit price); the item's
    # own price decides which candidates are used. "price weight total" wins
    # at a position only when it multiplies out exactly.
    pairs = []
    for i in range(1, len(tokens) - 1):
        price, price_txt = tokens[i]
        if "." not in price_txt or price <= 0:
            continue
        if i + 2 < len(tokens):
            weight, _ = tokens[i + 1]
            total, total_txt = tokens[i + 2]
            if "." in total_txt and weight > 0 and total > 0 and \
                    abs(weight * price - total) <= max(0.06, _PAIR_TOLERANCE * total):
                pairs.append((weight, price, total))
                continue
        total, total_txt = tokens[i + 1]
        if "." in total_txt and total > 0:
            pairs.append((tokens[i - 1][0], price, total))
    return pairs


def resolve_weighed_lines(items: Any, raw_text: Any = None, receipt_total: Any = None) -> list[dict]:
    """Return ``normalize_items(items)`` with per-kg lines turned into
    qty = weight (see the block comment above). Never raises.

    A missing qty is always filled (weight in the name, else line total /
    price). A qty that IS present is only replaced when the bill total is
    known, the lines as read do not add up to it, and the corrected lines do
    — "Ikan (1 KG) x3 RM10" keeps its 3 packs."""
    rows = [dict(it) for it in normalize_items(items)]
    try:
        hints = []
        for it in rows:
            name = it.get("name")
            price = _num(it.get("price"))
            if price is None:
                price = _price_from_name(name)
                if price is not None:
                    it["price"] = price
            line_total = _num(it.get("line_total"))
            if line_total is None:
                line_total = line_total_from_name(name)
                if line_total is not None:
                    it["line_total"] = line_total
            weight = sold_weight_from_name(name)
            best = None
            if price and line_total:
                best = round(line_total / price, 3)
            elif weight is not None and price:
                best = weight
            if _num(it.get("qty")) is None and best is not None:
                it["qty"] = best
                it["qty_unit"] = "kg"
                best = None
            hints.append(best)
        total = _to_number(receipt_total)
        if not total:
            return rows
        as_read = _line_sum(rows)
        if _within(as_read, total, EXACT_TOLERANCE):
            return rows
        if _within(as_read, total):
            # Close but not exact: standard order counts (30 birds, 40 legs)
            # can land within 5% of the bill by coincidence. Only the OCR
            # text's price / line-total columns may replace them, and only
            # when they reconcile strictly closer to the printed total.
            corrected = _from_raw_text(rows, raw_text)
            if corrected is not None and _within(_line_sum(corrected), total) \
                    and abs(_line_sum(corrected) - total) < abs(as_read - total):
                return corrected
            return rows
        # The lines as read do not add up: try the weights stated per line,
        # then the price / line-total columns of the OCR text.
        stated = []
        changed = False
        for it, best in zip(rows, hints):
            it = dict(it)
            if best is not None and _num(it.get("qty")) != best:
                it["qty"] = best
                it["qty_unit"] = "kg"
                changed = True
            stated.append(it)
        if changed and _within(_line_sum(stated), total):
            return stated
        corrected = _from_raw_text(rows, raw_text)
        if corrected is not None and _within(_line_sum(corrected), total):
            return corrected
    except Exception:  # pragma: no cover - defensive, never break the pipeline
        logger.exception("resolve_weighed_lines failed; using items as read")
    return rows


def _from_raw_text(rows: list[dict], raw_text: Any) -> list[dict] | None:
    pairs = _raw_price_pairs(raw_text)
    if not pairs:
        return None
    used = [False] * len(pairs)
    out = []
    changed = False
    for it in rows:
        it = dict(it)
        price = _num(it.get("price"))
        if price:
            for idx, (weight, p, line_total) in enumerate(pairs):
                if used[idx] or abs(p - price) > 0.005:
                    continue
                used[idx] = True
                tol = max(0.06, _PAIR_TOLERANCE * line_total)
                if weight and weight > 0 and abs(weight * price - line_total) <= tol:
                    new_qty = weight
                else:
                    new_qty = round(line_total / price, 3)
                if _num(it.get("qty")) != new_qty:
                    it["qty"] = new_qty
                    it["qty_unit"] = "kg"
                    changed = True
                break
        out.append(it)
    return out if changed else None
