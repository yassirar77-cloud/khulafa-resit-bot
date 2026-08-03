"""Second OCR pass that reads a supplier invoice as structured LINE ITEMS.

The main receipt prompt (``bot.OCR_PROMPT``) answers "who, when, how much" and
returns a loose ``items`` blob good enough for price history. Wastage analysis
needs something stricter: one object per PRINTED line, quantities separated
from units, and the unit kept EXACTLY as printed so
``uom_conversion`` can decide what a GUNI is instead of the model guessing.

This module owns that prompt and the parsing of its response. It has no
Telegram/Supabase imports so the prompt contract can be unit-tested offline;
``bot.py`` supplies the model call.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from date_utils import normalize_date
from money_utils import normalize_total

logger = logging.getLogger(__name__)

#: Fields every parsed response carries, so callers never guard on presence.
_EMPTY: dict[str, Any] = {
    "supplier": None,
    "invoice_no": None,
    "invoice_date": None,
    "lines": [],
    "subtotal": None,
    "total": None,
}

LINE_ITEMS_PROMPT = (
    "You are reading a Malaysian supplier invoice (often handwritten or "
    "dot-matrix printed, mixing English, Bahasa Malaysia, and Chinese).\n\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    "{\"supplier\": string|null, \"invoice_no\": string|null, "
    "\"invoice_date\": \"YYYY-MM-DD\"|null, "
    "\"lines\": [{\"line_no\": integer, \"item\": string, \"qty\": number|null, "
    "\"unit\": string|null, \"unit_price\": number|null, "
    "\"line_total\": number|null}], "
    "\"subtotal\": number|null, \"total\": number|null}\n\n"
    "Rules:\n"
    "1. ONE object per printed line. Do not merge two printed lines into one "
    "object, and do not split one printed line into two objects — even if a "
    "line names several products or wraps onto a second row of paper.\n"
    "2. line_no starts at 1 and follows the printed order, top to bottom.\n"
    "3. item is the product text exactly as printed, without the quantity, "
    "unit, or price columns.\n"
    "4. Keep unit EXACTLY as printed — KG, EKOR, GUNI, PAPAN, CTN, BTL, PKT, "
    "and so on. Do not translate it, expand it, pluralise it, or convert it to "
    "another unit. If no unit is printed, use null.\n"
    "5. qty is the printed quantity as a plain number (1.5, not \"1.5 KG\"). "
    "unit_price is the price for ONE unit; line_total is the amount for the "
    "whole line. Use null for any column that is not printed or not legible — "
    "never compute a missing value yourself.\n"
    "6. subtotal is the pre-tax/pre-rounding sum line if one is printed; total "
    "is the final amount payable. Either may be null.\n"
    "7. Do not include delivery charges, discounts, or the total row as line "
    "items unless they are printed as their own numbered product line.\n"
    "8. Numbers must be plain JSON numbers: 1234.50, not \"1,234.50\" or "
    "\"RM1,234.50\".\n\n"
    "No markdown, no code fences, no commentary. JSON only."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _strip_fences(content: str) -> str:
    stripped = content.strip()
    stripped = _FENCE_RE.sub("", stripped).strip()
    return stripped


def _to_number(value: Any) -> float | None:
    """Coerce an OCR'd numeric cell to float, or ``None`` if it isn't one."""
    return normalize_total(value)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_line(entry: Any, fallback_no: int) -> dict | None:
    """Normalise one ``lines[]`` entry. Returns ``None`` for unusable input."""
    if not isinstance(entry, dict):
        logger.warning("invoice_ocr: dropping non-dict line entry: %r", entry)
        return None

    item = _clean_text(entry.get("item")) or _clean_text(entry.get("name"))
    if item is None:
        logger.warning("invoice_ocr: dropping line %s with no item text", fallback_no)
        return None

    line_no = entry.get("line_no")
    if isinstance(line_no, bool) or not isinstance(line_no, (int, float)):
        line_no = fallback_no
    else:
        line_no = int(line_no)
    if line_no < 1:
        line_no = fallback_no

    return {
        "line_no": line_no,
        "item": item,
        "qty": _to_number(entry.get("qty")),
        # Unit is kept verbatim (only whitespace-trimmed): uom_conversion does
        # the folding, and receipt_items.unit_raw must show what the paper said.
        "unit": _clean_text(entry.get("unit")),
        "unit_price": _to_number(entry.get("unit_price")),
        "line_total": _to_number(entry.get("line_total")),
    }


def _dedupe_line_numbers(lines: list[dict]) -> list[dict]:
    """Guarantee unique, ascending ``line_no`` without reordering the lines.

    ``receipt_items`` is keyed on ``(receipt_id, line_no)``, so a model that
    emits two "line_no": 3 entries would otherwise have the second overwrite
    the first. Duplicates are renumbered to the next free slot; the printed
    order (the order the model returned) is what actually carries meaning.
    """
    seen: set[int] = set()
    next_free = 1
    for line in lines:
        n = line["line_no"]
        if n in seen:
            while next_free in seen:
                next_free += 1
            logger.warning(
                "invoice_ocr: duplicate line_no=%s renumbered to %s", n, next_free
            )
            n = next_free
        seen.add(n)
        line["line_no"] = n
    return lines


def parse_line_items_response(content: Any) -> dict:
    """Parse the model's reply into the canonical line-items dict.

    Always returns every key in the contract; a malformed reply degrades to
    empty fields rather than raising, so a bad OCR pass can never take down the
    receipt pipeline. Tolerates code fences and leading/trailing prose around
    the JSON object.
    """
    if not isinstance(content, str) or not content.strip():
        return dict(_EMPTY, lines=[])

    text = _strip_fences(content)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _OBJECT_RE.search(text)
        if match is None:
            logger.warning(
                "invoice_ocr: response contained no JSON object (%d chars)", len(text)
            )
            return dict(_EMPTY, lines=[])
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning(
                "invoice_ocr: response JSON did not parse (%d chars)", len(text)
            )
            return dict(_EMPTY, lines=[])

    if not isinstance(payload, dict):
        logger.warning("invoice_ocr: response JSON was %s, not an object", type(payload).__name__)
        return dict(_EMPTY, lines=[])

    raw_lines = payload.get("lines")
    lines: list[dict] = []
    if isinstance(raw_lines, list):
        for index, entry in enumerate(raw_lines, start=1):
            parsed = _parse_line(entry, index)
            if parsed is not None:
                lines.append(parsed)
    elif raw_lines is not None:
        logger.warning("invoice_ocr: 'lines' was %s, not a list", type(raw_lines).__name__)

    return {
        "supplier": _clean_text(payload.get("supplier")),
        "invoice_no": _clean_text(payload.get("invoice_no")),
        "invoice_date": normalize_date(payload.get("invoice_date")),
        "lines": _dedupe_line_numbers(lines),
        "subtotal": _to_number(payload.get("subtotal")),
        "total": _to_number(payload.get("total")),
    }
