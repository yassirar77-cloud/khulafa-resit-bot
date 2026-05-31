"""SHADOW OCR via Qwen3.6-Plus — measurement only, NOT a live OCR path.

This module exists to answer one question: *would Qwen be better than the
live GLM OCR on our problem receipts?* It calls Qwen3.6-Plus through the
DashScope OpenAI-compatible endpoint with the SAME receipt image and the
SAME extraction prompt the GLM chat path uses (``bot.OCR_PROMPT``), then
parses the response into the canonical receipt schema and scores it with the
SAME confidence heuristic + ``ocr_quality`` penalties as ``ocr_glm`` — so the
two providers' confidences are comparable.

HARD SAFETY RAILS (so a deploy can never route live receipts to Qwen):
  * Nothing in the production flow imports this module. It is only imported
    by ``scripts/qwen_shadow_backfill.py``.
  * ``extract_with_qwen_ocr`` refuses to run unless the explicit feature flag
    ``QWEN_SHADOW_ENABLED`` is truthy AND ``QWEN_API_KEY`` is set. A bare
    import or an accidental call in the wrong context raises immediately.

Credentials (Render env): ``QWEN_API_KEY``, ``QWEN_API_URL`` (default
``https://dashscope.aliyuncs.com/compatible-mode/v1``), ``QWEN_VL_MODEL``
(default ``qwen3.6-plus``). Free quota: 1M tokens, expires 2026-07-02 — the
backfill caps itself to stay well under it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

from ocr_quality import (
    CONF_PENALTY_DATE_OUT_OF_WINDOW,
    CONF_PENALTY_SPLIT_COLUMN,
    CONF_PENALTY_TOTAL_CONFLICT,
    has_rm_sen_split_column,
    normalize_amount_locale_aware,
    total_conflicts_with_item_sum,
    validate_date,
)

logger = logging.getLogger(__name__)

QWEN_API_URL = os.environ.get(
    "QWEN_API_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
QWEN_VL_MODEL = os.environ.get("QWEN_VL_MODEL", "qwen3.6-plus")
QWEN_OCR_TIMEOUT = float(os.environ.get("QWEN_OCR_TIMEOUT", "60"))


def shadow_enabled() -> bool:
    """True only when the operator has explicitly opted in to shadow runs.

    Gated on a dedicated flag (not just credentials) so that merely having
    ``QWEN_API_KEY`` present in the deploy env can never cause live OCR to
    reach Qwen.
    """
    return os.environ.get("QWEN_SHADOW_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Kept verbatim in sync with bot.OCR_PROMPT. We copy rather than import so the
# shadow tooling never drags in bot.py's import-time side effects (which require
# ZAI_*/SUPABASE_* env vars). If bot.OCR_PROMPT changes, update this too.
QWEN_OCR_PROMPT = (
    "You are a receipt OCR assistant specialised in Malaysian supplier invoices "
    "and receipts (often handwritten or dot-matrix printed, mixing English, "
    "Bahasa Malaysia, and Chinese). Extract the fields and respond ONLY with a "
    "compact JSON object using these keys: "
    "merchant (string), date (YYYY-MM-DD or null), total (number or null), "
    "currency (string, default \"MYR\" if a Malaysian supplier and not stated), "
    "items (array of objects, each an object with keys name (string, "
    "REQUIRED), qty (number or null), price (number or null) — never "
    "return plain strings; always wrap each item in a JSON object), "
    "raw_text (full transcription).\n\n"
    "Merchant guidance: many invoices come from local Malaysian suppliers such "
    "as BESTARI FARM, FOOK LEONG, SAIDA, BALAJI, HANEE, JASMINE, and MEWAH. "
    "Match these names even with OCR noise, spacing, or trailing words like "
    "ENTERPRISE, SDN BHD, TRADING, MARKETING, or SUPPLY. Prefer the supplier "
    "name printed at the top of the document over any customer or 'Bill To' "
    "name. If the merchant is ambiguous, use the most prominent letterhead.\n\n"
    "Date guidance: Malaysian dates are typically DD/MM/YYYY or DD-MM-YY. "
    "Convert to YYYY-MM-DD; if only two-digit year, assume 20YY.\n\n"
    "Total guidance: pick the final amount payable (look for GRAND TOTAL, "
    "TOTAL, JUMLAH, or AMOUNT DUE). Numbers may use commas as thousand "
    "separators; return as a plain number (e.g. 1234.50, not \"1,234.50\").\n\n"
    "Items: extract each line item separately. Put the product name in "
    "\"name\" and the numeric quantity in \"qty\" (e.g. name=\"Ayam\", qty=5). "
    "If the unit is non-numeric or part of the name (e.g. \"5kg\"), keep the "
    "full descriptor in name and set qty to the count of units sold. If a "
    "field is unreadable, use null. Even single-line and ice/water-only "
    "receipts must use the dict shape — never collapse items to a list of "
    "bare strings. No markdown, no commentary, JSON only."
)


def _build_client():
    """Lazily build an OpenAI SDK client pointed at the DashScope endpoint.

    Imported lazily so the module can be imported (and unit-tested) without
    the ``openai`` dependency or credentials present.
    """
    from openai import OpenAI

    api_key = os.environ["QWEN_API_KEY"]
    return OpenAI(api_key=api_key, base_url=QWEN_API_URL, timeout=QWEN_OCR_TIMEOUT)


def _heuristic_confidence(merchant, total, date, items) -> int:
    """Identical field-coverage heuristic to ocr_glm._heuristic_confidence."""
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


def _normalize_items(items) -> list[dict]:
    """Coerce Qwen's items into the {name, qty, price} dict shape.

    Tolerant of the model occasionally returning bare strings despite the
    prompt forbidding it.
    """
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict):
            name = it.get("name") or it.get("description") or ""
            out.append(
                {
                    "name": str(name).strip(),
                    "qty": normalize_amount_locale_aware(it.get("qty")),
                    "price": normalize_amount_locale_aware(it.get("price")),
                }
            )
        elif isinstance(it, str) and it.strip():
            out.append({"name": it.strip(), "qty": None, "price": None})
    return [it for it in out if it["name"]]


def parse_qwen_response(content: str) -> dict:
    """Parse Qwen's JSON reply into the canonical schema with a comparable score.

    Mirrors ocr_glm.parse_markdown_receipt's scoring: a field-coverage
    heuristic (30/30/20/20) docked by the same ``ocr_quality`` penalties for
    out-of-window dates, RM/Sen split columns, and item-sum conflicts — so
    ``qwen_confidence`` lines up with how GLM confidences were produced.
    """
    content = (content or "").strip()
    content = (
        content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    )
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        logger.warning("qwen shadow: non-JSON response (%d chars)", len(content))
        return {
            "merchant": None,
            "total": None,
            "currency": "MYR",
            "receipt_date": None,
            "items": [],
            "bill_to": None,
            "raw_text": content,
            "confidence": 0,
        }

    merchant = data.get("merchant") or None
    if isinstance(merchant, str):
        merchant = merchant.strip() or None
    total = normalize_amount_locale_aware(data.get("total"))
    items = _normalize_items(data.get("items"))
    raw_text = data.get("raw_text") or content

    # Prefer a window-validated date from the transcription; fall back to the
    # model's explicit `date` field if the regex finds nothing usable.
    receipt_date, date_flagged = validate_date(raw_text)
    if receipt_date is None:
        receipt_date = data.get("date") or None

    currency = data.get("currency") or "MYR"

    confidence = _heuristic_confidence(merchant, total, receipt_date, items)
    if total_conflicts_with_item_sum(total, items):
        confidence = max(0, confidence - CONF_PENALTY_TOTAL_CONFLICT)
    if date_flagged:
        confidence = max(0, confidence - CONF_PENALTY_DATE_OUT_OF_WINDOW)
    if has_rm_sen_split_column(raw_text):
        confidence = max(0, confidence - CONF_PENALTY_SPLIT_COLUMN)

    return {
        "merchant": merchant,
        "total": total,
        "currency": currency,
        "receipt_date": receipt_date,
        "items": items,
        "bill_to": None,
        "raw_text": raw_text,
        "confidence": confidence,
    }


def extract_with_qwen_ocr(
    image_bytes: bytes, *, mime_type: str = "image/jpeg"
) -> dict:
    """Run Qwen3.6-Plus shadow OCR on ``image_bytes`` and parse the response.

    Returns the canonical schema plus ``_total_tokens``/``_latency_s`` for the
    backfill's quota accounting. Raises ``RuntimeError`` if the shadow feature
    flag is off — this must never run in a live deploy by accident.
    """
    if not shadow_enabled():
        raise RuntimeError(
            "Qwen shadow OCR is disabled. Set QWEN_SHADOW_ENABLED=1 to run the "
            "measurement-only backfill. This path must never serve live receipts."
        )

    client = _build_client()
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"

    start = time.monotonic()
    response = client.chat.completions.create(
        model=QWEN_VL_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": QWEN_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        temperature=0.1,
    )
    latency = time.monotonic() - start

    content = response.choices[0].message.content or "{}"
    usage = getattr(response, "usage", None)
    total_tokens = getattr(usage, "total_tokens", None) if usage else None
    logger.info(
        "qwen shadow OCR: model=%s latency=%.2fs image_bytes=%d total_tokens=%s",
        QWEN_VL_MODEL,
        latency,
        len(image_bytes),
        total_tokens,
    )

    parsed = parse_qwen_response(content)
    parsed["_latency_s"] = round(latency, 3)
    parsed["_total_tokens"] = total_tokens
    return parsed
