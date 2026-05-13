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


def _maybe_rescue_embedded(entry: dict[str, Any]) -> dict[str, Any]:
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
