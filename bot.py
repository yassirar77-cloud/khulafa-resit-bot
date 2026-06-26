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
from datetime import date, datetime, timedelta, timezone
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
from image_store import probe_cloudinary, upload_receipt_image
from image_utils import resize_for_ocr
from items_utils import normalize_items
from money_utils import normalize_total
from ocr_quality import total_conflicts_with_item_sum
from pending_review import (
    apply_edits_to_parsed,
    build_review_reason,
    is_duplicate_review,
    resolve_confidence,
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
import merchant_auto_resolve
from merchant_auto_resolve import (
    fetch_review_queue as fetch_merchant_review_queue,
    format_resolve_report as format_merchant_resolve_report,
    format_review_queue as format_merchant_review_queue,
    undo_resolution as undo_merchant_resolution,
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
import digest
import food_cost_analytics
import kitchen_usage
import manager_registration
import order_generator
import reconciliation_service
import sales_analytics
import weekly_manager_reports as wmr
from digest_data import gather_digest_data, log_digest
from sales_ingest import run_ingest_once
from sales_parser import OUTLET_CANONICAL_BY_CODE
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
SALES_DAILY_SUMMARY_TABLE = "sales_daily_summary"
SALES_DAILY_TOP_ITEMS_TABLE = "sales_daily_top_items"
SALES_DAILY_TABLE = "sales_daily"
SALES_ITEMS_TABLE = "sales_items"
SALES_INGEST_LOG_TABLE = "sales_ingest_log"

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
_image_columns_available = True
_pending_image_column_available = True
_VERIFICATION_KEYS = ("verification_status", "verification_notes", "confidence")
_IMAGE_KEYS = ("photo_file_id", "image_url")


def store_receipt(record: dict) -> dict:
    global _outlet_column_available, _verification_columns_available, _bill_to_column_available, _receipt_type_column_available, _image_columns_available
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
    if not _image_columns_available:
        for key in _IMAGE_KEYS:
            payload.pop(key, None)
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
        elif any(k in payload for k in _IMAGE_KEYS) and any(k in msg for k in _IMAGE_KEYS):
            logger.warning(
                "receipts image columns missing — apply "
                "migrations/0027_receipt_image_persistence.sql. Saving without "
                "photo_file_id/image_url for now."
            )
            _image_columns_available = False
            for key in _IMAGE_KEYS:
                payload.pop(key, None)
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
    global _pending_image_column_available
    payload = dict(record)
    payload["parsed_date"] = normalize_date(payload.get("parsed_date"))
    if payload.get("parsed_total") is not None:
        payload["parsed_total"] = normalize_total(payload.get("parsed_total"))
    if not _pending_image_column_available:
        payload.pop("image_url", None)
    try:
        result = supabase.table(PENDING_REVIEW_TABLE).insert(payload).execute()
    except Exception as exc:
        # Graceful fallback if 0027 hasn't been applied yet — never block the
        # review queue over the new image_url column.
        if "image_url" in payload and "image_url" in str(exc).lower():
            logger.warning(
                "pending_review.image_url column missing — apply "
                "migrations/0027_receipt_image_persistence.sql. Queuing without "
                "image_url for now."
            )
            _pending_image_column_available = False
            payload.pop("image_url", None)
            result = supabase.table(PENDING_REVIEW_TABLE).insert(payload).execute()
        else:
            raise
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
        # Carry the image references forward so promoted receipts keep their
        # photo (previously dropped on promotion).
        "photo_file_id": pending.get("photo_file_id"),
        "image_url": pending.get("image_url"),
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

    def _final(status):
        # Resolved AFTER any corrections so the math-agreement check sees the
        # data we actually store. Math agreement -> 100; WRONG -> 80; etc.
        return resolve_confidence(
            status, confidence, parsed.get("items"), _to_float(parsed.get("total"))
        )

    if verdict == "CONFIRMED":
        _bump_verdict("CONFIRMED")
        final = _final("CONFIRMED")
        return (
            {"status": "CONFIRMED", "notes": notes, "confidence": final},
            f"✅ Verified ({final}%)",
        )

    if verdict == "PARTIAL":
        changes = _apply_corrections(parsed, corrections)
        _bump_verdict("PARTIAL")
        final = _final("PARTIAL")
        change_text = ", ".join(changes) if changes else (notes or "minor issues")
        return (
            {"status": "PARTIAL", "notes": notes, "confidence": final},
            f"⚠️ Verified with corrections ({final}%): {change_text}",
        )

    # verdict == "WRONG"
    _bump_verdict("WRONG")
    if confidence is not None and confidence < 50:
        # Don't auto-apply corrections — but math agreement can still vouch for
        # it (resolve_confidence returns 100), keeping a clean receipt out of
        # review even when the verifier is unsure.
        final = _final("WRONG")
        total = parsed.get("total")
        return (
            {"status": "WRONG", "notes": notes, "confidence": final},
            f"❓ OCR uncertain ({final}%) — please verify total RM{total} and items",
        )
    changes = _apply_corrections(parsed, corrections)
    final = _final("WRONG")
    summary = ", ".join(changes) if changes else (notes or "see verifier notes")
    return (
        {"status": "WRONG", "notes": notes, "confidence": final},
        f"✅ Auto-corrected ({final}%): {summary}",
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


def fetch_recent_pending_reviews(within_hours: int = 24) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    result = (
        supabase.table(PENDING_REVIEW_TABLE)
        .select("parsed_merchant, parsed_total, parsed_date, created_at, status")
        .eq("status", "pending")
        .gte("created_at", cutoff)
        .execute()
    )
    return result.data or []


async def route_to_review(
    message, context: ContextTypes.DEFAULT_TYPE, parsed: dict, verification: dict,
    image_url: str | None = None,
) -> None:
    confidence = verification.get("confidence")
    # De-dup: if an equivalent receipt is already pending review from the last
    # 24h (re-upload / re-process), don't queue or DM the reviewer again.
    try:
        recent = await asyncio.to_thread(fetch_recent_pending_reviews)
    except Exception:
        logger.warning("Could not check recent pending reviews for dedup", exc_info=True)
        recent = []
    if is_duplicate_review(recent, parsed):
        logger.info("Skipping duplicate review DM (already pending within 24h)")
        await message.reply_text(
            "🔎 This receipt is already in the review queue from earlier — not re-sending."
        )
        return
    ocr_conflict = total_conflicts_with_item_sum(
        _to_float(parsed.get("total")), parsed.get("items")
    )
    reason = build_review_reason(confidence, verification.get("status"), ocr_conflict)
    photo = message.photo[-1] if message.photo else None
    pending_record = {
        "telegram_message_id": message.message_id,
        "chat_id": message.chat_id,
        "photo_file_id": photo.file_id if photo else None,
        "image_url": image_url,
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

    # Persist the image for every receipt (re-OCR / model comparison / debug).
    # Capture the Telegram file_id (free fallback reference) and kick off a
    # best-effort Cloudinary archive of the ORIGINAL full-res bytes (before
    # resize downscales them) as a background task. It runs concurrently with
    # OCR — which dominates latency — so archival adds ~nothing to the live
    # flow, and upload_receipt_image never raises (returns None on failure).
    photo_file_id = photo.file_id
    image_upload_task = asyncio.create_task(
        asyncio.to_thread(upload_receipt_image, image_bytes)
    )

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
        # Receipt won't be saved, so the archive isn't needed — let the
        # background upload finish quietly rather than orphaning the task.
        image_upload_task.cancel()
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

    # Collect the background image archive result (started before OCR, so it has
    # overlapped the slow OCR/verification work). Never let it break the save.
    try:
        image_url = await image_upload_task
    except Exception:
        logger.warning("Receipt image archive task failed; continuing", exc_info=True)
        image_url = None

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
        "photo_file_id": photo_file_id,
        "image_url": image_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # PR #29b: low-confidence receipts do NOT auto-save. Route them to the
    # manual-review queue and DM an authorised reviewer instead, so bad OCR
    # never reaches `receipts`/`item_prices` and poisons price intelligence.
    # We gate on the stored confidence — the second-pass verifier score.
    if should_queue(verification["confidence"]):
        await route_to_review(message, context, parsed, verification, image_url=image_url)
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
    "/help — show this message\n"
    "\n"
    "Food cost:\n"
    "/food_cost_today — today's raw sales & purchases\n"
    "/food_cost_week — 7-day rolling food cost % per outlet\n"
    "/food_cost_month — month-to-date food cost % per outlet\n"
    "/food_cost_outlet <name> — one outlet's food cost trend\n"
    "/cash_no_receipt_today — POS cash payouts with no receipt\n"
    "/reconcile_now — re-run today/yesterday reconciliation\n"
    "/reconcile_date YYYY-MM-DD — re-run one historical date\n"
    "\n"
    "Merchant auto-resolve:\n"
    "/merchant_resolve_now — clear the unresolved-merchant backlog (auto-resolve, escalate, defer)\n"
    "/merchant_review — owner queue of escalated merchants (by RM at stake)\n"
    "/merchant_undo <log_id> — reverse one auto-resolution and re-reconcile\n"
    "\n"
    "Sales:\n"
    "/sales_today, /sales_yesterday — sales by outlet\n"
    "/sales_summary_today, /sales_customers_today, /sales_avg_ticket\n"
    "/top_items_sold — top items sold (last 7 days)\n"
    "\n"
    "Weekly manager reports:\n"
    "/gen_codes — generate one-time outlet registration codes\n"
    "/register <CODE> — register as an outlet's manager\n"
    "/weekly_report_now [recent | YYYY-MM-DD] — preview the weekly report\n"
    "\n"
    "Order drafts:\n"
    "/order_drafts_now — preview tomorrow's per-outlet order drafts"
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


async def cloudinary_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only on-demand re-run of the Cloudinary archival health probe.

    Lets the owner re-verify image archival anytime without a redeploy. Mirrors
    the startup probe; silently ignores non-reviewers like the other admin
    commands.
    """
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    ok, detail = await asyncio.to_thread(probe_cloudinary)
    icon = "✅" if ok else "⚠️"
    await message.reply_text(f"{icon} Cloudinary: {detail}")


async def kitchen_groups_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only debug: dump every group chat the bot has seen in receipts with
    its stored outlet text and the resolved kitchen outlet_code, plus which
    expected kitchen outlets are still missing. Lets the owner verify the
    chat_id -> outlet mapping (esp. the Klang/Sharfuddin group) against live data.

    Deliberately NOT reviewer-gated: it performs no mutation and only reads the
    chat->outlet mapping, and the silent reviewer guard was hiding it from the
    owner when YASSIR_CHAT_ID wasn't set. It still only replies in PRIVATE chats
    (so the chat_id list isn't dumped into a group) and logs every invocation."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    user_id = user.id if user else None
    chat = update.effective_chat
    chat_type = chat.type if chat else None
    logger.info(
        "kitchen_groups_debug invoked by user_id=%s chat_type=%s reviewer=%s",
        user_id, chat_type, is_reviewer(user_id),
    )
    # Read-only, but keep the chat_id list out of group chats.
    if chat_type not in ("private", None):
        await message.reply_text("DM me /kitchen_groups_debug in a private chat to see the mapping.")
        return

    try:
        from config.kitchen_groups import (
            EXPECTED_CODES,
            diagnostic_dump,
            missing_outlets,
            resolve_groups,
        )

        rows = await asyncio.to_thread(diagnostic_dump, supabase)
        # force=True: bypass the process cache so the dump reflects current receipts.
        mapping = await asyncio.to_thread(resolve_groups, supabase, force=True)
        missing = missing_outlets(mapping)
    except Exception:
        logger.exception("kitchen_groups_debug failed to build the dump")
        await message.reply_text("⚠️ Couldn't build the kitchen-groups dump — check the logs.")
        return

    enabled = kitchen_usage.kitchen_log_enabled()
    lines = [
        "🍳 Kitchen groups (chat_id → outlet):",
        f"(your user_id: {user_id} • reviewer: {is_reviewer(user_id)} • "
        f"KITCHEN_LOG_ENABLED: {enabled})",
        "",
    ]
    if not rows:
        lines.append("- (no group receipts seen yet)")
    for r in rows:
        code = r["code"] or "—(unresolved)"
        outlet_text = r["outlets"][0] if r["outlets"] else "(blank)"
        lines.append(f"- {r['chat_id']} → {code}  [{outlet_text}, {r['count']} receipts]")
    lines.append("")
    lines.append(
        f"Resolved {len(EXPECTED_CODES) - len(missing)}/{len(EXPECTED_CODES)} expected outlets"
        + (f" — missing: {', '.join(missing)}" if missing else " — all present")
    )
    if not enabled:
        lines.append("")
        lines.append("⚠️ Scheduled forms are OFF (set KITCHEN_LOG_ENABLED=true to turn on).")
    await message.reply_text("\n".join(lines))


async def kitchen_post_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: post ONE kitchen form right now for testing. Bypasses the
    KITCHEN_LOG_ENABLED safety gate (explicit single post).

    Usage:
      /kitchen_post_now              -> COOKED (6PM) form to THIS group
      /kitchen_post_now night        -> night-cook (12AM, additive) form here
      /kitchen_post_now left         -> LEFT form to this group
      /kitchen_post_now SEK20        -> COOKED form to SEK20's group
      /kitchen_post_now SEK20 night  -> night-cook form to SEK20's group
      /kitchen_post_now SEK20 left   -> LEFT form to SEK20's group
    """
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    from config.kitchen_groups import configured_groups

    args = [a.strip() for a in (context.args or []) if a.strip()]
    _LEFT_WORDS = {"left", "baki", "tutup"}
    _NIGHT_WORDS = {"night", "malam", "tambahan"}
    _COOKED_WORDS = {"cooked", "masak", "petang"}
    if any(a.lower() in _LEFT_WORDS for a in args):
        phase = kitchen_usage.PHASE_LEFT
    elif any(a.lower() in _NIGHT_WORDS for a in args):
        phase = kitchen_usage.PHASE_COOKED_NIGHT
    else:
        phase = kitchen_usage.PHASE_COOKED
    outlet_tokens = [a for a in args if a.lower() not in _LEFT_WORDS | _NIGHT_WORDS | _COOKED_WORDS]
    target_outlet = outlet_tokens[0].upper() if outlet_tokens else None

    try:
        groups = await asyncio.to_thread(configured_groups, supabase)
    except Exception:
        logger.exception("kitchen_post_now: group resolution failed")
        await message.reply_text("⚠️ Couldn't resolve kitchen groups — check the logs.")
        return
    code_to_chat = {code: cid for cid, code in groups}

    if target_outlet:
        chat_id = code_to_chat.get(target_outlet)
        outlet_code = target_outlet
        if chat_id is None:
            known = ", ".join(sorted(code_to_chat)) or "(none resolved)"
            await message.reply_text(
                f"No kitchen group resolved for {target_outlet}. Known: {known}"
            )
            return
    else:
        chat_id = message.chat_id
        outlet_code = next((c for cid, c in groups if cid == chat_id), None)
        if outlet_code is None:
            await message.reply_text(
                "This chat isn't a known kitchen group. Run it inside the outlet's "
                "group, or DM me /kitchen_post_now <OUTLET_CODE> [left]."
            )
            return

    try:
        posted = await kitchen_usage.post_one_form(context.application, chat_id, outlet_code, phase)
    except Exception as exc:
        if kitchen_usage._is_missing_table_error(exc):
            await message.reply_text(
                "⚠️ Kitchen tables aren't in the PostgREST schema cache yet — apply "
                "migration 0032 and run: NOTIFY pgrst, 'reload schema';"
            )
        else:
            logger.exception("kitchen_post_now failed")
            await message.reply_text("⚠️ Failed to post the form — check the logs.")
        return

    label = phase.upper()
    if posted:
        await message.reply_text(f"✅ Posted {label} form for {outlet_code} → chat {chat_id}.")
    else:
        await message.reply_text(
            f"ℹ️ {label} for {outlet_code} is already submitted today — nothing posted."
        )


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


# === PR #68: risk-weighted merchant auto-resolution (owner-only) =============

def _canonical_names_by_id() -> dict:
    return {c.get("id"): c.get("display_name") for c in _fetch_canonicals()}


async def merchant_resolve_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-pass backfill of the whole unresolved-merchant backlog. Auto-resolves
    confident matches, escalates risky ones, defers the long tail, and re-runs
    reconciliation for every business date a tagged receipt touches."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text("Resolving merchant backlog (auto-resolve + reconcile)…")
    try:
        stats = await asyncio.to_thread(
            merchant_auto_resolve.resolve_all, supabase, actor=_command_owner_id(update)
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner
        logger.exception("merchant_resolve_now failed")
        await message.reply_text(f"Auto-resolve failed: {exc}")
        return
    await message.reply_text(format_merchant_resolve_report(stats))


async def merchant_review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner review queue: active escalations ranked by RM at stake descending."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        queue = await asyncio.to_thread(fetch_merchant_review_queue, supabase)
        names = await asyncio.to_thread(_canonical_names_by_id)
    except Exception:
        logger.exception("merchant_review failed")
        await message.reply_text("Failed to read the review queue.")
        return
    await message.reply_text(format_merchant_review_queue(queue, names))


async def merchant_undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reverse one auto-resolution by its log id: untag receipts, drop the alias,
    and re-reconcile the affected dates so food cost reverts."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /merchant_undo <log_id>  (see /merchant_review)")
        return
    log_id = int(args[0])
    try:
        row = await asyncio.to_thread(undo_merchant_resolution, supabase, log_id)
    except Exception:
        logger.exception("merchant_undo failed")
        await message.reply_text("Failed to undo resolution.")
        return
    if row is None:
        await message.reply_text(
            f"Nothing to undo for log #{log_id} "
            "(not an auto-resolution, or already undone)."
        )
        return
    await message.reply_text(
        f"↩️ Undid resolution #{log_id}: {row.get('raw_merchant')!r} untagged, "
        f"alias removed, {len(row.get('affected_dates') or [])} date(s) re-reconciled."
    )


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


# === PR #34: daily digest preview (owner-only) ===============================

async def test_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    raw = os.environ.get("YASSIR_CHAT_ID")
    try:
        recipient = int(raw) if raw else None
    except ValueError:
        recipient = None
    if recipient is None:
        await message.reply_text("YASSIR_CHAT_ID is not set — can't send the digest.")
        return
    plain = bool(context.args) and context.args[0].lower() in ("plain", "--plain")
    now_my = datetime.now(MALAYSIA_TZ)
    try:
        data = await asyncio.to_thread(gather_digest_data, supabase, now_my)
    except Exception:
        logger.exception("test_digest gather failed")
        await message.reply_text("Failed to gather digest data.")
        return
    messages = digest.build_digest_messages(data, now_my)
    full_text = "\n\n".join(messages)
    message_bytes = len(full_text.encode("utf-8"))
    attempts = digest.parse_mode_attempts(plain)
    sent, error, used_fallback = 0, None, False
    for msg in messages:
        delivered, last_err = False, None
        for i, parse_mode in enumerate(attempts):
            try:
                await context.bot.send_message(
                    chat_id=recipient, text=msg, parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                delivered = True
                used_fallback = used_fallback or i > 0
                break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                logger.warning("test_digest send (parse_mode=%s) failed: %s", parse_mode, last_err)
        if delivered:
            sent += 1
        else:
            error = last_err
            break
    status = "success" if sent == len(messages) else ("failed" if sent == 0 else "partial")
    if status == "success" and used_fallback:
        error = "delivered as plain text (markdown parse fallback)"
    await asyncio.to_thread(
        log_digest, supabase, recipient, full_text, status, error, message_bytes
    )
    if status == "success":
        note = " (plain-text fallback)" if used_fallback else ""
        await message.reply_text(f"✅ Digest sent to YASSIR_CHAT_ID ({sent} message(s)){note}.")
    else:
        await message.reply_text(f"⚠️ Digest delivery {status} ({sent}/{len(messages)} sent). {error or ''}")


# === PR #35: POS sales ingestion + analytics (owner-only) ====================

def _my_today():
    return datetime.now(MALAYSIA_TZ).date()


def _business_date_list(n: int, end=None):
    end = end or _my_today()
    return [(end - timedelta(days=i)).isoformat() for i in range(n)]


def _month_to_date_dates(end=None):
    """Business dates from the 1st of ``end``'s month through ``end`` (newest
    first), for the month-to-date food cost view."""
    end = end or _my_today()
    n = (end - end.replace(day=1)).days + 1
    return _business_date_list(n, end)


def _fetch_sales_rows(business_dates):
    resp = (
        supabase.table(SALES_DAILY_TABLE)
        .select("outlet_canonical, total_sales, shift_type, shift_business_date")
        .in_("shift_business_date", business_dates)
        .execute()
    )
    return resp.data or []


def _fetch_sales_outlet_rows(outlet, business_dates):
    resp = (
        supabase.table(SALES_DAILY_TABLE)
        .select("outlet_canonical, total_sales, shift_type, shift_business_date")
        .eq("outlet_canonical", outlet)
        .in_("shift_business_date", business_dates)
        .execute()
    )
    return resp.data or []


def _fetch_sales_items_rows(business_dates):
    daily = (
        supabase.table(SALES_DAILY_TABLE)
        .select("id")
        .in_("shift_business_date", business_dates)
        .execute()
    )
    ids = [r["id"] for r in (daily.data or [])]
    if not ids:
        return []
    resp = (
        supabase.table(SALES_ITEMS_TABLE)
        .select("item_name, qty, amount")
        .in_("sales_daily_id", ids)
        .execute()
    )
    return resp.data or []


def _fetch_ingest_log_rows(since_iso):
    resp = (
        supabase.table(SALES_INGEST_LOG_TABLE)
        .select("*")
        .gte("ran_at", since_iso)
        .order("ran_at", desc=True)
        .execute()
    )
    return resp.data or []


def _resolve_outlet_name(query: str) -> str:
    """Resolve a user-typed outlet (e.g. 'klang') to a canonical name
    ('Klang B.Emas'). Falls back to the raw query if nothing matches."""
    q = query.strip().lower()
    names = sorted(set(OUTLET_CANONICAL_BY_CODE.values()))
    for name in names:
        if name.lower() == q:
            return name
    for name in names:
        if q and (q in name.lower() or name.lower() in q):
            return name
    return query.strip()


async def sales_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    today = _my_today().isoformat()
    try:
        rows = await asyncio.to_thread(_fetch_sales_rows, [today])
    except Exception:
        logger.exception("sales_today failed")
        await message.reply_text("Failed to fetch today's sales.")
        return
    by_outlet = sales_analytics.aggregate_sales_by_outlet(rows)
    await message.reply_text(
        sales_analytics.format_sales_by_outlet(f"Sales today ({today}):", by_outlet)
    )


async def sales_yesterday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    yesterday = (_my_today() - timedelta(days=1)).isoformat()
    try:
        rows = await asyncio.to_thread(_fetch_sales_rows, [yesterday])
    except Exception:
        logger.exception("sales_yesterday failed")
        await message.reply_text("Failed to fetch yesterday's sales.")
        return
    await message.reply_text(sales_analytics.format_yesterday_recap(yesterday, rows))


async def sales_outlet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    if not context.args:
        await message.reply_text("Usage: /sales_outlet <name>\nExample: /sales_outlet klang")
        return
    outlet = _resolve_outlet_name(" ".join(context.args))
    dates = _business_date_list(7)
    try:
        rows = await asyncio.to_thread(_fetch_sales_outlet_rows, outlet, dates)
    except Exception:
        logger.exception("sales_outlet failed")
        await message.reply_text("Failed to fetch outlet sales.")
        return
    await message.reply_text(sales_analytics.format_outlet_history(outlet, rows))


async def sales_ingest_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    since_iso = (
        datetime.now(MALAYSIA_TZ) - timedelta(hours=24)
    ).astimezone(timezone.utc).isoformat()
    try:
        rows = await asyncio.to_thread(_fetch_ingest_log_rows, since_iso)
    except Exception:
        logger.exception("sales_ingest_status failed")
        await message.reply_text("Failed to read ingest log.")
        return
    await message.reply_text(sales_analytics.format_ingest_status(rows))


async def sales_ingest_manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text("Fetching shift-close emails…")
    try:
        summary = await asyncio.to_thread(run_ingest_once, supabase)
    except KeyError as exc:
        await message.reply_text(
            f"Missing env var {exc}. Set GMAIL_INBOX and GMAIL_APP_PASSWORD on the service."
        )
        return
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner
        logger.exception("manual sales ingest failed")
        await message.reply_text(f"Ingest failed: {exc}")
        return
    await message.reply_text(
        "Sales ingest done —\n"
        f"• Fetched: {summary['fetched']}\n"
        f"• Inserted: {summary['inserted']}\n"
        f"• Skipped (duplicate): {summary['skipped']}\n"
        f"• Skipped (inactive): {summary['skipped_inactive']}\n"
        f"• Skipped (unknown): {summary['skipped_unknown']}\n"
        f"• Errors: {summary['errors']}"
    )


RECONCILIATION_TABLE = "purchase_reconciliation"
MATCH_LOG_TABLE = "purchase_match_log"


def _fetch_recon_rows(business_dates):
    resp = (
        supabase.table(RECONCILIATION_TABLE)
        .select("id, outlet_canonical, business_date, sales_total, "
                "total_food_purchases, food_cost_percent")
        .in_("business_date", business_dates)
        .execute()
    )
    return resp.data or []


def _fetch_recon_with_fallback():
    """Reconciliation rows for today, falling back to yesterday (sales D-files
    for today land the next morning). Returns ``(rows, label)``."""
    today = _my_today()
    yesterday = today - timedelta(days=1)
    rows = _fetch_recon_rows([today.isoformat()])
    if rows:
        return rows, today.isoformat()
    rows = _fetch_recon_rows([yesterday.isoformat()])
    return rows, f"yesterday ({yesterday.isoformat()})"


def _fetch_cash_no_receipt_alerts(recon_rows):
    """Type B (cash paid, no receipt) match-log entries for the given
    reconciliation rows, mapped back to their outlet."""
    id_to_outlet = {
        r.get("id"): r.get("outlet_canonical") for r in recon_rows if r.get("id") is not None
    }
    if not id_to_outlet:
        return []
    resp = (
        supabase.table(MATCH_LOG_TABLE)
        .select("reconciliation_id, amount, merchant_or_description, match_type")
        .in_("reconciliation_id", list(id_to_outlet))
        .eq("match_type", "B_cash_no_receipt")
        .execute()
    )
    alerts = [
        {
            "outlet": id_to_outlet.get(r.get("reconciliation_id")),
            "amount": r.get("amount"),
            "description": r.get("merchant_or_description"),
        }
        for r in (resp.data or [])
    ]
    return alerts


async def food_cost_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows, label = await asyncio.to_thread(_fetch_recon_with_fallback)
    except Exception:
        logger.exception("food_cost_today failed")
        await message.reply_text("Failed to compute food cost.")
        return
    await message.reply_text(food_cost_analytics.format_food_cost_today(label, rows))


async def food_cost_outlet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    if not context.args:
        await message.reply_text("Usage: /food_cost_outlet <name>\nExample: /food_cost_outlet jakel")
        return
    outlet = _resolve_outlet_name(" ".join(context.args))
    week_dates = set(_business_date_list(7))
    month_dates = set(_month_to_date_dates())
    all_dates = sorted(week_dates | month_dates)
    try:
        all_rows = await asyncio.to_thread(_fetch_recon_rows, all_dates)
    except Exception:
        logger.exception("food_cost_outlet failed")
        await message.reply_text("Failed to read food cost trend.")
        return
    week_rows = [r for r in all_rows if str(r.get("business_date")) in week_dates]
    week_outlet = [r for r in week_rows if r.get("outlet_canonical") == outlet]
    month_outlet = [
        r for r in all_rows
        if r.get("outlet_canonical") == outlet and str(r.get("business_date")) in month_dates
    ]
    _s, _p, group_pct = food_cost_analytics.group_food_cost(week_rows)
    await message.reply_text(
        food_cost_analytics.format_outlet_trend(outlet, week_outlet, group_pct, month_outlet)
    )


async def cash_no_receipt_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows, label = await asyncio.to_thread(_fetch_recon_with_fallback)
        alerts = await asyncio.to_thread(_fetch_cash_no_receipt_alerts, rows)
    except Exception:
        logger.exception("cash_no_receipt_today failed")
        await message.reply_text("Failed to read cash-no-receipt alerts.")
        return
    await message.reply_text(food_cost_analytics.format_cash_no_receipt(label, alerts))


async def reconcile_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    today = _my_today()
    dates = [today.isoformat(), (today - timedelta(days=1)).isoformat()]
    await message.reply_text("Reconciling receipts against POS payouts…")
    try:
        results = await asyncio.to_thread(
            reconciliation_service.run_reconciliation_for_dates, supabase, dates
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner
        logger.exception("reconcile_now failed")
        await message.reply_text(f"Reconciliation failed: {exc}")
        return
    lines = ["Reconciliation done —"]
    for res in results:
        lines.append(f"• {res['business_date']}: {res['outlets_processed']} outlets")
    await message.reply_text("\n".join(lines))


async def reconcile_date_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force a reconciliation re-run for one historical business date. Needed
    because /reconcile_now only refreshes today + yesterday, so a big sales day
    reconciled by old code (or before later receipts arrived) keeps its stale
    row and skews the 7-day rolling window."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    if not context.args:
        await message.reply_text(
            "Usage: /reconcile_date YYYY-MM-DD\nExample: /reconcile_date 2026-05-25"
        )
        return
    raw = context.args[0].strip()
    try:
        target = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        await message.reply_text(f"Bad date {raw!r}. Use YYYY-MM-DD (e.g. 2026-05-25).")
        return
    await message.reply_text(f"Reconciling {target.isoformat()}…")
    try:
        result = await asyncio.to_thread(
            reconciliation_service.run_reconciliation, supabase, target.isoformat()
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner
        logger.exception("reconcile_date failed")
        await message.reply_text(f"Reconciliation failed: {exc}")
        return
    await message.reply_text(
        f"Reconciliation done — {result['business_date']}: "
        f"{result['outlets_processed']} outlets"
    )


async def food_cost_week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    dates = _business_date_list(7)
    start, end = dates[-1], dates[0]
    try:
        rows = await asyncio.to_thread(_fetch_recon_rows, dates)
    except Exception:
        logger.exception("food_cost_week failed")
        await message.reply_text("Failed to compute food cost.")
        return
    await message.reply_text(
        food_cost_analytics.format_food_cost_week(f"{start} → {end}", rows)
    )


async def food_cost_month_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    dates = _month_to_date_dates()
    start, end = dates[-1], dates[0]
    try:
        rows = await asyncio.to_thread(_fetch_recon_rows, dates)
    except Exception:
        logger.exception("food_cost_month failed")
        await message.reply_text("Failed to compute food cost.")
        return
    await message.reply_text(
        food_cost_analytics.format_food_cost_month(f"{start} → {end}", rows)
    )


# === PR #67: weekly manager food-cost reports (Phase 1) ======================
#
# SAFETY: weekly messages route to the OWNER (prefixed "[TEST — ...]") until
# the owner flips MANAGER_DELIVERY_ENABLED. The owner ALWAYS gets a consolidated
# HQ summary regardless of the flag. Registration maps outlet -> manager but
# delivery to managers stays gated off.


async def gen_codes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: generate one fresh one-time registration code per outlet."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        codes = await asyncio.to_thread(
            manager_registration.create_registration_codes, supabase
        )
    except Exception:
        logger.exception("gen_codes failed")
        await message.reply_text("Failed to generate registration codes.")
        return
    lines = [
        "🔑 Outlet registration codes (one-time use):",
        "",
    ]
    for c in codes:
        lines.append(f"• {c['display']:<10} {c['code']}")
    lines += [
        "",
        "Give each outlet manager their code. They register by DMing this bot:",
        "/register <CODE>",
        "",
        "Generating new codes invalidates any older unused codes.",
    ]
    await message.reply_text("\n".join(lines))


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open to anyone: a manager redeems their one-time code here. We use the
    sender's chat_id + name as the delivery target."""
    message = update.effective_message
    if not message:
        return
    if not context.args:
        await message.reply_text(
            "Usage: /register <CODE>\nExample: /register SEK20-7K2A"
        )
        return
    user = update.effective_user
    chat = update.effective_chat
    manager_name = None
    if user:
        manager_name = (user.full_name or user.username or "").strip() or None
    chat_id = chat.id if chat else (user.id if user else None)
    if chat_id is None:
        return
    try:
        result = await asyncio.to_thread(
            manager_registration.register_manager,
            supabase, context.args[0], manager_name, chat_id,
        )
    except Exception:
        logger.exception("register failed")
        await message.reply_text(
            "Sorry, something went wrong registering you. Please try again."
        )
        return
    if not result.get("ok"):
        # Generic, leak-free error.
        await message.reply_text(result.get("error", manager_registration.INVALID_CODE_MESSAGE))
        return
    await message.reply_text(
        f"✅ Registered for {result['outlet_display']}. "
        "You'll receive that outlet's weekly food-cost summary here."
    )


def _weekly_window_for_anchor(anchor: date):
    """(prior_start, prior_end, before_start, before_end) for the full week
    before the week containing ``anchor``, plus the week before that."""
    pm, ps = wmr.prior_week_range(anchor)
    bpm, bps = wmr.week_before_range(anchor)
    return pm, ps, bpm, bps


def _latest_recon_date():
    """The most recent business_date in purchase_reconciliation that actually
    has sales (sales_total not null), or ``None`` if there's none. A freshly
    reconciled day whose sales D-file hasn't landed yet (sales_total null) is
    skipped — e.g. 28 May with no sales falls back to 27 May.

    Filtering in Python (rather than a NOT-NULL server filter) keeps this on the
    query surface the rest of the bot already uses. 200 most-recent outlet-day
    rows is ~20 business dates — comfortably more than enough to find one with
    sales."""
    resp = (
        supabase.table(RECONCILIATION_TABLE)
        .select("business_date, sales_total")
        .order("business_date", desc=True)
        .limit(200)
        .execute()
    )
    for r in resp.data or []:
        bd = r.get("business_date")
        if r.get("sales_total") is not None and bd:
            return datetime.fromisoformat(str(bd)).date()
    return None


def _recent_data_window():
    """The 7-day window ending on the latest date that HAS sales data, so the
    owner can preview real manager messages before a clean Mon–Sun exists.
    ``None`` if no reconciled day has sales yet."""
    latest = _latest_recon_date()
    if latest is None:
        return None
    return wmr.window_ending(latest)


def _gather_weekly_report(today=None, *, window=None) -> dict:
    """Build every per-outlet message + the HQ summary for a 7-day window.

    Default window is the prior full Mon–Sun (the Monday-09:00 schedule);
    ``window`` overrides it as (prior_start, prior_end, before_start,
    before_end) for on-demand previews. Reuses PR #63's sales-weighted rolling
    food cost; the week before is the "Last week: Z%" baseline. Outlets are
    sourced live from outlet_canonical (active=true). No Telegram I/O here —
    that keeps it testable and the send loop dumb."""
    if window is not None:
        pm, ps, bpm, bps = window
    else:
        today = today or _my_today()
        pm, ps, bpm, bps = _weekly_window_for_anchor(today)
    period_label = f"{pm.isoformat()} → {ps.isoformat()}"

    prior_dates = wmr.dates_in_range(pm, ps)
    before_dates = wmr.dates_in_range(bpm, bps)
    prior_rows = _fetch_recon_rows(prior_dates)
    before_rows = _fetch_recon_rows(before_dates)

    prior_by_outlet = food_cost_analytics.rolling_food_cost_by_outlet(prior_rows)
    before_by_outlet = food_cost_analytics.rolling_food_cost_by_outlet(before_rows)
    _s, _p, group_pct = food_cost_analytics.group_food_cost(prior_rows)
    incomplete = {
        d["outlet"] for d in food_cost_analytics.incomplete_period_dates(prior_rows)
    }
    outlets = manager_registration.load_active_outlets(supabase)
    managers = manager_registration.get_all_managers(supabase)
    enabled = wmr.delivery_enabled()

    messages: list[dict] = []
    hq_rows: list[dict] = []
    for outlet in outlets:
        this_pct = (prior_by_outlet.get(outlet.canonical) or {}).get("pct")
        last_pct = (before_by_outlet.get(outlet.canonical) or {}).get("pct")
        mgr = managers.get(outlet.code)
        # Skip outlets with no data AND no registered manager — nothing useful
        # to say, and no one waiting on it.
        if this_pct is None and last_pct is None and mgr is None:
            continue
        complete = this_pct is not None and outlet.canonical not in incomplete
        note = wmr.contextual_note(this_pct, last_pct, group_pct, complete=complete)
        body = wmr.format_manager_message(
            outlet.display, this_pct, group_pct, last_pct, note
        )
        decision = wmr.route_message(
            enabled, outlet.display,
            mgr.get("chat_id") if mgr else None,
            ALERT_CHAT_ID,
        )
        messages.append({
            "target": decision.target_chat_id,
            "text": decision.prefix + body,
            "outlet": outlet.code,
        })
        hq_rows.append({
            "display": outlet.display,
            "this_pct": this_pct,
            "last_pct": last_pct,
            "manager_name": mgr.get("manager_name") if mgr else None,
            "route_reason": decision.reason,
        })

    hq_summary = wmr.build_hq_summary(period_label, hq_rows, group_pct, enabled)
    return {
        "period_label": period_label,
        "messages": messages,
        "hq_summary": hq_summary,
        "enabled": enabled,
        "has_data": bool(prior_rows),
    }


async def post_weekly_manager_reports(application: Application, *, notify_chat_id=None,
                                      window=None) -> None:
    """Monday 09:00 MY job. Sends each per-outlet message to its routed target
    (owner while delivery is gated off), then ALWAYS sends the owner the
    consolidated HQ summary. ``window`` lets /weekly_report_now preview a
    different 7-day window."""
    try:
        bundle = await asyncio.to_thread(_gather_weekly_report, window=window)
    except Exception:
        logger.exception("weekly manager report: gather failed")
        if notify_chat_id is not None:
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=notify_chat_id,
                    text="Failed to build the weekly manager report.",
                )
        return

    # Graceful empty-window handling: say so plainly instead of going silent or
    # sending a blank summary. The owner (and the on-demand caller) are told.
    if not bundle["has_data"]:
        note = (
            f"📭 No reconciliation data for {bundle['period_label']} — nothing "
            "to report for that week yet.\n\nTip: /weekly_report_now recent "
            "previews the most recent 7 days that DO have data."
        )
        for chat in {ALERT_CHAT_ID, notify_chat_id} - {None}:
            with contextlib.suppress(Exception):
                await application.bot.send_message(chat_id=chat, text=note)
        logger.info("Weekly manager report: no data for %s", bundle["period_label"])
        return

    sent = 0
    for msg in bundle["messages"]:
        try:
            await application.bot.send_message(chat_id=msg["target"], text=msg["text"])
            sent += 1
        except Exception:
            logger.exception("weekly manager report: send failed for %s", msg.get("outlet"))
    # Owner ALWAYS gets the consolidated HQ summary, regardless of the flag.
    try:
        await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=bundle["hq_summary"])
    except Exception:
        logger.exception("weekly manager report: HQ summary send failed")
    logger.info(
        "Weekly manager report posted (%d outlet messages, delivery_enabled=%s)",
        sent, bundle["enabled"],
    )


async def weekly_report_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: trigger the weekly report on demand (for testing).

    Usage:
      /weekly_report_now              -> last full Mon–Sun (the live schedule)
      /weekly_report_now recent       -> most recent 7 days that have sales data
      /weekly_report_now YYYY-MM-DD   -> the 7-day week ending on that date
    """
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    arg = context.args[0].strip().lower() if context.args else ""
    window = None
    hint = "last full week"
    if arg in ("recent", "latest"):
        window = await asyncio.to_thread(_recent_data_window)
        if window is None:
            await message.reply_text("No reconciliation data with sales exists yet to preview.")
            return
        hint = f"most recent 7 days with data (ending {window[1].isoformat()})"
    elif arg:
        try:
            anchor = datetime.strptime(arg, "%Y-%m-%d").date()
        except ValueError:
            await message.reply_text(
                "Usage: /weekly_report_now [recent | YYYY-MM-DD]"
            )
            return
        window = wmr.window_ending(anchor)
        hint = f"week ending {anchor.isoformat()}"
    await message.reply_text(f"Building weekly manager report ({hint})…")
    await post_weekly_manager_reports(
        context.application, notify_chat_id=_command_owner_id(update), window=window
    )


# === Auto order-list generator (Phase 1) ====================================
# Evening (default 20:00 MY) per-outlet purchase-order drafts. Delivery reuses
# the weekly-report safety gate: while MANAGER_DELIVERY_ENABLED is False every
# draft routes to the owner with a [TEST] prefix; the owner always gets the HQ
# summary. Nothing is ever auto-sent to a supplier — managers review & edit, the
# office boy forwards. See order_generator / order_cadence / order_draft.

def _gather_order_drafts(today=None) -> dict:
    """Build every per-outlet order draft + an HQ summary for the owner.

    Reuses the live outlet registry (outlet_canonical) for display names and
    outlet_managers for routing, and the same route_message / delivery_enabled
    gate as the weekly report. No Telegram I/O here — that stays in the job."""
    import outlet_mapping

    today = today or _my_today()
    outlets = manager_registration.load_active_outlets(supabase)
    managers = manager_registration.get_all_managers(supabase)
    display_by_code = {o.code: o.display for o in outlets}
    enabled = wmr.delivery_enabled()

    # Prefer the live registry name; fall back to the internal-code display map
    # (so item_prices codes like "D" render as "D.U", never a bare letter).
    bundle = order_generator.gather_order_drafts(
        supabase, today=today,
        display_for=lambda code: display_by_code.get(code)
        or outlet_mapping.outlet_display_name(code),
    )

    messages: list[dict] = []
    hq_rows: list[dict] = []
    for o in bundle["outlets"]:
        code = o["outlet_code"]
        mgr = managers.get(code)
        decision = wmr.route_message(
            enabled, o["display"],
            mgr.get("chat_id") if mgr else None,
            ALERT_CHAT_ID,
        )
        # One per Telegram-safe chunk; the routing prefix rides on the first.
        for i, chunk in enumerate(o["messages"]):
            messages.append({
                "target": decision.target_chat_id,
                "text": (decision.prefix + chunk) if i == 0 else chunk,
                "outlet": code,
            })
        hq_rows.append({
            "display": o["display"],
            "lines": o["line_count"],
            "review": o["review_count"],
            "route_reason": decision.reason,
            "manager_name": mgr.get("manager_name") if mgr else None,
        })

    mode = (
        "🟢 LIVE — drafts delivered to registered managers"
        if enabled else
        "🧪 TEST MODE — every draft above was sent to you, NOT to managers"
    )
    hq_lines = [
        f"🧾 HQ Order-Draft Summary — for {bundle['target_day'].isoformat()}",
        "",
    ]
    if hq_rows:
        for r in hq_rows:
            if r["route_reason"] == "manager":
                who = f"→ {r['manager_name'] or 'manager'}"
            elif r["route_reason"] == "no_manager":
                who = "→ (no manager registered)"
            else:
                who = "→ you (test)"
            flag = f"  ⚠️{r['review']} review" if r["review"] else ""
            hq_lines.append(f"{r['display']:<12} {r['lines']} item(s){flag}  {who}")
    else:
        hq_lines.append("No outlets had items due tomorrow.")
    hq_lines += ["", mode]

    return {
        "target_day": bundle["target_day"],
        "messages": messages,
        "hq_summary": "\n".join(hq_lines),
        "enabled": enabled,
        "has_data": bundle["has_data"],
    }


async def post_order_drafts(application: Application, *, notify_chat_id=None) -> None:
    """Evening job: build and route per-outlet order drafts, then send the owner
    the HQ summary. Delivery is gated by MANAGER_DELIVERY_ENABLED (default off)."""
    try:
        bundle = await asyncio.to_thread(_gather_order_drafts)
    except Exception as exc:
        logger.exception("order drafts: gather failed")
        # Never silent: the owner always hears that the run crashed.
        alert = order_generator.failure_alert(gather_error=type(exc).__name__)
        for chat in {ALERT_CHAT_ID, notify_chat_id} - {None}:
            with contextlib.suppress(Exception):
                await application.bot.send_message(chat_id=chat, text=alert)
        return

    if not bundle["has_data"]:
        note = ("📭 No purchase history in the lookback window — no order drafts "
                "to build yet.")
        for chat in {ALERT_CHAT_ID, notify_chat_id} - {None}:
            with contextlib.suppress(Exception):
                await application.bot.send_message(chat_id=chat, text=note)
        return

    total = len(bundle["messages"])
    failed = 0
    for msg in bundle["messages"]:
        try:
            await application.bot.send_message(chat_id=msg["target"], text=msg["text"])
        except Exception:
            failed += 1
            logger.exception("order drafts: send failed for %s", msg.get("outlet"))
    hq_failed = False
    try:
        await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=bundle["hq_summary"])
    except Exception:
        hq_failed = True
        logger.exception("order drafts: HQ summary send failed")
    logger.info("Order drafts posted (%d/%d messages sent, delivery_enabled=%s)",
                total - failed, total, bundle["enabled"])

    # Never silent: surface any send failure to the owner so a swallowed
    # exception can't lose drafts unnoticed again.
    alert = order_generator.failure_alert(
        total_messages=total, failed_messages=failed, hq_failed=hq_failed)
    if alert:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=alert)


async def order_drafts_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: build the order drafts on demand (for testing the evening job)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text("Building order drafts…")
    await post_order_drafts(context.application, notify_chat_id=_command_owner_id(update))


async def top_items_sold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, 10, 50)
    dates = _business_date_list(7)
    try:
        rows = await asyncio.to_thread(_fetch_sales_items_rows, dates)
    except Exception:
        logger.exception("top_items_sold failed")
        await message.reply_text("Failed to read items sold.")
        return
    await message.reply_text(
        sales_analytics.format_top_items_sold(sales_analytics.top_items_sold(rows, n), n)
    )


# === PR #60: D-file (daily summary) analytics (owner-only) ===================

def _fetch_daily_summary_rows(business_dates):
    resp = (
        supabase.table(SALES_DAILY_SUMMARY_TABLE)
        .select("outlet_canonical, business_date, day_sales, customers, "
                "average_spent, take_away, dine_in")
        .in_("business_date", business_dates)
        .execute()
    )
    return resp.data or []


def _fetch_daily_top_items_rows(business_dates):
    daily = (
        supabase.table(SALES_DAILY_SUMMARY_TABLE)
        .select("id")
        .in_("business_date", business_dates)
        .execute()
    )
    ids = [r["id"] for r in (daily.data or [])]
    if not ids:
        return []
    resp = (
        supabase.table(SALES_DAILY_TOP_ITEMS_TABLE)
        .select("item_name, qty, amount")
        .in_("summary_id", ids)
        .execute()
    )
    return resp.data or []


def _fetch_daily_with_fallback():
    """Today's D-file rows, falling back to yesterday's when today is empty.

    D-files land ~07:00 covering YESTERDAY's business, so today is empty until
    the evening files arrive — the morning-after query should show the day that
    just closed. Returns ``(rows, label)``; the yesterday label is flagged."""
    today = _my_today()
    yesterday = today - timedelta(days=1)
    today_rows = _fetch_daily_summary_rows([today.isoformat()])
    yesterday_rows = [] if today_rows else _fetch_daily_summary_rows([yesterday.isoformat()])
    return sales_analytics.select_daily_dataset(
        today_rows, yesterday_rows,
        today.isoformat(), f"yesterday ({yesterday.isoformat()})",
    )


async def sales_summary_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows, label = await asyncio.to_thread(_fetch_daily_with_fallback)
    except Exception:
        logger.exception("sales_summary_today failed")
        await message.reply_text("Failed to fetch daily summary.")
        return
    await message.reply_text(sales_analytics.format_daily_summary(label, rows))


async def sales_customers_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows, label = await asyncio.to_thread(_fetch_daily_with_fallback)
    except Exception:
        logger.exception("sales_customers_today failed")
        await message.reply_text("Failed to fetch customer counts.")
        return
    await message.reply_text(sales_analytics.format_customers(label, rows))


async def sales_avg_ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows, label = await asyncio.to_thread(_fetch_daily_with_fallback)
    except Exception:
        logger.exception("sales_avg_ticket failed")
        await message.reply_text("Failed to fetch average ticket.")
        return
    await message.reply_text(sales_analytics.format_avg_ticket(label, rows))


async def sales_takeaway_split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    try:
        rows, label = await asyncio.to_thread(_fetch_daily_with_fallback)
    except Exception:
        logger.exception("sales_takeaway_split failed")
        await message.reply_text("Failed to fetch takeaway split.")
        return
    await message.reply_text(sales_analytics.format_takeaway_split(label, rows))


async def top_items_yesterday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    yesterday = (_my_today() - timedelta(days=1)).isoformat()
    try:
        rows = await asyncio.to_thread(_fetch_daily_top_items_rows, [yesterday])
    except Exception:
        logger.exception("top_items_yesterday failed")
        await message.reply_text("Failed to read top items.")
        return
    await message.reply_text(sales_analytics.format_top_items_group(yesterday, rows, 5))


async def poll_sales_emails() -> None:
    """APScheduler job: ingest unread shift-close emails (every 30 min, 24/7)."""
    if not os.environ.get("GMAIL_INBOX") or not os.environ.get("GMAIL_APP_PASSWORD"):
        logger.info("Sales ingest poll skipped: GMAIL_INBOX/GMAIL_APP_PASSWORD not set")
        return
    try:
        summary = await asyncio.to_thread(run_ingest_once, supabase)
        logger.info("Sales ingest poll: %s", summary)
    except Exception:
        logger.exception("Sales ingest poll failed")


async def run_bot() -> None:
    # concurrent_updates(True): process updates as independent tasks so a slow
    # handler (a multi-second OCR on an uploaded receipt) doesn't block other
    # updates. Without it PTB handles updates sequentially, so kitchen numpad
    # taps would queue behind an in-flight OCR and feel laggy.
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("compare", compare_command))
    app.add_handler(CommandHandler("advances", advances_command))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("cloudinary_check", cloudinary_check_command))
    app.add_handler(CommandHandler("kitchen_groups_debug", kitchen_groups_debug_command))
    app.add_handler(CommandHandler("kitchen_post_now", kitchen_post_now_command))
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
    app.add_handler(CommandHandler("merchant_resolve_now", merchant_resolve_now_command))
    app.add_handler(CommandHandler("merchant_review", merchant_review_command))
    app.add_handler(CommandHandler("merchant_undo", merchant_undo_command))
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
    app.add_handler(CommandHandler("test_digest", test_digest_command))
    app.add_handler(CommandHandler("sales_today", sales_today_command))
    app.add_handler(CommandHandler("sales_yesterday", sales_yesterday_command))
    app.add_handler(CommandHandler("sales_outlet", sales_outlet_command))
    app.add_handler(CommandHandler("sales_ingest_status", sales_ingest_status_command))
    app.add_handler(CommandHandler("sales_ingest_manual", sales_ingest_manual_command))
    app.add_handler(CommandHandler("food_cost_today", food_cost_today_command))
    app.add_handler(CommandHandler("food_cost_week", food_cost_week_command))
    app.add_handler(CommandHandler("food_cost_month", food_cost_month_command))
    app.add_handler(CommandHandler("food_cost_outlet", food_cost_outlet_command))
    app.add_handler(CommandHandler("gen_codes", gen_codes_command))
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("weekly_report_now", weekly_report_now_command))
    app.add_handler(CommandHandler("order_drafts_now", order_drafts_now_command))
    app.add_handler(CommandHandler("cash_no_receipt_today", cash_no_receipt_today_command))
    app.add_handler(CommandHandler("reconcile_now", reconcile_now_command))
    app.add_handler(CommandHandler("reconcile_date", reconcile_date_command))
    app.add_handler(CommandHandler("top_items_sold", top_items_sold_command))
    app.add_handler(CommandHandler("sales_summary_today", sales_summary_today_command))
    app.add_handler(CommandHandler("sales_customers_today", sales_customers_today_command))
    app.add_handler(CommandHandler("sales_avg_ticket", sales_avg_ticket_command))
    app.add_handler(CommandHandler("sales_takeaway_split", sales_takeaway_split_command))
    app.add_handler(CommandHandler("top_items_yesterday", top_items_yesterday_command))
    app.add_handler(
        CallbackQueryHandler(reparse_apply_all_callback, pattern=r"^reparse_applyall:(yes|no)$")
    )
    app.add_handler(
        CallbackQueryHandler(backfill_apply_all_callback, pattern=r"^backfill_applyall:(yes|no)$")
    )
    # Daily Kitchen Usage Log: tap-only numpad form (kdu: namespace). Init the
    # module with the shared Supabase client, then register its single callback
    # handler.
    kitchen_usage.init_kitchen_usage(supabase)
    kitchen_usage.register_handlers(app)
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
    # PR #35: poll the master inbox for shift-close emails every 30 min, 24/7.
    # In-process (no separate Render cron) — $0 extra.
    scheduler.add_job(
        poll_sales_emails,
        trigger="cron",
        minute="*/30",
        id="sales_ingest",
        replace_existing=True,
    )
    # PR #67: weekly manager food-cost reports — Monday 09:00 Asia/Kuala_Lumpur.
    # Delivery is gated by MANAGER_DELIVERY_ENABLED (default False): until the
    # owner flips it, every message routes to the owner with a [TEST] prefix,
    # and the owner always receives the consolidated HQ summary.
    scheduler.add_job(
        post_weekly_manager_reports,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=0,
        args=[app],
        id="weekly_manager_reports",
        replace_existing=True,
    )
    # Auto order-list generator — evening per-outlet purchase-order drafts
    # (default 20:00 MY, ahead of the 23:00 digest so it isn't buried). Gated by
    # MANAGER_DELIVERY_ENABLED: until the owner flips it, every draft routes to
    # the owner with a [TEST] prefix. Never auto-sends to suppliers.
    _order_draft_hour = order_generator.send_hour()
    scheduler.add_job(
        post_order_drafts,
        trigger="cron",
        hour=_order_draft_hour,
        minute=0,
        args=[app],
        id="order_drafts",
        replace_existing=True,
    )
    # Daily Kitchen Usage Log — same in-process scheduler as the 23:00 digest.
    # 18:00 COOKED form, 00:00 optional night-cook (additive) form, 02:00 LEFT
    # form, to each configured kitchen group. All three belong to the same
    # business_date (the 18:00 date — 00:00 and 02:00 fold back). They no-op
    # cleanly unless KITCHEN_LOG_ENABLED is set and groups resolve.
    scheduler.add_job(
        kitchen_usage.post_cooked_forms,
        trigger="cron",
        hour=18,
        minute=0,
        args=[app],
        id="kitchen_cooked_form",
        replace_existing=True,
    )
    scheduler.add_job(
        kitchen_usage.post_night_forms,
        trigger="cron",
        hour=0,
        minute=0,
        args=[app],
        id="kitchen_night_form",
        replace_existing=True,
    )
    scheduler.add_job(
        kitchen_usage.post_left_forms,
        trigger="cron",
        hour=2,
        minute=0,
        args=[app],
        id="kitchen_left_form",
        replace_existing=True,
    )
    # STAGE 2 of the kitchen digest — the real Used-vs-POS comparison. The 02:00
    # LEFT submission only confirms the save + usage (STAGE 1); same-day POS isn't
    # ingested until the ~7AM sales email. At 09:00 POS exists, so this posts the
    # v12-aware comparison (with dual-gate flags) for the business day that just
    # closed at 02:00. A retry at 11:00 catches outlets whose POS email was late
    # (it stays silent on outlets still missing POS — they were notified at 09:00).
    scheduler.add_job(
        kitchen_usage.post_comparison_digests,
        trigger="cron",
        hour=9,
        minute=0,
        args=[app],
        id="kitchen_comparison",
        replace_existing=True,
    )
    scheduler.add_job(
        kitchen_usage.post_comparison_digests_retry,
        trigger="cron",
        hour=11,
        minute=0,
        args=[app],
        id="kitchen_comparison_retry",
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

        # One-time Cloudinary archival health probe. Off-thread so it can't block
        # the event loop, and fully wrapped so a broken/misconfigured Cloudinary
        # only logs — it must never stop the bot from starting.
        try:
            probe_ok, probe_detail = await asyncio.to_thread(probe_cloudinary)
            if probe_ok:
                logger.info("CLOUDINARY PROBE: %s", probe_detail)
            else:
                logger.warning("CLOUDINARY PROBE: %s", probe_detail)
        except Exception:
            logger.warning("CLOUDINARY PROBE: probe errored; continuing", exc_info=True)

        # Kitchen-usage group resolution summary. Off-thread (it reads receipts)
        # and fully wrapped — surfaces any expected outlet that didn't resolve
        # (e.g. a group with no recent receipts) so it isn't silently skipped.
        try:
            from config.kitchen_groups import log_resolution_summary
            await asyncio.to_thread(log_resolution_summary, supabase)
        except Exception:
            logger.warning("KITCHEN GROUPS: resolution summary failed; continuing", exc_info=True)

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
