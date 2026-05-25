import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask, jsonify, render_template
from openai import OpenAI
from supabase import Client, create_client
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from audit_messages import build_big_purchase_message
from config.reviewers import REVIEWER_CHAT_IDS, is_reviewer
from date_utils import normalize_date
from image_utils import resize_for_ocr
from items_utils import normalize_items
from money_utils import normalize_total
from ocr_quality import total_conflicts_with_item_sum
from pending_review import (
    apply_edits_to_parsed,
    build_review_reason,
    serialize_parsed_for_review,
    should_queue,
)
from reparse import (
    apply_audit_row,
    format_preview,
    format_status,
    summarize_audit_rows,
)
from merchant_resolver import (
    CANONICAL_TABLE,
    ALIAS_TABLE,
    compute_coverage,
    format_coverage_report,
    format_merchant_list,
    format_merchant_show,
    format_pending_aliases,
    load_snapshot,
)
from backfill_canonical import (
    BACKFILL_AUDIT_TABLE,
    apply_backfill_audit_row,
    format_preview as format_backfill_preview,
    format_status as format_backfill_status,
    format_unmatched as format_backfill_unmatched,
    should_apply as backfill_should_apply,
    top_unmatched_from_audit,
)
import item_resolver
from item_resolver import (
    format_coverage_report as format_item_coverage,
    format_item_list,
    format_item_show,
    format_pending_aliases as format_item_pending_aliases,
)
from backfill_items import (
    ITEM_RESOLUTIONS_TABLE,
    format_status as format_item_backfill_status,
    format_unmatched as format_item_backfill_unmatched,
    top_unmatched_from_resolutions,
)
import analytics
from receipt_classifier import ReceiptType, classify_receipt

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ZAI_API_KEY = os.environ["ZAI_API_KEY"]
ZAI_BASE_URL = os.environ.get("ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
ALERT_CHAT_ID = int(os.environ["ALERT_CHAT_ID"])
HEALTH_PORT = int(os.environ.get("PORT", "10000"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

ZAI_MODEL = os.environ.get("ZAI_MODEL", "glm-4.6v-flash")
ZAI_VERIFY_MODEL = os.environ.get("ZAI_VERIFY_MODEL", ZAI_MODEL)
# OCR provider for the first pass. Default keeps the proven chat-completions
# path; set ZAI_OCR_PROVIDER=glm-ocr to use the cheaper layout_parsing endpoint.
ZAI_OCR_PROVIDER = os.environ.get("ZAI_OCR_PROVIDER", "glm-4.6v-flash")
IMAGE_RESIZE_ENABLED = os.environ.get("IMAGE_RESIZE_ENABLED", "true").lower() == "true"
IMAGE_MAX_DIM = int(os.environ.get("IMAGE_MAX_DIM", "1600"))
RECEIPTS_TABLE = "receipts"
AUDIT_TABLE = "audit_responses"
STAFF_ADVANCES_TABLE = "staff_advances"
FIXED_COSTS_TABLE = "fixed_costs"
PETTY_CASH_TABLE = "petty_cash"
PENDING_REVIEW_TABLE = "pending_review"
REPARSE_AUDIT_TABLE = "reparse_audit"

# Edit-flow conversation states (PR #29b manual review).
REVIEW_EDIT_TOTAL, REVIEW_EDIT_MERCHANT, REVIEW_EDIT_DATE = range(3)

# /reparse_preview and /reparse_apply batch sizes.
REPARSE_DEFAULT_N = 10
REPARSE_MAX_N = 50
MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")

BIG_PURCHASE_MULTIPLIER = 2.0
BIG_PURCHASE_LOOKBACK_DAYS = 14
NEW_SUPPLIER_THRESHOLD = 200.0
SUSPICIOUS_PRICE_RATIO = 1.20
SUSPICIOUS_ITEM_LOOKBACK_DAYS = 7
DUPLICATE_TOTAL_TOLERANCE = 0.05

KNOWN_SUPPLIERS = [
    # Spices & dry goods
    'BABAS', 'SAIDA', 'BALAJI', 'SHREE MAP JAYA',
    # Rice
    'JASMINE', 'BERAS',
    # Dairy
    'MEWAH', 'F&N', 'DUTCH LADY',
    # Meat & frozen
    'HANEE', 'BS FROZEN', 'BESTARI FARM', 'BESTARI',
    # Tea & coffee
    'CAMELLIAA', 'CAMELLIA', 'BOH',
    # Eggs
    'JY RESOURCES', 'JUTA RIA',
    # Plastics & packaging
    'REZA PLASTIC', 'REZA', 'HAMEED PLASTICS', 'HAMEED',
    # Vegetables
    'SAYUR', 'PASAR BORONG',
    # Drinks & wholesale
    'BESTARI WHOLESALE',
    # Seafood
    'FOOK LEONG', 'QUIWAVE OCEANIC', 'QUIWAVE',
    # Daily consumables
    'DAILY PAY',
    # Ice
    'EVEREST AISVARAM', 'EVEREST',
    # Catering
    'CATERERS AT TANJUNG', 'MYMOON',
    # Convenience
    'KK SUPERMART', 'KK MART',
    # Utility/common chains
    '99 SPEEDMART', '99', 'TESCO', 'LOTUSS', 'GIANT',
]

LEARNED_SUPPLIER_THRESHOLD = 3


def is_known_supplier(merchant) -> bool:
    """Check if merchant matches any known supplier (substring, case-insensitive)."""
    if not merchant:
        return False
    m = merchant.upper().strip()
    for known in KNOWN_SUPPLIERS:
        if known in m or m in known:
            return True
    return False


NON_PURCHASE_KEYWORDS = [
    'ADVANCE', 'ADVANS', 'ADVANCE SALARY',
    'PINJAM', 'PINJAMAN',
    'GAJI', 'SALARY', 'WAGES',
    'BONUS', 'KOMISEN', 'COMMISSION',
    'PETTY CASH', 'CASH OUT', 'WITHDRAW',
    'TIPS', 'BOCA',
    'REFUND', 'RETURN',
    'TRANSFER', 'BANK IN',
    'KILANG', 'TNB', 'BAYAR ELECTRIC', 'BAYAR AIR', 'BAYAR INTERNET',
]

# Explicit chat_id -> outlet overrides take precedence over title parsing.
GROUP_OUTLET_MAP: dict[int, str] = {}
OUTLET_TITLE_PREFIX = "khulafa"
OUTLET_TRAILING_NOISE = {"resit", "resits", "receipt", "receipts"}

zai_client = OpenAI(api_key=ZAI_API_KEY, base_url=ZAI_BASE_URL)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

OCR_PROMPT = (
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

flask_app = Flask(__name__)


@flask_app.get("/")
@flask_app.get("/health")
def health():
    return jsonify(status="ok", service="khulafa-resit-bot")


@flask_app.get("/webapp")
def webapp():
    return render_template(
        "dashboard.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
        receipts_table=RECEIPTS_TABLE,
    )


def run_health_server() -> None:
    flask_app.run(host="0.0.0.0", port=HEALTH_PORT, use_reloader=False)


async def extract_with_glm_chat(image_bytes: bytes) -> dict:
    """First-pass OCR via the glm-4.6v-flash chat completions endpoint.

    Returns a dict with the legacy schema {merchant, date, total, currency,
    items, raw_text}. ``bill_to`` is not extracted by this prompt.
    """
    start = time.monotonic()
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    response = await asyncio.to_thread(
        zai_client.chat.completions.create,
        model=ZAI_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        temperature=0.1,
    )
    latency = time.monotonic() - start
    content = response.choices[0].message.content or "{}"
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    usage = getattr(response, "usage", None)
    total_tokens = getattr(usage, "total_tokens", None) if usage else None
    logger.info(
        "glm-chat OCR response: latency=%.2fs image_bytes=%d resp_chars=%d total_tokens=%s",
        latency, len(image_bytes), len(content), total_tokens,
    )
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("glm-chat OCR returned non-JSON content (%d chars)", len(content))
        return {"raw_text": content, "_latency_s": round(latency, 3), "_total_tokens": total_tokens}
    parsed["date"] = normalize_date(parsed.get("date"))
    parsed["items"] = normalize_items(parsed.get("items"))
    parsed["_latency_s"] = round(latency, 3)
    parsed["_total_tokens"] = total_tokens
    return parsed


# Backwards-compatible alias so existing callers keep working.
extract_receipt = extract_with_glm_chat


VERIFY_PROMPT_TEMPLATE = (
    "You are a receipt audit assistant. A first-pass OCR extracted the following "
    "from this receipt photo:\n\n"
    "Merchant: {merchant}\n"
    "Date: {date}\n"
    "Total: RM{total}\n"
    "Items: {items_list}\n\n"
    "Re-examine the photo carefully and respond ONLY in JSON with:\n"
    "{{\n"
    "  \"verdict\": \"CONFIRMED\" | \"WRONG\" | \"PARTIAL\",\n"
    "  \"confidence\": 0-100,\n"
    "  \"errors\": [list of specific errors found, e.g. 'Total reads RM156.40 not RM165.40'],\n"
    "  \"corrections\": {{ \"merchant\": \"...\", \"total\": ..., \"items\": [...] }}  // only fields that need correction\n"
    "}}\n\n"
    "Be strict. If a total digit is unclear, flag it. If an item price doesn't "
    "match the item, flag it. If date format is mixed up, flag it. Return WRONG "
    "if any number is incorrect, PARTIAL if minor issues, CONFIRMED only if 100% "
    "accurate."
)


_VERDICT_COUNTS: dict[str, int] = {
    "CONFIRMED": 0,
    "PARTIAL": 0,
    "WRONG": 0,
    "UNCHECKED": 0,
}


def _format_items_for_prompt(items) -> str:
    if not isinstance(items, list) or not items:
        return "(none)"
    parts = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or "?"
        qty = it.get("qty")
        if qty is None:
            qty = it.get("quantity")
        price = it.get("price")
        bits = [str(name)]
        if qty not in (None, ""):
            bits.append(f"x{qty}")
        if price not in (None, ""):
            bits.append(f"RM{price}")
        parts.append(" ".join(bits))
    return "; ".join(parts) if parts else "(none)"


async def verify_extraction(image_bytes: bytes, extracted: dict) -> dict:
    """Second-pass audit of the OCR extraction. Returns dict with keys:
    verdict, confidence, errors, corrections. Raises on API failure."""
    merchant = extracted.get("merchant") or "(unknown)"
    date = extracted.get("receipt_date") or extracted.get("date") or "(unknown)"
    total = extracted.get("total")
    total_str = "(unknown)" if total in (None, "") else str(total)
    items_list = _format_items_for_prompt(extracted.get("items"))

    prompt = VERIFY_PROMPT_TEMPLATE.format(
        merchant=merchant, date=date, total=total_str, items_list=items_list
    )

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    response = await asyncio.to_thread(
        zai_client.chat.completions.create,
        model=ZAI_VERIFY_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        temperature=0.0,
    )
    content = response.choices[0].message.content or "{}"
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(content)

    verdict = str(parsed.get("verdict") or "").upper()
    if verdict not in ("CONFIRMED", "PARTIAL", "WRONG"):
        verdict = "WRONG"
    try:
        confidence = int(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    errors = parsed.get("errors") or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    corrections = parsed.get("corrections") or {}
    if not isinstance(corrections, dict):
        corrections = {}

    return {
        "verdict": verdict,
        "confidence": confidence,
        "errors": errors,
        "corrections": corrections,
    }


_outlet_column_available = True
_verification_columns_available = True
_bill_to_column_available = True
_receipt_type_column_available = True
_VERIFICATION_KEYS = ("verification_status", "verification_notes", "confidence")


def store_receipt(record: dict) -> dict:
    global _outlet_column_available, _verification_columns_available, _bill_to_column_available, _receipt_type_column_available
    payload = dict(record)
    # Postgres' date column rejects human formats like "25/4/26"; coerce to ISO
    # before insert. None passes through (column is nullable).
    payload["receipt_date"] = normalize_date(payload.get("receipt_date"))
    # Postgres' numeric column rejects "RM13.00"; strip currency/separators
    # before insert. None passes through (column is nullable).
    if "total" in payload:
        payload["total"] = normalize_total(payload.get("total"))
    if not _outlet_column_available:
        payload.pop("outlet", None)
    if not _verification_columns_available:
        for key in _VERIFICATION_KEYS:
            payload.pop(key, None)
    if not _bill_to_column_available:
        payload.pop("bill_to", None)
    if not _receipt_type_column_available:
        payload.pop("receipt_type", None)
    try:
        result = supabase.table(RECEIPTS_TABLE).insert(payload).execute()
    except Exception as exc:
        msg = str(exc).lower()
        if "outlet" in payload and "outlet" in msg:
            logger.warning(
                "receipts.outlet column missing — apply migrations/0001_add_outlet_column.sql. "
                "Saving without outlet for now."
            )
            _outlet_column_available = False
            payload.pop("outlet", None)
            result = supabase.table(RECEIPTS_TABLE).insert(payload).execute()
        elif any(k in payload for k in _VERIFICATION_KEYS) and any(k in msg for k in _VERIFICATION_KEYS):
            logger.warning(
                "receipts verification columns missing — apply "
                "migrations/0002_add_verification_columns.sql. Saving without "
                "verification fields for now."
            )
            _verification_columns_available = False
            for key in _VERIFICATION_KEYS:
                payload.pop(key, None)
            result = supabase.table(RECEIPTS_TABLE).insert(payload).execute()
        elif "bill_to" in payload and "bill_to" in msg:
            logger.warning(
                "receipts.bill_to column missing — apply migrations/add_bill_to_column.sql. "
                "Saving without bill_to for now."
            )
            _bill_to_column_available = False
            payload.pop("bill_to", None)
            result = supabase.table(RECEIPTS_TABLE).insert(payload).execute()
        elif "receipt_type" in payload and "receipt_type" in msg:
            logger.warning(
                "receipts.receipt_type column missing — apply "
                "migrations/0004_receipt_classifier.sql. Saving without "
                "receipt_type for now."
            )
            _receipt_type_column_available = False
            payload.pop("receipt_type", None)
            result = supabase.table(RECEIPTS_TABLE).insert(payload).execute()
        else:
            raise
    return result.data[0] if result.data else record


def store_staff_advance(
    receipt_id, outlet: str | None, staff_name: str | None,
    amount: float | None, advance_date: str | None, issued_by: str | None,
) -> None:
    payload = {
        "receipt_id": receipt_id,
        "outlet": outlet or "UNKNOWN",
        "staff_name": staff_name,
        "amount": normalize_total(amount) or 0,
        "advance_date": normalize_date(advance_date) or _today_my(),
        "issued_by": issued_by,
    }
    supabase.table(STAFF_ADVANCES_TABLE).insert(payload).execute()


def store_fixed_cost(
    receipt_id, outlet: str | None, category: str, vendor: str | None,
    amount: float | None, cost_date: str | None,
) -> None:
    payload = {
        "receipt_id": receipt_id,
        "outlet": outlet or "UNKNOWN",
        "category": category,
        "vendor": vendor,
        "amount": normalize_total(amount) or 0,
        "cost_date": normalize_date(cost_date) or _today_my(),
    }
    supabase.table(FIXED_COSTS_TABLE).insert(payload).execute()


def store_petty_cash(
    receipt_id, outlet: str | None, description: str | None,
    amount: float | None, cost_date: str | None,
) -> None:
    payload = {
        "receipt_id": receipt_id,
        "outlet": outlet or "UNKNOWN",
        "description": description,
        "amount": normalize_total(amount) or 0,
        "cost_date": normalize_date(cost_date) or _today_my(),
    }
    supabase.table(PETTY_CASH_TABLE).insert(payload).execute()


# === PR #29b: low-confidence manual-review queue =============================

def store_pending_review(record: dict) -> dict:
    payload = dict(record)
    payload["parsed_date"] = normalize_date(payload.get("parsed_date"))
    if payload.get("parsed_total") is not None:
        payload["parsed_total"] = normalize_total(payload.get("parsed_total"))
    result = supabase.table(PENDING_REVIEW_TABLE).insert(payload).execute()
    return result.data[0] if result.data else record


def fetch_pending_review(review_id) -> dict | None:
    result = (
        supabase.table(PENDING_REVIEW_TABLE)
        .select("*")
        .eq("id", review_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def update_pending_review(
    review_id, status: str, reviewer_chat_id, edited_data: dict | None = None
) -> None:
    payload = {
        "status": status,
        "reviewer_chat_id": reviewer_chat_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    if edited_data is not None:
        payload["edited_data"] = edited_data
    supabase.table(PENDING_REVIEW_TABLE).update(payload).eq("id", review_id).execute()


def promote_pending_to_receipt(pending: dict, edits: dict | None = None) -> dict:
    """Copy an approved/edited ``pending_review`` row into ``receipts``.

    Only the parsed fields survive the queue (the table doesn't carry
    raw_text / receipt_type), so promoted rows default to UNKNOWN type and do
    not re-run price aggregation — a documented v1 limitation."""
    parsed = {
        "merchant": pending.get("parsed_merchant"),
        "total": pending.get("parsed_total"),
        "receipt_date": pending.get("parsed_date"),
        "items": pending.get("parsed_items") or [],
    }
    parsed = apply_edits_to_parsed(parsed, edits)
    merchant_raw = parsed.get("merchant")
    chat_id = pending.get("chat_id")
    record = {
        "chat_id": chat_id,
        "message_id": pending.get("telegram_message_id"),
        "merchant": merchant_raw.upper().strip() if isinstance(merchant_raw, str) else merchant_raw,
        "outlet": derive_outlet(chat_id, None),
        "receipt_date": parsed.get("receipt_date"),
        "total": parsed.get("total"),
        "currency": "MYR",
        "items": parsed.get("items"),
        "verification_status": "MANUAL_REVIEW",
        "verification_notes": f"approved via review queue (pending #{pending.get('id')})",
        "confidence": pending.get("confidence"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return store_receipt(record)


def derive_outlet(chat_id: int | None, chat_title: str | None) -> str | None:
    if chat_id is not None and chat_id in GROUP_OUTLET_MAP:
        return GROUP_OUTLET_MAP[chat_id]
    if not chat_title:
        return None
    cleaned = chat_title.strip()
    if cleaned.lower().startswith(OUTLET_TITLE_PREFIX):
        cleaned = cleaned[len(OUTLET_TITLE_PREFIX):].strip(" -_:")
    tokens = cleaned.split()
    while tokens and tokens[-1].lower() in OUTLET_TRAILING_NOISE:
        tokens.pop()
    if not tokens:
        return None
    remainder = " ".join(tokens)
    if any(ch.isdigit() for ch in remainder):
        return remainder.upper()
    return remainder.title()


def format_alert(record: dict, parsed: dict, outlet: str | None = None) -> str:
    merchant = parsed.get("merchant") or "Unknown merchant"
    total = parsed.get("total")
    currency = parsed.get("currency") or ""
    date = parsed.get("receipt_date") or parsed.get("date") or "—"
    bill_to = parsed.get("bill_to") or record.get("bill_to")
    user = record.get("telegram_username") or record.get("telegram_user_id")
    total_str = f"{total} {currency}".strip() if total is not None else "n/a"
    lines = ["New receipt logged", f"From: {user}"]
    if outlet:
        lines.append(f"Outlet: {outlet}")
    lines.extend([
        f"Merchant: {merchant}",
        f"Date: {date}",
        f"Total: {total_str}",
    ])
    if bill_to:
        lines.append(f"Bill To: {bill_to}")
    item_lines = format_items(parsed.get("items"))
    if item_lines:
        lines.append("")
        lines.append("Items:")
        lines.extend(item_lines)
    return "\n".join(lines)


def format_items(items) -> list[str]:
    if not isinstance(items, list):
        return []
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or "?"
        qty = item.get("qty")
        if qty is None:
            qty = item.get("quantity")
        price = item.get("price")
        qty_part = f" x{qty}" if qty not in (None, "") else ""
        price_part = f" — {price}" if price not in (None, "") else ""
        lines.append(f"• {name}{qty_part}{price_part}")
    return lines


def _to_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _today_my() -> str:
    return datetime.now(MALAYSIA_TZ).date().isoformat()


def _check_big_purchase(chat_id: int, total: float) -> str | None:
    since = (datetime.now(MALAYSIA_TZ).date() - timedelta(days=BIG_PURCHASE_LOOKBACK_DAYS)).isoformat()
    res = (
        supabase.table(RECEIPTS_TABLE)
        .select("total")
        .eq("chat_id", chat_id)
        .gte("receipt_date", since)
        .execute()
    )
    totals = [t for r in (res.data or []) if (t := _to_float(r.get("total"))) is not None]
    if len(totals) < 3:
        return None
    avg = sum(totals) / len(totals)
    if avg > 0 and total > BIG_PURCHASE_MULTIPLIER * avg:
        return build_big_purchase_message(total, avg, len(totals))
    return None


def _check_new_supplier(chat_id: int, merchant: str, total: float, current_id) -> str | None:
    if not merchant or total <= NEW_SUPPLIER_THRESHOLD:
        return None
    if is_known_supplier(merchant):
        return None
    res = (
        supabase.table(RECEIPTS_TABLE)
        .select("id")
        .eq("chat_id", chat_id)
        .eq("merchant", merchant)
        .limit(LEARNED_SUPPLIER_THRESHOLD + 1)
        .execute()
    )
    rows = res.data or []
    if current_id is not None:
        rows = [r for r in rows if r.get("id") != current_id]
    if len(rows) >= LEARNED_SUPPLIER_THRESHOLD:
        return None
    if rows:
        return None
    return (
        "புதிய கடை! ஏன் வழக்கமான கடைல வாங்கல? / "
        f"Supplier baru ({merchant})! Kenapa tak beli dari supplier biasa?"
    )


def _check_suspicious_items(chat_id: int, merchant: str, items: list) -> str | None:
    if not merchant or not isinstance(items, list) or not items:
        return None
    since = (
        datetime.now(MALAYSIA_TZ).date() - timedelta(days=SUSPICIOUS_ITEM_LOOKBACK_DAYS)
    ).isoformat()
    res = (
        supabase.table(RECEIPTS_TABLE)
        .select("items")
        .eq("chat_id", chat_id)
        .eq("merchant", merchant)
        .gte("receipt_date", since)
        .execute()
    )

    history: dict[str, list[float]] = {}
    for row in res.data or []:
        # Receipts saved before the items-schema fix may have stored bare
        # strings; normalize defensively so .get() never hits a non-dict.
        for prev in normalize_items(row.get("items")):
            name = (prev.get("name") or "").strip().lower()
            price = _to_float(prev.get("price"))
            if name and price is not None:
                history.setdefault(name, []).append(price)

    flagged = []
    for it in normalize_items(items):
        name = (it.get("name") or "").strip()
        price = _to_float(it.get("price"))
        if not name or price is None:
            continue
        prev_prices = history.get(name.lower())
        if not prev_prices:
            continue
        avg = sum(prev_prices) / len(prev_prices)
        if avg > 0 and price > SUSPICIOUS_PRICE_RATIO * avg:
            flagged.append(f"{name} (RM{price:.2f} vs avg RM{avg:.2f})")

    if not flagged:
        return None
    return (
        "விலை அதிகம்! வேற இடத்துல cheap கிடைக்குமா check பண்ணினீங்களா? / "
        "Harga mahal dari minggu lepas! Sudah check tempat lain ke? "
        f"({'; '.join(flagged[:3])})"
    )


def _check_duplicate_receipt(
    chat_id: int, merchant: str, total: float, receipt_date: str | None, current_id
) -> str | None:
    if not merchant or not receipt_date or total <= 0:
        return None
    res = (
        supabase.table(RECEIPTS_TABLE)
        .select("id, total")
        .eq("chat_id", chat_id)
        .eq("merchant", merchant)
        .eq("receipt_date", receipt_date)
        .execute()
    )
    for row in res.data or []:
        if current_id is not None and row.get("id") == current_id:
            continue
        prev_total = _to_float(row.get("total"))
        if prev_total is None or prev_total <= 0:
            continue
        if abs(prev_total - total) / max(prev_total, total) <= DUPLICATE_TOTAL_TOLERANCE:
            return (
                "இதே கடையிலிருந்து இரண்டு முறை! "
                f"Same shop ({merchant}) 2 kali hari ni — sengaja ke?"
            )
    return None


def should_skip_audit(receipt_data):
    merchant = (receipt_data.get('merchant') or '').upper()
    items = receipt_data.get('items') or []
    items_text = ' '.join(str(i).upper() for i in items)
    combined = f'{merchant} {items_text}'
    for keyword in NON_PURCHASE_KEYWORDS:
        if keyword in combined:
            return f'non_purchase:{keyword}'
    if merchant in ('UNKNOWN MERCHANT', 'UNKNOWN', '', 'N/A'):
        return 'unknown_merchant'
    total = receipt_data.get('total') or 0
    if total < 50:
        return f'small_amount:RM{total}'
    return None


def run_audit_checks(stored: dict, parsed: dict) -> list[tuple[str, str]]:
    receipt_data = {
        "merchant": stored.get("merchant"),
        "items": parsed.get("items"),
        "total": _to_float(stored.get("total")),
    }
    skip_reason = should_skip_audit(receipt_data)
    if skip_reason:
        logger.info(f'Skipping audit checks: {skip_reason}')
        return []

    chat_id = stored.get("chat_id")
    total = _to_float(stored.get("total"))
    merchant = stored.get("merchant")
    receipt_date = stored.get("receipt_date")
    current_id = stored.get("id")
    items = parsed.get("items") if isinstance(parsed.get("items"), list) else []

    findings: list[tuple[str, str]] = []
    if chat_id is None or total is None:
        return findings

    try:
        if (q := _check_duplicate_receipt(chat_id, merchant, total, receipt_date, current_id)):
            findings.append(("duplicate_receipt", q))
    except Exception:
        logger.exception("duplicate_receipt check failed")

    try:
        if (q := _check_new_supplier(chat_id, merchant, total, current_id)):
            findings.append(("new_supplier", q))
    except Exception:
        logger.exception("new_supplier check failed")

    try:
        if (q := _check_big_purchase(chat_id, total)):
            findings.append(("big_purchase", q))
    except Exception:
        logger.exception("big_purchase check failed")

    try:
        if (q := _check_suspicious_items(chat_id, merchant, items)):
            findings.append(("suspicious_item", q))
    except Exception:
        logger.exception("suspicious_item check failed")

    return findings


def insert_audit_question(
    receipt_id, chat_id: int, question_type: str, question_text: str, question_message_id: int
) -> None:
    payload = {
        "receipt_id": receipt_id,
        "chat_id": chat_id,
        "question_type": question_type,
        "question_text": question_text,
        "question_message_id": question_message_id,
    }
    supabase.table(AUDIT_TABLE).insert(payload).execute()


def save_audit_reply(chat_id: int, reply_to_message_id: int, manager_reply: str) -> bool:
    res = (
        supabase.table(AUDIT_TABLE)
        .select("id")
        .eq("chat_id", chat_id)
        .eq("question_message_id", reply_to_message_id)
        .is_("replied_at", "null")
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return False
    audit_id = rows[0]["id"]
    supabase.table(AUDIT_TABLE).update(
        {
            "manager_reply": manager_reply,
            "replied_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", audit_id).execute()
    return True


async def ask_audit_questions(
    context: ContextTypes.DEFAULT_TYPE,
    stored: dict,
    findings: list[tuple[str, str]],
) -> None:
    chat_id = stored.get("chat_id")
    receipt_id = stored.get("id")
    if chat_id is None or not findings:
        return

    for question_type, question_text in findings:
        try:
            sent = await context.bot.send_message(chat_id=chat_id, text=question_text)
        except Exception:
            logger.exception("Failed to post audit question")
            continue
        try:
            await asyncio.to_thread(
                insert_audit_question,
                receipt_id,
                chat_id,
                question_type,
                question_text,
                sent.message_id,
            )
        except Exception:
            logger.exception("Failed to record audit question")


def _apply_corrections(parsed: dict, corrections: dict) -> list[str]:
    """Mutate parsed in place with whitelisted correction fields. Returns
    a short list of human-readable change descriptions."""
    changes: list[str] = []
    if not isinstance(corrections, dict):
        return changes
    for key in ("merchant", "date", "total", "currency", "items"):
        if key not in corrections:
            continue
        new_val = corrections[key]
        if key == "date":
            # Verifier returns dates in human formats (e.g. "25/4/26"); coerce
            # to ISO so OCR and verifier outputs share one shape downstream.
            new_val = normalize_date(new_val)
        elif key == "total":
            # Verifier returns totals in human formats (e.g. "RM13.00"); strip
            # currency/separators so downstream insert sees a plain number.
            new_val = normalize_total(new_val)
        elif key == "items":
            # Verifier may return items as bare strings just like the OCR
            # pass; normalize so downstream .get() calls don't blow up.
            new_val = normalize_items(new_val)
        old_val = parsed.get(key)
        if new_val == old_val:
            continue
        parsed[key] = new_val
        if key == "items":
            changes.append("items updated")
        elif key == "total":
            changes.append(f"total {old_val}→{new_val}")
        else:
            changes.append(f"{key} {old_val}→{new_val}")
    return changes


def _bump_verdict(verdict: str) -> None:
    _VERDICT_COUNTS[verdict] = _VERDICT_COUNTS.get(verdict, 0) + 1
    total = sum(_VERDICT_COUNTS.values())
    logger.info(
        "OCR verification stats — total=%d CONFIRMED=%d PARTIAL=%d WRONG=%d UNCHECKED=%d",
        total,
        _VERDICT_COUNTS.get("CONFIRMED", 0),
        _VERDICT_COUNTS.get("PARTIAL", 0),
        _VERDICT_COUNTS.get("WRONG", 0),
        _VERDICT_COUNTS.get("UNCHECKED", 0),
    )


async def run_verification(image_bytes: bytes, parsed: dict) -> tuple[dict, str]:
    """Run the second-pass verification, mutating `parsed` if corrections
    apply. Returns ({status, notes, confidence}, user_reply_prefix)."""
    try:
        result = await verify_extraction(image_bytes, parsed)
    except Exception as exc:
        logger.exception("Verification failed: %s", exc)
        _bump_verdict("UNCHECKED")
        return (
            {"status": "UNCHECKED", "notes": f"verifier error: {exc}", "confidence": None},
            "",
        )

    verdict = result["verdict"]
    confidence = result["confidence"]
    errors = result["errors"]
    corrections = result["corrections"]
    notes = "; ".join(str(e) for e in errors) if errors else None

    if verdict == "CONFIRMED":
        _bump_verdict("CONFIRMED")
        return (
            {"status": "CONFIRMED", "notes": notes, "confidence": confidence},
            f"✅ Verified ({confidence}%)",
        )

    if verdict == "PARTIAL":
        changes = _apply_corrections(parsed, corrections)
        _bump_verdict("PARTIAL")
        change_text = ", ".join(changes) if changes else (notes or "minor issues")
        return (
            {"status": "PARTIAL", "notes": notes, "confidence": confidence},
            f"⚠️ Verified with corrections ({confidence}%): {change_text}",
        )

    # verdict == "WRONG"
    _bump_verdict("WRONG")
    if confidence < 50:
        total = parsed.get("total")
        return (
            {"status": "WRONG", "notes": notes, "confidence": confidence},
            f"❓ OCR uncertain ({confidence}%) — please verify total RM{total} and items",
        )
    changes = _apply_corrections(parsed, corrections)
    summary = ", ".join(changes) if changes else (notes or "see verifier notes")
    return (
        {"status": "WRONG", "notes": notes, "confidence": confidence},
        f"✅ Auto-corrected ({confidence}%): {summary}",
    )


def _review_keyboard(review_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save as-is", callback_data=f"review:{review_id}:save"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"review:{review_id}:edit"),
        InlineKeyboardButton("❌ Discard", callback_data=f"review:{review_id}:discard"),
    ]])


def _format_review_caption(pending: dict) -> str:
    conf = pending.get("confidence")
    items = pending.get("parsed_items") or []
    return (
        "🔎 Receipt needs review\n"
        f"Merchant: {pending.get('parsed_merchant') or '—'}\n"
        f"Total: RM{pending.get('parsed_total') if pending.get('parsed_total') is not None else '—'}\n"
        f"Date: {pending.get('parsed_date') or '—'}\n"
        f"Items: {len(items)}\n"
        f"Confidence: {conf if conf is not None else '—'}\n"
        f"Reason: {pending.get('reason') or '—'}"
    )


async def _dm_reviewers(context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    keyboard = _review_keyboard(pending.get("id"))
    caption = _format_review_caption(pending)
    photo_file_id = pending.get("photo_file_id")
    for reviewer_id in REVIEWER_CHAT_IDS:
        try:
            if photo_file_id:
                await context.bot.send_photo(
                    chat_id=reviewer_id, photo=photo_file_id,
                    caption=caption, reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    chat_id=reviewer_id, text=caption, reply_markup=keyboard,
                )
        except Exception:
            logger.exception("Failed to DM reviewer %s", reviewer_id)


async def route_to_review(
    message, context: ContextTypes.DEFAULT_TYPE, parsed: dict, verification: dict
) -> None:
    confidence = verification.get("confidence")
    ocr_conflict = total_conflicts_with_item_sum(
        _to_float(parsed.get("total")), parsed.get("items")
    )
    reason = build_review_reason(confidence, verification.get("status"), ocr_conflict)
    photo = message.photo[-1] if message.photo else None
    pending_record = {
        "telegram_message_id": message.message_id,
        "chat_id": message.chat_id,
        "photo_file_id": photo.file_id if photo else None,
        "confidence": confidence,
        "reason": reason,
        "status": "pending",
        **serialize_parsed_for_review(parsed),
    }
    try:
        stored = await asyncio.to_thread(store_pending_review, pending_record)
    except Exception:
        logger.exception("Failed to queue receipt for manual review")
        await message.reply_text(
            "Couldn't queue this receipt for review — please try resending."
        )
        return
    logger.info(
        "Receipt queued for review: pending_id=%s confidence=%s reason=%s",
        stored.get("id"), confidence, reason,
    )
    await message.reply_text(
        f"🔎 Receipt flagged for review (confidence {confidence if confidence is not None else '—'}). "
        "A reviewer will confirm it shortly."
    )
    await _dm_reviewers(context, stored)


async def _finalize_review(
    review_id, reviewer_chat_id, status: str, edits: dict | None = None
) -> dict | None:
    """Promote a pending row to `receipts` (for approve/edit) and flip its
    status. Returns the stored receipt, or None if the row is gone/handled."""
    pending = await asyncio.to_thread(fetch_pending_review, review_id)
    if not pending or pending.get("status") != "pending":
        return None
    stored = await asyncio.to_thread(promote_pending_to_receipt, pending, edits)
    await asyncio.to_thread(
        update_pending_review, review_id, status, reviewer_chat_id, edits
    )
    return stored


async def handle_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the ✅ Save as-is and ❌ Discard inline buttons. The ✏️ Edit
    button is the entry point of the edit ConversationHandler instead."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    reviewer_chat_id = query.from_user.id if query.from_user else None
    if not is_reviewer(reviewer_chat_id):
        logger.info("Ignoring review callback from non-reviewer chat_id=%s", reviewer_chat_id)
        return
    try:
        _, raw_id, action = (query.data or "").split(":", 2)
        review_id = int(raw_id)
    except (ValueError, AttributeError):
        return

    if action == "discard":
        await asyncio.to_thread(
            update_pending_review, review_id, "rejected", reviewer_chat_id
        )
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Discarded — nothing saved to receipts.")
        return

    if action == "save":
        try:
            stored = await _finalize_review(review_id, reviewer_chat_id, "approved")
        except Exception:
            logger.exception("Failed to approve pending review %s", review_id)
            await query.message.reply_text("Failed to save — please retry.")
            return
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)
        if stored is None:
            await query.message.reply_text("Already handled.")
        else:
            await query.message.reply_text(
                f"✅ Saved receipt #{stored.get('id')} as-is."
            )


async def review_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    reviewer_chat_id = query.from_user.id if query.from_user else None
    if not is_reviewer(reviewer_chat_id):
        logger.info("Ignoring edit callback from non-reviewer chat_id=%s", reviewer_chat_id)
        return ConversationHandler.END
    try:
        _, raw_id, _action = (query.data or "").split(":", 2)
        review_id = int(raw_id)
    except (ValueError, AttributeError):
        return ConversationHandler.END
    pending = await asyncio.to_thread(fetch_pending_review, review_id)
    if not pending or pending.get("status") != "pending":
        await query.message.reply_text("This item was already handled.")
        return ConversationHandler.END
    context.user_data["review_id"] = review_id
    context.user_data["review_edits"] = {}
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Editing. Current total: RM{pending.get('parsed_total')}.\n"
        "Send the corrected total, or 'skip' to keep it."
    )
    return REVIEW_EDIT_TOTAL


async def review_edit_total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if text.lower() != "skip":
        value = normalize_total(text)
        if value is None:
            await update.effective_message.reply_text(
                "Couldn't read that as a number. Send a total like 42.00, or 'skip'."
            )
            return REVIEW_EDIT_TOTAL
        context.user_data["review_edits"]["total"] = value
    await update.effective_message.reply_text(
        "Corrected merchant? Send the name, or 'skip'."
    )
    return REVIEW_EDIT_MERCHANT


async def review_edit_merchant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if text.lower() != "skip":
        context.user_data["review_edits"]["merchant"] = text
    await update.effective_message.reply_text(
        "Corrected date (YYYY-MM-DD)? Send it, or 'skip'."
    )
    return REVIEW_EDIT_DATE


async def review_edit_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if text.lower() != "skip":
        iso = normalize_date(text)
        if iso is None:
            await update.effective_message.reply_text(
                "Couldn't read that date. Send YYYY-MM-DD, or 'skip'."
            )
            return REVIEW_EDIT_DATE
        context.user_data["review_edits"]["receipt_date"] = iso

    review_id = context.user_data.get("review_id")
    edits = context.user_data.get("review_edits", {})
    reviewer_chat_id = update.effective_user.id if update.effective_user else None
    try:
        stored = await _finalize_review(review_id, reviewer_chat_id, "edited", edits)
    except Exception:
        logger.exception("Failed to save edited review %s", review_id)
        await update.effective_message.reply_text("Failed to save edits — please retry.")
        return ConversationHandler.END
    finally:
        context.user_data.pop("review_id", None)
        context.user_data.pop("review_edits", None)
    if stored is None:
        await update.effective_message.reply_text("This item was already handled.")
    else:
        await update.effective_message.reply_text(
            f"✏️ Saved receipt #{stored.get('id')} with your edits."
        )
    return ConversationHandler.END


async def review_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("review_id", None)
    context.user_data.pop("review_edits", None)
    await update.effective_message.reply_text("Edit cancelled — item left pending.")
    return ConversationHandler.END


def build_review_edit_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(review_edit_start, pattern=r"^review:\d+:edit$")
        ],
        states={
            REVIEW_EDIT_TOTAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, review_edit_total)
            ],
            REVIEW_EDIT_MERCHANT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, review_edit_merchant)
            ],
            REVIEW_EDIT_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, review_edit_date)
            ],
        },
        fallbacks=[CommandHandler("cancel", review_edit_cancel)],
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.photo:
        return

    chat = message.chat
    chat_title = chat.title if chat else None
    outlet = derive_outlet(message.chat_id, chat_title)
    logger.info(
        "Receipt photo received: chat_id=%s chat_title=%r outlet=%s ocr_provider=%s",
        message.chat_id,
        chat_title,
        outlet,
        ZAI_OCR_PROVIDER,
    )

    await message.reply_text("Processing receipt…")

    photo = message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    if IMAGE_RESIZE_ENABLED:
        image_bytes = await asyncio.to_thread(
            resize_for_ocr, image_bytes, IMAGE_MAX_DIM
        )

    ocr_start = time.monotonic()
    try:
        if ZAI_OCR_PROVIDER == "glm-ocr":
            from ocr_glm import extract_with_glm_ocr
            parsed = await extract_with_glm_ocr(image_bytes)
        else:
            parsed = await extract_with_glm_chat(image_bytes)
    except Exception:
        logger.exception("OCR failed (provider=%s)", ZAI_OCR_PROVIDER)
        await message.reply_text("Failed to read receipt. Try a clearer photo.")
        return
    ocr_latency = time.monotonic() - ocr_start
    logger.info(
        "OCR pipeline complete: provider=%s total_latency=%.2fs merchant=%r total=%s",
        ZAI_OCR_PROVIDER, ocr_latency, parsed.get("merchant"), parsed.get("total"),
    )

    # Normalise to a single internal shape: chat returns "date", glm-ocr returns
    # "receipt_date". Use receipt_date as canonical going forward.
    if parsed.get("receipt_date") is None and parsed.get("date") is not None:
        parsed["receipt_date"] = parsed.get("date")

    # Single safety net for both OCR providers: ensure items is always a list
    # of dicts before anything (verifier, alerts, audit, Supabase) reads it.
    parsed["items"] = normalize_items(parsed.get("items"))

    verification, verify_prefix = await run_verification(image_bytes, parsed)

    # PR #24: classify receipt type before any downstream logic. Only
    # SUPPLIER_PURCHASE receipts trigger price aggregation, spike alerts,
    # and anomaly checks. STAFF_ADVANCE/UTILITY/RENT_LICENSE/PETTY_CASH
    # are routed to their own side tables; UNKNOWN gets a manual review
    # prompt.
    # PR #28: pass merchant explicitly. Some OCR responses return a clean
    # `merchant` field but a sparse `raw_text` that omits the header,
    # which caused 132+ EVEREST/MYMOON/BABAS receipts to be mis-classified
    # as UNKNOWN because the whitelist never saw the merchant name.
    classification = classify_receipt(
        ocr_text=parsed.get("raw_text") or "",
        parsed_items=parsed.get("items"),
        total=_to_float(parsed.get("total")),
        merchant=parsed.get("merchant"),
    )

    user = update.effective_user
    merchant_raw = parsed.get("merchant")
    record = {
        "telegram_user_id": user.id if user else None,
        "telegram_username": user.username if user else None,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "merchant": merchant_raw.upper().strip() if isinstance(merchant_raw, str) else merchant_raw,
        "outlet": outlet,
        "receipt_date": parsed.get("receipt_date") or parsed.get("date"),
        "total": parsed.get("total"),
        "currency": parsed.get("currency"),
        "items": parsed.get("items"),
        "bill_to": parsed.get("bill_to"),
        "raw_text": parsed.get("raw_text"),
        "verification_status": verification["status"],
        "verification_notes": verification["notes"],
        "confidence": verification["confidence"],
        "receipt_type": classification.receipt_type.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # PR #29b: low-confidence receipts do NOT auto-save. Route them to the
    # manual-review queue and DM an authorised reviewer instead, so bad OCR
    # never reaches `receipts`/`item_prices` and poisons price intelligence.
    # We gate on the stored confidence — the second-pass verifier score.
    if should_queue(verification["confidence"]):
        await route_to_review(message, context, parsed, verification)
        return

    try:
        stored = await asyncio.to_thread(store_receipt, record)
    except Exception:
        logger.exception("Supabase insert failed")
        await message.reply_text("Saved OCR locally but database write failed.")
        stored = record

    user_alert = format_alert(stored, parsed)
    if verify_prefix:
        user_alert = f"{verify_prefix}\n\n{user_alert}"
    ops_alert = format_alert(stored, parsed, outlet=outlet)
    await message.reply_text(user_alert)
    try:
        await context.bot.send_message(chat_id=ALERT_CHAT_ID, text=ops_alert)
    except Exception:
        logger.exception("Failed to send alert to ALERT_CHAT_ID")

    # PR #24: route non-purchase receipts to their side tables and skip
    # everything below. Only SUPPLIER_PURCHASE flows through price
    # aggregation, spike detection, anomaly detection, and audit checks.
    receipt_id = stored.get("id")
    receipt_type = classification.receipt_type

    if receipt_type == ReceiptType.STAFF_ADVANCE:
        try:
            await asyncio.to_thread(
                store_staff_advance,
                receipt_id,
                outlet,
                classification.extracted_staff_name,
                _to_float(stored.get("total")),
                stored.get("receipt_date"),
                classification.extracted_vendor,
            )
            logger.info(
                "Staff advance logged: receipt=%s staff=%s amount=%s",
                receipt_id, classification.extracted_staff_name, stored.get("total"),
            )
            if not classification.extracted_staff_name:
                await message.reply_text(
                    "💰 Advance dicatat tapi nama staff tak dapat extract. "
                    "Reply /advances untuk update nama."
                )
        except Exception:
            logger.exception("Failed to store staff advance")
        return

    if receipt_type in (ReceiptType.UTILITY, ReceiptType.RENT_LICENSE):
        category = "utility" if receipt_type == ReceiptType.UTILITY else "rent_license"
        try:
            await asyncio.to_thread(
                store_fixed_cost,
                receipt_id,
                outlet,
                category,
                classification.extracted_vendor,
                _to_float(stored.get("total")),
                stored.get("receipt_date"),
            )
            logger.info(
                "Fixed cost logged: receipt=%s category=%s vendor=%s amount=%s",
                receipt_id, category, classification.extracted_vendor, stored.get("total"),
            )
        except Exception:
            logger.exception("Failed to store fixed cost")
        return

    if receipt_type == ReceiptType.PETTY_CASH:
        try:
            description = classification.extracted_vendor or stored.get("merchant")
            await asyncio.to_thread(
                store_petty_cash,
                receipt_id,
                outlet,
                description,
                _to_float(stored.get("total")),
                stored.get("receipt_date"),
            )
            logger.info(
                "Petty cash logged: receipt=%s desc=%s amount=%s",
                receipt_id, description, stored.get("total"),
            )
        except Exception:
            logger.exception("Failed to store petty cash")
        return

    if receipt_type == ReceiptType.UNKNOWN:
        try:
            await context.bot.send_message(
                chat_id=ALERT_CHAT_ID,
                text=(
                    "⚠️ Manual review needed — resit tak dapat classify.\n"
                    f"Outlet: {outlet or '—'}  Merchant: {stored.get('merchant') or '—'}  "
                    f"Total: RM{stored.get('total') or '—'}\n"
                    "Tolong check dan tag jenis resit secara manual."
                ),
            )
        except Exception:
            logger.exception("Failed to send manual review alert")
        return

    # Fall-through: SUPPLIER_PURCHASE only.
    # === Price aggregation: per-item rows into item_prices ===
    # Passive data collection for PR #24 (price-spike detection). Failure
    # here MUST NOT crash the receipt pipeline — broad except + lazy import.
    try:
        from price_aggregation import classify_and_extract_items, save_item_prices
        from outlet_mapping import outlet_from_chat_title

        if receipt_id is not None:
            price_records = classify_and_extract_items(parsed.get("items"))
            inserted = await asyncio.to_thread(
                save_item_prices,
                supabase,
                receipt_id,
                stored.get("receipt_date"),
                outlet_from_chat_title(chat_title),
                stored.get("chat_id"),
                stored.get("merchant"),
                price_records,
            )
            if inserted:
                logger.info(
                    "Saved %d item prices for receipt %s", inserted, receipt_id
                )
    except Exception as e:
        logger.warning("Price aggregation failed (non-critical): %s", e)
    # === End price aggregation ===

    # === Price spike detection (PR #25) ===
    # Compare each just-saved item against historical averages (merchant
    # scoped first, global fallback). Send Style A alert per spike to
    # ALERT_CHAT_ID. Failure MUST NOT crash the receipt pipeline —
    # broad except + lazy import, same pattern as PR #23b.
    try:
        from price_aggregation import classify_and_extract_items
        from price_spike_detection import detect_spikes, format_spike_message

        receipt_id = stored.get("id")
        if receipt_id is not None:
            price_records = classify_and_extract_items(parsed.get("items"))
            spikes = await asyncio.to_thread(
                detect_spikes,
                supabase,
                price_records,
                receipt_id,
                stored.get("merchant"),
            )
            for spike in spikes:
                msg = format_spike_message(spike)
                if not msg:
                    continue
                try:
                    await context.bot.send_message(
                        chat_id=ALERT_CHAT_ID, text=msg
                    )
                except Exception:
                    logger.exception(
                        "Failed to send spike alert to ALERT_CHAT_ID"
                    )
            if spikes:
                logger.info(
                    "Price spikes alerted: %d for receipt %s",
                    len(spikes),
                    receipt_id,
                )
    except Exception as e:
        logger.warning("Price spike detection failed (non-critical): %s", e)
    # === End price spike detection ===

    # === Intelligence layer: anomaly detection ===
    try:
        from item_canonicalization import canonicalize_supplier
        from historical_context import detect_anomaly
        from outlet_mapping import outlet_from_chat_title

        outlet_code = outlet_from_chat_title(chat_title)
        merchant_for_canon = stored.get("merchant") or parsed.get("merchant")
        canon_result = canonicalize_supplier(merchant_for_canon)
        canonical_category = canon_result.get("canonical")
        total_for_anomaly = _to_float(stored.get("total"))

        if outlet_code and canonical_category and total_for_anomaly:
            anomaly = detect_anomaly(
                outlet_code, canonical_category, total_for_anomaly
            )
            if anomaly.get("is_anomaly"):
                anomaly_text = (
                    anomaly["message_short"] + "\n\n" + anomaly["message_detail"]
                )
                await message.reply_text(anomaly_text)
                logger.info(
                    "Anomaly detected: outlet=%s category=%s amount=%s severity=%s",
                    outlet_code, canonical_category, total_for_anomaly,
                    anomaly["severity"],
                )
            else:
                logger.info(
                    "No anomaly: outlet=%s category=%s amount=%s",
                    outlet_code, canonical_category, total_for_anomaly,
                )
    except Exception as e:
        logger.warning("Anomaly detection failed (non-critical): %s", e)
    # === End intelligence layer ===

    try:
        findings = await asyncio.to_thread(run_audit_checks, stored, parsed)
    except Exception:
        logger.exception("Audit checks failed")
        findings = []

    if findings:
        await ask_audit_questions(context, stored, findings)


async def handle_audit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    reply_to = message.reply_to_message
    if not reply_to:
        return
    bot_id = context.bot.id
    if not reply_to.from_user or reply_to.from_user.id != bot_id:
        return
    # /advances repayment confirmations are also bot-replied messages; route
    # them first so a "Y" doesn't get saved as an audit answer.
    if await handle_advances_confirmation(update, context):
        return
    try:
        saved = await asyncio.to_thread(
            save_audit_reply, message.chat_id, reply_to.message_id, message.text
        )
    except Exception:
        logger.exception("Failed to save audit reply")
        return
    if saved:
        try:
            await message.reply_text("Terima kasih, jawapan disimpan. ✅")
        except Exception:
            logger.exception("Failed to ack audit reply")


HELP_TEXT = (
    "Send a receipt photo and I'll OCR it, store it, and reply with the "
    "details.\n\n"
    "Commands:\n"
    "/start — short greeting\n"
    "/summary — today's spending grouped by merchant\n"
    "/compare <item> — compare an item's unit price across outlets\n"
    "/advances — list outstanding staff advances (PAYOUT / PINJAM)\n"
    "/advances <staff> — history for one staff member\n"
    "/advances <outlet> — open advances at one outlet\n"
    "/advances <staff> repaid [amount] — mark repaid (Y/N confirm)\n"
    "/dashboard — open the Mini App dashboard\n"
    "/help — show this message"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Send a receipt photo and I'll log it. Use /help to see commands."
    )


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    chat = message.chat
    logger.info(
        "/dashboard from chat_id=%s chat_type=%s user=%s",
        message.chat_id,
        chat.type if chat else None,
        update.effective_user.id if update.effective_user else None,
    )
    if not WEBAPP_URL:
        await message.reply_text(
            "Dashboard URL not configured. Set WEBAPP_URL to the public /webapp endpoint."
        )
        return

    is_private = chat is not None and chat.type == "private"
    try:
        if is_private:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open dashboard", web_app=WebAppInfo(url=WEBAPP_URL))]]
            )
            await message.reply_text(
                "Tap below to open the Khulafa Resit Monitor dashboard.",
                reply_markup=keyboard,
            )
        else:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open dashboard", url=WEBAPP_URL)]]
            )
            await message.reply_text(
                "Open the dashboard below. For the full Mini App experience, "
                "message the bot directly and run /dashboard there.",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except Exception:
        logger.exception("Failed to send /dashboard reply")
        await message.reply_text(f"Dashboard: {WEBAPP_URL}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


def fetch_today_receipts(user_id: int, today_iso: str) -> list[dict]:
    result = (
        supabase.table(RECEIPTS_TABLE)
        .select("merchant, total, currency")
        .eq("telegram_user_id", user_id)
        .eq("receipt_date", today_iso)
        .execute()
    )
    return result.data or []


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    today_iso = datetime.now(MALAYSIA_TZ).date().isoformat()
    try:
        rows = await asyncio.to_thread(fetch_today_receipts, user.id, today_iso)
    except Exception:
        logger.exception("Summary query failed")
        await message.reply_text("Failed to fetch today's summary.")
        return

    if not rows:
        await message.reply_text(f"No receipts logged for {today_iso}.")
        return

    by_merchant: dict[str, float] = {}
    currency = ""
    for row in rows:
        merchant = row.get("merchant") or "Unknown"
        total = row.get("total")
        if not isinstance(total, (int, float)):
            continue
        if not currency and row.get("currency"):
            currency = row["currency"]
        by_merchant[merchant] = by_merchant.get(merchant, 0.0) + float(total)

    if not by_merchant:
        await message.reply_text(
            f"Receipts found for {today_iso} but none had a numeric total."
        )
        return

    suffix = f" {currency}" if currency else ""
    lines = [f"Summary for {today_iso}:"]
    for merchant, amount in sorted(by_merchant.items(), key=lambda kv: -kv[1]):
        lines.append(f"• {merchant}: {amount:.2f}{suffix}")
    lines.append(f"Total: {sum(by_merchant.values()):.2f}{suffix}")
    await message.reply_text("\n".join(lines))


def fetch_user_items(user_id: int) -> list[dict]:
    result = (
        supabase.table(RECEIPTS_TABLE)
        .select("merchant, currency, receipt_date, items")
        .eq("telegram_user_id", user_id)
        .execute()
    )
    return result.data or []


def collect_item_matches(rows: list[dict], query: str) -> list[dict]:
    needle = query.lower()
    matches: list[dict] = []
    for row in rows:
        items = row.get("items")
        if not isinstance(items, list):
            continue
        merchant = row.get("merchant") or "Unknown"
        currency = row.get("currency") or ""
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or needle not in name.lower():
                continue
            price = item.get("price")
            if not isinstance(price, (int, float)):
                continue
            qty_raw = item.get("qty")
            if qty_raw is None:
                qty_raw = item.get("quantity")
            try:
                qty = float(qty_raw) if qty_raw not in (None, "") else 1.0
            except (TypeError, ValueError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0
            matches.append({
                "merchant": merchant,
                "currency": currency,
                "unit_price": float(price) / qty,
                "raw_name": name,
            })
    return matches


async def compare_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if not context.args:
        await message.reply_text(
            "Usage: /compare <item>\nExample: /compare ais batu"
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        await message.reply_text("Usage: /compare <item>")
        return

    try:
        rows = await asyncio.to_thread(fetch_user_items, user.id)
    except Exception:
        logger.exception("Compare query failed")
        await message.reply_text("Failed to fetch receipts for compare.")
        return

    matches = collect_item_matches(rows, query)
    if not matches:
        await message.reply_text(f"No item matching \"{query}\" found in your receipts.")
        return

    by_merchant: dict[str, dict] = {}
    for m in matches:
        bucket = by_merchant.setdefault(
            m["merchant"], {"sum": 0.0, "count": 0, "currency": ""}
        )
        bucket["sum"] += m["unit_price"]
        bucket["count"] += 1
        if not bucket["currency"] and m["currency"]:
            bucket["currency"] = m["currency"]

    rankings = sorted(
        (
            (merchant, data["sum"] / data["count"], data["currency"], data["count"])
            for merchant, data in by_merchant.items()
        ),
        key=lambda entry: entry[1],
    )

    def fmt(entry: tuple) -> str:
        merchant, avg, currency, count = entry
        cur = f" {currency}" if currency else ""
        sample_word = "sample" if count == 1 else "samples"
        return f"{merchant} — {avg:.2f}{cur} per unit ({count} {sample_word})"

    lines = [
        f"Compare \"{query}\": {len(matches)} line(s) across "
        f"{len(by_merchant)} outlet(s)",
        "",
    ]
    if len(rankings) == 1:
        lines.append(f"Only outlet: {fmt(rankings[0])}")
    else:
        lines.append(f"Cheapest: {fmt(rankings[0])}")
        lines.append(f"Most expensive: {fmt(rankings[-1])}")
        if len(rankings) > 2:
            lines.append("")
            lines.append("All outlets (cheapest first):")
            for entry in rankings:
                lines.append(f"• {fmt(entry)}")

    await message.reply_text("\n".join(lines))


# === /advances commands (PR #24) ===========================================

# Known outlet codes — kept loose; pattern is "SEK-N" or alphanumeric chunks.
# Anything matching this regex is treated as an outlet filter rather than a
# staff name.
_OUTLET_TOKEN_RE = re.compile(r"^[A-Z]{2,5}[-_]?\d{0,3}$")


def _fetch_open_advances(
    staff_name: str | None = None, outlet: str | None = None
) -> list[dict]:
    q = (
        supabase.table(STAFF_ADVANCES_TABLE)
        .select("id, outlet, staff_name, amount, advance_date, issued_by, repaid")
        .eq("repaid", False)
    )
    if staff_name:
        q = q.ilike("staff_name", staff_name)
    if outlet:
        q = q.ilike("outlet", outlet)
    return q.order("advance_date", desc=True).execute().data or []


def _fetch_staff_history(staff_name: str) -> list[dict]:
    return (
        supabase.table(STAFF_ADVANCES_TABLE)
        .select("id, outlet, amount, advance_date, repaid, repaid_date, repaid_method")
        .ilike("staff_name", staff_name)
        .order("advance_date", desc=True)
        .execute()
        .data or []
    )


def _mark_advances_repaid(
    staff_name: str, partial_amount: float | None = None
) -> tuple[int, float]:
    """Mark open advances for `staff_name` repaid. If `partial_amount` is
    set, apply it FIFO across oldest-first open advances until exhausted.
    Returns (rows_updated, amount_applied).
    """
    open_rows = (
        supabase.table(STAFF_ADVANCES_TABLE)
        .select("id, amount")
        .ilike("staff_name", staff_name)
        .eq("repaid", False)
        .order("advance_date", desc=False)
        .execute()
        .data or []
    )
    if not open_rows:
        return 0, 0.0

    today = _today_my()
    if partial_amount is None:
        ids = [r["id"] for r in open_rows]
        total = sum(_to_float(r.get("amount")) or 0.0 for r in open_rows)
        supabase.table(STAFF_ADVANCES_TABLE).update(
            {"repaid": True, "repaid_date": today, "repaid_method": "salary_deduction"}
        ).in_("id", ids).execute()
        return len(ids), total

    remaining = float(partial_amount)
    applied = 0.0
    updated = 0
    for r in open_rows:
        amt = _to_float(r.get("amount")) or 0.0
        if remaining <= 0:
            break
        if remaining + 0.01 >= amt:
            supabase.table(STAFF_ADVANCES_TABLE).update(
                {"repaid": True, "repaid_date": today, "repaid_method": "cash_return"}
            ).eq("id", r["id"]).execute()
            remaining -= amt
            applied += amt
            updated += 1
        else:
            # Partial — reduce the advance amount, leave it open.
            supabase.table(STAFF_ADVANCES_TABLE).update(
                {"amount": round(amt - remaining, 2)}
            ).eq("id", r["id"]).execute()
            applied += remaining
            updated += 1
            remaining = 0
            break
    return updated, applied


def _format_advances_by_outlet(rows: list[dict]) -> str:
    if not rows:
        return "✅ Tiada advance outstanding."
    by_outlet: dict[str, list[dict]] = {}
    for r in rows:
        by_outlet.setdefault(r.get("outlet") or "UNKNOWN", []).append(r)
    lines: list[str] = []
    grand_total = 0.0
    for outlet in sorted(by_outlet.keys()):
        outlet_rows = by_outlet[outlet]
        lines.append(f"💰 Advances belum bayar — {outlet}")
        outlet_total = 0.0
        for r in outlet_rows:
            amt = _to_float(r.get("amount")) or 0.0
            outlet_total += amt
            name = r.get("staff_name") or "(unknown)"
            date = r.get("advance_date") or "—"
            lines.append(f"  • {name:<12s} RM{amt:>8.2f}   ({date})")
        lines.append("  " + "─" * 21)
        lines.append(f"  Total outstanding: RM{outlet_total:.2f}")
        lines.append("")
        grand_total += outlet_total
    if len(by_outlet) > 1:
        lines.append(f"Grand total: RM{grand_total:.2f}")
    lines.append("/advances <nama>  → tengok history")
    return "\n".join(lines).rstrip()


def _format_staff_history(staff_name: str, rows: list[dict]) -> str:
    if not rows:
        return f"Tiada advance dijumpai untuk {staff_name}."
    lines = [f"📒 History advances — {staff_name.title()}", ""]
    open_total = 0.0
    repaid_total = 0.0
    for r in rows:
        amt = _to_float(r.get("amount")) or 0.0
        date = r.get("advance_date") or "—"
        outlet = r.get("outlet") or "—"
        if r.get("repaid"):
            repaid_total += amt
            rdate = r.get("repaid_date") or "?"
            lines.append(f"  ✅ {date}  {outlet:<8s} RM{amt:>8.2f}  (paid {rdate})")
        else:
            open_total += amt
            lines.append(f"  ⏳ {date}  {outlet:<8s} RM{amt:>8.2f}  (outstanding)")
    lines.append("")
    lines.append(f"Outstanding: RM{open_total:.2f}")
    lines.append(f"Repaid:      RM{repaid_total:.2f}")
    if open_total > 0:
        lines.append("")
        lines.append(f"/advances {staff_name} repaid          → mark all paid")
        lines.append(f"/advances {staff_name} repaid <amount> → partial repayment")
    return "\n".join(lines)


async def advances_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    args = context.args or []

    # /advances  → all open advances across all outlets, grouped by outlet
    if not args:
        try:
            rows = await asyncio.to_thread(_fetch_open_advances)
        except Exception:
            logger.exception("Failed to fetch advances")
            await message.reply_text("Gagal ambil data advances.")
            return
        await message.reply_text(_format_advances_by_outlet(rows))
        return

    first = args[0]
    rest = args[1:]

    # /advances <staff_name> repaid [amount]  → confirmation prompt
    if rest and rest[0].lower() == "repaid":
        staff_name = first
        partial: float | None = None
        if len(rest) >= 2:
            try:
                partial = float(rest[1].replace("RM", "").replace(",", ""))
            except ValueError:
                await message.reply_text(
                    f"Amount tak valid: {rest[1]}\n"
                    f"Usage: /advances {staff_name} repaid <amount>"
                )
                return

        try:
            open_rows = await asyncio.to_thread(_fetch_open_advances, staff_name)
        except Exception:
            logger.exception("Failed to look up open advances")
            await message.reply_text("Gagal check advances.")
            return
        if not open_rows:
            await message.reply_text(f"Tiada advance outstanding untuk {staff_name}.")
            return

        total_open = sum(_to_float(r.get("amount")) or 0.0 for r in open_rows)
        if partial is None:
            prompt = (
                f"⚠️ Confirm: mark SEMUA advance untuk {staff_name.title()} as repaid?\n"
                f"  {len(open_rows)} advance(s), total RM{total_open:.2f}\n"
                f"Reply Y untuk confirm, N untuk batal."
            )
        else:
            prompt = (
                f"⚠️ Confirm: partial repayment RM{partial:.2f} untuk {staff_name.title()}?\n"
                f"  Current outstanding: RM{total_open:.2f} across {len(open_rows)} advance(s)\n"
                f"Reply Y untuk confirm, N untuk batal."
            )

        sent = await message.reply_text(prompt)
        # Store the pending action keyed by the prompt message_id so the Y/N
        # reply can find it. chat_data is per-chat persisted state.
        pending = context.chat_data.setdefault("pending_advance_repayments", {})
        pending[sent.message_id] = {
            "staff_name": staff_name,
            "partial": partial,
            "asked_at": datetime.now(timezone.utc).isoformat(),
        }
        return

    # /advances <outlet>  vs  /advances <staff_name>
    token = first.upper()
    if _OUTLET_TOKEN_RE.match(token):
        try:
            rows = await asyncio.to_thread(_fetch_open_advances, None, token)
        except Exception:
            logger.exception("Failed to fetch advances by outlet")
            await message.reply_text("Gagal ambil data advances.")
            return
        await message.reply_text(_format_advances_by_outlet(rows))
        return

    # /advances <staff_name>  → full history
    try:
        rows = await asyncio.to_thread(_fetch_staff_history, first)
    except Exception:
        logger.exception("Failed to fetch staff history")
        await message.reply_text("Gagal ambil history.")
        return
    await message.reply_text(_format_staff_history(first, rows))


async def handle_advances_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """If the message is a Y/N reply to a pending repayment prompt, execute
    (or cancel) it. Returns True if handled, False otherwise so the audit
    reply handler can still process unrelated replies.
    """
    message = update.effective_message
    if not message or not message.text:
        return False
    reply_to = message.reply_to_message
    if not reply_to:
        return False
    pending = context.chat_data.get("pending_advance_repayments") or {}
    action = pending.get(reply_to.message_id)
    if not action:
        return False

    answer = message.text.strip().lower()
    if answer in ("y", "yes", "ya"):
        try:
            updated, applied = await asyncio.to_thread(
                _mark_advances_repaid, action["staff_name"], action.get("partial")
            )
        except Exception:
            logger.exception("Failed to mark advances repaid")
            await message.reply_text("Gagal update database.")
            pending.pop(reply_to.message_id, None)
            return True
        if updated == 0:
            await message.reply_text("Tiada advance outstanding untuk update.")
        else:
            await message.reply_text(
                f"✅ Done. {updated} advance(s) updated, RM{applied:.2f} repaid."
            )
        pending.pop(reply_to.message_id, None)
        return True

    if answer in ("n", "no", "tidak", "batal"):
        await message.reply_text("Batal. Tiada perubahan dibuat.")
        pending.pop(reply_to.message_id, None)
        return True

    # Anything else — leave it alone and let the audit handler try it.
    return False


# === End /advances commands ================================================


def _fetch_today_receipts() -> list[dict]:
    now_my = datetime.now(MALAYSIA_TZ)
    start_local = datetime.combine(now_my.date(), datetime.min.time(), tzinfo=MALAYSIA_TZ)
    end_local = start_local + timedelta(days=1)
    res = (
        supabase.table(RECEIPTS_TABLE)
        .select("*")
        .gte("created_at", start_local.astimezone(timezone.utc).isoformat())
        .lt("created_at", end_local.astimezone(timezone.utc).isoformat())
        .execute()
    )
    return res.data or []


def build_daily_summary(rows: list[dict]) -> str:
    today = datetime.now(MALAYSIA_TZ).date().isoformat()

    grand_total = 0.0
    by_outlet: dict[str, dict] = {}
    by_supplier: dict[str, float] = {}
    failed = 0

    for r in rows:
        outlet_label = r.get("outlet") or f"Chat {r.get('chat_id')}"
        total = _to_float(r.get("total"))
        merchant = r.get("merchant")

        outlet = by_outlet.setdefault(outlet_label, {"total": 0.0, "count": 0})
        outlet["count"] += 1

        if total is None or not merchant:
            failed += 1
        if total is not None:
            grand_total += total
            outlet["total"] += total
            if merchant:
                by_supplier[merchant] = by_supplier.get(merchant, 0.0) + total

    lines = [
        f"📊 Ringkasan Harian — {today}",
        "",
        f"💰 Jumlah perbelanjaan: RM{grand_total:.2f}",
        f"🧾 Jumlah resit: {len(rows)}",
        f"⚠️ Resit gagal OCR: {failed}",
    ]

    if by_outlet:
        lines.append("")
        lines.append("🏪 Mengikut outlet (tertinggi dahulu):")
        sorted_outlets = sorted(by_outlet.items(), key=lambda x: x[1]["total"], reverse=True)
        for label, data in sorted_outlets:
            lines.append(f"  • {label}: RM{data['total']:.2f} ({data['count']} resit)")

    if by_supplier:
        lines.append("")
        lines.append("🥇 3 pembekal teratas hari ini:")
        top = sorted(by_supplier.items(), key=lambda x: x[1], reverse=True)[:3]
        for i, (m, t) in enumerate(top, 1):
            lines.append(f"  {i}. {m} — RM{t:.2f}")

    if not rows:
        lines.append("")
        lines.append("Tiada resit direkodkan hari ini.")

    return "\n".join(lines)


async def post_daily_summary(application: Application) -> None:
    try:
        rows = await asyncio.to_thread(_fetch_today_receipts)
    except Exception:
        logger.exception("Daily summary: fetch failed")
        return
    summary = build_daily_summary(rows)
    try:
        await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
        logger.info("Daily summary posted (%d receipts)", len(rows))
    except Exception:
        logger.exception("Daily summary: send failed")


# === PR #29c: historical OCR re-parse review commands ========================

def _command_owner_id(update: Update):
    user = update.effective_user
    return user.id if user else None


def _parse_reparse_n(args, default: int, maximum: int) -> int:
    if not args:
        return default
    try:
        n = int(args[0])
    except (ValueError, TypeError):
        return default
    return max(1, min(maximum, n))


def _fetch_audit_rows_for_status() -> list:
    result = (
        supabase.table(REPARSE_AUDIT_TABLE)
        .select("applied, old_total, new_total, old_date, new_date")
        .execute()
    )
    return result.data or []


def _fetch_pending_audit_rows(limit: int) -> list:
    result = (
        supabase.table(REPARSE_AUDIT_TABLE)
        .select("*")
        .eq("applied", False)
        .order("id", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


def _count_pending_audit() -> int:
    result = (
        supabase.table(REPARSE_AUDIT_TABLE).select("id").eq("applied", False).execute()
    )
    return len(result.data or [])


def _apply_pending_audit_rows(rows: list, applied_by_chat_id) -> int:
    applied = 0
    for row in rows:
        try:
            if apply_audit_row(supabase, row, applied_by_chat_id):
                applied += 1
        except Exception:
            logger.exception("Failed to apply reparse audit row %s", row.get("id"))
    return applied


async def reparse_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows = await asyncio.to_thread(_fetch_audit_rows_for_status)
    except Exception:
        logger.exception("reparse_status failed")
        await message.reply_text("Failed to read reparse audit.")
        return
    await message.reply_text(format_status(summarize_audit_rows(rows)))


async def reparse_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, REPARSE_DEFAULT_N, REPARSE_MAX_N)
    try:
        rows = await asyncio.to_thread(_fetch_pending_audit_rows, n)
    except Exception:
        logger.exception("reparse_preview failed")
        await message.reply_text("Failed to read pending changes.")
        return
    await message.reply_text(format_preview(rows))


async def reparse_apply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, REPARSE_DEFAULT_N, REPARSE_MAX_N)
    chat_id = _command_owner_id(update)
    try:
        rows = await asyncio.to_thread(_fetch_pending_audit_rows, n)
        applied = await asyncio.to_thread(_apply_pending_audit_rows, rows, chat_id)
    except Exception:
        logger.exception("reparse_apply failed")
        await message.reply_text("Failed to apply changes.")
        return
    await message.reply_text(
        f"✅ Applied {applied} correction(s). Check /reparse_status for what's left."
    )


async def reparse_apply_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        pending = await asyncio.to_thread(_count_pending_audit)
    except Exception:
        logger.exception("reparse_apply_all count failed")
        await message.reply_text("Failed to read pending changes.")
        return
    if pending == 0:
        await message.reply_text("No pending reparse changes to apply.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, apply all", callback_data="reparse_applyall:yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="reparse_applyall:no"),
    ]])
    await message.reply_text(
        f"⚠️ DANGER: apply ALL {pending} pending correction(s) to live receipts? "
        "This updates real rows and cannot be auto-undone. Confirm:",
        reply_markup=keyboard,
    )


async def reparse_apply_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    chat_id = query.from_user.id if query.from_user else None
    if not is_reviewer(chat_id):
        logger.info("Ignoring reparse apply-all callback from non-reviewer %s", chat_id)
        return
    try:
        _, choice = (query.data or "").split(":", 1)
    except ValueError:
        return
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    if choice != "yes":
        await query.message.reply_text("Cancelled — no changes applied.")
        return
    try:
        rows = await asyncio.to_thread(_fetch_pending_audit_rows, 1_000_000)
        applied = await asyncio.to_thread(_apply_pending_audit_rows, rows, chat_id)
    except Exception:
        logger.exception("reparse_apply_all failed")
        await query.message.reply_text("Failed to apply changes.")
        return
    await query.message.reply_text(f"✅ Applied ALL {applied} pending correction(s).")


# === PR #30: merchant canonical review commands (owner-only) =================

def _fetch_canonicals() -> list:
    result = (
        supabase.table(CANONICAL_TABLE)
        .select("id, display_name, legal_name, category, notes")
        .execute()
    )
    return result.data or []


def _fetch_canonical(canonical_id) -> dict | None:
    result = (
        supabase.table(CANONICAL_TABLE).select("*").eq("id", canonical_id).limit(1).execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def _fetch_aliases(canonical_id=None) -> list:
    query = supabase.table(ALIAS_TABLE).select(
        "id, alias_text, canonical_id, match_confidence, created_via"
    )
    if canonical_id is not None:
        query = query.eq("canonical_id", canonical_id)
    return query.execute().data or []


def _fetch_pending_aliases() -> list:
    return (
        supabase.table(ALIAS_TABLE)
        .select("id, alias_text, canonical_id, match_confidence, created_via")
        .eq("created_via", "fuzzy_auto")
        .order("id", desc=False)
        .execute()
        .data
        or []
    )


def _alias_counts() -> dict:
    counts: dict = {}
    for a in _fetch_aliases():
        cid = a.get("canonical_id")
        counts[cid] = counts.get(cid, 0) + 1
    return counts


def _compute_merchant_coverage() -> dict:
    rows = supabase.table(RECEIPTS_TABLE).select("merchant").execute().data or []
    counts: dict = {}
    for r in rows:
        name = (r.get("merchant") or "").strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    aliases, canonicals = load_snapshot(supabase)
    return compute_coverage(list(counts.items()), aliases, canonicals)


async def merchant_coverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        summary = await asyncio.to_thread(_compute_merchant_coverage)
    except Exception:
        logger.exception("merchant_coverage failed")
        await message.reply_text("Failed to compute coverage.")
        return
    await message.reply_text(format_coverage_report(summary))


async def merchant_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        canonicals = await asyncio.to_thread(_fetch_canonicals)
        counts = await asyncio.to_thread(_alias_counts)
    except Exception:
        logger.exception("merchant_list failed")
        await message.reply_text("Failed to read merchants.")
        return
    await message.reply_text(format_merchant_list(canonicals, counts))


async def merchant_show_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /merchant_show <canonical_id>")
        return
    cid = int(args[0])
    try:
        canonical = await asyncio.to_thread(_fetch_canonical, cid)
        aliases = await asyncio.to_thread(_fetch_aliases, cid)
    except Exception:
        logger.exception("merchant_show failed")
        await message.reply_text("Failed to read merchant.")
        return
    await message.reply_text(format_merchant_show(canonical, aliases))


async def merchant_aliases_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        aliases = await asyncio.to_thread(_fetch_pending_aliases)
    except Exception:
        logger.exception("merchant_aliases_pending failed")
        await message.reply_text("Failed to read pending aliases.")
        return
    await message.reply_text(format_pending_aliases(aliases))


async def merchant_confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /merchant_confirm <alias_id>")
        return
    alias_id = int(args[0])
    try:
        await asyncio.to_thread(
            lambda: supabase.table(ALIAS_TABLE)
            .update({"created_via": "fuzzy_confirmed"})
            .eq("id", alias_id)
            .execute()
        )
    except Exception:
        logger.exception("merchant_confirm failed")
        await message.reply_text("Failed to confirm alias.")
        return
    await message.reply_text(f"✅ Alias #{alias_id} confirmed.")


async def merchant_reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /merchant_reject <alias_id>")
        return
    alias_id = int(args[0])
    try:
        await asyncio.to_thread(
            lambda: supabase.table(ALIAS_TABLE).delete().eq("id", alias_id).execute()
        )
    except Exception:
        logger.exception("merchant_reject failed")
        await message.reply_text("Failed to reject alias.")
        return
    await message.reply_text(f"❌ Alias #{alias_id} deleted.")


async def merchant_add_alias_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await message.reply_text("Usage: /merchant_add_alias <canonical_id> <alias_text>")
        return
    canonical_id = int(args[0])
    alias_text = " ".join(args[1:]).strip()
    try:
        await asyncio.to_thread(
            lambda: supabase.table(ALIAS_TABLE)
            .insert({
                "alias_text": alias_text,
                "canonical_id": canonical_id,
                "match_confidence": 100,
                "created_via": "manual",
            })
            .execute()
        )
    except Exception:
        logger.exception("merchant_add_alias failed")
        await message.reply_text(
            "Failed to add alias (it may already exist — aliases are unique)."
        )
        return
    await message.reply_text(f"✅ Added alias {alias_text!r} -> canonical #{canonical_id}.")


# === PR #31: canonical-merchant backfill commands (owner-only) ===============

BACKFILL_DEFAULT_N = 10
BACKFILL_MAX_N = 200


def _backfill_status_counts() -> dict:
    with_merchant = (
        supabase.table(RECEIPTS_TABLE).select("id").not_.is_("merchant", "null").execute().data or []
    )
    backfilled = (
        supabase.table(RECEIPTS_TABLE).select("id").not_.is_("merchant_canonical_id", "null").execute().data or []
    )
    pending = (
        supabase.table(RECEIPTS_TABLE).select("id")
        .is_("merchant_canonical_id", "null").not_.is_("merchant", "null").execute().data or []
    )
    audit = (
        supabase.table(BACKFILL_AUDIT_TABLE)
        .select("matched_canonical_id, confidence, applied").execute().data or []
    )
    return {
        "with_merchant": len(with_merchant),
        "backfilled": len(backfilled),
        "pending": len(pending),
        "no_match": sum(1 for r in audit if not backfill_should_apply(r)),
    }


def _fetch_pending_backfill_rows(limit: int) -> list:
    return (
        supabase.table(BACKFILL_AUDIT_TABLE).select("*")
        .eq("applied", False).order("id", desc=False).limit(limit).execute().data or []
    )


def _count_applicable_pending_backfill() -> int:
    rows = (
        supabase.table(BACKFILL_AUDIT_TABLE)
        .select("matched_canonical_id, confidence, applied").eq("applied", False).execute().data or []
    )
    return sum(1 for r in rows if backfill_should_apply(r))


def _apply_pending_backfill_rows(rows: list) -> int:
    applied = 0
    for row in rows:
        try:
            if apply_backfill_audit_row(supabase, row):
                applied += 1
        except Exception:
            logger.exception("Failed to apply backfill audit row %s", row.get("id"))
    return applied


def _fetch_unmatched_backfill(limit: int) -> list:
    rows = (
        supabase.table(BACKFILL_AUDIT_TABLE)
        .select("matched_canonical_id, confidence, applied, raw_merchant").execute().data or []
    )
    return top_unmatched_from_audit(rows, limit)


async def backfill_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        counts = await asyncio.to_thread(_backfill_status_counts)
    except Exception:
        logger.exception("backfill_status failed")
        await message.reply_text("Failed to read backfill status.")
        return
    await message.reply_text(format_backfill_status(counts))


async def backfill_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, BACKFILL_DEFAULT_N, BACKFILL_MAX_N)
    try:
        rows = await asyncio.to_thread(_fetch_pending_backfill_rows, n)
    except Exception:
        logger.exception("backfill_preview failed")
        await message.reply_text("Failed to read pending backfill rows.")
        return
    await message.reply_text(format_backfill_preview(rows))


async def backfill_apply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, BACKFILL_DEFAULT_N, BACKFILL_MAX_N)
    try:
        rows = await asyncio.to_thread(_fetch_pending_backfill_rows, n)
        applied = await asyncio.to_thread(_apply_pending_backfill_rows, rows)
    except Exception:
        logger.exception("backfill_apply failed")
        await message.reply_text("Failed to apply backfill rows.")
        return
    await message.reply_text(
        f"✅ Tagged {applied} receipt(s) with a canonical merchant. "
        "Check /backfill_status for what's left."
    )


async def backfill_apply_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        applicable = await asyncio.to_thread(_count_applicable_pending_backfill)
    except Exception:
        logger.exception("backfill_apply_all count failed")
        await message.reply_text("Failed to read pending backfill rows.")
        return
    if applicable == 0:
        await message.reply_text("No applicable pending backfill rows to apply.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, apply all", callback_data="backfill_applyall:yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="backfill_applyall:no"),
    ]])
    await message.reply_text(
        f"⚠️ Tag ALL {applicable} confident (>= 80) pending receipt(s) with their "
        "canonical merchant? This updates real rows. Confirm:",
        reply_markup=keyboard,
    )


async def backfill_apply_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not is_reviewer(query.from_user.id if query.from_user else None):
        return
    try:
        _, choice = (query.data or "").split(":", 1)
    except ValueError:
        return
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    if choice != "yes":
        await query.message.reply_text("Cancelled — no receipts tagged.")
        return
    try:
        rows = await asyncio.to_thread(_fetch_pending_backfill_rows, 1_000_000)
        applied = await asyncio.to_thread(_apply_pending_backfill_rows, rows)
    except Exception:
        logger.exception("backfill_apply_all failed")
        await query.message.reply_text("Failed to apply backfill rows.")
        return
    await query.message.reply_text(f"✅ Tagged ALL {applied} pending receipt(s).")


async def backfill_unmatched_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        pairs = await asyncio.to_thread(_fetch_unmatched_backfill, 30)
    except Exception:
        logger.exception("backfill_unmatched failed")
        await message.reply_text("Failed to read unmatched merchants.")
        return
    await message.reply_text(format_backfill_unmatched(pairs))


# === PR #32: item canonical review commands (owner-only) =====================

def _fetch_item_canonicals() -> list:
    return (
        supabase.table(item_resolver.CANONICAL_TABLE)
        .select("id, display_name, category, unit, notes").execute().data or []
    )


def _fetch_item_canonical(canonical_id) -> dict | None:
    rows = (
        supabase.table(item_resolver.CANONICAL_TABLE).select("*").eq("id", canonical_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _fetch_item_aliases(canonical_id=None) -> list:
    query = supabase.table(item_resolver.ALIAS_TABLE).select(
        "id, alias_text, canonical_id, match_confidence, created_via"
    )
    if canonical_id is not None:
        query = query.eq("canonical_id", canonical_id)
    return query.execute().data or []


def _fetch_item_pending_aliases() -> list:
    return (
        supabase.table(item_resolver.ALIAS_TABLE)
        .select("id, alias_text, canonical_id, match_confidence, created_via")
        .eq("created_via", "fuzzy_auto").order("id", desc=False).execute().data or []
    )


def _item_alias_counts() -> dict:
    counts: dict = {}
    for a in _fetch_item_aliases():
        cid = a.get("canonical_id")
        counts[cid] = counts.get(cid, 0) + 1
    return counts


def _compute_item_coverage() -> dict:
    rows = supabase.table(RECEIPTS_TABLE).select("items").execute().data or []
    counts: dict = {}
    for r in rows:
        items = r.get("items") or []
        if not isinstance(items, list):
            continue
        for it in items:
            name = (it.get("name") if isinstance(it, dict) else it) or ""
            name = str(name).strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    aliases, canonicals = item_resolver.load_snapshot(supabase)
    return item_resolver.compute_coverage(list(counts.items()), aliases, canonicals)


async def item_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        canonicals = await asyncio.to_thread(_fetch_item_canonicals)
        counts = await asyncio.to_thread(_item_alias_counts)
    except Exception:
        logger.exception("item_list failed")
        await message.reply_text("Failed to read items.")
        return
    await message.reply_text(format_item_list(canonicals, counts))


async def item_show_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /item_show <canonical_id>")
        return
    cid = int(args[0])
    try:
        canonical = await asyncio.to_thread(_fetch_item_canonical, cid)
        aliases = await asyncio.to_thread(_fetch_item_aliases, cid)
    except Exception:
        logger.exception("item_show failed")
        await message.reply_text("Failed to read item.")
        return
    await message.reply_text(format_item_show(canonical, aliases))


async def item_coverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        summary = await asyncio.to_thread(_compute_item_coverage)
    except Exception:
        logger.exception("item_coverage failed")
        await message.reply_text("Failed to compute item coverage.")
        return
    await message.reply_text(format_item_coverage(summary))


async def item_aliases_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        aliases = await asyncio.to_thread(_fetch_item_pending_aliases)
    except Exception:
        logger.exception("item_aliases_pending failed")
        await message.reply_text("Failed to read pending item aliases.")
        return
    await message.reply_text(format_item_pending_aliases(aliases))


async def item_confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /item_confirm <alias_id>")
        return
    alias_id = int(args[0])
    try:
        await asyncio.to_thread(
            lambda: supabase.table(item_resolver.ALIAS_TABLE)
            .update({"created_via": "fuzzy_confirmed"}).eq("id", alias_id).execute()
        )
    except Exception:
        logger.exception("item_confirm failed")
        await message.reply_text("Failed to confirm item alias.")
        return
    await message.reply_text(f"✅ Item alias #{alias_id} confirmed.")


async def item_reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /item_reject <alias_id>")
        return
    alias_id = int(args[0])
    try:
        await asyncio.to_thread(
            lambda: supabase.table(item_resolver.ALIAS_TABLE).delete().eq("id", alias_id).execute()
        )
    except Exception:
        logger.exception("item_reject failed")
        await message.reply_text("Failed to reject item alias.")
        return
    await message.reply_text(f"❌ Item alias #{alias_id} deleted.")


async def item_add_alias_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await message.reply_text("Usage: /item_add_alias <canonical_id> <alias_text>")
        return
    canonical_id = int(args[0])
    alias_text = " ".join(args[1:]).strip()
    try:
        await asyncio.to_thread(
            lambda: supabase.table(item_resolver.ALIAS_TABLE)
            .insert({
                "alias_text": alias_text,
                "canonical_id": canonical_id,
                "match_confidence": 100,
                "created_via": "manual",
            }).execute()
        )
    except Exception:
        logger.exception("item_add_alias failed")
        await message.reply_text(
            "Failed to add item alias (it may already exist — aliases are unique)."
        )
        return
    await message.reply_text(f"✅ Added item alias {alias_text!r} -> canonical #{canonical_id}.")


# === PR #32b: item resolution backfill status (owner-only) ===================

def _item_backfill_status_counts() -> dict:
    rows = (
        supabase.table(ITEM_RESOLUTIONS_TABLE)
        .select("canonical_id, match_tier").execute().data or []
    )
    resolved = sum(1 for r in rows if r.get("canonical_id") is not None)
    low_conf = sum(1 for r in rows if r.get("match_tier") == "low_confidence")
    no_match = sum(1 for r in rows if r.get("match_tier") == "none")
    return {
        "total": len(rows),
        "resolved": resolved,
        "low_conf": low_conf,
        "no_match": no_match,
    }


def _fetch_item_backfill_unmatched(limit: int) -> list:
    rows = (
        supabase.table(ITEM_RESOLUTIONS_TABLE)
        .select("canonical_id, raw_name").is_("canonical_id", "null").execute().data or []
    )
    return top_unmatched_from_resolutions(rows, limit)


async def item_backfill_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        counts = await asyncio.to_thread(_item_backfill_status_counts)
    except Exception:
        logger.exception("item_backfill_status failed")
        await message.reply_text("Failed to read item backfill status.")
        return
    await message.reply_text(format_item_backfill_status(counts))


async def item_backfill_unmatched_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        pairs = await asyncio.to_thread(_fetch_item_backfill_unmatched, 30)
    except Exception:
        logger.exception("item_backfill_unmatched failed")
        await message.reply_text("Failed to read unmatched items.")
        return
    await message.reply_text(format_item_backfill_unmatched(pairs))


# === PR #33: price_movements analytics (owner-only) ==========================

def _fetch_pm_rows(columns: str, item_canonical_id=None) -> list:
    query = supabase.table(analytics.PRICE_MOVEMENTS_VIEW).select(columns)
    if item_canonical_id is not None:
        query = query.eq("item_canonical_id", item_canonical_id)
    return query.execute().data or []


async def refresh_analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        await asyncio.to_thread(analytics.refresh, supabase)
    except Exception:
        logger.exception("refresh_analytics failed")
        await message.reply_text("Failed to refresh price_movements.")
        return
    await message.reply_text("✅ Refreshed price_movements. See /price_movements_status.")


async def price_movements_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows = await asyncio.to_thread(_fetch_pm_rows, "receipt_date")
    except Exception:
        logger.exception("price_movements_status failed")
        await message.reply_text("Failed to read price_movements.")
        return
    await message.reply_text(analytics.format_status(analytics.summarise_status(rows)))


async def top_items_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, 10, 50)
    try:
        rows = await asyncio.to_thread(
            _fetch_pm_rows, "item_canonical_id, item_display_name, item_category, line_total"
        )
    except Exception:
        logger.exception("top_items failed")
        await message.reply_text("Failed to read price_movements.")
        return
    await message.reply_text(analytics.format_top_items(analytics.top_items(rows, n)))


async def top_suppliers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, 10, 50)
    try:
        rows = await asyncio.to_thread(
            _fetch_pm_rows, "merchant_canonical_id, merchant_display_name, merchant_category, line_total"
        )
    except Exception:
        logger.exception("top_suppliers failed")
        await message.reply_text("Failed to read price_movements.")
        return
    await message.reply_text(analytics.format_top_suppliers(analytics.top_suppliers(rows, n)))


async def price_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /price_history <item_canonical_id>")
        return
    item_id = int(args[0])
    try:
        rows = await asyncio.to_thread(
            _fetch_pm_rows,
            "item_canonical_id, receipt_date, merchant_display_name, qty, unit_price, line_total",
            item_id,
        )
    except Exception:
        logger.exception("price_history failed")
        await message.reply_text("Failed to read price_movements.")
        return
    await message.reply_text(
        analytics.format_price_history(item_id, analytics.price_history(rows, item_id))
    )


async def run_bot() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("compare", compare_command))
    app.add_handler(CommandHandler("advances", advances_command))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("reparse_status", reparse_status_command))
    app.add_handler(CommandHandler("reparse_preview", reparse_preview_command))
    app.add_handler(CommandHandler("reparse_apply", reparse_apply_command))
    app.add_handler(CommandHandler("reparse_apply_all", reparse_apply_all_command))
    app.add_handler(CommandHandler("merchant_coverage", merchant_coverage_command))
    app.add_handler(CommandHandler("merchant_list", merchant_list_command))
    app.add_handler(CommandHandler("merchant_show", merchant_show_command))
    app.add_handler(CommandHandler("merchant_aliases_pending", merchant_aliases_pending_command))
    app.add_handler(CommandHandler("merchant_confirm", merchant_confirm_command))
    app.add_handler(CommandHandler("merchant_reject", merchant_reject_command))
    app.add_handler(CommandHandler("merchant_add_alias", merchant_add_alias_command))
    app.add_handler(CommandHandler("backfill_status", backfill_status_command))
    app.add_handler(CommandHandler("backfill_preview", backfill_preview_command))
    app.add_handler(CommandHandler("backfill_apply", backfill_apply_command))
    app.add_handler(CommandHandler("backfill_apply_all", backfill_apply_all_command))
    app.add_handler(CommandHandler("backfill_unmatched", backfill_unmatched_command))
    app.add_handler(CommandHandler("item_list", item_list_command))
    app.add_handler(CommandHandler("item_show", item_show_command))
    app.add_handler(CommandHandler("item_coverage", item_coverage_command))
    app.add_handler(CommandHandler("item_aliases_pending", item_aliases_pending_command))
    app.add_handler(CommandHandler("item_confirm", item_confirm_command))
    app.add_handler(CommandHandler("item_reject", item_reject_command))
    app.add_handler(CommandHandler("item_add_alias", item_add_alias_command))
    app.add_handler(CommandHandler("item_backfill_status", item_backfill_status_command))
    app.add_handler(CommandHandler("item_backfill_unmatched", item_backfill_unmatched_command))
    app.add_handler(CommandHandler("refresh_analytics", refresh_analytics_command))
    app.add_handler(CommandHandler("price_movements_status", price_movements_status_command))
    app.add_handler(CommandHandler("top_items", top_items_command))
    app.add_handler(CommandHandler("top_suppliers", top_suppliers_command))
    app.add_handler(CommandHandler("price_history", price_history_command))
    app.add_handler(
        CallbackQueryHandler(reparse_apply_all_callback, pattern=r"^reparse_applyall:(yes|no)$")
    )
    app.add_handler(
        CallbackQueryHandler(backfill_apply_all_callback, pattern=r"^backfill_applyall:(yes|no)$")
    )
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # PR #29b manual review: the edit conversation must be registered before
    # the audit-reply handler so a reviewer's in-flow text replies are routed
    # to the conversation, not mistaken for an audit reply.
    app.add_handler(build_review_edit_conversation())
    app.add_handler(
        CallbackQueryHandler(handle_review_action, pattern=r"^review:\d+:(save|discard)$")
    )
    app.add_handler(
        MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, handle_audit_reply)
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    def on_polling_error(error: TelegramError) -> None:
        if isinstance(error, Conflict):
            logger.warning(
                "Polling conflict: another instance is using this bot token. "
                "Backing off and retrying."
            )
        else:
            logger.error("Polling error: %s", error, exc_info=error)

    scheduler = AsyncIOScheduler(timezone=MALAYSIA_TZ)
    scheduler.add_job(
        post_daily_summary,
        trigger="cron",
        hour=23,
        minute=0,
        args=[app],
        id="daily_summary",
        replace_existing=True,
    )

    async with app:
        await app.start()
        with contextlib.suppress(Exception):
            await app.bot.set_my_commands([
                BotCommand("start", "Greeting"),
                BotCommand("summary", "Today's spending grouped by merchant"),
                BotCommand("compare", "Compare an item's unit price across outlets"),
                BotCommand("advances", "Staff cash advances (PAYOUT/PINJAM) tracker"),
                BotCommand("dashboard", "Open the Mini App dashboard"),
                BotCommand("help", "Show command list"),
            ])
        scheduler.start()
        logger.info("Scheduler started: daily summary at 23:00 Asia/Kuala_Lumpur")
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            error_callback=on_polling_error,
        )
        logger.info("Bot started (health on :%d)", HEALTH_PORT)
        try:
            await stop.wait()
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()


def main() -> None:
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
