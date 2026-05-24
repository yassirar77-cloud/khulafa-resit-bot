"""GLM-OCR layout_parsing pipeline for receipt extraction.

Calls Z.ai's ``POST /api/paas/v4/layout_parsing`` endpoint with the
``glm-ocr`` model and parses the returned Markdown into the same schema
the rest of the bot uses:

    {merchant, total, currency, receipt_date, items, bill_to, raw_text,
     confidence}

Items is a list of ``{name, qty, price}`` dicts. ``raw_text`` is the
full Markdown returned by the API, kept verbatim for audit. The parser
is intentionally permissive — handwritten Malaysian supplier invoices
have unpredictable layouts.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time

from ocr_quality import (
    CONF_PENALTY_DATE_OUT_OF_WINDOW,
    CONF_PENALTY_DECIMAL_FIX,
    CONF_PENALTY_INCOMPLETE_ITEMS,
    CONF_PENALTY_SPLIT_COLUMN,
    correct_total_with_items,
    has_rm_sen_split_column,
    line_items_incomplete,
    normalize_amount_locale_aware,
    validate_date,
)

logger = logging.getLogger(__name__)

GLM_OCR_MODEL = os.environ.get("ZAI_OCR_MODEL", "glm-ocr")
GLM_OCR_TIMEOUT = float(os.environ.get("ZAI_OCR_TIMEOUT", "60"))


def _layout_parsing_url() -> str:
    base = os.environ.get(
        "ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"
    ).rstrip("/")
    return f"{base}/layout_parsing"


def _call_layout_parsing(image_bytes: bytes, mime_type: str) -> dict:
    import httpx  # imported lazily so the parser can be unit-tested without the dep

    api_key = os.environ["ZAI_API_KEY"]
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"
    payload = {"model": GLM_OCR_MODEL, "file": data_url}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=GLM_OCR_TIMEOUT) as client:
        response = client.post(_layout_parsing_url(), headers=headers, json=payload)
    response.raise_for_status()
    return response.json()


async def extract_with_glm_ocr(
    image_bytes: bytes, *, mime_type: str = "image/jpeg"
) -> dict:
    """Run glm-ocr on ``image_bytes`` and parse the Markdown response.

    Always returns the canonical schema with ``confidence`` (0-100) set
    by a heuristic over which fields parsed. Raises on transport error
    so the caller can decide whether to fall back.
    """
    start = time.monotonic()
    try:
        api = await asyncio.to_thread(_call_layout_parsing, image_bytes, mime_type)
    except Exception as exc:
        latency = time.monotonic() - start
        logger.exception(
            "glm-ocr request failed after %.2fs (image=%d bytes): %s",
            latency,
            len(image_bytes),
            exc,
        )
        raise

    latency = time.monotonic() - start
    md = api.get("md_results") or ""
    usage = api.get("usage") or {}
    logger.info(
        "glm-ocr response ok: latency=%.2fs image_bytes=%d md_chars=%d total_tokens=%s",
        latency,
        len(image_bytes),
        len(md),
        usage.get("total_tokens"),
    )
    parsed = parse_markdown_receipt(md)
    parse_ok = parsed["merchant"] is not None and parsed["total"] is not None
    logger.info(
        "glm-ocr parse: ok=%s merchant=%r total=%s date=%s items=%d bill_to=%r confidence=%d",
        parse_ok,
        parsed["merchant"],
        parsed["total"],
        parsed["receipt_date"],
        len(parsed["items"]),
        parsed["bill_to"],
        parsed["confidence"],
    )
    if not parse_ok:
        # Dump the full md_results so we can inspect what glm-ocr actually
        # returned for failing receipts. Capped at 2000 chars to keep log
        # lines readable; raw_text stays in the DB for full audit.
        logger.warning(
            "glm-ocr parse failed — md_results (truncated to 2000 chars):\n%s",
            md[:2000],
        )
    parsed["_latency_s"] = round(latency, 3)
    parsed["_total_tokens"] = usage.get("total_tokens")
    parsed["_md_chars"] = len(md)
    return parsed


# --- Markdown parsing -----------------------------------------------------

_TOTAL_LABEL_RE = re.compile(
    r"(?im)(?:GRAND\s*TOTAL|JUMLAH\s*BESAR|JUMLAH|TOTAL|AMOUNT\s*DUE|AMOUNT|BAYAR)"
    r"[^\d\n]*?(?:RM|MYR)?\s*"
    r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
)
_RM_AMOUNT_RE = re.compile(
    r"(?:RM|MYR)\s*"
    r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
)
_BILL_TO_RE = re.compile(
    r"(?im)^[\s\W]*(BILL\s*TO|BILLED\s*TO|CUSTOMER|PELANGGAN|TUAN|KEPADA)\s*[:\-]\s*(.+)$"
)
_ITEM_SPLIT_RE = re.compile(r"\s*(?:[+&,/]|\band\b|\bdan\b)\s*", re.IGNORECASE)


def _normalize_amount(s) -> float | None:
    return normalize_amount_locale_aware(s)


def _strip_md(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    return text.strip()


def _find_merchant(md: str) -> str | None:
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            cleaned = _strip_md(line)
            if cleaned:
                return cleaned
        m = re.fullmatch(r"\*\*(.+?)\*\*", line)
        if m:
            return m.group(1).strip()
    for raw in md.splitlines():
        line = raw.strip()
        if not line or line.startswith(("|", "-", "=", "_", "<")):
            continue
        return _strip_md(line)
    return None


def _find_total(md: str) -> float | None:
    candidates: list[float] = []
    for m in _TOTAL_LABEL_RE.finditer(md):
        amt = _normalize_amount(m.group(1))
        if amt is not None:
            candidates.append(amt)
    if candidates:
        return max(candidates)
    rm_amounts = [_normalize_amount(s) for s in _RM_AMOUNT_RE.findall(md)]
    rm_amounts = [a for a in rm_amounts if a is not None]
    if rm_amounts:
        return max(rm_amounts)
    return None


def _find_bill_to(md: str) -> str | None:
    for raw in md.splitlines():
        m = _BILL_TO_RE.match(raw)
        if m:
            value = _strip_md(m.group(2)).strip(" :|-")
            value = value.split("|")[0].strip()
            if value:
                return value
    for raw in md.splitlines():
        if "|" not in raw:
            continue
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = cells[0].lower()
        if any(k in label for k in ("bill to", "billed to", "customer", "pelanggan", "kepada")):
            value = _strip_md(cells[1])
            if value:
                return value
    return None


def _split_aggregated_name(name: str) -> list[str]:
    parts = [p.strip() for p in _ITEM_SPLIT_RE.split(name) if p.strip()]
    return parts or [name.strip()]


def _parse_table_items(md: str) -> list[dict]:
    items: list[dict] = []
    headers: list[str] = []
    in_body = False
    for raw in md.splitlines():
        line = raw.strip()
        if not (line.startswith("|") and line.endswith("|")):
            headers = []
            in_body = False
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            in_body = True
            continue
        if not in_body:
            headers = [c.lower() for c in cells]
            continue
        row = dict(zip(headers, cells)) if headers else {}
        name = (
            row.get("description")
            or row.get("item")
            or row.get("particulars")
            or row.get("name")
            or row.get("perkara")
            or (cells[0] if cells else "")
        )
        if not name:
            continue
        qty = _normalize_amount(
            row.get("qty") or row.get("quantity") or row.get("kuantiti") or row.get("kty")
        )
        price = _normalize_amount(
            row.get("amount")
            or row.get("price")
            or row.get("total")
            or row.get("harga")
            or row.get("jumlah")
            or (cells[-1] if len(cells) > 1 else None)
        )
        parts = _split_aggregated_name(name)
        if len(parts) > 1:
            for p in parts[:-1]:
                items.append({"name": p, "qty": None, "price": None})
            items.append({"name": parts[-1], "qty": qty, "price": price})
        else:
            items.append({"name": parts[0], "qty": qty, "price": price})
    return items


_LINE_SKIP_RE = re.compile(
    r"(?i)\b(grand\s*total|total|jumlah|amount\s*due|amount|bayar|bill\s*to|"
    r"billed\s*to|customer|pelanggan|kepada|date|tarikh|invoice|receipt|"
    r"resit|no\.?|nombor|tel|phone|address|alamat)\b"
)
_LINE_PRICE_RE = re.compile(
    r"(?i)(?:RM|MYR)\s*(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*$"
)


def _parse_line_items(md: str) -> list[dict]:
    items: list[dict] = []
    for raw in md.splitlines():
        line = raw.strip().lstrip("-•*").strip()
        if not line or line.startswith(("|", "#", ">")):
            continue
        if _LINE_SKIP_RE.search(line):
            continue
        m = _LINE_PRICE_RE.search(line)
        if not m:
            continue
        price = _normalize_amount(m.group(1))
        name = line[: m.start()].strip(" -:|")
        if not name or len(name) < 2:
            continue
        parts = _split_aggregated_name(name)
        if len(parts) > 1:
            for p in parts[:-1]:
                items.append({"name": p, "qty": None, "price": None})
            items.append({"name": parts[-1], "qty": None, "price": price})
        else:
            items.append({"name": name, "qty": None, "price": price})
    return items


def _heuristic_confidence(merchant, total, date, items) -> int:
    score = 0
    if merchant:
        score += 30
    if total is not None:
        score += 30
    if date:
        score += 20
    if items:
        score += 20
    return score


def parse_markdown_receipt(md: str) -> dict:
    """Parse the ``md_results`` Markdown into the canonical schema.

    Per-field misses log a WARNING with a 200-char head of the markdown so
    real failure modes show up in production logs without changing parser
    behaviour.
    """
    if not md:
        return {
            "merchant": None,
            "total": None,
            "currency": "MYR",
            "receipt_date": None,
            "items": [],
            "bill_to": None,
            "raw_text": "",
            "confidence": 0,
        }

    md_head = md[:200]

    merchant = _find_merchant(md)
    if merchant is None:
        logger.warning(
            "glm-ocr parse: no merchant heading/bold found; md head=%r", md_head
        )

    total = _find_total(md)
    if total is None:
        logger.warning(
            "glm-ocr parse: no GRAND TOTAL/TOTAL/JUMLAH match; md head=%r", md_head
        )

    receipt_date, date_flagged = validate_date(md)
    if receipt_date is None:
        logger.warning("glm-ocr parse: date regex failed; md head=%r", md_head)

    bill_to = _find_bill_to(md)

    items = _parse_table_items(md) or _parse_line_items(md)
    if not items:
        logger.warning(
            "glm-ocr parse: no items section found; md head=%r", md_head
        )

    total, total_corrected = correct_total_with_items(total, items, raw_text=md)
    split_column_flagged = has_rm_sen_split_column(md)
    items_incomplete = line_items_incomplete(md, items)

    confidence = _heuristic_confidence(merchant, total, receipt_date, items)
    if total_corrected:
        confidence = max(0, confidence - CONF_PENALTY_DECIMAL_FIX)
    elif items_incomplete:
        confidence = max(0, confidence - CONF_PENALTY_INCOMPLETE_ITEMS)
        logger.warning(
            "glm-ocr parse: line items look incomplete (parsed %d, raw shows "
            "more numbered rows) — total left uncorrected, confidence docked",
            len(items) if items else 0,
        )
    if date_flagged:
        confidence = max(0, confidence - CONF_PENALTY_DATE_OUT_OF_WINDOW)
    if split_column_flagged:
        confidence = max(0, confidence - CONF_PENALTY_SPLIT_COLUMN)
        logger.warning(
            "glm-ocr parse: RM/Sen split column detected — total may be unreliable"
        )

    return {
        "merchant": merchant,
        "total": total,
        "currency": "MYR",
        "receipt_date": receipt_date,
        "items": items,
        "bill_to": bill_to,
        "raw_text": md,
        "confidence": confidence,
    }
