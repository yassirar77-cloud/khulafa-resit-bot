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
from db_pagination import fetch_all_pages
from webapp_auth import verify_init_data
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
import bill_analysis
import cashier_names
import digest
import director_ask
import director_feed
import director_sql
import log_redact
import group_reports
import food_cost_analytics
import kitchen_usage
import manager_registration
import staff_chat
import staff_ack
import staff_ai
import staff_anomaly
import staff_digest
import staff_issues
import staff_learning
import staff_live
import staff_nudge
import staff_ops
import staff_orders
import staff_voice
from outlet_group_bot import OutletGroupBot
import key_stock_daily
import item_sales_watch
import demand_forecast
import missing_bills
import monthly_consumption
import human_touch
import known_merchants
import outlet_resolver
import outside_purchase
import overbuy_check
import overbuy_watch
import supervisor
import order_generator
import order_proposal
import po_mismatch
import order_sanity
import reconciliation_service
import sales_analytics
import shop_price_comparison
import weekly_manager_reports as wmr
from digest_data import gather_digest_data, log_digest
from sales_ingest import run_ingest_once
from sales_parser import OUTLET_CANONICAL_BY_CODE
from receipt_classifier import ReceiptType, classify_receipt

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# The Bot API puts the token in every request URL and httpx logged each URL:
# mask secrets in every log line and quiet the per-request loggers.
log_redact.install()
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ZAI_API_KEY = os.environ["ZAI_API_KEY"]
ZAI_BASE_URL = os.environ.get("ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
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

# Memory guard for receipt OCR. concurrent_updates(True) (see run_bot) lets PTB
# process every update as an independent task, so a burst of receipt photos
# (e.g. several outlets uploading at shift close) would otherwise decode N full
# phone photos with Pillow AT THE SAME TIME — each decode is width*height*3
# bytes plus an exif-transpose copy plus the resize intermediate, so a single
# 12MP photo transiently costs ~80-100MB and N of them in parallel is the most
# likely OOM trigger on a small instance. This semaphore caps how many receipts
# sit in the heavy region (download -> decode -> OCR -> verify -> archive) at
# once, bounding peak RSS regardless of how many photos arrive together. Numpad
# taps and text commands are unaffected — they never take this lock.
OCR_MAX_CONCURRENCY = max(1, int(os.environ.get("OCR_MAX_CONCURRENCY", "2")))
_ocr_semaphore = asyncio.Semaphore(OCR_MAX_CONCURRENCY)


def _release_freed_memory() -> None:
    """Best-effort return of freed heap arenas to the OS after a receipt.

    CPython frees the large image buffers when they go out of scope, but glibc's
    allocator keeps the arenas mapped, so RSS ratchets UP after each OCR burst
    and never comes back down — which reads on Render's memory graph like a leak
    even though no Python object is retained. A gc pass + malloc_trim hands the
    pages back. Linux/glibc only and fully guarded; a no-op anywhere else."""
    import gc

    gc.collect()
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:  # noqa: BLE001 - non-glibc / unavailable: harmless to skip
        pass
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
    # Cheese (Bega Super Slice, Klang)
    'FRIZZ STATION', 'FRIZZ',
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
    # The AI wording provider's line: when DeepSeek last answered and what
    # today's calls have cost in tokens (staff_ai.status; in-memory, per
    # process, reset each Malaysian day).
    try:
        ai = staff_ai.status()
    except Exception:
        ai = {"error": "status unavailable"}
    return jsonify(status="ok", service="khulafa-resit-bot", staff_ai=ai)


@flask_app.get("/webapp")
def webapp():
    # The page itself is a static shell — data comes from /webapp/data, which
    # verifies Telegram WebApp initData server-side. No Supabase credentials
    # (not even the anon key) are shipped to the browser.
    return render_template("dashboard.html")


@flask_app.get("/webapp/data")
def webapp_data():
    from flask import request

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_id = verify_init_data(init_data, TELEGRAM_BOT_TOKEN)
    if user_id is None:
        return jsonify(error="unauthorized"), 401
    if not is_reviewer(user_id):
        return jsonify(error="forbidden"), 403
    try:
        rows = (
            supabase.table(RECEIPTS_TABLE)
            .select("*")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("webapp: receipts fetch failed")
        return jsonify(error="upstream"), 502
    return jsonify(rows)


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
        elif "outlet" in payload and "outlet" in str(exc).lower() and "column" in str(exc).lower():
            # Same fallback for 0035's outlet column.
            logger.warning(
                "pending_review.outlet column missing — apply "
                "migrations/0035_pending_review_outlet.sql. Queuing without outlet."
            )
            payload.pop("outlet", None)
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


def claim_pending_review(
    review_id, status: str, reviewer_chat_id, edited_data: dict | None = None
) -> bool:
    """Compare-and-swap the row from 'pending' to `status`. Returns False if
    another reviewer got there first — the same item is DM'd to every
    reviewer, so two near-simultaneous taps are a real possibility."""
    payload = {
        "status": status,
        "reviewer_chat_id": reviewer_chat_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    if edited_data is not None:
        payload["edited_data"] = edited_data
    result = (
        supabase.table(PENDING_REVIEW_TABLE)
        .update(payload)
        .eq("id", review_id)
        .eq("status", "pending")
        .execute()
    )
    return bool(result.data)


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
        # Prefer the outlet captured at queue time (0035): derive_outlet with
        # no chat title can only consult the (empty) static map -> NULL.
        "outlet": pending.get("outlet") or derive_outlet(chat_id, None),
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


TELEGRAM_MAX_MESSAGE = 4096


def chunk_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE) -> list[str]:
    """Split text into <=limit pieces, preferring newline boundaries, so a
    receipt with very many items can't push a reply past Telegram's 4096-char
    cap (BadRequest would kill the handler mid-pipeline)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 1, limit)
        if cut == -1:
            cut = limit
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def _reply_chunked(message, text: str) -> None:
    for chunk in chunk_message(text):
        await message.reply_text(chunk)


async def _callback_reply(query, context, text: str) -> None:
    """Reply in a callback's chat. Buttons tapped on messages older than 48h
    arrive with an InaccessibleMessage (no reply_text) — the DB action has
    usually already run by the time we reply, so falling back to a plain
    send_message keeps the reviewer informed instead of raising."""
    message = query.message
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text)
        return
    chat = getattr(message, "chat", None)
    chat_id = chat.id if chat is not None else (
        query.from_user.id if query.from_user else None
    )
    if chat_id is not None:
        await context.bot.send_message(chat_id=chat_id, text=text)


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


def _check_big_purchase(chat_id: int, total: float, current_id=None) -> str | None:
    since = (datetime.now(MALAYSIA_TZ).date() - timedelta(days=BIG_PURCHASE_LOOKBACK_DAYS)).isoformat()
    q = (
        supabase.table(RECEIPTS_TABLE)
        .select("id, total")
        .eq("chat_id", chat_id)
        .gte("receipt_date", since)
    )
    res = q.execute()
    rows = res.data or []
    # The receipt under audit is already inserted — exclude it so it doesn't
    # inflate the baseline it's being compared against.
    if current_id is not None:
        rows = [r for r in rows if r.get("id") != current_id]
    totals = [t for r in rows if (t := _to_float(r.get("total"))) is not None]
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


def _check_suspicious_items(chat_id: int, merchant: str, items: list,
                            found: list | None = None) -> str | None:
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
            if found is not None:
                found.append(name)

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


def run_audit_checks(stored: dict, parsed: dict,
                     details: dict | None = None) -> list[tuple[str, str]]:
    """The receipt audit findings ``[(type, question)]``. ``details`` (when
    given) also gets ``{"suspicious_item": <first item name>}``."""
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
        if (q := _check_big_purchase(chat_id, total, current_id)):
            findings.append(("big_purchase", q))
    except Exception:
        logger.exception("big_purchase check failed")

    try:
        names: list = []
        if (q := _check_suspicious_items(chat_id, merchant, items, names)):
            findings.append(("suspicious_item", q))
            if details is not None and names:
                details["suspicious_item"] = names[0]
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


def save_audit_reply(chat_id: int, reply_to_message_id: int, manager_reply: str):
    """Capture a manager's reply to a tracked question. Returns the question
    row (truthy) when one matched, else ``None`` — the caller uses the row
    to report the answer upward to the owner."""
    res = (
        supabase.table(AUDIT_TABLE)
        .select("id, chat_id, question_type, question_text")
        .eq("chat_id", chat_id)
        .eq("question_message_id", reply_to_message_id)
        .is_("replied_at", "null")
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    row = rows[0]
    replied_at = datetime.now(timezone.utc).isoformat()
    supabase.table(AUDIT_TABLE).update(
        {
            "manager_reply": manager_reply,
            "replied_at": replied_at,
        }
    ).eq("id", row["id"]).execute()
    # A reply to the daily NUDGE closes the original question it points at,
    # and the owner note should show the real question, not the nudge.
    original_id = supervisor.linked_original_id(row)
    if original_id is not None:
        original = supervisor.close_question(
            supabase, original_id, manager_reply, replied_at
        )
        if original:
            return original
    return row


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
        question_text = supervisor.with_reply_footer(question_text)
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
        if key == "date":
            # handle_photo copies date→receipt_date BEFORE verification runs,
            # and the stored record prefers receipt_date — keep them in sync or
            # the correction is silently discarded at insert time.
            parsed["receipt_date"] = new_val
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
    image_url: str | None = None, outlet: str | None = None,
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
        await message.reply_text(_receipt_text(message.chat_id, "review_dup"))
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
        # Captured at queue time — the chat title isn't available at promotion
        # time, so without this every promoted receipt stored outlet=NULL.
        "outlet": outlet,
        "confidence": confidence,
        "reason": reason,
        "status": "pending",
        **serialize_parsed_for_review(parsed),
    }
    try:
        stored = await asyncio.to_thread(store_pending_review, pending_record)
    except Exception as exc:
        # 0035's partial unique index closes the check-then-act dedup race: a
        # second copy of the same photo racing past is_duplicate_review loses
        # the insert — treat it exactly like the pre-checked duplicate.
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            logger.info("Duplicate review insert blocked by unique index")
            await message.reply_text(_receipt_text(message.chat_id, "review_dup"))
            return
        logger.exception("Failed to queue receipt for manual review")
        await message.reply_text(_receipt_text(message.chat_id, "review_failed"))
        return
    logger.info(
        "Receipt queued for review: pending_id=%s confidence=%s reason=%s",
        stored.get("id"), confidence, reason,
    )
    await message.reply_text(_receipt_text(
        message.chat_id, "review_flagged",
        confidence=confidence if confidence is not None else "—"))
    await _dm_reviewers(context, stored)


async def _finalize_review(
    review_id, reviewer_chat_id, status: str, edits: dict | None = None
) -> dict | None:
    """Promote a pending row to `receipts` (for approve/edit) and flip its
    status. Returns the stored receipt, or None if the row is gone/handled."""
    pending = await asyncio.to_thread(fetch_pending_review, review_id)
    if not pending or pending.get("status") != "pending":
        return None
    # Claim the row BEFORE promoting: every reviewer gets a DM for the same
    # item, so two near-simultaneous taps would otherwise both pass the check
    # above and insert duplicate receipts.
    claimed = await asyncio.to_thread(
        claim_pending_review, review_id, status, reviewer_chat_id, edits
    )
    if not claimed:
        return None
    try:
        return await asyncio.to_thread(promote_pending_to_receipt, pending, edits)
    except Exception:
        # Release the claim so the item stays retryable instead of being
        # marked approved with no receipt row.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                update_pending_review, review_id, "pending", reviewer_chat_id
            )
        raise


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
        await _callback_reply(query, context, "❌ Discarded — nothing saved to receipts.")
        return

    if action == "save":
        try:
            stored = await _finalize_review(review_id, reviewer_chat_id, "approved")
        except Exception:
            logger.exception("Failed to approve pending review %s", review_id)
            await _callback_reply(query, context, "Failed to save — please retry.")
            return
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)
        if stored is None:
            await _callback_reply(query, context, "Already handled.")
        else:
            await _callback_reply(
                query, context, f"✅ Saved receipt #{stored.get('id')} as-is."
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
        await _callback_reply(query, context, "This item was already handled.")
        return ConversationHandler.END
    context.user_data["review_id"] = review_id
    context.user_data["review_edits"] = {}
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    await _callback_reply(
        query, context,
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


# In the outlet groups a bill is confirmed with a reaction on the photo, not
# a text reply: 👀 while it is being read, 👌 once saved (Telegram doesn't
# allow ✅ as a bot reaction). Text only when something needs the staff:
# unreadable, sent for review, price rise, mini market / unusual invoice.
RECEIPT_READING, RECEIPT_SAVED = "👀", "👌"

# The few bill texts staff still see, in the cashier's language (outlet
# groups); every other chat keeps the English wording.
_RECEIPT_TEXTS = {
    "reading": {
        "english": "Processing receipt…", "bm": "Sedang baca bil…",
        "tamil": "Bill-ஐ படிக்கிறேன்…", "bengali": "Bill porchi…",
        "indonesian": "Sedang membaca nota…",
    },
    "read_failed": {
        "english": "Failed to read receipt. Try a clearer photo.",
        "bm": "Bil tak dapat dibaca. Tolong hantar gambar yang lebih jelas 🙏",
        "tamil": "Bill-ஐ படிக்க முடியல. கொஞ்சம் தெளிவா photo எடுத்து அனுப்புங்க 🙏",
        "bengali": "Bill porte parlam na. Aro porishkar chobi pathan 🙏",
        "indonesian": "Nota tidak terbaca. Tolong kirim foto yang lebih jelas 🙏",
    },
    "save_failed": {
        "english": "Saved OCR locally but database write failed.",
        "bm": "Bil diterima tapi tak dapat disimpan. Pejabat akan semak.",
        "tamil": "Bill வந்துச்சு, ஆனா save ஆகல. Office பாத்துக்கும்.",
        "bengali": "Bill peyechi, kintu save hoy nai. Office dekhbe.",
        "indonesian": "Nota diterima tapi tidak bisa disimpan. Kantor akan cek.",
    },
    "review_dup": {
        "english": "🔎 This receipt is already in the review queue from earlier — not re-sending.",
        "bm": "🔎 Bil ini sudah dalam semakan pejabat.",
        "tamil": "🔎 இந்த bill ஏற்கனவே office check-ல இருக்கு.",
        "bengali": "🔎 Ei bill age thekei office-er check-e ache.",
        "indonesian": "🔎 Nota ini sudah dalam pengecekan kantor.",
    },
    "review_failed": {
        "english": "Couldn't queue this receipt for review — please try resending.",
        "bm": "Bil tak dapat dihantar untuk semakan — tolong hantar gambar sekali lagi.",
        "tamil": "Bill-ஐ check-க்கு அனுப்ப முடியல — photo-வை மறுபடி அனுப்புங்க.",
        "bengali": "Bill check-e pathano gelo na — chobi abar pathan.",
        "indonesian": "Nota tidak bisa dikirim untuk dicek — tolong kirim fotonya lagi.",
    },
    "review_flagged": {
        "english": "🔎 Receipt flagged for review (confidence {confidence}). "
                   "A reviewer will confirm it shortly.",
        "bm": "🔎 Bil ini kurang jelas — pejabat akan semak dulu.",
        "tamil": "🔎 இந்த bill கொஞ்சம் தெளிவா இல்ல — office முதல்ல check பண்ணும்.",
        "bengali": "🔎 Ei bill ektu osposhto — office age dekhbe.",
        "indonesian": "🔎 Nota ini kurang jelas — kantor akan cek dulu.",
    },
    "advance_noname": {
        "english": "💰 Advance recorded, but the staff name couldn't be read. "
                   "Reply /advances to update the name.",
        "bm": "💰 Advance dicatat, tapi nama staff tak jelas pada resit. "
              "Guna /advances untuk kemas kini nama.",
        "tamil": "💰 Advance பதிவு ஆச்சு, ஆனா staff பெயர் தெரியல. "
                 "/advances-ல பெயரை போடுங்க.",
        "bengali": "💰 Advance likha holo, kintu staff-er naam bojha jay nai. "
                   "/advances diye naam din.",
        "indonesian": "💰 Advance dicatat, tapi nama staff tidak terbaca. "
                      "Pakai /advances untuk memperbarui nama.",
    },
}


def _receipt_text(chat_id, key: str, **values) -> str:
    """A bill text for ``chat_id``: the cashier's language in an outlet
    group, English anywhere else."""
    table = _RECEIPT_TEXTS[key]
    if cashier_names.is_outlet_group(chat_id):
        text = cashier_names.pick(table, cashier_names.language_for_chat(chat_id))
    else:
        text = table["english"]
    return text.format(**values) if values else text


async def _react(bot, message, emoji) -> bool:
    """Set (or with ``None`` clear) the bot's reaction on a message."""
    try:
        await bot.set_message_reaction(
            chat_id=message.chat_id, message_id=message.message_id, reaction=emoji)
        return True
    except Exception:
        logger.warning("reaction %s failed in chat %s", emoji, message.chat_id, exc_info=True)
        return False


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

    # An outlet group gets reactions instead of confirmation texts.
    quiet = cashier_names.outlet_for_chat(message.chat_id) is not None
    # A live outlet group gets at most ONE question per bill, from the staff
    # question system (buttons, cashier's language, daily limit) — the old
    # Tamil price-spike note, the anomaly text and the audit questions only
    # feed it (staff_ops.upload_question).
    ops_group = (quiet and staff_live.is_live(cashier_names.outlet_for_chat(message.chat_id))
                 and staff_chat.style() != staff_chat.CLASSIC)
    ops_candidates: dict = {}
    if not (quiet and await _react(context.bot, message, RECEIPT_READING)):
        await message.reply_text(_receipt_text(message.chat_id, "reading"))

    photo = message.photo[-1]
    photo_file_id = photo.file_id

    # Everything from here to collecting the archived image_url holds large byte
    # buffers and decoded Pillow images in memory, so it runs under the OCR
    # semaphore to cap how many receipts decode at once (see OCR_MAX_CONCURRENCY).
    # image_bytes/image_upload_task are bound to None first so the `finally` can
    # release them unconditionally even if get_file/download raises.
    image_bytes = None
    image_upload_task = None
    await _ocr_semaphore.acquire()
    try:
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())

        # Persist the image for every receipt (re-OCR / model comparison /
        # debug). Kick off a best-effort Cloudinary archive of the ORIGINAL
        # full-res bytes (before resize downscales them) as a background task.
        # It runs concurrently with OCR — which dominates latency — so archival
        # adds ~nothing to the live flow, and upload_receipt_image never raises
        # (returns None on failure).
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
            if quiet:
                await _react(context.bot, message, None)
            await message.reply_text(_receipt_text(message.chat_id, "read_failed"))
            return
        ocr_latency = time.monotonic() - ocr_start
        logger.info(
            "OCR pipeline complete: provider=%s total_latency=%.2fs merchant=%r total=%s",
            ZAI_OCR_PROVIDER, ocr_latency, parsed.get("merchant"), parsed.get("total"),
        )

        # Normalise to a single internal shape: chat returns "date", glm-ocr
        # returns "receipt_date". Use receipt_date as canonical going forward.
        if parsed.get("receipt_date") is None and parsed.get("date") is not None:
            parsed["receipt_date"] = parsed.get("date")

        # Single safety net for both OCR providers: ensure items is always a
        # list of dicts before anything (verifier, alerts, audit, Supabase)
        # reads it.
        parsed["items"] = normalize_items(parsed.get("items"))

        verification, verify_prefix = await run_verification(image_bytes, parsed)

        # Collect the background image archive result (started before OCR, so it
        # has overlapped the slow OCR/verification work). Never let it break the
        # save.
        try:
            image_url = await image_upload_task
        except Exception:
            logger.warning("Receipt image archive task failed; continuing", exc_info=True)
            image_url = None
    finally:
        # Drop the big buffers BEFORE releasing the lock so the next waiting
        # receipt doesn't decode while this one's image is still resident, and
        # hand the freed arenas back to the OS so RSS doesn't ratchet up across
        # a burst. image_upload_task already owns its own reference to the
        # original bytes for as long as the upload runs.
        image_bytes = None
        image_upload_task = None
        _release_freed_memory()
        _ocr_semaphore.release()

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
        "photo_file_id": photo_file_id,
        "image_url": image_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # PR #29b: low-confidence receipts do NOT auto-save. Route them to the
    # manual-review queue and DM an authorised reviewer instead, so bad OCR
    # never reaches `receipts`/`item_prices` and poisons price intelligence.
    # We gate on the stored confidence — the second-pass verifier score.
    if should_queue(verification["confidence"]):
        await route_to_review(
            message, context, parsed, verification, image_url=image_url, outlet=outlet
        )
        return

    try:
        stored = await asyncio.to_thread(store_receipt, record)
    except Exception:
        logger.exception("Supabase insert failed")
        await message.reply_text(_receipt_text(message.chat_id, "save_failed"))
        stored = record

    user_alert = format_alert(stored, parsed)
    if verify_prefix:
        user_alert = f"{verify_prefix}\n\n{user_alert}"
    ops_alert = format_alert(stored, parsed, outlet=outlet)
    # Outlet group: a 👌 on the photo is the confirmation (the full list stays
    # in the director's summaries). A failed save already got its text above.
    # If the reaction can't be set, fall back to the text reply.
    saved_quietly = (quiet and stored.get("id") is not None
                     and await _react(context.bot, message, RECEIPT_SAVED))
    try:
        if not saved_quietly:
            await _reply_chunked(message, user_alert)
    except Exception:
        # The receipt is already stored — never let a reply failure abort the
        # downstream routing (side tables, price aggregation, audit checks).
        logger.exception("Failed to send receipt confirmation reply")
    # Focus mode (default): the director no longer gets every receipt from
    # every shop — they are in the 23:59 summary and the 23:00 digest. See
    # director_feed for the full policy.
    if director_feed.wants_receipt_feed():
        try:
            for chunk in chunk_message(ops_alert):
                await context.bot.send_message(chat_id=ALERT_CHAT_ID, text=chunk)
        except Exception:
            logger.exception("Failed to send alert to ALERT_CHAT_ID")

    # PR #24: route non-purchase receipts to their side tables and skip
    # everything below. Only SUPPLIER_PURCHASE flows through price
    # aggregation, spike detection, anomaly detection, and audit checks.
    receipt_id = stored.get("id")
    receipt_type = classification.receipt_type

    # Pinpoint Target: a bill from a shop that is neither an approved supplier
    # nor known for this outlet is an outside purchase — pinpoint the cashier,
    # count the strike, reply under the receipt (outside_purchase). A bill
    # from a known supplier is checked for overbuying against sales instead
    # (overbuy_check). Purchases only: advances, utilities, rent and petty
    # cash are not stock. ONE BILL = ONE MESSAGE: when either sent the cashier
    # something, the mini-market "why?" and the invoice question stay quiet.
    pinpoint_sent = False
    if receipt_type in _OUTSIDE_RECEIPT_TYPES:
        pinpoint_sent, outcome = await outside_purchase_on_upload(context, stored, message)
        if outcome == "skip" and not pinpoint_sent:
            pinpoint_sent = await overbuy_on_upload(context, stored, message)

    # Staff questions v2: a mini market buy gets one "why?" in the group,
    # whatever the receipt was classified as (most come in as UNKNOWN) —
    # unless the pinpoint reply already asked.
    if staff_ops.is_minimarket(stored.get("merchant")) and not pinpoint_sent:
        await staff_ops_on_upload(context.application, stored, message, supplier=False)

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
                await message.reply_text(_receipt_text(message.chat_id, "advance_noname"))
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
                # Issue #79: powers the line-total-vs-receipt-total check
                # in the price_sanity gate.
                _to_float(stored.get("total")),
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
        from price_spike_detection import (
            detect_spikes,
            format_spike_message,
            format_spike_message_tamil,
        )

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
                # Small jumps still reach the manager below and the 21:30
                # bill analysis; only the big ones interrupt the director.
                if not director_feed.wants_spike(spike):
                    continue
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
            if spikes and ops_group:
                # Live group: the price rise becomes the bill's one question
                # (buttons, cashier's language) instead of the Tamil note.
                top = max(spikes, key=lambda sp: float(sp.get("percent_increase") or 0))
                canonical = str(top.get("canonical_item") or "")
                variant = str(top.get("shop_variant") or "").strip()
                import order_items
                ops_candidates["pricerise"] = {
                    "item": canonical,
                    "label": variant.title() if variant else order_items.display_name(canonical),
                    "percent": round(float(top.get("percent_increase") or 0)),
                }
                logger.info("Price spikes: %d for receipt %s (staff question)",
                            len(spikes), receipt_id)
            elif spikes:
                logger.info(
                    "Price spikes alerted: %d for receipt %s",
                    len(spikes),
                    receipt_id,
                )
                # Tamil copy for the outlet's own manager: the price went
                # up, ask the supplier why, and here's who is still
                # cheaper. Routed through the MANAGER_DELIVERY_ENABLED
                # gate (owner gets a [TEST]-prefixed preview while it is
                # off), same as the weekly reports. Own try/except: a
                # manager-lookup failure must not undo the director alert
                # already sent above.
                try:
                    from outlet_mapping import (
                        outlet_display_name,
                        outlet_from_chat_title,
                    )
                    import weekly_manager_reports as wmr

                    # A bill uploaded in a registered outlet group gets its
                    # question in that same group — the group IS the
                    # manager. The chat-title rules are only the fallback
                    # (they send Klang's title to SEK6 and miss Signature,
                    # SEK 15 and Kl Sg Besi entirely).
                    outlet_code = cashier_names.outlet_for_chat(message.chat_id)
                    if outlet_code:
                        mgr = {"chat_id": message.chat_id, "manager_name": None}
                    else:
                        outlet_code = outlet_from_chat_title(chat_title)
                        mgr = None
                    if outlet_code:
                        if mgr is None:
                            mgr = await asyncio.to_thread(
                                manager_registration.get_manager,
                                supabase,
                                outlet_code,
                            )
                        decision = wmr.route_message(
                            wmr.delivery_enabled(),
                            outlet_display_name(outlet_code),
                            mgr.get("chat_id") if mgr else None,
                            ALERT_CHAT_ID,
                        )
                        for spike in spikes:
                            tamil = format_spike_message_tamil(spike)
                            if not tamil:
                                continue
                            tamil = human_touch.personalise(
                                supervisor.with_reply_footer(tamil),
                                mgr.get("manager_name") if mgr else None,
                                decision.target_chat_id,
                            )
                            try:
                                await human_touch.show_typing(
                                    context.bot, decision.target_chat_id
                                )
                                sent = await context.bot.send_message(
                                    chat_id=decision.target_chat_id,
                                    text=decision.prefix + tamil,
                                )
                                # Track the question only when it reached the
                                # real manager — a [TEST] preview to the owner
                                # is not a question anyone owes an answer to.
                                if decision.reason == "manager":
                                    await asyncio.to_thread(
                                        supervisor.log_question,
                                        supabase,
                                        decision.target_chat_id,
                                        sent.message_id,
                                        "price_spike",
                                        tamil,
                                        receipt_id,
                                    )
                            except Exception:
                                logger.exception(
                                    "Failed to send Tamil spike alert "
                                    "(outlet=%s, route=%s)",
                                    outlet_code,
                                    decision.reason,
                                )
                except Exception:
                    logger.exception(
                        "Tamil manager spike alert failed (non-critical)"
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
            if anomaly.get("is_anomaly") and ops_group:
                ops_candidates.setdefault("bigbuy", {})
                logger.info("Anomaly (staff question): outlet=%s category=%s amount=%s",
                            outlet_code, canonical_category, total_for_anomaly)
            elif anomaly.get("is_anomaly"):
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

    details: dict = {}
    try:
        findings = await asyncio.to_thread(run_audit_checks, stored, parsed, details)
    except Exception:
        logger.exception("Audit checks failed")
        findings = []

    if not ops_group:
        if findings:
            await ask_audit_questions(context, stored, findings)
        return

    # Live group: the audit findings, the price rise and the anomaly feed the
    # bill's ONE staff question (the most useful one, within the daily limit).
    for question_type, _text in findings:
        if question_type == "duplicate_receipt":
            ops_candidates["dupbill"] = {}
        elif question_type == "new_supplier":
            ops_candidates["newsupplier"] = {}
        elif question_type == "big_purchase":
            ops_candidates.setdefault("bigbuy", {})
        elif question_type == "suspicious_item" and "pricerise" not in ops_candidates:
            name = details.get("suspicious_item")
            if name:
                ops_candidates["pricerise"] = {"item": "", "label": staff_ops.pos_item_label(name)}
    if not staff_ops.is_minimarket(stored.get("merchant")) and not pinpoint_sent:
        await staff_ops_on_upload(context.application, stored, message, supplier=True,
                                  candidates=ops_candidates)


# === Pinpoint Target: outside purchases + cashier strikes (outside_purchase) ===

_OUTSIDE_RECEIPT_TYPES = (ReceiptType.SUPPLIER_PURCHASE, ReceiptType.UNKNOWN)


def _outside_review_keyboard(purchase_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Beli Luar ✅", callback_data=f"ob:{purchase_id}:yes"),
        InlineKeyboardButton("Supplier Kita ❌", callback_data=f"ob:{purchase_id}:no"),
    ]])


async def _outside_send_strike(bot, result: dict, *, chat_id, reply_to_message_id=None) -> None:
    """The strike message to the cashier — a reply under the receipt in the
    outlet group (BM + Tamil), or a DM when SCOLD_CHANNEL=dm and the cashier
    has linked their account (falls back to the group when the DM fails) —
    plus the full report to the director chat from the scold threshold on."""
    row = result["row"]
    strike_no = result.get("strike_no")
    history = result.get("history") or []
    contact = result.get("attribution") or {}
    # Message counting starts from the switch to live: the cashier is told
    # the live strike number, management the full one.
    shown_no = result.get("live_strike_no") if "live_strike_no" in result else strike_no
    shown_history = outside_purchase.live_rows(history) if "live_strike_no" in result else history
    sent_dm = False
    if not outside_purchase.is_live():
        # Shadow: nothing reaches the cashier; the director still gets the
        # report when the (full) strike count crosses the threshold.
        if strike_no and strike_no >= outside_purchase.scold_threshold():
            try:
                report = "[SHADOW — not sent to the cashier]\n" + outside_purchase.management_report(
                    row, strike_no, history)
                for chunk in chunk_message(report):
                    await bot.send_message(chat_id=ALERT_CHAT_ID, text=chunk)
            except Exception:
                logger.exception("outside purchase: shadow report failed (purchase %s)", row.get("id"))
        return False
    if outside_purchase.scold_channel() == "dm" and contact.get("telegram_user_id"):
        try:
            await bot.send_message(
                chat_id=contact["telegram_user_id"],
                text=outside_purchase.group_message(
                    row, shown_no, shown_history, contact.get("language") or "bm"),
            )
            sent_dm = True
        except Exception:
            # The cashier has not started the bot: the group gets it instead.
            logger.info("outside purchase: DM to cashier failed, replying in the group",
                        exc_info=True)
    sent_group = False
    if not sent_dm and chat_id is not None:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=outside_purchase.group_message(row, shown_no, shown_history, "bm_tamil"),
                reply_to_message_id=reply_to_message_id, allow_sending_without_reply=True,
            )
            sent_group = True
        except Exception:
            logger.exception("outside purchase: group message failed (purchase %s)", row.get("id"))
    if (sent_dm or sent_group) and row.get("id") is not None:
        await asyncio.to_thread(outside_purchase.mark_notified, supabase, row["id"])
    if strike_no and strike_no >= outside_purchase.scold_threshold():
        try:
            report = outside_purchase.management_report(row, strike_no, history)
            for chunk in chunk_message(report):
                await bot.send_message(chat_id=ALERT_CHAT_ID, text=chunk)
        except Exception:
            logger.exception("outside purchase: management report failed (purchase %s)", row.get("id"))
    return sent_dm or sent_group


async def outside_purchase_on_upload(context, stored: dict, message) -> tuple[bool, str]:
    """After a bill is saved: if the shop is not an approved supplier, record
    the outside purchase, pinpoint the cashier on shift, count the strike and
    reply under the receipt. A grey-zone merchant (fuzzy, low-confidence OCR,
    unreadable) goes to the director chat with [Beli Luar ✅] [Supplier Kita ❌]
    instead — never an automatic strike. Returns ``(sent to the cashier,
    outcome)`` with outcome skip / pending / count / allowed_only / error.
    Never breaks the receipt pipeline."""
    try:
        if stored.get("id") is None:
            return False, "skip"
        group_code = cashier_names.outlet_for_chat(message.chat_id)
        result = await asyncio.to_thread(
            outside_purchase.process_receipt, supabase, stored, group_code=group_code)
        if not result:
            return False, "skip"
        row = result["row"]
        if result["action"] == "pending":
            await context.bot.send_message(
                chat_id=ALERT_CHAT_ID,
                text=outside_purchase.pending_alert(row, result.get("match")),
                reply_markup=_outside_review_keyboard(row.get("id")),
            )
            logger.info("outside purchase: #%s held for review (%s)", row.get("id"),
                        (result.get("match") or {}).get("tier"))
            return False, "pending"
        if result["action"] != "count":
            logger.info("outside purchase: #%s recorded without a strike (%s)",
                        row.get("id"), result["action"])
            return False, result["action"]
        logger.info("outside purchase: #%s counted — strike %s for %s at %s (%s)", row.get("id"),
                    result.get("strike_no"), row.get("cashier_name"), row.get("outlet"),
                    outside_purchase.mode())
        sent = await _outside_send_strike(context.bot, result, chat_id=message.chat_id,
                                          reply_to_message_id=message.message_id)
        return sent, "count"
    except Exception:
        logger.exception("outside purchase: upload hook failed (receipt %s)", stored.get("id"))
        return False, "error"


# --- overbuy from known suppliers (overbuy_check) --------------------------------

def _overbuy_reason_markup(flag_id, language) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)]
                                 for label, data in overbuy_check.reason_buttons(flag_id, language)])


def _overbuy_decision_markup(flag_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)
                                  for label, data in overbuy_check.decision_buttons(flag_id)]])


async def _overbuy_alert_management(bot, flag: dict, *, shadow: bool) -> None:
    """The director's copy (with the sales numbers) — buttons only when the
    cashier was actually asked."""
    try:
        sent = await bot.send_message(
            chat_id=ALERT_CHAT_ID, text=overbuy_check.management_alert(flag, shadow=shadow),
            reply_markup=None if shadow else _overbuy_decision_markup(flag.get("id")))
        if flag.get("id") is not None:
            await asyncio.to_thread(overbuy_check.set_fields, supabase, flag["id"],
                                    alert_message_id=sent.message_id)
            flag["alert_message_id"] = sent.message_id
    except Exception:
        logger.exception("overbuy: management alert failed (flag %s)", flag.get("id"))


async def _overbuy_refresh_alert(bot, flag: dict, *, keep_buttons: bool = True) -> None:
    """Rewrite the director's alert with the cashier's reason / the outcome."""
    if not flag.get("alert_message_id"):
        return
    with contextlib.suppress(Exception):
        await bot.edit_message_text(
            chat_id=ALERT_CHAT_ID, message_id=flag["alert_message_id"],
            text=overbuy_check.management_alert(flag),
            reply_markup=_overbuy_decision_markup(flag["id"]) if keep_buttons else None)


async def overbuy_on_upload(context, stored: dict, message) -> bool:
    """A known-supplier bill: much more of an item than the outlet's usual
    rate while yesterday's sales were not higher -> ask the cashier why (no
    sales figure in the question), tell the director with the numbers. Shadow
    mode records the flag and tells the director only. Returns True when a
    question reached the group. Never breaks the receipt pipeline."""
    try:
        if stored.get("id") is None or outside_purchase.is_own_outlet(stored.get("merchant")):
            return False
        group_code = cashier_names.outlet_for_chat(message.chat_id)
        roster = await asyncio.to_thread(outside_purchase.load_roster, supabase)
        result = await asyncio.to_thread(
            overbuy_check.process_bill, supabase, stored, group_code=group_code, roster=roster)
        sent = False
        for flag in result.get("flags") or []:
            live = flag.get("status") == overbuy_check.PENDING
            if live:
                try:
                    q = await context.bot.send_message(
                        chat_id=message.chat_id,
                        text=overbuy_check.cashier_question(flag, "bm_tamil"),
                        reply_markup=_overbuy_reason_markup(flag.get("id"), "bm_tamil"),
                        reply_to_message_id=message.message_id, allow_sending_without_reply=True)
                    sent = True
                    await asyncio.to_thread(overbuy_check.set_fields, supabase, flag["id"],
                                            question_message_id=q.message_id)
                except Exception:
                    logger.exception("overbuy: question failed (flag %s)", flag.get("id"))
            logger.info("overbuy: flag #%s %s %s %s (%s)", flag.get("id"), flag.get("outlet"),
                        flag.get("item"), flag.get("qty"), outside_purchase.mode())
            await _overbuy_alert_management(context.bot, flag, shadow=not live)
        return sent
    except Exception:
        logger.exception("overbuy: upload hook failed (receipt %s)", stored.get("id"))
        return False


async def _overbuy_send_strike(bot, outcome: dict) -> None:
    """After a rejection or a no-reply: the tiered reply under the bill (live
    mode only, live rows counted) and the director's report at the threshold."""
    row, strike_no, history = outcome["row"], outcome.get("strike_no"), outcome.get("history") or []
    if not strike_no:
        return
    if outside_purchase.is_live() and row.get("chat_id"):
        shown_history = outside_purchase.live_rows(history)
        shown_no = len(shown_history) or None
        if shown_no:
            try:
                await bot.send_message(
                    chat_id=row["chat_id"],
                    text=overbuy_check.strike_message(row, shown_no, shown_history, "bm_tamil"),
                    reply_to_message_id=row.get("receipt_message_id"), allow_sending_without_reply=True)
            except Exception:
                logger.exception("overbuy: strike message failed (flag %s)", row.get("id"))
    if strike_no >= outside_purchase.scold_threshold():
        prefix = "" if outside_purchase.is_live() else "[SHADOW — not sent to the cashier]\n"
        with contextlib.suppress(Exception):
            for chunk in chunk_message(prefix + overbuy_check.management_strike_report(row, strike_no, history)):
                await bot.send_message(chat_id=ALERT_CHAT_ID, text=chunk)


async def handle_overbuy_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The cashier's button under the overbuy question (ov:<id>:<code>)."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    m = re.match(r"^ov:(\d+):(stock|order|supplier|other)$", query.data or "")
    if not m:
        return
    flag_id, code = int(m.group(1)), m.group(2)
    flag = await asyncio.to_thread(overbuy_check._get, supabase, flag_id)
    if not flag or flag.get("status") not in (overbuy_check.PENDING, overbuy_check.ANSWERED):
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)
        return
    language = "bm_tamil"
    if code == "other":
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)
        prompt = await _callback_prompt(query, context, overbuy_check.other_prompt(language))
        await asyncio.to_thread(overbuy_check.set_fields, supabase, flag_id, reason_code="other",
                                prompt_message_id=prompt.message_id if prompt else None)
        return
    row = await asyncio.to_thread(overbuy_check.answer, supabase, flag_id, code)
    if not row:
        return
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    await _callback_prompt(query, context, overbuy_check.thanks_text(
        overbuy_check.reason_label(code, "bm"), language))
    await _overbuy_refresh_alert(context.bot, row)


async def _callback_prompt(query, context, text: str):
    """Reply in a callback's chat and hand back the sent message (or None)."""
    message = query.message
    try:
        if message is not None and hasattr(message, "reply_text"):
            return await message.reply_text(text)
        chat = getattr(message, "chat", None)
        chat_id = chat.id if chat is not None else (query.from_user.id if query.from_user else None)
        if chat_id is not None:
            return await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("overbuy: prompt failed")
    return None


async def handle_overbuy_reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A typed reason: the reply to the bot's "type your reason" prompt."""
    message = update.effective_message
    if not message or not message.reply_to_message or not message.text:
        return
    flag = await asyncio.to_thread(overbuy_check.flag_by_prompt, supabase, message.chat_id,
                                   message.reply_to_message.message_id)
    if not flag:
        return
    row = await asyncio.to_thread(overbuy_check.answer, supabase, flag["id"], "other", message.text)
    if not row:
        return
    with contextlib.suppress(Exception):
        await message.reply_text(overbuy_check.thanks_text(row.get("reason") or "", "bm_tamil"))
    await _overbuy_refresh_alert(context.bot, row)
    logger.info("overbuy: typed reason for flag #%s", flag["id"])


async def handle_overbuy_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[Terima] / [Tolak] on the director's overbuy alert (ovm:<id>:accept|reject)."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    reviewer = query.from_user.id if query.from_user else None
    chat = getattr(query.message, "chat", None)
    if not outside_purchase.admin_allowed(chat.id if chat is not None else None, reviewer,
                                          ALERT_CHAT_ID, is_reviewer):
        return
    m = re.match(r"^ovm:(\d+):(accept|reject)$", query.data or "")
    if not m:
        return
    flag_id, accept = int(m.group(1)), m.group(2) == "accept"
    outcome = await asyncio.to_thread(overbuy_check.decide, supabase, flag_id, accept, reviewer)
    if not outcome:
        await _callback_reply(query, context, f"Overbuy #{flag_id} sudah diputuskan.")
        return
    row = outcome["row"]
    row["alert_message_id"] = row.get("alert_message_id") or (query.message.message_id if query.message else None)
    await _overbuy_refresh_alert(context.bot, row, keep_buttons=False)
    if accept:
        await _callback_reply(query, context, f"✔ Overbuy #{flag_id} diterima — tiada strike.")
        return
    await _overbuy_send_strike(context.bot, outcome)
    await _callback_reply(query, context,
                          f"✖ Overbuy #{flag_id} ditolak — overbuy strike {outcome.get('strike_no') or '—'} "
                          f"untuk {row.get('cashier') or '?'} ({row.get('outlet') or '?'}).")


async def overbuy_no_reply_tick(application: Application) -> None:
    """Every 30 min: overbuy questions unanswered for 12h -> no_reply (a
    strike) + a line to the director."""
    try:
        expired = await asyncio.to_thread(overbuy_check.expire_no_reply, supabase)
    except Exception:
        logger.exception("overbuy: no-reply tick failed")
        return
    for outcome in expired:
        row = outcome["row"]
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=overbuy_check.no_reply_alert(row))
        await _overbuy_refresh_alert(application.bot, row, keep_buttons=False)
        await _overbuy_send_strike(application.bot, outcome)


async def refresh_known_merchants_job(application: Application) -> None:
    """Nightly 03:30 MY: recount the known merchants per outlet (streamed)."""
    try:
        suppliers = await asyncio.to_thread(
            lambda: supabase.table(outside_purchase.SUPPLIERS_TABLE).select("*").execute().data or [])
        summary = await asyncio.to_thread(
            known_merchants.refresh, supabase, suppliers, group_codes=cashier_names.group_chats())
        logger.info("known merchants: refresh %s", summary)
        if summary.get("seeded"):
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=f"🏪 Known-merchant baseline seeded: {summary['seeded']} rows. "
                         "Review with /merchant_known <outlet>; remove mini markets with /buang_merchant.")
    except Exception:
        logger.exception("known merchants: nightly refresh failed")


def _shadow_rows(day):
    outside = outside_purchase.fetch_purchases(supabase, since=day - timedelta(days=1), until=day)
    outside = [r for r in outside if r.get("mode") == outside_purchase.SHADOW
               or str(r.get("created_at") or "")[:10] == day.isoformat()]
    overbuy = overbuy_check.fetch_flags(supabase, since=day - timedelta(days=1), until=day)
    overbuy = [r for r in overbuy if r.get("status") == overbuy_check.SHADOW]
    return outside, overbuy


async def post_shadow_summary(application: Application, *, notify_chat_id=None, force=False) -> None:
    """Daily 21:45 MY while in shadow mode (and /pinpoint_shadow): what would
    have been sent to the cashiers today, to the director chat."""
    if not force and outside_purchase.is_live():
        return
    today = _my_today()
    try:
        outside, overbuy = await asyncio.to_thread(_shadow_rows, today)
        text = outside_purchase.shadow_summary(outside, overbuy, today)
    except Exception:
        logger.exception("pinpoint shadow summary failed")
        text = "⚠️ Pinpoint shadow summary failed — see logs."
    for chat in {ALERT_CHAT_ID, notify_chat_id} - {None}:
        await _send_chunked_to(application, chat, text)


async def pinpoint_shadow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /pinpoint_shadow — today's would-have-been-sent summary."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    await post_shadow_summary(context.application, notify_chat_id=message.chat_id
                              if message.chat_id != ALERT_CHAT_ID else None, force=True)


async def lebih_beli_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /lebih_beli [outlet] [days] — overbuy flags per cashier and item."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    args = list(context.args or [])
    days = outside_purchase.strike_window_days()
    if args and args[-1].isdigit():
        days = max(1, min(int(args.pop()), 365))
    outlet = " ".join(args).strip() or None
    if outlet and not outlet_resolver.canonical_outlet(outlet):
        await message.reply_text(f"Outlet {outlet} tak dikenali. Contoh: /lebih_beli SEK20 30")
        return
    today = _my_today()
    rows = await asyncio.to_thread(overbuy_check.fetch_flags, supabase, outlet=outlet,
                                   since=today - timedelta(days=days), until=today)
    await _reply_chunked(message, overbuy_check.format_summary(rows, outlet, days))


async def merchant_known_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /merchant_known <outlet> — the shops known for that outlet;
    /merchant_known all — the full per-outlet report."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    arg = " ".join(context.args or []).strip()
    if not arg:
        await message.reply_text("Usage: /merchant_known <outlet>  atau  /merchant_known all")
        return
    if arg.lower() == "all":
        rows = await asyncio.to_thread(known_merchants.load, supabase)
        await _reply_chunked(message, known_merchants.format_report(rows))
        return
    if not outlet_resolver.canonical_outlet(arg):
        await message.reply_text(f"Outlet {arg} tak dikenali.")
        return
    rows = await asyncio.to_thread(known_merchants.load, supabase, arg)
    await _reply_chunked(message, known_merchants.format_known(rows, arg))


async def buang_merchant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /buang_merchant <outlet> <name> — this shop is NOT a regular
    supplier of that outlet; its bills become pin targets again."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    args = list(context.args or [])
    if len(args) < 2:
        await message.reply_text("Usage: /buang_merchant <outlet> <nama>  e.g. /buang_merchant SEK6 PASAR MINI A M")
        return
    outlet, name = args[0], " ".join(args[1:]).strip()
    if not outlet_resolver.canonical_outlet(outlet):
        await message.reply_text(f"Outlet {outlet} tak dikenali.")
        return
    row = await asyncio.to_thread(known_merchants.remove, supabase, outlet, name, _command_owner_id(update))
    if not row:
        await message.reply_text(f"{name} tidak ada dalam senarai dikenali untuk {outlet}.")
        return
    await message.reply_text(f"✖ {row.get('canonical_merchant')} dibuang dari senarai dikenali "
                             f"{row.get('outlet')}. Bil dari kedai ini akan ditanda beli luar.")


def _outside_admin(update: Update) -> bool:
    """Admin commands answer in the director chat or to a reviewer anywhere;
    a cashier typing one in an outlet group gets nothing."""
    message = update.effective_message
    return message is not None and outside_purchase.admin_allowed(
        message.chat_id, _command_owner_id(update), ALERT_CHAT_ID, is_reviewer)


def _receipt_chat_ref(receipt_id):
    """``(chat_id, message_id)`` of a stored receipt, for replying under it later."""
    if receipt_id is None:
        return None, None
    rows = (supabase.table(RECEIPTS_TABLE).select("chat_id, message_id")
            .eq("id", receipt_id).execute().data or [])
    if not rows:
        return None, None
    return rows[0].get("chat_id"), rows[0].get("message_id")


async def handle_outside_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[Beli Luar ✅] / [Supplier Kita ❌] on a held outside purchase."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    reviewer = query.from_user.id if query.from_user else None
    chat = getattr(query.message, "chat", None)
    if not is_reviewer(reviewer) and not (chat is not None and chat.id == ALERT_CHAT_ID):
        return
    m = re.match(r"^ob:(\d+):(yes|no)$", query.data or "")
    if not m:
        return
    purchase_id, choice = int(m.group(1)), m.group(2)
    if choice == "no":
        row = await asyncio.to_thread(
            outside_purchase.mark_false_positive, supabase, purchase_id, reviewer)
        if row is None:
            text = f"#{purchase_id} sudah diselesaikan."
        else:
            text = (f"✖ #{purchase_id} ditanda bukan beli luar: {row.get('merchant_raw') or '?'}.\n"
                    f"Supaya tak ditanya lagi: /tambah_supplier {row.get('merchant_raw') or '<nama>'}")
    else:
        result = await asyncio.to_thread(
            outside_purchase.confirm_outside, supabase, purchase_id, reviewer)
        if result is None:
            text = f"#{purchase_id} sudah diselesaikan."
        else:
            row = result["row"]
            roster = await asyncio.to_thread(outside_purchase.load_roster, supabase)
            result["attribution"] = outside_purchase.cashier_contact(
                roster, row.get("outlet"), row.get("cashier_name"))
            chat_id, message_id = await asyncio.to_thread(_receipt_chat_ref, row.get("receipt_id"))
            await _outside_send_strike(context.bot, result, chat_id=chat_id,
                                       reply_to_message_id=message_id)
            who = row.get("cashier_name") or "cashier tak dikenal pasti"
            text = (f"✅ #{purchase_id} dikira beli luar — strike {result.get('strike_no') or '—'} "
                    f"untuk {who} ({row.get('outlet') or '?'}).")
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    await _callback_reply(query, context, text)


async def beli_luar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /beli_luar [outlet] [days] — outside purchases per cashier."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    args = list(context.args or [])
    days = outside_purchase.strike_window_days()
    if args and args[-1].isdigit():
        days = max(1, min(int(args.pop()), 365))
    outlet = " ".join(args).strip() or None
    if outlet and not outlet_resolver.canonical_outlet(outlet):
        await message.reply_text(
            f"Outlet {outlet} tak dikenali. Contoh: /beli_luar SEK20 30, /beli_luar Vista, /beli_luar 60")
        return
    today = _my_today()
    rows = await asyncio.to_thread(
        outside_purchase.fetch_purchases, supabase,
        outlet=outlet, since=today - timedelta(days=days), until=today)
    await _reply_chunked(message, outside_purchase.format_summary(rows, outlet, days))


async def beli_luar_cashier_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /beli_luar_cashier <name> — one cashier's full history."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    name = " ".join(context.args or []).strip()
    if not name:
        await message.reply_text("Usage: /beli_luar_cashier <nama>  e.g. /beli_luar_cashier Rahim")
        return
    today = _my_today()
    rows = await asyncio.to_thread(
        outside_purchase.fetch_purchases, supabase, since=today - timedelta(days=365))
    await _reply_chunked(message, outside_purchase.format_cashier_history(rows, name))


async def izin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /izin <purchase_id> <reason> — an approved emergency, strike removed."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    args = list(context.args or [])
    if not args or not args[0].lstrip("#").isdigit() or len(args) < 2:
        await message.reply_text("Usage: /izin <purchase_id> <sebab>  e.g. /izin 12 gas habis, saya benarkan")
        return
    purchase_id = int(args[0].lstrip("#"))
    row = await asyncio.to_thread(
        outside_purchase.excuse, supabase, purchase_id, " ".join(args[1:]), _command_owner_id(update))
    if row is None:
        await message.reply_text(f"#{purchase_id} tak dijumpai atau sudah diizinkan.")
        return
    await message.reply_text(
        f"🆗 #{purchase_id} diizinkan — {row.get('cashier_name') or '?'} ({row.get('outlet') or '?'}), "
        f"{row.get('merchant_raw') or '?'}. Strike dibuang. Sebab: {row.get('excused_reason')}")


async def bukan_beli_luar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /bukan_beli_luar <purchase_id> — false positive, this is a supplier."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    args = list(context.args or [])
    if not args or not args[0].lstrip("#").isdigit():
        await message.reply_text("Usage: /bukan_beli_luar <purchase_id>")
        return
    purchase_id = int(args[0].lstrip("#"))
    row = await asyncio.to_thread(
        outside_purchase.mark_false_positive, supabase, purchase_id, _command_owner_id(update))
    if row is None:
        await message.reply_text(f"#{purchase_id} tak dijumpai atau sudah ditanda.")
        return
    await message.reply_text(
        f"✖ #{purchase_id} ditanda bukan beli luar ({row.get('merchant_raw') or '?'}). "
        f"Supaya tak ditanya lagi: /tambah_supplier {row.get('merchant_raw') or '<nama>'}")


async def tambah_supplier_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /tambah_supplier <name> — approve a supplier; a name that already
    matches one of ours is saved as its alias. ``/tambah_supplier <alias> = <supplier>``
    pins the alias to a specific supplier."""
    message = update.effective_message
    if not message or not _outside_admin(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await message.reply_text(
            "Usage: /tambah_supplier <nama>  atau  /tambah_supplier <alias> = <supplier>\n"
            "e.g. /tambah_supplier PASARAYA BORONG SNS ALI\n"
            "     /tambah_supplier BESTARI FARM SDN BHD = BESTARI FARM (M) SDN BHD")
        return
    name, alias_of = text, None
    if "=" in text:
        name, alias_of = (part.strip() for part in text.split("=", 1))
    result = await asyncio.to_thread(
        outside_purchase.add_supplier, supabase, name, alias_of, _command_owner_id(update))
    if not result.get("ok"):
        await message.reply_text(f"⚠️ {result.get('error')}")
        return
    kind = result.get("kind")
    if kind == "new":
        await message.reply_text(f"✅ Supplier baru diluluskan: {result['supplier']}")
    elif kind == "alias":
        await message.reply_text(f"✅ Alias disimpan: {name.upper()} → {result['supplier']}")
    else:
        await message.reply_text(f"ℹ️ {result['supplier']} sudah ada dalam senarai supplier.")


def _daftar_markup(buttons) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)]
                                 for label, data in buttons])


_DAFTAR_TEXT = {
    "outlet": "Pilih outlet anda.\nஉங்க outlet-ஐ தேர்ந்தெடுங்க.",
    "shift": "Pilih shift anda.\nஉங்க shift-ஐ தேர்ந்தெடுங்க.",
    "name": "Pilih nama anda.\nஉங்க பெயரை தேர்ந்தெடுங்க.",
    "empty": "Tiada nama cashier untuk outlet/shift ini — minta pengurusan tambah dalam cashier_roster.\n"
             "இந்த outlet/shift-க்கு cashier பெயர் இல்ல — management-கிட்ட சொல்லுங்க.",
}


async def daftar_cashier_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any cashier, in their outlet group: /daftar_cashier — link your Telegram
    account to your roster row (outlet + shift + name via buttons)."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    roster = await asyncio.to_thread(outside_purchase.load_roster, supabase)
    outlets = outside_purchase.roster_outlets(roster)
    if not outlets:
        await message.reply_text(_DAFTAR_TEXT["empty"])
        return
    code = cashier_names.outlet_for_chat(message.chat_id)
    known = outlet_resolver.canonical_outlet(code) if code else None
    if known in outlets:
        await message.reply_text(
            f"{known}\n" + _DAFTAR_TEXT["shift"],
            reply_markup=_daftar_markup(
                outside_purchase.register_shift_buttons(user.id, outlets.index(known))))
        return
    await message.reply_text(
        _DAFTAR_TEXT["outlet"],
        reply_markup=_daftar_markup(outside_purchase.register_outlet_buttons(roster, user.id)))


async def handle_daftar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The /daftar_cashier buttons: outlet -> shift -> name -> linked."""
    query = update.callback_query
    if not query:
        return
    parsed = outside_purchase.parse_register_callback(query.data)
    if not parsed:
        await query.answer()
        return
    tapper = query.from_user.id if query.from_user else None
    if tapper != parsed["user_id"]:
        await query.answer("Bukan untuk anda — taip /daftar_cashier sendiri.", show_alert=True)
        return
    await query.answer()
    roster = await asyncio.to_thread(outside_purchase.load_roster, supabase)
    outlets = outside_purchase.roster_outlets(roster)
    try:
        if parsed["step"] == "outlet":
            idx = parsed["outlet_idx"]
            if not 0 <= idx < len(outlets):
                return
            await query.edit_message_text(
                f"{outlets[idx]}\n" + _DAFTAR_TEXT["shift"],
                reply_markup=_daftar_markup(outside_purchase.register_shift_buttons(tapper, idx)))
        elif parsed["step"] == "shift":
            idx = parsed["outlet_idx"]
            buttons = outside_purchase.register_name_buttons(roster, tapper, idx, parsed["shift"])
            if not buttons:
                await query.edit_message_text(_DAFTAR_TEXT["empty"])
                return
            label = outlets[idx] if 0 <= idx < len(outlets) else "?"
            await query.edit_message_text(
                f"{label} · {parsed['shift']}\n" + _DAFTAR_TEXT["name"],
                reply_markup=_daftar_markup(buttons))
        else:
            row = await asyncio.to_thread(
                outside_purchase.link_cashier, supabase, parsed["roster_id"], tapper)
            if row is None:
                await query.edit_message_text("Rekod tak dijumpai. Cuba /daftar_cashier semula.")
                return
            logger.info("outside purchase: cashier %s (%s %s) linked to telegram %s",
                        row.get("cashier_name"), row.get("outlet"), row.get("shift"), tapper)
            await query.edit_message_text(
                f"✅ {row.get('cashier_name')} ({row.get('outlet')}, {row.get('shift')}) "
                "dipautkan dengan akaun Telegram anda.\n"
                f"✅ {row.get('cashier_name')} ({row.get('outlet')}, {row.get('shift')}) "
                "உங்க Telegram account-ஓட link ஆயிடுச்சு.")
    except Exception:
        logger.exception("outside purchase: /daftar_cashier step failed")


async def _with_outside_section(text: str, year: int, month: int) -> str:
    """Append the month's "Beli Luar" block to the monthly close report."""
    try:
        first, last = monthly_consumption.month_bounds(year, month)
        rows = await asyncio.to_thread(
            outside_purchase.fetch_purchases, supabase,
            since=date.fromisoformat(first), until=date.fromisoformat(last))
        flags = await asyncio.to_thread(
            overbuy_check.fetch_flags, supabase,
            since=date.fromisoformat(first), until=date.fromisoformat(last))
        return (text + "\n\n" + outside_purchase.monthly_section(rows, year, month)
                + "\n\n" + overbuy_check.monthly_section(flags, year, month))
    except Exception:
        logger.exception("outside purchase: monthly section failed (%s-%s)", year, month)
        return text


def _manager_name_for_chat(chat_id):
    """The registered manager name behind a chat, for the personal ack.
    ``None`` (-> generic 'boss') when the chat isn't a manager DM."""
    try:
        for row in manager_registration.get_all_managers(supabase).values():
            if row.get("chat_id") == chat_id:
                return row.get("manager_name")
    except Exception:
        logger.debug("manager name lookup failed", exc_info=True)
    return None


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
        # Close the loop like a human: thank them BY NAME in their language
        # (wording rotates daily so it never reads templated), and report
        # the answer upward so the owner hears it without asking — which is
        # exactly what the ack, truthfully, says will happen.
        try:
            name = await asyncio.to_thread(
                _manager_name_for_chat, message.chat_id
            )
            if cashier_names.is_outlet_group(message.chat_id):
                # Outlet group: a short thanks in the cashier's language.
                await message.reply_text(staff_live.thanks_text(
                    cashier_names.language_for_chat(message.chat_id)))
            else:
                await message.reply_text(human_touch.ack(name, message.chat_id))
        except Exception:
            logger.exception("Failed to ack audit reply")
        note = supervisor.format_owner_reply_note(saved, message.text)
        if note and message.chat_id != ALERT_CHAT_ID:
            with contextlib.suppress(Exception):
                await context.bot.send_message(chat_id=ALERT_CHAT_ID, text=note)


HELP_TEXT = (
    "Send a receipt photo and I'll OCR it, store it, and reply with the "
    "details.\n\n"
    "Commands:\n"
    "/start — short greeting\n"
    "/summary — today's spending grouped by merchant\n"
    "/compare <item> — compare an item's unit price across outlets\n"
    "/shop_prices <item> — every shop we buy that item from: names, dates, "
    "prices, quantities and the outlet\n"
    "   (aliases: /all_prices, /harga — works for any item, e.g. ayam, telur, minyak)\n"
    "   name an outlet and/or a cut to narrow it:\n"
    "   \u2022 /shop_prices bistro ayam whole leg — Bistro's whole leg only\n"
    "   \u2022 /shop_prices sek 6 ayam — everything SEK-6 bought\n"
    "   add 'cuts' for the per-cut price comparison, 'debug' for what was "
    "filtered out\n"
    "/ask <question> — ask about any item in plain words, English or Malay\n"
    "   (aliases: /tanya, /cari, /search). Examples:\n"
    "   \u2022 beras beli kat mana — which shops sell it, cheapest first\n"
    "   \u2022 bila last beli ayam — the last purchases, shop and price\n"
    "   \u2022 berapa belanja telur bulan ni — spend and quantity for a period\n"
    "   \u2022 which branch pays most for gula — branch-by-branch comparison\n"
    "   In the alert group and owner DMs you can drop the /ask and just type "
    "the question.\n"
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
    "/monthly_kg [YYYY-MM | last] — kg ayam/daging/kambing dll dibeli bulan itu\n"
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
    "/order_drafts_now — preview tomorrow's per-outlet order drafts\n"
    "\n"
    "Missing bills:\n"
    "/missing_bills_now — check which regular suppliers' bills stopped "
    "being uploaded\n"
    "\n"
    "Bill analysis (nightly 21:30):\n"
    "/bill_analysis_now — analyse today's bills: price increases vs each "
    "shop's previous price + what every branch pays for every item\n"
    "/outlet_prices [item] — what each branch last paid, cheapest first "
    "(alias: /branch_prices; no item = every item)\n"
    "\n"
    "Overbuying:\n"
    "/overbuy_now — check which outlets kept ordering the same despite "
    "falling sales\n"
    "/key_stock_now — check yesterday's key-stock buying vs the full 24h "
    "day's sales\n"
    "/slow_items_now — which items sold under each shop's usual yesterday "
    "(full 24h day)\n"
    "/activate_outlet <code> <name> — activate a newly-detected POS outlet "
    "and pull in its held emails\n"
    "\n"
    "Cook to demand:\n"
    "/cook_plan_now — today's per-outlet cook quantities forecast from the "
    "shop's own sales history\n"
    "/forecast_accuracy [days] — how close those forecasts have been\n"
    "\n"
    "Questions:\n"
    "/questions_now — which manager questions are still unanswered\n"
    "/cashier — cashier on shift per outlet group; /cashier SEK20 night Ismath to change\n"
    "/ping_managers — test message to every outlet group (director only)\n"
    "/lang SEK20 morning tamil — language a cashier reads\n"
    "/staff_preview <check-in> — preview the natural check-in now\n"
    "/staff_samples [n] [check-in] — Tamil samples with back-translations (with a check-in: Tamil + BM, pass rate)\n"
    "/form_chase_now — remind every group whose kitchen form is still "
    "not keyed in\n"
    "/scoreboard_now — 7-day question response scoreboard per chat\n"
    "\n"
    "Data quality:\n"
    "/price_quarantine [n] — latest garbage price rows the sanity gate "
    "kept out of item_prices, with reject reasons\n"
    "\n"
    "Beli luar (outside purchases + cashier strikes):\n"
    "/beli_luar [outlet] [days] — per cashier: count, RM, extra cost vs "
    "supplier, top items (default 30 days)\n"
    "/beli_luar_cashier <name> — one cashier's full history with dates and items\n"
    "/izin <id> <reason> — excuse an approved emergency buy (strike removed)\n"
    "/bukan_beli_luar <id> — mark a false positive (it was a supplier)\n"
    "/tambah_supplier <name> [= <supplier>] — approve a supplier or add an alias\n"
    "/daftar_cashier — (cashier, in the outlet group) link your Telegram "
    "account to your shift\n"
    "/lebih_beli [outlet] [days] — overbuy flags: cashier, item, times, extra qty/RM, reasons\n"
    "/merchant_known <outlet> | all — shops known for an outlet (bills from unknown shops are pinned)\n"
    "/buang_merchant <outlet> <name> — remove a shop from the known list\n"
    "/pinpoint_shadow — what shadow mode WOULD have sent to cashiers today "
    "(OUTSIDE_PURCHASE_MODE=shadow|live)"
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
    # The command list has outgrown one Telegram message (4096 chars).
    await _reply_chunked(update.effective_message, HELP_TEXT)


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
    if not message or not is_reviewer(_command_owner_id(update)):
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
        await _reply_chunked(message, _format_advances_by_outlet(rows))
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

    # /advances <outlet>  vs  /advances <staff_name>. Outlet codes and short
    # staff names can both be 2-5 plain letters (KLANG vs DINA), so an
    # outlet-shaped token only wins when advance rows actually carry that
    # outlet — otherwise fall through to staff history instead of wrongly
    # replying "tiada advance outstanding" for a staff member who owes.
    token = first.upper()
    if _OUTLET_TOKEN_RE.match(token):
        try:
            rows = await asyncio.to_thread(_fetch_open_advances, None, token)
        except Exception:
            logger.exception("Failed to fetch advances by outlet")
            await message.reply_text("Gagal ambil data advances.")
            return
        if rows:
            await _reply_chunked(message, _format_advances_by_outlet(rows))
            return

    # /advances <staff_name>  → full history
    try:
        rows = await asyncio.to_thread(_fetch_staff_history, first)
    except Exception:
        logger.exception("Failed to fetch staff history")
        await message.reply_text("Gagal ambil history.")
        return
    await _reply_chunked(message, _format_staff_history(first, rows))


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
    # Only a reviewer may confirm/cancel — otherwise anyone in the group
    # could reply "Y" to a reviewer's pending repayment prompt.
    if not is_reviewer(_command_owner_id(update)):
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


def _fetch_pending_audit_rows(limit: int | None) -> list:
    """Pending reparse-audit rows, oldest first. ``limit=None`` means ALL
    rows, paginated past the PostgREST per-request cap — a bare huge .limit()
    is clamped server-side to ~1000, which made /reparse_apply_all report
    "applied ALL" after applying only the first page."""
    def q():
        return (
            supabase.table(REPARSE_AUDIT_TABLE)
            .select("*")
            .eq("applied", False)
            .order("id", desc=False)
        )
    if limit is None:
        return fetch_all_pages(q)
    result = q().limit(limit).execute()
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
        await _callback_reply(query, context, "Cancelled — no changes applied.")
        return
    try:
        rows = await asyncio.to_thread(_fetch_pending_audit_rows, None)
        applied = await asyncio.to_thread(_apply_pending_audit_rows, rows, chat_id)
    except Exception:
        logger.exception("reparse_apply_all failed")
        await _callback_reply(query, context, "Failed to apply changes.")
        return
    await _callback_reply(query, context, f"✅ Applied ALL {applied} pending correction(s).")


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


def _fetch_pending_backfill_rows(limit: int | None) -> list:
    """``limit=None`` = ALL rows, paginated (see _fetch_pending_audit_rows)."""
    def q():
        return (
            supabase.table(BACKFILL_AUDIT_TABLE).select("*")
            .eq("applied", False).order("id", desc=False)
        )
    if limit is None:
        return fetch_all_pages(q)
    return q().limit(limit).execute().data or []


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
        await _callback_reply(query, context, "Cancelled — no receipts tagged.")
        return
    try:
        rows = await asyncio.to_thread(_fetch_pending_backfill_rows, None)
        applied = await asyncio.to_thread(_apply_pending_backfill_rows, rows)
    except Exception:
        logger.exception("backfill_apply_all failed")
        await _callback_reply(query, context, "Failed to apply backfill rows.")
        return
    await _callback_reply(query, context, f"✅ Tagged ALL {applied} pending receipt(s).")


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


async def price_quarantine_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/price_quarantine [n]`` — latest rows the issue-#79 sanity gate
    kept out of item_prices (OCR column merges, future dates, history
    outliers), with the reject reasons for threshold tuning."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n = _parse_reparse_n(context.args, 10, 50)
    try:
        from price_sanity import fetch_recent_quarantine, format_quarantine_rows

        rows = await asyncio.to_thread(fetch_recent_quarantine, supabase, n)
    except Exception:
        logger.exception("price_quarantine failed")
        await message.reply_text("Failed to read item_price_quarantine.")
        return
    await _reply_chunked(message, format_quarantine_rows(rows))


async def shop_prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/shop_prices <item>`` — every shop we buy ANY item from.

    Shop names, dates, prices, quantities and the outlet that bought it,
    in one message, so the director can check any item without waiting for
    it to spike.

    The default view is ITEM-LEVEL. It used to group by ``item_variant``,
    which is the raw receipt line with the pack size stripped — so every
    OCR spelling and brand prefix became its own "cut" (ayam alone made 41
    of them: "I AYAM", "1ST AYAM", "MR CM AYAM"). Capped at a few blocks,
    that split one item into one-shop blocks and hid most of the real
    suppliers behind "+38 more type(s)", then reported "only one supplier
    has priced this item". ``/shop_prices <item> cuts`` still gives the
    per-cut comparison when that is what's wanted.

    Answers in the alert group (where the alerts land) and to reviewers
    anywhere; supplier pricing isn't for arbitrary outlet groups.
    """
    message = update.effective_message
    if not message:
        return
    in_alert_chat = message.chat_id == ALERT_CHAT_ID
    if not in_alert_chat and not is_reviewer(_command_owner_id(update)):
        return

    query = " ".join(context.args or []).strip()
    if not query:
        await message.reply_text(
            "Usage: /shop_prices <item>\n"
            "Example: /shop_prices beras — every shop we buy it from, with "
            "dates, prices and the outlet.\n"
            "Add 'cuts' for the per-cut price comparison "
            "(/shop_prices ayam cuts), or 'debug' to see what was filtered out."
        )
        return

    # "cuts" (or the old "variants") asks for the per-cut comparison.
    parts = query.split()
    per_cut = len(parts) > 1 and parts[-1].lower() in ("cuts", "cut", "variants")
    if per_cut:
        query = " ".join(parts[:-1])
        builder = shop_price_comparison.build_shop_price_report
    else:
        builder = director_ask.build_item_report

    try:
        text = await asyncio.to_thread(builder, supabase, query)
    except Exception:
        logger.exception("shop_prices failed (query=%r)", query)
        await message.reply_text("Failed to read item prices.")
        return
    await _reply_chunked(message, text)


# === Plain-language item search ("ask the bot") ==============================
# Every number below is already in the database, but only reachable by
# remembering the right slash command. The question the director actually
# types is "beras beli kat mana" — so answer that. Parsing and reporting
# live in ``director_ask``; this is only the Telegram glue.

def _ask_allowed(update: Update) -> bool:
    """Same policy as /shop_prices: the alert group, or a reviewer
    anywhere. Supplier pricing isn't for arbitrary outlet groups."""
    message = update.effective_message
    if not message:
        return False
    return message.chat_id == ALERT_CHAT_ID or is_reviewer(_command_owner_id(update))


async def _send_answer(message, question: str) -> None:
    try:
        text = await asyncio.to_thread(
            director_ask.answer_question, supabase, question, _my_today()
        )
    except Exception:
        logger.exception("ask failed (question=%r)", question)
        await message.reply_text("Failed to answer that. Try /help.")
        return
    await _reply_chunked(message, text)


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/ask <question>`` — ask about any item in plain words.

    "/ask beras beli kat mana", "/ask bila last beli ayam", "/ask berapa
    belanja telur bulan ni". Aliases: /tanya, /cari, /search.
    """
    message = update.effective_message
    if not message or not _ask_allowed(update):
        return
    question = " ".join(context.args or []).strip()
    if not question:
        await message.reply_text(director_ask.HELP_TEXT)
        return
    await _send_answer(message, question)


async def handle_ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer an item question typed WITHOUT a slash command.

    The director shouldn't have to remember ``/ask`` either. The whole
    risk of a bare text handler is answering things nobody asked, so the
    bar depends on where the message landed:

    - In a reviewer's private chat every message is addressed to the bot,
      so a bare item name ("beras") is enough.
    - In the alert group the text has to BOTH resolve to a real item AND
      carry a question word or a question mark, so ordinary chatter
      ("sudah hantar", "ok") is never answered.

    Anywhere else it stays quiet. /shop_prices answers a reviewer in any
    chat, but that is a typed command; un-prompted supplier prices landing
    in an outlet group because the owner happened to mention ayam is not
    the same thing. Those chats still get an answer — through /ask.

    Anything that doesn't clear the bar is left alone silently: a bot that
    replies "I don't understand" to every group message is worse than one
    that says nothing.
    """
    message = update.effective_message
    if not message or not message.text:
        return
    question = message.text.strip()
    if not question or question.startswith("/"):
        return
    chat = update.effective_chat
    reviewer = is_reviewer(_command_owner_id(update))
    private_reviewer = bool(chat and chat.type == "private") and reviewer
    if not private_reviewer and message.chat_id != ALERT_CHAT_ID:
        return

    parsed = director_ask.parse_question(question, today=_my_today())
    if private_reviewer:
        interesting = bool(parsed.get("canonical")) or (
            parsed.get("intent") == director_ask.INTENT_ITEMS
        )
    else:
        interesting = bool(parsed.get("confident"))
    if interesting:
        await _send_answer(message, question)
        return
    # Anything else the item search can't take becomes one read-only SELECT
    # (director_sql) — when DIRECTOR_QA is on. In the group only questions
    # are answered, so the owners' chatter is left alone.
    if director_sql.enabled() and (private_reviewer or director_sql.looks_like_question(question)):
        await _answer_sql(message, question, _command_owner_id(update))


def _run_readonly_sql(sql: str) -> list[dict]:
    """The one door to free SQL: the director_sql() function (migrations/0055),
    read-only role, 5-second timeout."""
    data = supabase.rpc(director_sql.RPC, {"q": sql}).execute().data
    return data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])


async def _answer_sql(message, question: str, user_id) -> None:
    result = await asyncio.to_thread(
        director_sql.run, question, run_sql=_run_readonly_sql)
    try:
        await asyncio.to_thread(
            lambda: supabase.table(director_sql.LOG_TABLE)
            .insert(director_sql.log_row(result, chat_id=message.chat_id, user_id=user_id))
            .execute())
    except Exception:
        logger.exception("director sql: log failed (migrations/0055 applied?)")
    logger.info("director sql: %s (%d rows, %d ms) %s", "ok" if result["ok"] else "no",
                result["row_count"], result["ms"], result.get("error") or "")
    await _reply_chunked(message, result["text"])


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


def _fetch_sales_daily_latency(outlet_code, since_date):
    """Recent sales_daily rows for one kitchen outlet (bridged via outlet_join_keys),
    carrying the timestamps that separate send-side from poll-side latency:
    shift_close_at, received_at (email Date), created_at (our ingest time)."""
    import kitchen_usage as ku
    keys = ku.outlet_join_keys(outlet_code)
    resp = (
        supabase.table("sales_daily")
        .select("outlet_code, outlet_canonical, shift_type, shift_no, "
                "shift_close_at, shift_business_date, received_at, created_at")
        .gte("shift_business_date", since_date)
        .order("shift_business_date", desc=True)
        .execute()
    )
    return [
        r for r in (resp.data or [])
        if (ku.outlet_join_keys(r.get("outlet_code")) & keys)
        or (ku.outlet_join_keys(r.get("outlet_canonical")) & keys)
    ]


async def sales_ingest_latency_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner check: did the POS shift emails arrive AND get ingested in time for
    the 09:00 comparison? Shows close → recv(email Date) → ingest(created_at) per
    shift with a send-side vs poll-side verdict. Usage: /sales_ingest_latency [OUTLET]."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    parts = (message.text or "").split()
    outlet = parts[1].upper() if len(parts) > 1 else "KLANG"
    since_date = (datetime.now(MALAYSIA_TZ).date() - timedelta(days=5)).isoformat()
    try:
        rows = await asyncio.to_thread(_fetch_sales_daily_latency, outlet, since_date)
    except Exception:
        logger.exception("sales_ingest_latency failed")
        await message.reply_text("Failed to read sales_daily.")
        return
    await message.reply_text(sales_analytics.format_ingest_latency(outlet, rows))


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
    text = (
        "Sales ingest done —\n"
        f"• Fetched: {summary['fetched']}\n"
        f"• Inserted: {summary['inserted']}\n"
        f"• Skipped (duplicate): {summary['skipped']}\n"
        f"• Skipped (inactive): {summary['skipped_inactive']}\n"
        f"• Skipped (monthly report): {summary.get('skipped_report', 0)}\n"
        f"• Skipped (unknown): {summary['skipped_unknown']}\n"
        f"• Errors: {summary['errors']}"
    )
    dead = summary.get("dead_letter", 0)
    stuck = summary.get("dead_letter_subjects") or []
    if dead:
        text += f"\n• Dead letters (stuck, failing 3+ times): {dead}"
        for s in stuck[:5]:
            text += f"\n   - {s}"
    parked = summary.get("parked", 0)
    if parked:
        text += (
            f"\n• Parked (unparseable content, marked read, won't retry): {parked}"
        )
        for s in (summary.get("parked_subjects") or [])[:5]:
            text += f"\n   - {s}"
    new_codes = summary.get("new_outlets") or []
    if new_codes:
        text += "\n\n" + _new_outlet_alert_text(new_codes)
    if summary.get("migration_0038_needed"):
        text += (
            "\n\n⚠️ Apply migrations/0038_sales_daily_summary_multi_close.sql "
            "in Supabase — the DB is still rejecting 24h shops' second "
            "daily-close email, so half those days' sales can't be stored."
        )
    await message.reply_text(text)


async def activate_outlet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: activate a (new/placeholder) POS outlet and pull in its
    held emails. Usage: /activate_outlet S-44 Shop Name Here"""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    args = context.args or []
    if not args:
        await message.reply_text(
            "Usage: /activate_outlet <code> [shop name]\n"
            "Example: /activate_outlet S-44 Khulafa 44"
        )
        return
    code = args[0].strip().upper()
    if code.startswith("D-"):
        code = "S-" + code[2:]
    elif not code.startswith("S-"):
        code = "S-" + code
    name = " ".join(args[1:]).strip() or code[2:]

    def _activate():
        resp = (
            supabase.table("outlet_canonical")
            .update({"canonical_name": name, "active": True, "confirmed": True})
            .eq("code", code)
            .execute()
        )
        return bool(resp.data)

    try:
        found = await asyncio.to_thread(_activate)
    except Exception:
        logger.exception("activate_outlet failed for %s", code)
        await message.reply_text(f"Failed to activate {code}.")
        return
    if not found:
        await message.reply_text(
            f"{code} is not in outlet_canonical yet — it appears there "
            "automatically the first time its POS email arrives."
        )
        return
    await message.reply_text(
        f"✅ {code} activated as “{name}”. Pulling in its held emails now…"
    )
    # Targeted recovery sweep: the outlet's emails were marked read while it
    # was a placeholder, so re-scan SEEN mail — but only subjects matching this
    # outlet's S-/D- codes (message-id dedup keeps it idempotent).
    since = datetime.now(MALAYSIA_TZ) - timedelta(days=30)
    totals = {"fetched": 0, "inserted": 0, "skipped": 0, "errors": 0}
    try:
        for token in (code, "D-" + code[2:]):
            s = await asyncio.to_thread(
                run_ingest_once, supabase,
                since=since, unseen_only=False, subject_token=token,
            )
            for k in totals:
                totals[k] += s.get(k, 0)
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner
        logger.exception("activate_outlet recovery sweep failed for %s", code)
        await message.reply_text(
            f"{code} is active, but the email sweep failed: {exc}\n"
            "New emails will still ingest normally from the next poll."
        )
        return
    await message.reply_text(
        f"Recovery sweep for {code} (last 30 days) —\n"
        f"• Fetched: {totals['fetched']}\n"
        f"• Inserted: {totals['inserted']}\n"
        f"• Skipped (already stored): {totals['skipped']}\n"
        f"• Errors: {totals['errors']}\n"
        "From now on this shop counts like every other outlet."
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


async def monthly_kg_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/monthly_kg [YYYY-MM | last]`` — kg of ayam/daging/kambing (and the
    other tracked proteins) bought in a month, rolled up from analysed
    receipts. Defaults to the current month-to-date. Answers in the alert
    group and to reviewers anywhere, same policy as /shop_prices."""
    message = update.effective_message
    if not message:
        return
    in_alert_chat = message.chat_id == ALERT_CHAT_ID
    if not in_alert_chat and not is_reviewer(_command_owner_id(update)):
        return
    today = _my_today()
    arg = context.args[0] if context.args else ""
    parsed = monthly_consumption.parse_month_arg(arg, today)
    if parsed is None:
        await message.reply_text(
            "Usage: /monthly_kg [YYYY-MM | last]\n"
            "Contoh: /monthly_kg — bulan ini setakat hari ini\n"
            "        /monthly_kg last — bulan lepas penuh\n"
            "        /monthly_kg 2026-07 — bulan tertentu"
        )
        return
    year, month = parsed
    try:
        text = await asyncio.to_thread(
            monthly_consumption.build_monthly_report, supabase, year, month, today
        )
    except Exception:
        logger.exception("monthly_kg failed (%s-%s)", year, month)
        await message.reply_text("Failed to build the monthly kg report.")
        return
    text = await _with_outside_section(text, year, month)
    await _reply_chunked(message, text)


async def post_monthly_kg_report(application: Application) -> None:
    """1st of the month 09:30 MY job: last month's kg-per-protein report to
    the alert group. Build failures degrade to a stock message inside
    build_monthly_report, so this only guards the send itself."""
    today = _my_today()
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1
    text = await asyncio.to_thread(
        monthly_consumption.build_monthly_report, supabase, year, month, today
    )
    text = await _with_outside_section(text, year, month)
    try:
        await _send_chunked_to(application, ALERT_CHAT_ID, text)
    except Exception:
        logger.exception("monthly kg report: send failed (%s-%s)", year, month)


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
    user = update.effective_user
    chat = update.effective_chat
    # DM-only: registration binds weekly food-cost delivery to THIS chat_id.
    # Typed in the outlet's staff group it would (a) expose the one-time code
    # to the whole group and (b) deliver the outlet's financials to the whole
    # group instead of the manager.
    if chat is not None and chat.type != "private":
        with contextlib.suppress(Exception):
            await message.delete()  # remove the exposed code from the group
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ /register hanya melalui DM — message bot ini secara private, "
                 "jangan guna code dalam group.",
        )
        return
    if not context.args:
        await message.reply_text(
            "Usage: /register <CODE>\nExample: /register SEK20-7K2A"
        )
        return
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
        # Food-cost % is a money report: director only, never a staff group
        # (the HQ summary below carries every outlet's number).
        if group_reports.blocked(group_reports.FOOD_COST, msg["target"], ALERT_CHAT_ID):
            continue
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


# === Missing supplier-bill watch ============================================
# Months of receipts define each supplier's upload rhythm per outlet chat.
# When a regular supplier suddenly goes quiet, the nightly job asks that chat
# in Tamil + Malay why nothing was uploaded ("BESTARI FARM bill எங்க?") — a
# forgotten photo is caught within days instead of surfacing as a hole in the
# monthly numbers. Delivery reuses the weekly-report safety gate: while
# MANAGER_DELIVERY_ENABLED is False every question routes to the owner with a
# [TEST] prefix. The owner gets an English summary whenever anything is quiet.

def _gather_missing_bills(today=None) -> dict:
    rows = missing_bills.load_supplier_bill_rows(supabase, today=today)
    entries = missing_bills.find_missing_bills(rows, today=today)
    return {"entries": entries, "enabled": wmr.delivery_enabled()}


async def post_missing_bill_checks(application: Application, *,
                                   notify_chat_id=None) -> None:
    """Nightly 21:00 MY job (after the day's uploads, before the 23:00
    digest). Asks each outlet chat about its quiet suppliers, then sends the
    owner one consolidated summary. ``notify_chat_id`` additionally reports
    the all-clear for the on-demand command."""
    try:
        bundle = await asyncio.to_thread(_gather_missing_bills)
    except Exception:
        logger.exception("missing bill check: gather failed")
        if notify_chat_id is not None:
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=notify_chat_id,
                    text="Failed to run the missing-bill check.",
                )
        return

    entries = bundle["entries"]
    enabled = bundle["enabled"]
    sent = 0
    for entry in entries:
        # ask_today paces the chasing: overdue day 1, then every 3 days —
        # the rest of the quiet suppliers still appear in the owner summary.
        if not entry.get("ask_today"):
            continue
        # A live outlet gets the natural 21:05 bills check-in instead.
        if _staff_live_now(cashier_names.outlet_for_chat(entry.get("chat_id"))):
            continue
        text = missing_bills.format_missing_bill_message(entry)
        if not text:
            continue
        text = supervisor.with_reply_footer(text)
        if enabled:
            target, prefix = entry["chat_id"], ""
        else:
            where = entry.get("outlet") or f"chat {entry['chat_id']}"
            target = ALERT_CHAT_ID
            prefix = f"[TEST — would go to the {where} group]\n\n"
        try:
            sent_msg = await application.bot.send_message(
                chat_id=target, text=prefix + text
            )
            sent += 1
            if enabled:
                await asyncio.to_thread(
                    supervisor.log_question,
                    supabase, target, sent_msg.message_id,
                    "missing_bill", text,
                )
        except Exception:
            logger.exception(
                "missing bill check: send failed (outlet=%s, supplier=%s)",
                entry.get("outlet"), entry.get("supplier"),
            )

    summary = missing_bills.format_owner_summary(entries)
    if summary:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
    if notify_chat_id is not None and not entries:
        with contextlib.suppress(Exception):
            await application.bot.send_message(
                chat_id=notify_chat_id,
                text="✅ Every regular supplier's bills are coming in on rhythm "
                     "— nothing has gone quiet.",
            )
    logger.info(
        "Missing bill check: %d quiet supplier(s), %d question(s) sent, "
        "delivery_enabled=%s",
        len(entries), sent, enabled,
    )


async def missing_bills_now_command(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: run the missing-bill check on demand (for testing)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text("Checking supplier upload rhythms…")
    await post_missing_bill_checks(
        context.application, notify_chat_id=_command_owner_id(update)
    )


# === Bill analysis (every bill, every item, every shop) =====================
# Nightly 21:30 MY, after the day's uploads and the 21:00 missing-bill check,
# ahead of the 23:00 digest. Two passes over the cleaned item_prices corpus
# (see bill_analysis.py):
#   1. every line on the bills uploaded in the last 24h against the SAME
#      shop's previous price for the same cut — increases to the owners,
#      and in Tamil to the manager whose bill it was, with who sells it
#      cheaper and which branch pays less;
#   2. every item bought by two or more outlets in the last 30 days — what
#      each branch last paid and from whom, cheapest first; each manager is
#      told the items another branch buys cheaper (and where they are the
#      cheapest themselves).
# Owners always get the English reports in the alert group. Manager notes
# ride the MANAGER_DELIVERY_ENABLED gate like every other manager message.

def _registry_code_for_outlet(outlet_code, registry_outlets) -> str | None:
    """item_prices outlet code (``D``, ``BISTRO7``) -> outlet_canonical
    registration code (``DAMANSARA``, ``BISTRO7``), so the right manager is
    found. Bridges on the exact code first, then on the canonical name."""
    from outlet_resolver import canonical_outlet

    code = str(outlet_code or "").strip().upper()
    if not code:
        return None
    for o in registry_outlets:
        if o.code.upper() == code:
            return o.code
    canonical = canonical_outlet(code)
    if canonical:
        for o in registry_outlets:
            if o.canonical == canonical or canonical_outlet(o.code) == canonical:
                return o.code
    return None


def _gather_bill_analysis(today=None) -> dict:
    bundle = bill_analysis.gather_bill_analysis(supabase, today=today)
    routes = {}
    if bundle.get("outlet_codes"):
        try:
            outlets = manager_registration.load_active_outlets(supabase)
            managers = manager_registration.get_all_managers(supabase)
            for code in bundle["outlet_codes"]:
                reg = _registry_code_for_outlet(code, outlets)
                mgr = managers.get(reg) if reg else None
                display = next(
                    (o.display for o in outlets if reg and o.code == reg), None
                ) or bill_analysis.outlet_label(code)
                routes[code] = {
                    "display": display,
                    "manager_chat_id": mgr.get("chat_id") if mgr else None,
                    "manager_name": mgr.get("manager_name") if mgr else None,
                }
        except Exception:
            logger.exception("bill analysis: manager routing lookup failed")
    bundle["routes"] = routes
    bundle["enabled"] = wmr.delivery_enabled()
    return bundle


async def _send_chunked_to(application: Application, chat_id, text: str) -> bool:
    ok = True
    for chunk in chunk_message(text):
        try:
            await application.bot.send_message(chat_id=chat_id, text=chunk)
        except Exception:
            ok = False
            logger.exception("bill analysis: send failed (chat=%s)", chat_id)
    return ok


async def post_bill_analysis(application: Application, *,
                             notify_chat_id=None) -> None:
    """Nightly 21:30 MY job (and ``/bill_analysis_now``). Owners get the
    price-change report and the outlet comparison; each outlet manager gets
    one Tamil note about their own bills, via the delivery gate."""
    try:
        bundle = await asyncio.to_thread(_gather_bill_analysis)
    except Exception:
        logger.exception("bill analysis: gather failed")
        for chat in {ALERT_CHAT_ID, notify_chat_id} - {None}:
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=chat, text="⚠️ Bill analysis failed to run — see logs."
                )
        return

    price_report = bill_analysis.format_owner_price_report(bundle)
    outlet_report = bill_analysis.format_owner_outlet_report(bundle)
    if price_report:
        await _send_chunked_to(application, ALERT_CHAT_ID, price_report)
    if outlet_report:
        await _send_chunked_to(application, ALERT_CHAT_ID, outlet_report)
    if notify_chat_id is not None and not price_report and not outlet_report:
        with contextlib.suppress(Exception):
            await application.bot.send_message(
                chat_id=notify_chat_id,
                text="No bills were uploaded in the last 24h and no item was "
                     "bought by two or more outlets recently — nothing to analyse.",
            )

    enabled = bundle.get("enabled", False)
    delivered: list[dict] = []
    for code in bundle.get("outlet_codes") or []:
        slice_ = bill_analysis.entries_for_outlet(bundle, code)
        text = bill_analysis.format_manager_note(code, slice_)
        if not text:
            continue
        route = (bundle.get("routes") or {}).get(code) or {}
        decision = wmr.route_message(
            enabled,
            route.get("display") or bill_analysis.outlet_label(code),
            route.get("manager_chat_id"),
            ALERT_CHAT_ID,
        )
        if group_reports.blocked(
            group_reports.BILL_ANALYSIS, decision.target_chat_id, ALERT_CHAT_ID
        ):
            continue
        text = human_touch.personalise(
            supervisor.with_reply_footer(text),
            route.get("manager_name"),
            decision.target_chat_id,
        )
        try:
            await human_touch.show_typing(application.bot, decision.target_chat_id)
            first_id = None
            for i, chunk in enumerate(chunk_message(decision.prefix + text)):
                sent_msg = await application.bot.send_message(
                    chat_id=decision.target_chat_id, text=chunk
                )
                if i == 0:
                    first_id = sent_msg.message_id
            if decision.reason == "manager" and first_id is not None:
                await asyncio.to_thread(
                    supervisor.log_question,
                    supabase, decision.target_chat_id, first_id,
                    "bill_analysis", text,
                )
            delivered.append({
                "outlet_code": code,
                "display": route.get("display") or bill_analysis.outlet_label(code),
                "reason": decision.reason,
                "manager_name": route.get("manager_name"),
                "increases": len(slice_["increases"]),
                "pays_more": len(slice_["pays_more"]),
            })
        except Exception:
            logger.exception("bill analysis: manager note send failed (outlet=%s)", code)

    summary = bill_analysis.format_owner_delivery_summary(delivered, enabled)
    if summary:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
    stats = bundle.get("stats") or {}
    logger.info(
        "Bill analysis: %d bill(s), %d increase(s), %d decrease(s), "
        "%d item(s) compared across outlets, %d manager note(s), delivery_enabled=%s",
        stats.get("new_bills", 0), len(bundle.get("increases") or []),
        len(bundle.get("decreases") or []), len(bundle.get("comparisons") or []),
        len(delivered), enabled,
    )


async def bill_analysis_now_command(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: run the nightly bill analysis on demand."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text("Analysing every bill, every item, every shop…")
    await post_bill_analysis(
        context.application, notify_chat_id=_command_owner_id(update)
    )


async def outlet_prices_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/outlet_prices [item]`` — what each branch last paid, cheapest
    first: one item, or every item two or more branches buy. Answers in the
    alert group and to reviewers anywhere, like /shop_prices."""
    message = update.effective_message
    if not message:
        return
    in_alert_chat = message.chat_id == ALERT_CHAT_ID
    if not in_alert_chat and not is_reviewer(_command_owner_id(update)):
        return
    query = " ".join(context.args or []).strip()
    try:
        text = await asyncio.to_thread(
            bill_analysis.build_outlet_price_report, supabase, query
        )
    except Exception:
        logger.exception("outlet_prices failed (query=%r)", query)
        await message.reply_text("Failed to build the outlet price comparison.")
        return
    await _reply_chunked(message, text)


# === Question follow-up (the human-supervisor memory) =======================
# Every Tamil question the bot asks a real manager/group is logged in
# audit_responses (see supervisor.py). This job is the part a human boss
# does and dashboards don't: it REMEMBERS. Once a day it nudges every
# question that has sat unanswered since yesterday (one nudge per question,
# stateless — the 20-44h window can only match a question once), and shows
# the owner who has answered and who has gone quiet.

async def post_question_reminders(application: Application, *,
                                  notify_chat_id=None) -> None:
    """Daily 17:00 MY: nudge yesterday's unanswered questions in their own
    chats (as a reply, so the original question is quoted), then send the
    owner the who-went-quiet overview."""
    try:
        due = await asyncio.to_thread(
            supervisor.open_questions_between, supabase
        )
    except Exception:
        logger.exception("question reminders: read failed")
        due = []

    # Live outlet groups: their questions live in the staff question system,
    # which reminds once, combined — no separate 17:00 nudge there.
    due = [q for q in due
           if not _staff_live_now(cashier_names.outlet_for_chat(q.get("chat_id")))]
    nudged = 0
    for q in due:
        # Single attempt only: allow_sending_without_reply already covers a
        # deleted original, so a raise here is a transport error — and with
        # those the message may have been delivered anyway, so a blind
        # retry risks double-nudging.
        try:
            await human_touch.show_typing(application.bot, q["chat_id"])
            sent = await application.bot.send_message(
                chat_id=q["chat_id"],
                text=supervisor.REMINDER_TEXT,
                reply_to_message_id=q.get("question_message_id"),
                allow_sending_without_reply=True,
            )
            nudged += 1
            # Log the nudge linked to the original: a manager who replies
            # to the NUDGE (the newest message) must close the loop too.
            await asyncio.to_thread(
                supervisor.log_reminder,
                supabase, q["chat_id"], sent.message_id, q,
            )
        except Exception:
            logger.exception(
                "question reminders: send failed (chat=%s)", q.get("chat_id")
            )

    try:
        pending = await asyncio.to_thread(
            supervisor.open_questions_since, supabase
        )
    except Exception:
        logger.exception("question reminders: pending read failed")
        pending = []
    summary = supervisor.format_owner_pending(pending)
    if summary:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
    if notify_chat_id is not None and not pending:
        with contextlib.suppress(Exception):
            await application.bot.send_message(
                chat_id=notify_chat_id,
                text="✅ Every tracked question has been answered — "
                     "nothing pending.",
            )
    logger.info(
        "Question reminders: %d nudged, %d still pending", nudged, len(pending)
    )


async def form_chase_now_command(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: chase every unfilled kitchen form right now."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    forms = await asyncio.to_thread(
        kitchen_usage.find_unsubmitted_forms, supabase
    )
    if not forms:
        await message.reply_text(
            "✅ Every posted kitchen form has been submitted — nothing to chase."
        )
        return
    if not kitchen_usage.kitchen_log_enabled():
        # Safety gate off: show what WOULD be chased instead of going silent.
        from outlet_mapping import outlet_display_name
        await message.reply_text(
            "[KITCHEN_LOG_ENABLED is off — listing instead of chasing]\n\n"
            + kitchen_usage.render_form_escalation(forms, outlet_display_name)
        )
        return
    await message.reply_text(
        f"Chasing {len(forms)} unfilled form(s) in their groups…"
    )
    await kitchen_usage.post_form_reminders(context.application)


async def cashier_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: show or change the cashier on shift per outlet group.

    ``/cashier`` lists every group; ``/cashier SEK20 night Ismath`` saves a
    name (several words are fine: ``/cashier SEK6 night Mahadir / Pandi``)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await asyncio.to_thread(cashier_names.refresh)
    args = context.args or []
    if not args:
        await message.reply_text(cashier_names.format_roster())
        return
    if len(args) < 3:
        await message.reply_text(
            "Usage: /cashier <CODE> <morning|night> <name>\n"
            "e.g. /cashier SEK20 night Ismath"
        )
        return
    code = args[0].strip().upper()
    known = set(cashier_names.group_chats().values())
    if code not in known:
        await message.reply_text(
            f"Unknown outlet {code}. Registered groups: "
            + (", ".join(sorted(known)) or "none")
        )
        return
    result = await asyncio.to_thread(
        cashier_names.set_name,
        supabase, code, args[1], " ".join(args[2:]), _command_owner_id(update),
    )
    if not result.get("ok"):
        await message.reply_text(f"⚠️ {result.get('error')}")
        return
    logger.info(
        "cashier: %s %s -> %r (by %s)",
        result["outlet_code"], result["shift"], result["name"],
        _command_owner_id(update),
    )
    await message.reply_text(
        f"✅ {result['outlet_code']} {result['shift']}: {result['name']}"
    )


async def ping_managers_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: one test message to every registered outlet chat,
    addressed to the cashier on shift, each delivery logged for checking."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await asyncio.to_thread(cashier_names.refresh)
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(manager_registration.MANAGERS_TABLE)
            .select("*").execute().data or []
        )
    except Exception:
        logger.exception("ping_managers: could not read outlet_managers")
        await message.reply_text("⚠️ Could not read outlet_managers — see logs.")
        return
    text = cashier_names.ping_text()
    shift, _start = cashier_names.shift_at()
    results = []
    for row in sorted(rows, key=lambda r: str(r.get("outlet_code") or "")):
        code = row.get("outlet_code")
        chat_id = row.get("chat_id")
        name = cashier_names.name_on_shift(chat_id) or row.get("manager_name")
        try:
            sent = await context.bot.send_message(chat_id=chat_id, text=text)
            logger.info(
                "ping_managers: outlet=%s chat=%s shift=%s name=%s delivered "
                "message_id=%s", code, chat_id, shift, name, sent.message_id,
            )
            results.append(f"✅ {code} → {name}")
        except TelegramError as e:
            new_id = getattr(e, "new_chat_id", None)
            logger.warning(
                "ping_managers: outlet=%s chat=%s shift=%s FAILED %s: %s%s",
                code, chat_id, shift, type(e).__name__, e,
                f" (new chat id {new_id})" if new_id else "",
            )
            detail = f"group moved, new chat id {new_id}" if new_id else str(e)
            results.append(f"❌ {code} → {type(e).__name__}: {detail}")
    await message.reply_text(
        f"🔔 Test sent to {len(rows)} outlet chat(s), shift: {shift}\n\n"
        + ("\n".join(results) or "No outlet_managers rows.")
    )


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: set the language a cashier reads, per outlet and shift.
    ``/lang SEK20 morning tamil``; ``/lang`` alone lists everyone."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await asyncio.to_thread(cashier_names.refresh)
    args = context.args or []
    if not args:
        await message.reply_text(cashier_names.format_roster())
        return
    if len(args) != 3:
        await message.reply_text(
            "Usage: /lang <CODE> <morning|night> <language>\n"
            "Languages: " + ", ".join(staff_chat.LANGUAGES)
        )
        return
    code = args[0].strip().upper()
    known = set(cashier_names.group_chats().values())
    if code not in known:
        await message.reply_text(
            f"Unknown outlet {code}. Registered groups: "
            + (", ".join(sorted(known)) or "none")
        )
        return
    language = staff_chat.normalize_language(args[2])
    if language is None:
        await message.reply_text(
            f"Unknown language {args[2]!r}. Use one of: "
            + ", ".join(staff_chat.LANGUAGES)
        )
        return
    result = await asyncio.to_thread(
        cashier_names.set_language,
        supabase, code, args[1], language, _command_owner_id(update),
    )
    if not result.get("ok"):
        await message.reply_text(f"⚠️ {result.get('error')}")
        return
    logger.info("lang: %s %s -> %s", result["outlet_code"], result["shift"], language)
    await message.reply_text(
        f"✅ {result['outlet_code']} {result['shift']}: {language}"
    )


# === Natural staff chat — preview ==========================================
# Each check-in (staff_chat.SLOTS) is written for every outlet group from the
# real data, worded by the AI provider, fact-checked, and — in preview mode —
# sent ONLY to the director chat as one digest per check-in. Nothing reaches a
# shop group from here yet.

def _staff_slot_facts(slot, registry_code, chat_id, today, bills_by_chat):
    """Facts for one outlet's check-in; ``None`` = nothing true to say."""
    if slot not in staff_chat.DATA_SLOTS:
        return {}
    codes = staff_chat.data_codes(registry_code)
    if slot == "order":
        # Tomorrow's proposed order: the median of the last 4 same-weekday
        # buys (order_proposal). Thin history -> ask what to order instead.
        lines = order_proposal.build(_order_history_rows(codes, today),
                                     target_day=today + timedelta(days=1))
        verdict = order_sanity.assess(
            order_sanity.fetch_history(supabase, codes, today), order_proposal.draft_lines(lines)
        )
        if not verdict["ok"]:
            logger.info("staff chat order %s: proposal not used (%s)",
                        registry_code, verdict["reason"])
            return {"ask": True}
        kept = {ln["item"] for ln in verdict["lines"]}
        lines = [ln for ln in lines if ln["item"] in kept]
        facts = staff_chat.order_facts(order_proposal.draft_lines(lines))
        facts["_proposal"] = lines
        return facts
    if slot == "stock":
        rows = (
            supabase.table("order_drafts")
            .select("item, qty, pack, supplier, outlet, due_date")
            .in_("outlet", codes).eq("due_date", today.isoformat())
            .execute().data or []
        )
        # Thin buying history -> no draft numbers at all (order_sanity):
        # nothing to ask about stock.
        verdict = order_sanity.assess(
            order_sanity.fetch_history(supabase, codes, today), rows
        )
        if not verdict["ok"]:
            logger.info("staff chat stock %s: draft not used (%s)",
                        registry_code, verdict["reason"])
            return None
        return staff_chat.stock_facts(verdict["lines"])
    if slot == "cook":
        rows = (
            supabase.table("kitchen_demand_forecast")
            .select("item_code, unit, recommend_qty, usual_cooked, action, outlet_code")
            .in_("outlet_code", codes).eq("business_date", today.isoformat())
            .execute().data or []
        )
        return staff_chat.cook_facts(rows)
    if slot == "bills":
        return staff_chat.bills_facts(bills_by_chat.get(chat_id) or [])
    return None


def _order_history_rows(codes, today) -> list[dict]:
    """Receipts plus what cashiers ordered (staff_order_items) for these
    outlet codes over the proposal window — the rows order_proposal reads."""
    lookback = order_proposal.WEEKS * 7 + 1
    try:
        receipts = fetch_all_pages(
            lambda: supabase.table("item_prices")
            .select("outlet_code, canonical_item, qty, receipt_date, created_at")
            .in_("outlet_code", list(codes))
            .gte("receipt_date", (today - timedelta(days=lookback)).isoformat())
            .lte("receipt_date", today.isoformat())
            .order("id")
        )
    except Exception:
        logger.exception("order proposal: item_prices read failed")
        receipts = []
    staff = [r for r in staff_orders.fetch_history_rows(
        supabase, today=today, lookback=lookback, codes=codes)
        if str(r.get("receipt_date") or "")[:10] <= today.isoformat()]
    return staff_orders.merge(receipts, staff)


def _order_proposal_lines(registry_code, today) -> list[dict]:
    """Tomorrow's proposal for one outlet, sanity-gated like the check-in."""
    codes = staff_chat.data_codes(registry_code)
    lines = order_proposal.build(_order_history_rows(codes, today),
                                 target_day=today + timedelta(days=1))
    verdict = order_sanity.assess(order_sanity.fetch_history(supabase, codes, today),
                                  order_proposal.draft_lines(lines))
    if not verdict["ok"]:
        return []
    kept = {ln["item"] for ln in verdict["lines"]}
    return [ln for ln in lines if ln["item"] in kept]


class _DraftItems(dict):
    def get_codes(self, codes):
        return [row for code in codes for row in self.get(code, [])]


_unsaved_drafts_cache: dict = {}


def _unsaved_order_items(today) -> _DraftItems:
    """Tomorrow's order draft per outlet code, computed but NOT persisted.
    Cached per day so one preview computes it once for all outlets."""
    if today not in _unsaved_drafts_cache:
        bundle = order_generator.gather_order_drafts(
            supabase, today=today, persist=False
        )
        _unsaved_drafts_cache.clear()
        _unsaved_drafts_cache[today] = _DraftItems(
            {o["outlet_code"]: o.get("items") or [] for o in bundle["outlets"]}
        )
    return _unsaved_drafts_cache[today]


def _anomaly_metrics(code, today) -> list[dict]:
    """The outlet's numbers for staff_anomaly.detect: yesterday's items sold
    and leftovers per dish, today's order quantity per item, each with the
    same weekday over the trailing 4 weeks."""
    codes = staff_chat.data_codes(code)
    yesterday = today - timedelta(days=1)
    prior_y = [yesterday - timedelta(days=7 * k) for k in range(1, 5)]
    prior_t = [today - timedelta(days=7 * k) for k in range(1, 5)]
    metrics: list[dict] = []
    # No "sales" metric here: the anomaly question would quote yesterday's
    # items-sold count and the usual to the cashier, and sales figures are
    # management-only. Wastage and order quantities are the cashier's own.
    try:
        rows = [r for r in demand_forecast.load_usage_rows(
            supabase, prior_y[-1].isoformat(), yesterday.isoformat())
            if r.get("outlet_code") in codes and r.get("left_qty") is not None]
        per: dict = {}
        for r in rows:
            d = date.fromisoformat(str(r["business_date"])[:10])
            per.setdefault(r["item_code"], {})[d] = float(r["left_qty"] or 0)
        for item, days in per.items():
            if yesterday in days:
                metrics.append({"metric": "wastage", "item": item, "today": days[yesterday],
                                "usual": [days[d] for d in prior_y if d in days],
                                "unit": kitchen_usage.ITEM_BY_CODE.get(item, {}).get("unit") or ""})
    except Exception:
        logger.exception("anomaly: wastage lookup failed (%s)", code)
    try:
        per = {}
        for r in _order_history_rows(codes, today):
            item = str(r.get("canonical_item") or "").lower()
            try:
                d = date.fromisoformat(str(r.get("receipt_date"))[:10])
                qty = float(r.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if item and qty > 0:
                days = per.setdefault(item, {})
                days[d] = days.get(d, 0.0) + qty
        for item, days in per.items():
            if today in days:
                metrics.append({"metric": "order", "item": item, "today": days[today],
                                "usual": [days[d] for d in prior_t if d in days],
                                "unit": order_proposal._unit(item, [])})
    except Exception:
        logger.exception("anomaly: order lookup failed (%s)", code)
    return metrics


def _anomalies_asked_today(today) -> dict:
    """``{outlet_code: {(metric, item_code)}}`` already asked about today."""
    since = datetime.combine(today, datetime.min.time(), MALAYSIA_TZ).isoformat()
    out: dict = {}
    try:
        rows = (supabase.table(staff_chat.LOG_TABLE).select("outlet_code, facts")
                .eq("kind", staff_anomaly.KIND).gte("created_at", since).execute().data or [])
    except Exception:
        logger.info("anomaly: asked-today lookup failed (migrations/0050 applied?)")
        return out
    for r in rows:
        f = r.get("facts") or {}
        out.setdefault(r.get("outlet_code"), set()).add((f.get("metric"), f.get("item_code")))
    return out


_NO_DATA = {
    "stock": "no order draft for today — nothing to ask",
    "order": "no order draft for tomorrow — nothing to ask",
    "cook": "no cook plan change today — nothing to ask",
    "bills": "no missing supplier bills — nothing to ask",
}


def _recent_staff_texts(slot, today, days: int = 3) -> dict:
    """``{outlet_code: [recent texts]}`` this check-in produced on the last
    few days — passed to the AI so it says it differently. Best effort."""
    since = datetime.combine(
        today - timedelta(days=days), datetime.min.time(), MALAYSIA_TZ
    ).isoformat()
    try:
        rows = (
            supabase.table(staff_chat.LOG_TABLE)
            .select("outlet_code, final_text, created_at")
            .eq("slot", slot).gte("created_at", since)
            .order("created_at", desc=True).limit(100)
            .execute().data or []
        )
    except Exception:
        logger.exception("staff preview: recent texts lookup failed")
        return {}
    out: dict = {}
    for r in rows:
        text = r.get("final_text")
        if text and text not in out.setdefault(r.get("outlet_code"), []):
            out[r["outlet_code"]].append(text)
    return out


def _phrasing_examples() -> list[dict]:
    """The latest week's fast-reply wordings (staff_learning). ``[]`` until
    the Monday job has run or when the table is missing."""
    try:
        rows = (supabase.table(staff_learning.TABLE).select("*")
                .order("week_start", desc=True).order("rank").limit(300).execute().data or [])
    except Exception:
        logger.info("staff learning: examples read failed (migrations/0056 applied?)")
        return []
    if not rows:
        return []
    latest = rows[0]["week_start"]
    return [r for r in rows if r.get("week_start") == latest]


def _avg_prompt_tokens(slot) -> float | None:
    """The average prompt size of recent check-ins for this slot, so the
    examples never push it past that average plus 30%."""
    since = (datetime.now(MALAYSIA_TZ) - timedelta(days=14)).isoformat()
    try:
        rows = (supabase.table(staff_chat.LOG_TABLE).select("tokens_in")
                .eq("slot", slot).gte("created_at", since).not_.is_("tokens_in", "null")
                .order("created_at", desc=True).limit(200).execute().data or [])
    except Exception:
        return None
    values = [float(r["tokens_in"]) for r in rows if r.get("tokens_in")]
    return sum(values) / len(values) if values else None


async def post_phrasing_examples(application: Application, *, notify: bool = False) -> None:
    """Monday 08:00: keep the three wordings per language and check-in that
    got the fastest replies last week (staff_learning)."""
    if staff_chat.style() == staff_chat.CLASSIC or not staff_live.live_outlets():
        return
    today = _my_today()
    week = staff_learning.week_start(today - timedelta(days=7))
    since = datetime.combine(week, datetime.min.time(), MALAYSIA_TZ).isoformat()
    until = datetime.combine(week + timedelta(days=7), datetime.min.time(), MALAYSIA_TZ).isoformat()
    try:
        threads = await asyncio.to_thread(lambda: fetch_all_pages(
            lambda: supabase.table(staff_live.TABLE)
            .select("language, slot, question_text, status, asked_at, answered_at, reply_clear")
            .eq("status", staff_live.ANSWERED).gte("asked_at", since).lt("asked_at", until)
            .order("id")))
    except Exception:
        logger.exception("staff learning: thread read failed")
        return
    rows = staff_learning.pick(threads, week=week)
    try:
        await asyncio.to_thread(
            lambda: supabase.table(staff_learning.TABLE).delete()
            .eq("week_start", week.isoformat()).execute())
        if rows:
            await asyncio.to_thread(
                lambda: supabase.table(staff_learning.TABLE).insert(rows).execute())
    except Exception:
        logger.exception("staff learning: write failed (migrations/0056 applied?)")
        return
    logger.info("staff learning: %d phrasing example(s) for week %s", len(rows), week)
    if notify:
        await _send_chunked_to(application, ALERT_CHAT_ID, staff_learning.format_report(rows))


async def phrasing_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /phrasing_now — rebuild last week's examples and show them."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await post_phrasing_examples(context.application, notify=True)


def _build_staff_preview(slot, today):
    """All outlets' messages for one check-in, plus the log rows."""
    cashier_names.refresh()
    groups = sorted(cashier_names.group_chats().items(), key=lambda kv: kv[1])
    shift = staff_chat.SLOTS[slot][0]
    bills_by_chat = {}
    if slot == "bills":
        handed = _recent_handins(today)
        for entry in _gather_missing_bills(today=today)["entries"]:
            outlet = cashier_names.outlet_for_chat(entry.get("chat_id"))
            if (outlet, str(entry.get("supplier") or "").upper()) in handed:
                continue    # the paper bill went to the boss; don't re-ask this week
            if entry.get("ask_today"):
                bills_by_chat.setdefault(entry["chat_id"], []).append(entry)
    vocabulary = staff_chat.item_vocabulary()
    names = cashier_names.all_names()
    recent = _recent_staff_texts(slot, today)
    earlier = _answers_today(today) if staff_live.live_outlets() else {}
    asked_anomalies = _anomalies_asked_today(today)
    examples = _phrasing_examples()
    token_budget = staff_learning.budget(_avg_prompt_tokens(slot))
    rows, logs = [], []
    for chat_id, code in groups:
        cashier = cashier_names.name_for(code, shift)
        language = cashier_names.language_for(code, shift)
        live = staff_live.is_live(code)
        row = {"outlet_code": code, "cashier": cashier, "language": language,
               "chat_id": chat_id, "live": live, "slot": slot}
        # A number far off its usual replaces the generic check-in with a
        # targeted question (staff_anomaly); one per check-in, never repeated.
        anomaly = None
        try:
            anomaly = staff_anomaly.detect(_anomaly_metrics(code, today),
                                           asked_recently=asked_anomalies.get(code, set()))
        except Exception:
            logger.exception("staff preview: anomaly check failed (%s %s)", slot, code)
        message_slot = slot
        if anomaly:
            facts, message_slot = anomaly, staff_anomaly.SLOT
            row["message_slot"] = message_slot
            asked_anomalies.setdefault(code, set()).add((anomaly["metric"], anomaly["item_code"]))
        else:
            try:
                facts = _staff_slot_facts(slot, code, chat_id, today, bills_by_chat)
            except Exception:
                logger.exception("staff preview: facts failed (%s %s)", slot, code)
                row["skip"] = "data lookup failed — see logs"
                rows.append(row)
                continue
        if facts is None:
            row["skip"] = _NO_DATA.get(slot, "nothing to ask")
            rows.append(row)
            continue
        proposal = facts.pop("_proposal", None) if isinstance(facts, dict) else None
        if live and earlier.get(code):
            # What they told us earlier today, so the question can refer to it.
            facts = dict(facts, earlier_today=earlier[code][-3:])
        own = {p.strip() for p in cashier.split("/")}
        result = staff_chat.build_message(
            message_slot, language, facts,
            seed=staff_chat.seed_for(slot, code, today),
            vocabulary=vocabulary, other_names=sorted(names - own),
            avoid=recent.get(code, []),
            examples=staff_learning.select(examples, language, slot, token_budget),
        )
        row["result"] = result
        row["facts"] = facts
        if proposal:
            row["thread_facts"] = {**facts, "proposal": proposal}
        if slot == "bills" and not anomaly:
            row["thread_facts"] = {**facts, **staff_chat.bills_detail(
                bills_by_chat.get(chat_id) or [])}
        rows.append(row)
        log_fn = staff_anomaly.log_row if anomaly else staff_chat.log_row
        logs.append(log_fn(
            slot, code, chat_id, cashier, language, facts, result,
            "natural" if live else staff_chat.PREVIEW,
        ))
    _insert_staff_logs(logs, "staff preview")
    return rows


# Columns later migrations add to staff_chat_log (0046 meaning check, 0050
# kind / nudge_no). A log insert that fails is retried without them.
_OPTIONAL_LOG_COLUMNS = ("back_translation", "meaning_ok", "kind", "nudge_no",
                         "voice_file_id", "transcript")


def _insert_staff_logs(logs: list[dict], tag: str) -> None:
    """Write staff_chat_log rows. Until the later migrations add their
    columns, retry without them so the audit trail is never lost."""
    if not logs:
        return
    try:
        supabase.table(staff_chat.LOG_TABLE).insert(logs).execute()
        return
    except Exception:
        logger.warning("%s: log insert failed; retrying without optional columns", tag)
    try:
        trimmed = [{k: v for k, v in r.items() if k not in _OPTIONAL_LOG_COLUMNS}
                   for r in logs]
        supabase.table(staff_chat.LOG_TABLE).insert(trimmed).execute()
    except Exception:
        logger.exception("%s: log insert failed", tag)


async def run_staff_preview(application: Application, slot: str, *,
                            force: bool = False) -> None:
    """Scheduled check-in. Only runs when STAFF_CHAT_STYLE=preview (or when
    the director asks with /staff_preview); the digest goes to the director
    chat only."""
    if not force and staff_chat.style() != staff_chat.PREVIEW:
        return
    if not force and not staff_live.slot_enabled(slot):
        logger.info("staff chat %s: paused (STAFF_CHAT_SLOTS)", slot)
        return
    try:
        rows = await asyncio.to_thread(_build_staff_preview, slot, _my_today())
    except Exception:
        logger.exception("staff preview failed (slot=%s)", slot)
        with contextlib.suppress(Exception):
            await application.bot.send_message(
                chat_id=ALERT_CHAT_ID, text=f"⚠️ Staff chat preview ({slot}) failed — see logs."
            )
        return
    ai = sum(1 for r in rows if r.get("result", {}).get("source") == "ai")
    fallback = sum(1 for r in rows if r.get("result", {}).get("source") == "template")
    logger.info(
        "staff preview %s: %d outlet(s), %d ai, %d template, %d skipped",
        slot, len(rows), ai, fallback, len(rows) - ai - fallback,
    )
    for row in rows:
        if row.get("live") and row.get("result") and not force:
            try:
                row["live_outcome"] = await _live_send_or_queue(application, row)
            except Exception:
                logger.exception("staff live: send failed (%s %s)", slot, row["outlet_code"])
                row["live_outcome"] = "send failed"
    preview_rows = [r for r in rows if not (r.get("live") and not force)]
    if preview_rows:
        await _send_chunked_to(
            application, ALERT_CHAT_ID, staff_chat.format_preview(slot, preview_rows)
        )


# === Live staff chat ========================================================
# Outlets in STAFF_CHAT_LIVE_OUTLETS get the check-ins in their group, one
# question at a time (staff_live). Everything else stays in preview.

def _staff_live_now(outlet_code) -> bool:
    """True when this outlet's natural check-ins are live (and so replace
    the classic cook plan and missing-bill question)."""
    return staff_chat.style() != staff_chat.CLASSIC and staff_live.is_live(outlet_code)


def _thread_select(**eq):
    q = supabase.table(staff_live.TABLE).select("*")
    for k, v in eq.items():
        q = q.eq(k, v)
    return q


def _active_thread(chat_id, reply_to_message_id=None):
    """The open question a message answers: the one it replies to, else the
    latest open one in that group."""
    rows = (
        _thread_select(chat_id=chat_id)
        .in_("status", list(staff_live.ACTIVE))
        .order("asked_at", desc=True).limit(10).execute().data or []
    )
    if reply_to_message_id is not None:
        for r in rows:
            if r.get("message_id") == reply_to_message_id:
                return r
    return rows[0] if rows else None


def _thread_update(thread_id, fields: dict) -> None:
    supabase.table(staff_live.TABLE).update(fields).eq("id", thread_id).execute()


def _answers_today(today) -> dict:
    """``{outlet_code: [English summaries of today's answers]}``."""
    since = datetime.combine(today, datetime.min.time(), MALAYSIA_TZ).isoformat()
    try:
        rows = (
            _thread_select(status=staff_live.ANSWERED)
            .gte("answered_at", since).order("answered_at").execute().data or []
        )
    except Exception:
        logger.exception("staff live: answers lookup failed")
        return {}
    out: dict = {}
    for r in rows:
        if r.get("reply_en"):
            out.setdefault(r["outlet_code"], []).append(
                f"{r.get('slot')}: {r['reply_en']}"
            )
    return out


def _markup(thread_id, slot, facts, language):
    rows = staff_live.keyboard(thread_id, staff_live.button_set(slot, facts), language)
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
         for row in rows]
    )


async def _live_send_or_queue(application, row) -> str:
    """Send a live check-in with its tap-to-answer buttons. Other questions
    may still be open; each expires on its own. Not limited: the 21:05 bills
    question always goes (staff_ops.may_send leaves room for it)."""
    result, facts = row["result"], row.get("facts") or {}
    now = datetime.now(MALAYSIA_TZ)
    question_en = result.get("english") or staff_chat.render_template(
        row.get("message_slot") or row["slot"], "english", facts
    )
    # Other questions may still be open (the 03:00 leftover, an invoice
    # question): each has its own buttons and expires on its own after an hour.
    record = staff_live.thread_row(
        outlet_code=row["outlet_code"], chat_id=row["chat_id"], slot=row["slot"],
        text=result["text"], question_en=question_en,
        facts=row.get("thread_facts") or facts,
        language=row["language"], cashier=row["cashier"], now=now,
        status=staff_live.OPEN,
    )
    inserted = await asyncio.to_thread(
        lambda: supabase.table(staff_live.TABLE).insert(record).execute().data or []
    )
    thread_id = inserted[0]["id"] if inserted else None
    try:
        sent = await application.bot.send_message(
            chat_id=row["chat_id"], text=result["text"],
            reply_markup=_markup(thread_id, row["slot"], facts, row["language"]),
        )
    except Exception:
        if thread_id is not None:
            await asyncio.to_thread(_thread_update, thread_id, {"status": staff_live.DROPPED})
        raise
    if thread_id is not None:
        await asyncio.to_thread(_thread_update, thread_id, {"message_id": sent.message_id})
    return "sent"


async def _release(application, thread) -> None:
    sent = await application.bot.send_message(
        chat_id=thread["chat_id"], text=thread["question_text"],
        reply_markup=_markup(thread["id"], thread.get("slot"), thread.get("facts"),
                             thread.get("language")),
    )
    await asyncio.to_thread(_thread_update, thread["id"], {
        "status": staff_live.OPEN,
        "asked_at": datetime.now(MALAYSIA_TZ).isoformat(),
        "message_id": sent.message_id,
    })


def _recent_handins(today) -> set[tuple[str, str]]:
    """``{(outlet_code, SUPPLIER)}`` whose paper bill was handed to the boss
    in the last 7 days — not asked about again this week."""
    since = (today - timedelta(days=7)).isoformat()
    try:
        rows = (supabase.table("staff_bill_handins").select("outlet_code, supplier")
                .gte("created_at", since).execute().data or [])
    except Exception:
        logger.exception("staff live: hand-in lookup failed")
        return set()
    return {(r.get("outlet_code"), str(r.get("supplier") or "").upper()) for r in rows}


def _record_handin(thread) -> None:
    row = staff_live.handin_row(thread)
    if not row:
        return
    try:
        supabase.table("staff_bill_handins").insert(row).execute()
        logger.info("staff live: bill handed in %s %s", row["outlet_code"], row["supplier"])
    except Exception:
        logger.exception("staff live: hand-in save failed (thread %s)", thread.get("id"))


async def handle_staff_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on a check-in's button: it counts as the answer. "Change" /
    "Problem" / "Something ran out" also ask for the details in text."""
    query = update.callback_query
    parsed = staff_live.parse_callback(query.data if query else None)
    if not parsed:
        return
    thread_id, code = parsed
    rows = await asyncio.to_thread(
        lambda: _thread_select(id=thread_id).limit(1).execute().data or []
    )
    thread = rows[0] if rows else None
    message = query.message
    if not thread or not message or thread.get("chat_id") != message.chat_id:
        with contextlib.suppress(Exception):
            await query.answer()
        return
    language = thread.get("language")
    now = datetime.now(MALAYSIA_TZ)
    outcome = staff_live.tap_outcome(thread, code, now)
    if outcome != "answer":
        with contextlib.suppress(Exception):
            await query.answer(staff_live.thanks_text(language))
            await query.edit_message_reply_markup(reply_markup=None)
        return
    label = next((b.text for row in (message.reply_markup.inline_keyboard
                                      if message.reply_markup else [])
                  for b in row if b.callback_data == query.data), code)
    fields = staff_live.tap_fields(code, label, now)
    await asyncio.to_thread(_thread_update, thread_id, fields)
    thread.update(fields)
    logger.info("staff live: tap %s %s -> %s", thread.get("outlet_code"),
                thread.get("slot"), code)
    if fields["reply_status"] == staff_live.HANDED_IN:
        await asyncio.to_thread(_record_handin, thread)
    if thread.get("slot") == "order" and code == "ok":
        await _save_order_answer(thread, {"status": "ok", "items": []}, fields["reply_text"])
    with contextlib.suppress(Exception):
        await query.answer(staff_live.thanks_text(language))
        await query.edit_message_reply_markup(reply_markup=None)
    prompt = staff_live.detail_prompt(code, language) if fields["awaiting_detail"] else None
    if prompt:
        await context.bot.send_message(chat_id=message.chat_id, text=prompt,
                                       reply_to_message_id=message.message_id,
                                       allow_sending_without_reply=True)


def _detail_thread(chat_id):
    """The last answered question here that is waiting for typed details."""
    rows = (
        _thread_select(chat_id=chat_id, status=staff_live.ANSWERED)
        .eq("awaiting_detail", True)
        .order("answered_at", desc=True).limit(1).execute().data or []
    )
    return rows[0] if rows else None


CLOSED_TABLE = "outlet_closed_days"


def _closed_outlets(day) -> set[str]:
    """Outlets marked closed for ``day`` (/closed). A failed read closes none."""
    try:
        rows = (supabase.table(CLOSED_TABLE).select("outlet_code")
                .eq("day", day.isoformat()).execute().data or [])
    except Exception:
        logger.exception("staff live: closed-days lookup failed")
        return set()
    return {str(r.get("outlet_code") or "").upper() for r in rows}


def _nudge_off_outlets(day) -> set[str]:
    """Outlets whose nudges the director silenced for ``day`` (/nudge_off).
    A failed read silences none."""
    try:
        rows = (supabase.table(staff_nudge.OFF_TABLE).select("outlet_code")
                .eq("day", day.isoformat()).execute().data or [])
    except Exception:
        logger.info("staff live: nudge-off lookup failed (migrations/0057 applied?)")
        return set()
    return {str(r.get("outlet_code") or "").upper() for r in rows}


async def nudge_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /nudge_off SEK20 today — no more follow-up nudges to
    that outlet for the rest of today. The outlet is NOT marked closed: its
    check-ins still go out and still expire. Logged (kind = 'nudge_off')."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    cashier_names.refresh()
    known = set(cashier_names.group_chats().values())
    today = _my_today()
    code, reason = staff_nudge.parse_off_args(context.args, known)
    if code is None:
        if reason == "usage":
            off = await asyncio.to_thread(_nudge_off_outlets, today)
            await message.reply_text(
                f"Nudges off today: {', '.join(sorted(off)) or 'none'}\n"
                "Usage: /nudge_off <OUTLET> today"
            )
        else:
            await message.reply_text(reason)
        return
    by = _command_owner_id(update)
    try:
        await asyncio.to_thread(
            lambda: supabase.table(staff_nudge.OFF_TABLE)
            .upsert(staff_nudge.off_row(code, today, by), on_conflict="outlet_code,day").execute())
    except Exception:
        logger.exception("/nudge_off failed")
        await message.reply_text("Couldn't save that — see logs (is migrations/0057 applied?).")
        return
    await asyncio.to_thread(
        _insert_staff_logs, [staff_nudge.off_log_row(code, today, by, message.chat_id)],
        "nudge off")
    logger.info("staff live: nudges off for %s on %s (by %s)", code, today.isoformat(), by)
    await message.reply_text(
        f"🔕 {code}: no more nudges today ({today.isoformat()}). "
        "Check-ins still go out; the outlet is not marked closed."
    )


async def _send_nudge(application, thread, now) -> None:
    """One follow-up nudge (staff_nudge): AI-worded in the cashier's
    language, fact-checked, plain template on any failure; the thread
    counts it and the log keeps it (kind = 'nudge')."""
    nudge_no = int(thread.get("nudge_count") or 0) + 1
    own = {p.strip() for p in str(thread.get("cashier") or "").split("/")}
    result = await asyncio.to_thread(
        staff_nudge.build, thread, _outlet_label(thread.get("outlet_code")), now, nudge_no,
        other_names=sorted(cashier_names.all_names() - own),
    )
    await application.bot.send_message(
        chat_id=thread["chat_id"], text=result["text"],
        reply_to_message_id=thread.get("message_id"), allow_sending_without_reply=True,
    )
    await asyncio.to_thread(_thread_update, thread["id"], {
        "status": staff_live.REMINDED, "reminded_at": now.isoformat(),
        "nudge_count": nudge_no,
    })
    await asyncio.to_thread(
        _insert_staff_logs, [staff_nudge.log_row(thread, result, "natural")], "staff nudge")
    logger.info("staff live: nudge %d %s %s (%s)", nudge_no, thread.get("outlet_code"),
                thread.get("slot"), result["source"])


async def closed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /closed SEK20 [YYYY-MM-DD] [reason] — mark an outlet
    closed for a day (default today): no nudges go to it. /closed alone
    lists today's closures."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    cashier_names.refresh()
    known = sorted(set(cashier_names.group_chats().values()))
    args = list(context.args or [])
    today = _my_today()
    if not args:
        closed = await asyncio.to_thread(_closed_outlets, today)
        await message.reply_text(
            f"Closed today: {', '.join(sorted(closed)) or 'none'}\n"
            "Usage: /closed <OUTLET> [YYYY-MM-DD] [reason]"
        )
        return
    code = args.pop(0).strip().upper()
    if code not in known:
        await message.reply_text(f"Unknown outlet {code}. Known: " + ", ".join(known))
        return
    day = today
    if args:
        try:
            day = date.fromisoformat(args[0])
            args.pop(0)
        except ValueError:
            pass
    reason = " ".join(args).strip() or None
    row = {"outlet_code": code, "day": day.isoformat(), "reason": reason,
           "marked_by": _command_owner_id(update)}
    try:
        await asyncio.to_thread(
            lambda: supabase.table(CLOSED_TABLE).upsert(row, on_conflict="outlet_code,day").execute())
    except Exception:
        logger.exception("/closed failed")
        await message.reply_text("Couldn't save that — see logs (is migrations/0050 applied?).")
        return
    await message.reply_text(f"✅ {code} marked closed on {day.isoformat()} — no nudges that day.")


async def staff_live_tick(application: Application) -> None:
    """Every 10 min: follow-up nudges (staff_nudge) and the plain reminders,
    no-reply expiry, drop stale queued questions, release the next queued
    question in a quiet group."""
    if staff_chat.style() == staff_chat.CLASSIC or not staff_live.live_outlets():
        return
    try:
        threads = await asyncio.to_thread(
            lambda: supabase.table(staff_live.TABLE).select("*")
            .in_("status", [staff_live.QUEUED, *staff_live.ACTIVE])
            .execute().data or []
        )
    except Exception:
        logger.exception("staff live tick: thread read failed")
        return
    now = datetime.now(MALAYSIA_TZ)
    live = staff_live.live_outlets()
    for t in threads:
        if t.get("outlet_code") not in live and t.get("status") == staff_live.QUEUED:
            await asyncio.to_thread(_thread_update, t["id"], {"status": staff_live.DROPPED})
    threads = [t for t in threads if t.get("outlet_code") in live]
    # No nudges for outlets closed today (/closed) or silenced today
    # (/nudge_off); their questions still expire like everyone else's.
    closed = await asyncio.to_thread(_closed_outlets, now.date())
    silenced = closed | await asyncio.to_thread(_nudge_off_outlets, now.date())
    actions = staff_live.plan_tick(threads, now, silenced)
    for _action, t in [a for a in actions if a[0] == "nudge"]:
        try:
            await _send_nudge(application, t, now)
        except Exception:
            logger.exception("staff live tick: nudge failed (thread %s)", t.get("id"))
    # One reminder message per group, however many questions are waiting.
    for chat_id, due in staff_live.group_reminders(actions).items():
        latest = max(due, key=lambda t: str(t.get("asked_at") or ""))
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=staff_live.reminder_text(latest.get("language"), len(due)),
                reply_to_message_id=latest.get("message_id"),
                allow_sending_without_reply=True,
            )
            for t in due:
                await asyncio.to_thread(_thread_update, t["id"], {
                    "status": staff_live.REMINDED, "reminded_at": now.isoformat()})
            logger.info("staff live: reminded %s (%d question(s))",
                        latest.get("outlet_code"), len(due))
        except Exception:
            logger.exception("staff live tick: reminder failed (chat %s)", chat_id)
    for action, t in actions:
        if action in ("remind", "nudge"):
            continue
        try:
            if action == "expire":
                await asyncio.to_thread(_thread_update, t["id"], {"status": staff_live.NO_REPLY})
            elif action == "drop":
                await asyncio.to_thread(_thread_update, t["id"], {"status": staff_live.DROPPED})
            elif action == "release":
                await _release(application, t)
            logger.info("staff live: %s %s %s", action, t.get("outlet_code"), t.get("slot"))
        except Exception:
            logger.exception("staff live tick: %s failed (thread %s)", action, t.get("id"))


async def handle_staff_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A staff message in a LIVE outlet group while a check-in is open:
    read it, save the answer (or ask once more), then release the next
    queued question. Runs alongside the other handlers (group 1)."""
    message = update.effective_message
    if not message or not message.text or staff_chat.style() == staff_chat.CLASSIC:
        return
    if message.from_user and message.from_user.is_bot:
        return
    code = cashier_names.outlet_for_chat(message.chat_id)
    if not code or not staff_live.is_live(code):
        return
    await _handle_staff_text(message, context, message.text, code)


async def handle_staff_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A voice note in a LIVE outlet group: transcribe it (staff_voice) and
    read it exactly like a typed reply; when that isn't possible, ask the
    cashier to type it. The note is kept in staff_chat_log (kind = 'voice')."""
    message = update.effective_message
    voice = message.voice if message else None
    if not message or not voice or staff_chat.style() == staff_chat.CLASSIC:
        return
    if message.from_user and message.from_user.is_bot:
        return
    code = cashier_names.outlet_for_chat(message.chat_id)
    if not code or not staff_live.is_live(code):
        return
    language = _cashier_language(code)
    # Transcription language: the shift's /lang setting as given; when none
    # is set, no language is sent and Whisper detects it (lang=auto). The
    # wording of replies still uses the outlet default.
    shift, _day = cashier_names.shift_at(datetime.now(MALAYSIA_TZ))
    stt_language = cashier_names.language_for(code, shift, default=None)
    thread = await asyncio.to_thread(_active_thread, message.chat_id,
                                     message.reply_to_message.message_id
                                     if message.reply_to_message else None)
    if staff_voice.too_long(voice.duration):
        transcript = staff_voice.failed("too_long", f"{voice.duration}s", language)
    else:
        try:
            tg_file = await context.bot.get_file(voice.file_id)
            audio = bytes(await tg_file.download_as_bytearray())
        except Exception as exc:
            logger.warning("staff voice: download failed (%s): %s", code, exc)
            transcript = staff_voice.failed("download_error", str(exc)[:200], language)
        else:
            transcript = await asyncio.to_thread(staff_voice.transcribe, audio, stt_language)
    text = staff_voice.accept(transcript)
    await asyncio.to_thread(_insert_staff_logs, [staff_voice.log_row(
        thread, outlet_code=code, chat_id=message.chat_id, language=language,
        file_id=voice.file_id, transcript=transcript, text=text or "", accepted=bool(text),
        duration=voice.duration)],
        "staff voice")
    if not text:
        if thread:
            await message.reply_text(staff_voice.type_instead_text(language))
        why = staff_voice.bounce(transcript) or {"reason": "unknown", "detail": "", "level": "info"}
        log = logger.warning if why["level"] == "warning" else logger.info
        log("staff voice: %s asked to type — %s%s (lang=%s, %ss)", code, why["reason"],
            f" {why['detail']}" if why["detail"] else "",
            (transcript or {}).get("language") or ("auto" if stt_language is None
                                                   else staff_voice.language_code(language)),
            voice.duration)
        return
    logger.info("staff voice: %s transcribed %d chars (lang=%s)", code, len(text),
                (transcript or {}).get("language"))
    await _handle_staff_text(message, context, text, code)


async def voice_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /voice_stats — voice notes this week per outlet:
    transcribed vs bounced, with the bounce reasons."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    today = _my_today()
    week = today - timedelta(days=today.weekday())
    since = datetime.combine(week, datetime.min.time(), MALAYSIA_TZ).isoformat()
    try:
        rows = await asyncio.to_thread(lambda: fetch_all_pages(
            lambda: supabase.table(staff_chat.LOG_TABLE)
            .select("outlet_code, source, facts, problems, created_at")
            .eq("kind", staff_voice.KIND).gte("created_at", since).order("id")))
    except Exception:
        logger.exception("/voice_stats failed")
        await message.reply_text("Couldn't read the voice log — see logs.")
        return
    await _send_chunked_to(context.application, message.chat_id,
                           staff_voice.format_stats(rows, _outlet_label, since=week))


async def _handle_staff_text(message, context, text: str, code: str) -> None:
    """The reply flow for a staff message's words — typed or transcribed."""
    # Honesty: "am I talking to a person?" always gets the true answer —
    # with or without an open question, and it is never taken as an answer.
    if staff_live.asks_if_bot(text):
        await _answer_honestly(message, code)
        return
    now = datetime.now(MALAYSIA_TZ)
    waiting = await asyncio.to_thread(_detail_thread, message.chat_id)
    if staff_live.awaiting_detail(waiting, now):
        await _save_detail(waiting, message, code, text)
        return
    reply_to = message.reply_to_message
    thread = await asyncio.to_thread(
        _active_thread, message.chat_id, reply_to.message_id if reply_to else None)
    if not thread:
        # Nothing is being asked in this group: say so once per
        # NOTHING_OPEN_EVERY so the cashier knows they were heard (a voice
        # note still shows its transcript), then stop — nothing to parse.
        if _prompt_due("nothing_open", message.chat_id, now):
            language = _cashier_language(code)
            await message.reply_text(staff_ack.nothing_open(
                language, transcript=text if getattr(message, "voice", None) else None))
            logger.info("staff live: %s message with no open question", code)
        return
    is_reply = bool(reply_to and reply_to.message_id == thread.get("message_id"))
    parsed = await asyncio.to_thread(
        staff_live.parse_reply, thread.get("question_en"), thread.get("question_text"),
        text, staff_ai.complete_json,
    )
    if parsed and parsed.get("asks_if_bot"):
        await _answer_honestly(message, code)
        return
    action = staff_live.decide_reply(thread, parsed, is_reply_to_question=is_reply)
    now = datetime.now(MALAYSIA_TZ)
    if action == "clarify":
        await message.reply_text(staff_live.clarify_text(thread.get("language")))
        await asyncio.to_thread(_thread_update, thread["id"], {"clarify_sent_at": now.isoformat()})
        return
    if action != "answer":
        # The reader says this is not an answer to the open question: say so
        # and ask which question it is for (at most once per group per
        # UNMATCHED_EVERY, so staff talking among themselves are not pestered).
        if parsed is not None and not parsed.get("is_answer") and _prompt_due("unmatched", message.chat_id, now):
            open_threads = await asyncio.to_thread(_open_threads, message.chat_id)
            await message.reply_text(staff_ack.unmatched(thread.get("language"), open_threads))
            logger.info("staff live: %s message not matched to a question", code)
        return
    await asyncio.to_thread(_thread_update, thread["id"], {
        "status": staff_live.ANSWERED,
        "answered_at": now.isoformat(),
        "reply_text": text,
        "reply_en": (parsed or {}).get("summary_en") or None,
        "reply_status": (parsed or {}).get("status") or "other",
        "reply_clear": bool(parsed and parsed.get("clear")),
        "answer_source": "voice" if message.voice else "text",
    })
    if (parsed or {}).get("status") == staff_live.HANDED_IN:
        await asyncio.to_thread(_record_handin, thread)
    logger.info("staff live: answered %s %s (%s)", code, thread.get("slot"),
                (parsed or {}).get("status"))
    # Order answers with items + quantities become order history, so the
    # drafts learn what this outlet really buys (staff_orders).
    if thread.get("slot") == "order":
        await _save_order_answer(thread, parsed, text)
    if thread.get("slot") == po_mismatch.SLOT:
        await _save_po_explanation(thread, parsed, text)
    await _flag_issue(context.application, thread, parsed, text)
    await _acknowledge(message, thread, parsed, text)
    # One question at a time: the group's next queued check-in goes now.
    await staff_live_tick(context.application)


# "Which question is that for?" and "nothing is being asked right now" go to a
# group at most once per window each, so staff chatter is not answered
# message by message.
UNMATCHED_EVERY = NOTHING_OPEN_EVERY = timedelta(minutes=10)
_prompt_last: dict = {}


def _prompt_due(kind: str, chat_id, now) -> bool:
    last = _prompt_last.get((kind, chat_id))
    if last is not None and now - last < UNMATCHED_EVERY:
        return False
    _prompt_last[(kind, chat_id)] = now
    return True


def _unmatched_due(chat_id, now) -> bool:      # kept for older call sites / tests
    return _prompt_due("unmatched", chat_id, now)


def _open_threads(chat_id) -> list[dict]:
    """The questions still open in a group, oldest first."""
    return (_thread_select(chat_id=chat_id).in_("status", list(staff_live.ACTIVE))
            .order("asked_at").limit(5).execute().data or [])


async def _acknowledge(message, thread, parsed, text) -> None:
    """One line back in the cashier's language saying what was understood
    (staff_ack); a voice note also gets its transcript so it can be
    corrected. Never breaks the reply flow."""
    try:
        ack = staff_ack.acknowledgement(
            parsed, thread.get("language"), slot=thread.get("slot"),
            transcript=text if getattr(message, "voice", None) else None,
            vocabulary=staff_chat.item_vocabulary(),
        )
        await message.reply_text(ack)
    except Exception:
        logger.exception("staff ack: failed (thread %s)", thread.get("id"))


async def _save_detail(thread, message, code, text=None) -> None:
    """Typed (or spoken) details after a "Change" / "Problem" tap: read them,
    add them to that answer, and learn any order items."""
    text = text if text is not None else message.text
    parsed = await asyncio.to_thread(
        staff_live.parse_reply, thread.get("question_en"), thread.get("question_text"),
        text, staff_ai.complete_json,
    )
    await asyncio.to_thread(_thread_update, thread["id"], staff_live.detail_fields(
        thread, text, (parsed or {}).get("summary_en")))
    if thread.get("slot") == "order":
        await _save_order_answer(thread, parsed, text, details=True)
    await _flag_issue(context.application, thread, parsed, text,
                      force=thread.get("reply_status") == "problem")
    await _acknowledge(message, thread, parsed, text)
    logger.info("staff live: details for %s %s", code, thread.get("slot"))


async def _save_order_answer(thread, parsed, text, *, details: bool = False) -> None:
    """What an order reply means for tomorrow's order. With a proposal in
    the thread (order_proposal): "ok" saves every line as confirmed; items
    in the reply are applied as edits and the whole order is saved. Without
    one (the open "what do you need?" question) only the items are saved."""
    code = thread.get("outlet_code")
    items = (parsed or {}).get("items") or []
    proposal = (thread.get("facts") or {}).get("proposal") or []
    status = (parsed or {}).get("status")
    if not proposal:
        if items:
            rows = staff_orders.rows_for_reply(thread, items, text)
            saved = await asyncio.to_thread(staff_orders.save, supabase, rows)
            logger.info("staff live: saved %d order items for %s", saved, code)
        return
    lines, changed = order_proposal.apply_edits(proposal, items)
    confirmed = status == "ok" or bool(items) or (details and bool(changed))
    if not confirmed:
        return
    rows = order_proposal.order_rows(thread, lines, text, confirmed=True)
    saved = await asyncio.to_thread(staff_orders.save, supabase, rows)
    logger.info("staff live: order %s for %s saved (%d lines, %d changed: %s)",
                "corrected" if changed else "confirmed", code, saved, len(changed),
                ", ".join(changed) or "-")


async def _flag_issue(application, thread, parsed, text, *, force: bool = False) -> None:
    """Save the issue a reply reports (staff_issues); forward an urgent one to
    the director chat at once. Never breaks the reply flow."""
    try:
        issue = staff_issues.from_reply(parsed, text, force=force)
        if not issue:
            return
        row = staff_issues.row(thread, issue, text, datetime.now(MALAYSIA_TZ))
        try:
            inserted = await asyncio.to_thread(
                lambda: supabase.table(staff_issues.TABLE).insert(row).execute().data or [])
            if inserted:
                row = inserted[0]
        except Exception:
            logger.exception("staff issues: save failed (is migrations/0051 applied?)")
        logger.info("staff issues: %s %s %s%s", thread.get("outlet_code"), issue["type"],
                    issue.get("summary_en"), " URGENT" if issue.get("urgent") else "")
        if issue.get("urgent"):
            await _send_chunked_to(application, ALERT_CHAT_ID,
                                   staff_issues.urgent_text(row, _outlet_label))
    except Exception:
        logger.exception("staff issues: flagging failed (thread %s)", thread.get("id"))


async def issues_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /issues — every open staff issue, urgent first."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    rows = await asyncio.to_thread(_open_issues)
    await _send_chunked_to(context.application, message.chat_id,
                           staff_issues.format_open(rows, _outlet_label))


async def resolve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /resolve <id> — close one staff issue."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    arg = (context.args[0].strip().lstrip("#") if context.args else "")
    if not arg.isdigit():
        await message.reply_text("Usage: /resolve <id>  (see /issues)")
        return
    fields = {"resolved_at": datetime.now(MALAYSIA_TZ).isoformat(),
              "resolved_by": _command_owner_id(update)}
    try:
        changed = await asyncio.to_thread(
            lambda: supabase.table(staff_issues.TABLE).update(fields)
            .eq("id", int(arg)).is_("resolved_at", "null").execute().data or [])
    except Exception:
        logger.exception("/resolve failed")
        await message.reply_text("Couldn't update that issue — see logs.")
        return
    if not changed:
        await message.reply_text(f"Issue #{arg} not found or already resolved.")
        return
    r = changed[0]
    await message.reply_text(f"✅ Resolved #{arg}: {_outlet_label(r.get('outlet_code'))} · "
                             f"{r.get('type')} — {r.get('summary_en') or r.get('raw_reply')}")


async def _answer_honestly(message, code) -> None:
    shift, _day = cashier_names.shift_at(datetime.now(MALAYSIA_TZ))
    language = cashier_names.language_for(code, shift)
    await message.reply_text(staff_live.honest_reply(language))
    logger.info("staff live: answered 'are you a bot?' honestly in %s", code)


# === Staff questions v2 (staff_ops) ==========================================
# Cost, wastage and food quality in the live outlet groups: 03:00 leftover,
# 09:00 sales note, 10:30 wastage, 16:00 taste check or tip, invoice and mini
# market questions on upload, Monday praise. Max 5 staff messages per group
# per day (staff_ops.may_send). Each slot can be paused with STAFF_CHAT_SLOTS.

def _ops_db() -> Client:
    """A fresh client per scheduled run: the shared one is not safe to use
    from two worker threads at once (see _sales_supabase)."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _ops_ready(slot) -> bool:
    return (staff_chat.style() != staff_chat.CLASSIC
            and bool(staff_live.live_outlets()) and staff_live.slot_enabled(slot))


def _outlet_label(code) -> str:
    import outlet_mapping
    return "D.U" if str(code).upper() == "DAMANSARA" else outlet_mapping.outlet_display_name(code)


def _ops_groups() -> list[tuple[int, str]]:
    """``[(chat_id, registry_code)]`` of the live outlet groups."""
    cashier_names.refresh()
    return [(chat_id, code) for chat_id, code in
            sorted(cashier_names.group_chats().items(), key=lambda kv: kv[1])
            if staff_live.is_live(code)]


def _ops_threads_since(db, since_iso, columns="*"):
    return fetch_all_pages(
        lambda: db.table(staff_live.TABLE).select(columns)
        .gte("created_at", since_iso).order("id")
    )


def _ops_counts(db, code, now) -> tuple[int, int]:
    since = datetime.combine(now.date(), datetime.min.time(), MALAYSIA_TZ).isoformat()
    rows = (db.table(staff_live.TABLE).select("outlet_code, slot, status, asked_at")
            .eq("outlet_code", code).gte("asked_at", since).execute().data or [])
    return staff_ops.count_today(rows, code, now.date(), MALAYSIA_TZ)


async def _ops_send(application, db, *, code, chat_id, slot, text, question_en,
                    facts, info=False, record_if_capped=False, reply_to=None) -> str:
    """Send one staff_ops message to a live group (with its buttons), within
    the daily limit. Returns sent / capped / failed."""
    now = datetime.now(MALAYSIA_TZ)
    shift, _day = cashier_names.shift_at(now)
    language = cashier_names.language_for(code, shift)
    cashier = cashier_names.name_for(code, shift)
    sent_today, events = await asyncio.to_thread(_ops_counts, db, code, now)
    if not staff_ops.may_send(slot, sent_today, events):
        logger.info("staff ops: %s %s not sent (daily limit: %d sent, %d events)",
                    code, slot, sent_today, events)
        if record_if_capped:
            record = staff_live.thread_row(
                outlet_code=code, chat_id=chat_id, slot=slot, text=text,
                question_en=question_en, facts={**facts, "not_asked": True},
                language=language, cashier=cashier, now=now, status=staff_live.DROPPED)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    lambda: db.table(staff_live.TABLE).insert(record).execute())
        return "capped"
    record = staff_live.thread_row(
        outlet_code=code, chat_id=chat_id, slot=slot, text=text, question_en=question_en,
        facts=facts, language=language, cashier=cashier, now=now,
        status=staff_live.INFO if info else staff_live.OPEN)
    record["asked_at"] = now.isoformat()
    inserted = await asyncio.to_thread(
        lambda: db.table(staff_live.TABLE).insert(record).execute().data or [])
    thread_id = inserted[0]["id"] if inserted else None
    markup = None if info else _markup(thread_id, slot, facts, language)
    try:
        sent = await application.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup,
            reply_to_message_id=reply_to, allow_sending_without_reply=True)
    except Exception:
        logger.exception("staff ops: send failed (%s %s)", code, slot)
        if thread_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(lambda: db.table(staff_live.TABLE).update(
                    {"status": staff_live.DROPPED}).eq("id", thread_id).execute())
        return "failed"
    if thread_id is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(lambda: db.table(staff_live.TABLE).update(
                {"message_id": sent.message_id}).eq("id", thread_id).execute())
    logger.info("staff ops: sent %s %s", code, slot)
    return "sent"


def _cashier_language(code, now=None) -> str:
    shift, _day = cashier_names.shift_at(now or datetime.now(MALAYSIA_TZ))
    return cashier_names.language_for(code, shift)


# --- sales data -------------------------------------------------------------

def _summary_items(db, summary_ids) -> dict:
    """``{summary_id: {ITEM: qty}}`` from the POS full-day item list."""
    out: dict = {}
    ids = list(summary_ids)
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        rows = fetch_all_pages(
            lambda: db.table("sales_daily_itemwise").select("summary_id, item_name, qty")
            .in_("summary_id", chunk).order("id"))
        for r in rows:
            name = str(r.get("item_name") or "").strip().upper()
            if name:
                d = out.setdefault(r["summary_id"], {})
                d[name] = d.get(name, 0.0) + float(r.get("qty") or 0)
    return out


def _full_day_counts(db, code, dates) -> dict:
    """``{date: items sold}`` from the POS full-day (D) reports."""
    rows = (db.table("sales_daily_summary").select("id, business_date")
            .eq("outlet_code", f"D-{code}")
            .in_("business_date", [d.isoformat() for d in dates])
            .execute().data or [])
    items = _summary_items(db, [r["id"] for r in rows])
    out: dict = {}
    for r in rows:
        d = date.fromisoformat(str(r["business_date"])[:10])
        out[d] = out.get(d, 0.0) + sum(items.get(r["id"], {}).values())
    return {d: v for d, v in out.items() if v > 0}


def _day_shift_counts(db, code, dates) -> dict:
    """``{date: items sold}`` from the day-shift (S) reports — the latest
    report per day when the POS sent one twice."""
    rows = (db.table("sales_daily").select("id, shift_business_date")
            .eq("outlet_code", f"S-{code}").eq("shift_type", "day")
            .in_("shift_business_date", [d.isoformat() for d in dates])
            .execute().data or [])
    latest: dict = {}
    for r in rows:
        d = date.fromisoformat(str(r["shift_business_date"])[:10])
        if d not in latest or r["id"] > latest[d]:
            latest[d] = r["id"]
    if not latest:
        return {}
    ids = list(latest.values())
    items = fetch_all_pages(
        lambda: db.table("sales_items").select("sales_daily_id, qty")
        .in_("sales_daily_id", ids).order("id"))
    per_id: dict = {}
    for r in items:
        per_id[r["sales_daily_id"]] = per_id.get(r["sales_daily_id"], 0.0) + float(r.get("qty") or 0)
    return {d: per_id.get(i, 0.0) for d, i in latest.items() if per_id.get(i)}


def _sales_note(db, code, today) -> dict | None:
    """Yesterday vs the usual same weekday. The full-day report when it is
    already in (by 09:00), else the day-shift report."""
    yesterday = today - timedelta(days=1)
    dates = [yesterday] + [yesterday - timedelta(weeks=k) for k in range(1, 7)]
    full_day = True
    counts = _full_day_counts(db, code, dates)
    if yesterday not in counts:
        full_day = False
        counts = _day_shift_counts(db, code, dates)
    if yesterday not in counts:
        return None
    signal = staff_ops.sales_signal(counts[yesterday],
                                    [v for d, v in counts.items() if d != yesterday])
    if not signal:
        return None
    return {**signal, "full_day": full_day, "weekday": yesterday.weekday(),
            "date": yesterday.isoformat()}


def _item_drop_for(db, code, today) -> dict | None:
    """The one menu item selling clearly less (full-day reports, ~3 weeks)."""
    start = today - timedelta(days=26)
    rows = (db.table("sales_daily_summary").select("id, business_date")
            .eq("outlet_code", f"D-{code}").gte("business_date", start.isoformat())
            .execute().data or [])
    if not rows:
        return None
    latest = max(date.fromisoformat(str(r["business_date"])[:10]) for r in rows)
    if (today - latest).days > 4:
        return None     # POS reports stale — nothing trustworthy to say
    items = _summary_items(db, [r["id"] for r in rows])
    daily: dict = {}
    for r in rows:
        d = str(r["business_date"])[:10]
        day = daily.setdefault(d, {})
        for name, qty in items.get(r["id"], {}).items():
            day[name] = day.get(name, 0.0) + qty
    since = (today - timedelta(days=staff_ops.DROP_REPEAT_DAYS)).isoformat()
    asked = (db.table(staff_live.TABLE).select("facts")
             .eq("outlet_code", code).eq("slot", "afternoon").gte("created_at", since)
             .execute().data or [])
    recent = [(a.get("facts") or {}).get("item") for a in asked]
    return staff_ops.item_drop(daily, asked_recently=[r for r in recent if r])


# --- scheduled slots ----------------------------------------------------------

async def run_staff_ops(application: Application, slot: str) -> None:
    """03:00 leftover · 09:00 sales · 10:30 wastage · 16:00 afternoon ·
    Mon 11:05 praise, for every live outlet group."""
    if not _ops_ready(slot):
        logger.info("staff ops %s: off (style / live outlets / STAFF_CHAT_SLOTS)", slot)
        return
    today = _my_today()
    db = await asyncio.to_thread(_ops_db)
    groups = await asyncio.to_thread(_ops_groups)
    praise = {}
    if slot == "praise":
        since = datetime.combine(today - timedelta(days=7), datetime.min.time(),
                                 MALAYSIA_TZ).isoformat()
        week = await asyncio.to_thread(_ops_threads_since, db, since)
        praise = staff_ops.weekly_winners(week, [c for _chat, c in groups], _outlet_label)
        if not praise:
            logger.info("staff ops praise: no winners this week")
            return
    filled: set = set()
    if slot == "leftover":
        # The 02:00 kitchen form ("Rekod Baki") already asks for leftovers;
        # the 03:00 question only goes to groups that didn't fill it in.
        filled = await asyncio.to_thread(_left_form_filled, db, today - timedelta(days=1))
    outcomes: dict = {}
    for chat_id, code in groups:
        if chat_id in filled:
            outcomes[code] = "02:00 form filled"
            continue
        language = _cashier_language(code)
        try:
            msg = await asyncio.to_thread(_ops_message, db, slot, code, today, language, praise)
        except Exception:
            logger.exception("staff ops: %s facts failed for %s", slot, code)
            outcomes[code] = "failed"
            continue
        if not msg:
            outcomes[code] = "nothing"
            continue
        outcomes[code] = await _ops_send(application, db, code=code, chat_id=chat_id,
                                         slot=slot, **msg)
    logger.info("staff ops %s: %s", slot,
                ", ".join(f"{c} {o}" for c, o in sorted(outcomes.items())))


def _left_form_filled(db, business_date) -> set:
    """Chats that submitted the 02:00 leftover form for ``business_date``
    (at 03:00 that is yesterday's date). A failed read asks everyone."""
    try:
        rows = (db.table(kitchen_usage.SESSION_TABLE).select("chat_id")
                .eq("phase", kitchen_usage.PHASE_LEFT)
                .eq("business_date", business_date.isoformat())
                .in_("status", ["submitted", "submitting"])
                .execute().data or [])
    except Exception:
        logger.exception("staff ops: 02:00 form lookup failed")
        return set()
    return {r.get("chat_id") for r in rows}


def _ops_message(db, slot, code, today, language, praise) -> dict | None:
    """``{text, question_en, facts, info}`` for one group, or None."""
    if slot == "leftover":
        return {"text": staff_ops.leftover_text(today, language),
                "question_en": staff_ops.leftover_text(today, "english"), "facts": {}}
    if slot == "wastage":
        return {"text": staff_ops.wastage_text(language),
                "question_en": staff_ops.wastage_text("english"), "facts": {}}
    if slot == "sales":
        note = _sales_note(db, code, today)
        if not note:
            return None
        args = (note, note["weekday"], )
        # The count and the usual stay out of the stored facts too.
        facts = {k: v for k, v in note.items() if k not in ("count", "usual")}
        return {"text": staff_ops.sales_text(*args, language, full_day=note["full_day"]),
                "question_en": staff_ops.sales_text(*args, "english", full_day=note["full_day"]),
                "facts": facts, "info": True}
    if slot == "afternoon":
        drop = _item_drop_for(db, code, today)
        if drop:
            facts = {k: drop[k] for k in ("item", "label", "drop_pct", "shop_pct")}
            return {"text": staff_ops.itemdrop_text(drop["label"], language),
                    "question_en": staff_ops.itemdrop_text(drop["label"], "english"),
                    "facts": facts, "record_if_capped": True}
        return {"text": staff_ops.tip_text(today, language),
                "question_en": staff_ops.tip_text(today, "english"),
                "facts": {"tip": staff_ops.tip_index(today)}, "info": True}
    if slot == "praise":
        return {"text": staff_ops.praise_text(praise, language),
                "question_en": staff_ops.praise_text(praise, "english"),
                "facts": praise, "info": True}
    return None


# --- on upload: invoice check and mini market ----------------------------------

def _invoice_inputs(db, receipt_id, chat_id, receipt_date):
    """This invoice's lines and the outlet's other purchases (8 weeks)."""
    lines_rows = (db.table("item_prices").select("canonical_item, qty")
                  .eq("receipt_id", receipt_id).execute().data or [])
    lines: dict = {}
    for r in lines_rows:
        item = r.get("canonical_item")
        if item:
            lines[item] = lines.get(item, 0.0) + float(r.get("qty") or 0)
    rdate = date.fromisoformat(str(receipt_date)[:10])
    start = (rdate - timedelta(weeks=8)).isoformat()
    history = fetch_all_pages(
        lambda: db.table("item_prices")
        .select("receipt_id, receipt_date, merchant, canonical_item, qty")
        .eq("chat_id", chat_id).gte("receipt_date", start)
        .lte("receipt_date", rdate.isoformat()).neq("receipt_id", receipt_id).order("id"))
    days = {str(h.get("receipt_date"))[:10] for h in history}
    return ([{"canonical_item": k, "qty": v} for k, v in lines.items()], history, len(days))


def _po_lines(code, day) -> list[dict]:
    """The purchase order for ``day``: what the cashier confirmed for it
    (staff_order_items), else the saved order draft. ``[{item, qty, unit}]``."""
    codes = staff_chat.data_codes(code)
    rows = (supabase.table(staff_orders.TABLE).select("canonical_item, qty, unit, created_at")
            .in_("outlet_code", codes).eq("order_for", day.isoformat())
            .not_.is_("canonical_item", "null").order("created_at").execute().data or [])
    if rows:
        latest: dict = {}
        for r in rows:
            latest[str(r["canonical_item"]).lower()] = r
        return [{"item": item, "qty": r.get("qty"), "unit": r.get("unit") or ""}
                for item, r in latest.items()]
    drafts = (supabase.table("order_drafts").select("item, qty, pack")
              .in_("outlet", codes).eq("due_date", day.isoformat())
              .neq("status", "cancelled").execute().data or [])
    return [{"item": str(d["item"]).lower(), "qty": d.get("qty"), "unit": d.get("pack") or ""}
            for d in drafts if d.get("item") and d.get("qty")]


def _usual_prices(code, merchant, day, items) -> dict:
    """``{item: usual unit price}`` this outlet paid the same supplier over 8
    weeks (median), for the items on the bill."""
    if not items:
        return {}
    codes = staff_chat.data_codes(code)
    rows = (supabase.table("item_prices").select("canonical_item, unit_price, merchant")
            .in_("outlet_code", codes).in_("canonical_item", list(items))
            .gte("receipt_date", (day - timedelta(weeks=8)).isoformat())
            .lt("receipt_date", day.isoformat()).limit(2000).execute().data or [])
    same = [r for r in rows if str(r.get("merchant") or "").upper() == str(merchant or "").upper()]
    use = same or rows
    per: dict = {}
    for r in use:
        try:
            price = float(r.get("unit_price"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            per.setdefault(str(r["canonical_item"]).lower(), []).append(price)
    import statistics
    return {item: statistics.median(v) for item, v in per.items() if len(v) >= 2}


def _po_mismatches(code, stored) -> list[dict]:
    """The bill's lines against the order for its date. ``[]`` when there is
    no order for that day or nothing differs."""
    try:
        day = date.fromisoformat(str(stored.get("receipt_date"))[:10])
    except (TypeError, ValueError):
        return []
    order = _po_lines(code, day)
    if not order:
        return []
    bill = po_mismatch.receipt_lines(stored.get("items"))
    usual = _usual_prices(code, stored.get("merchant"), day, [b["item"] for b in bill])
    return po_mismatch.compare(bill, order, usual)


async def _save_po_explanation(thread, parsed, text) -> None:
    """The cashier's answer to a bill-vs-order question, on the receipt row."""
    receipt_id = (thread.get("facts") or {}).get("receipt_id")
    if receipt_id is None:
        return
    fields = {"po_explanation": po_mismatch.explanation(parsed, text),
              "po_explained_at": datetime.now(MALAYSIA_TZ).isoformat()}
    try:
        await asyncio.to_thread(
            lambda: supabase.table(RECEIPTS_TABLE).update(fields).eq("id", receipt_id).execute())
        logger.info("po mismatch: explanation saved for receipt %s", receipt_id)
    except Exception:
        logger.exception("po mismatch: explanation save failed (migrations/0053 applied?)")


def _already_asked(db, receipt_id) -> bool:
    rows = (db.table(staff_live.TABLE).select("id")
            .eq("facts->>receipt_id", str(receipt_id)).limit(1).execute().data or [])
    return bool(rows)


def _fresh_receipt(receipt_date, days: int = 3) -> bool:
    try:
        d = date.fromisoformat(str(receipt_date)[:10])
    except ValueError:
        return False
    return (_my_today() - d).days <= days


async def staff_ops_on_upload(application, stored: dict, message, *, supplier: bool,
                              candidates: dict | None = None) -> None:
    """After a bill is saved in a live outlet group: a mini market receipt
    gets "why from the mini market?"; a supplier invoice far above the
    outlet's usual (or a rarely bought item) gets "why?"; a normal supplier
    invoice needs nothing more (handle_photo already reacted 👌). Never
    breaks the receipt pipeline."""
    try:
        code = cashier_names.outlet_for_chat(message.chat_id)
        receipt_id = stored.get("id")
        if not code or not staff_live.is_live(code) or receipt_id is None:
            return
        if staff_chat.style() == staff_chat.CLASSIC:
            return
        merchant = stored.get("merchant")
        if not _fresh_receipt(stored.get("receipt_date")):
            return
        db = supabase
        if staff_ops.is_minimarket(merchant):
            if not staff_live.slot_enabled("minimarket"):
                return
            if await asyncio.to_thread(_already_asked, db, receipt_id):
                return
            names = [i.get("name") or i.get("description") or ""
                     for i in (stored.get("items") or []) if isinstance(i, dict)]
            shop = staff_ops.shop_name(merchant)
            items = staff_ops.minimarket_items(names)
            language = _cashier_language(code)
            await _ops_send(
                application, db, code=code, chat_id=message.chat_id, slot="minimarket",
                text=staff_ops.minimarket_text(shop, items, language),
                question_en=staff_ops.minimarket_text(shop, items, "english"),
                facts={"receipt_id": receipt_id, "shop": shop, "items": items},
                record_if_capped=True, reply_to=message.message_id)
            return
        if not supplier:
            return
        if await asyncio.to_thread(_already_asked, db, receipt_id):
            return
        # The bill against the order for that day (po_mismatch): the lines
        # that differ are the bill's one question, ahead of the invoice checks.
        if staff_live.slot_enabled(po_mismatch.SLOT):
            mismatches = await asyncio.to_thread(_po_mismatches, code, stored)
            if mismatches:
                supplier_name = (staff_chat._short_supplier(merchant)
                                 or str(merchant or "").title() or "supplier")
                language = _cashier_language(code)
                po_facts = po_mismatch.facts(receipt_id, supplier_name, mismatches)
                await _ops_send(
                    application, db, code=code, chat_id=message.chat_id, slot=po_mismatch.SLOT,
                    text=po_mismatch.question(mismatches, supplier_name, language),
                    question_en=po_mismatch.question(mismatches, supplier_name, "english"),
                    facts=po_facts, record_if_capped=True, reply_to=message.message_id)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        lambda: db.table(RECEIPTS_TABLE).update({"po_mismatch": mismatches})
                        .eq("id", receipt_id).execute())
                return
        if not staff_live.slot_enabled("invoice"):
            return
        lines, history, outlet_days = await asyncio.to_thread(
            _invoice_inputs, db, receipt_id, message.chat_id, stored.get("receipt_date"))
        flag = (staff_ops.invoice_flag(lines, history, receipt_date=stored.get("receipt_date"),
                                       merchant=merchant, outlet_days=outlet_days)
                if lines else None)
        flag = staff_ops.upload_question(flag, candidates)
        if not flag:
            return      # the 👌 on the photo (handle_photo) already confirms it
        import order_items
        supplier_name = staff_chat._short_supplier(merchant) or str(merchant or "").title()
        language = _cashier_language(code)
        item = flag.get("item") or ""
        facts = {"receipt_id": receipt_id, "kind": flag["kind"], "item": item,
                 "item_label": flag.get("label") or (order_items.display_name(item) if item else ""),
                 "supplier": supplier_name}
        if flag["kind"] in ("high", "rare"):
            facts.update(qty_text=staff_ops.qty_text(flag["qty"], item),
                         usual_text=staff_ops.qty_text(flag.get("usual") or 0, item))
        if flag.get("percent"):
            facts["percent"] = flag["percent"]
        await _ops_send(
            application, db, code=code, chat_id=message.chat_id, slot="invoice",
            text=staff_ops.invoice_text(flag, supplier_name, language),
            question_en=staff_ops.invoice_text(flag, supplier_name, "english"),
            facts=facts, record_if_capped=True, reply_to=message.message_id)
    except Exception:
        logger.exception("staff ops: upload check failed (receipt %s)", stored.get("id"))


# --- /draft <OUTLET> --------------------------------------------------------------

def _draft_text(code, today) -> str:
    codes = staff_chat.data_codes(code)
    due = today + timedelta(days=1)
    rows = (supabase.table("order_drafts").select("item, qty, pack, supplier, outlet, due_date")
            .in_("outlet", codes).eq("due_date", due.isoformat()).execute().data or [])
    saved = bool(rows)
    if not rows:
        rows = _unsaved_order_items(today).get_codes(codes)
    if not rows:
        return f"No order draft for {_outlet_label(code)} for {due.isoformat()}."
    by_supplier: dict = {}
    for r in rows:
        by_supplier.setdefault(r.get("supplier") or "—", []).append(r)
    lines = [f"🧾 Order draft — {_outlet_label(code)} for {due.isoformat()}"
             + ("" if saved else " (not saved yet)"), ""]
    for sup in sorted(by_supplier):
        lines.append(f"{sup}:")
        for r in by_supplier[sup]:
            qty = staff_chat.fmt_qty(r.get("qty"), None) or r.get("qty")
            pack = f" {r['pack']}" if r.get("pack") else ""
            lines.append(f"  • {r.get('item')} {qty}{pack}")
    return "\n".join(lines)


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /order SEK20 — tomorrow's proposed order for one outlet
    (median of the last 4 same-weekday buys, order_proposal)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    cashier_names.refresh()
    known = sorted(set(cashier_names.group_chats().values()))
    code = (context.args[0].strip().upper() if context.args else "")
    if code not in known:
        await message.reply_text("Usage: /order <outlet>\n" + ", ".join(known))
        return
    today = _my_today()
    try:
        lines = await asyncio.to_thread(_order_proposal_lines, code, today)
    except Exception:
        logger.exception("/order failed for %s", code)
        await message.reply_text("Couldn't build the proposal — see logs.")
        return
    await _send_chunked_to(context.application, message.chat_id, order_proposal.format_director(
        _outlet_label(code), today + timedelta(days=1), lines))


async def draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /draft SEK20 — one outlet's full order draft."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    cashier_names.refresh()
    known = sorted(set(cashier_names.group_chats().values()))
    code = (context.args[0].strip().upper() if context.args else "")
    if code not in known:
        await message.reply_text("Usage: /draft <outlet>\n" + ", ".join(known))
        return
    try:
        text = await asyncio.to_thread(_draft_text, code, _my_today())
    except Exception:
        logger.exception("/draft failed for %s", code)
        await message.reply_text("Couldn't read the draft — see logs.")
        return
    await _send_chunked_to(context.application, message.chat_id, text)


async def post_staff_morning_summary(application: Application) -> None:
    """08:30: the director's daily replies line per live outlet, what went
    unanswered, and the problems staff reported — last 24 hours."""
    if staff_chat.style() == staff_chat.CLASSIC or not staff_live.live_outlets():
        return
    since = (datetime.now(MALAYSIA_TZ) - timedelta(hours=24)).isoformat()
    try:
        threads = await asyncio.to_thread(
            lambda: supabase.table(staff_live.TABLE).select("*")
            .gte("created_at", since).execute().data or []
        )
    except Exception:
        logger.exception("staff morning summary: read failed")
        return
    text = staff_live.format_morning_summary(threads, _outlet_label)
    if text and datetime.now(MALAYSIA_TZ).weekday() == 0:
        week_since = (datetime.now(MALAYSIA_TZ) - timedelta(days=7)).isoformat()
        try:
            week = await asyncio.to_thread(
                lambda: supabase.table(staff_live.TABLE).select("*")
                .eq("slot", "minimarket").gte("created_at", week_since)
                .execute().data or []
            )
            text += "\n".join([""] + staff_ops.weekly_minimarket(week, _outlet_label)) \
                if week else ""
        except Exception:
            logger.exception("staff morning summary: weekly mini market read failed")
    if text:
        await _send_chunked_to(application, ALERT_CHAT_ID, text)


ISSUES_TABLE = "staff_issues"


def _open_issues(since_iso=None) -> list[dict]:
    """Open staff_issues rows (migrations/0051), newest last. ``[]`` until the
    table exists or on any failure."""
    try:
        q = supabase.table(ISSUES_TABLE).select("*").is_("resolved_at", "null")
        if since_iso:
            q = q.gte("created_at", since_iso)
        return q.order("id").execute().data or []
    except Exception:
        logger.info("staff issues: read failed (table missing?)")
        return []


def _gather_night_digest(today) -> tuple[dict, dict]:
    """Facts for the 23:30 digest from today's threads and open issues."""
    since = datetime.combine(today, datetime.min.time(), MALAYSIA_TZ).isoformat()
    threads = _ops_threads_since(supabase, since)
    outlets = {code: _outlet_label(code) for _chat, code in _ops_groups()}
    issues = [{"outlet": _outlet_label(i.get("outlet_code")), "type": i.get("type"),
               "summary_en": i.get("summary_en"), "urgent": i.get("urgent")}
              for i in _open_issues(since)]
    return staff_digest.gather(threads, outlets, issues, day=today), outlets


async def post_staff_night_digest(application: Application, *, force: bool = False) -> None:
    """23:30: the director's plain-English digest of the day's staff replies,
    ordered by concern (staff_digest). AI-worded, fact-checked line by line,
    plain list on any failure."""
    if not force and (staff_chat.style() == staff_chat.CLASSIC or not staff_live.live_outlets()):
        return
    today = _my_today()
    try:
        facts, outlets = await asyncio.to_thread(_gather_night_digest, today)
    except Exception:
        logger.exception("staff digest: gather failed")
        with contextlib.suppress(Exception):
            await application.bot.send_message(
                chat_id=ALERT_CHAT_ID, text="⚠️ Staff digest failed — see logs.")
        return
    result = await asyncio.to_thread(
        staff_digest.build, facts, all_labels=list(outlets.values()),
        vocabulary=staff_chat.item_vocabulary(),
    )
    await asyncio.to_thread(_insert_staff_logs, [staff_digest.log_row(facts, result)],
                            "staff digest")
    logger.info("staff digest: %s (%d problem(s))", result["source"], len(result["problems"]))
    if not result["text"]:
        if force:
            await application.bot.send_message(chat_id=ALERT_CHAT_ID,
                                               text="No staff check-ins went out today.")
        return
    await _send_chunked_to(application, ALERT_CHAT_ID, result["text"])


async def staff_digest_now_command(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /staff_digest_now — today's digest, right now."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await post_staff_night_digest(context.application, force=True)


def _build_tamil_samples(n, today, slot=None, languages=("tamil",)):
    """``n`` check-ins across slots (or just ``slot``) and outlets, real
    facts, full checks — for the director to review. Languages take turns.
    Logged with mode 'sample'."""
    cashier_names.refresh()
    groups = sorted(cashier_names.group_chats().items(), key=lambda kv: kv[1])
    slots = [slot] if slot else list(staff_chat.SLOTS)
    vocabulary = staff_chat.item_vocabulary()
    names = cashier_names.all_names()
    bills_by_chat: dict = {}
    for entry in _gather_missing_bills(today=today)["entries"]:
        bills_by_chat.setdefault(entry["chat_id"], []).append(entry)
    rows, logs, tries = [], [], 0
    while len(rows) < n and tries < n * 4 and groups:
        slot = slots[tries % len(slots)]
        chat_id, code = groups[(tries // len(slots) + tries) % len(groups)]
        tries += 1
        try:
            facts = _staff_slot_facts(slot, code, chat_id, today, bills_by_chat)
        except Exception:
            logger.exception("staff samples: facts failed (%s %s)", slot, code)
            continue
        if facts is None:
            continue
        facts.pop("_proposal", None)
        cashier = cashier_names.name_for(code, staff_chat.SLOTS[slot][0])
        own = {p.strip() for p in cashier.split("/")}
        language = languages[len(rows) % len(languages)]
        result = staff_chat.build_message(
            slot, language, facts,
            seed=staff_chat.seed_for(slot, code, today) + f"-s{tries}",
            vocabulary=vocabulary, other_names=sorted(names - own),
        )
        rows.append({"slot": slot, "outlet_code": code, "cashier": cashier,
                     "language": language, "result": result})
        logs.append(staff_chat.log_row(
            slot, code, chat_id, cashier, language, facts, result, "sample"
        ))
    _insert_staff_logs(logs, "staff samples")
    return rows


async def staff_samples_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: /staff_samples [n] [check-in] — n (default 10) Tamil
    check-ins with back-translations, to review the wording. With a
    check-in (e.g. /staff_samples 10 lunch): only that one, Tamil and BM
    taking turns, with the pass rate. Never sent to groups."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    n, slot = 10, None
    for arg in context.args or []:
        arg = arg.strip().lower()
        if arg.isdigit():
            n = max(1, min(20, int(arg)))
        elif arg in staff_chat.SLOTS:
            slot = arg
    languages = ("tamil", "bm") if slot else ("tamil",)
    label = f"{slot} (Tamil + BM)" if slot else "Tamil"
    await message.reply_text(f"Writing {n} {label} samples…")
    rows = await asyncio.to_thread(
        _build_tamil_samples, n, _my_today(), slot, languages
    )
    await _send_chunked_to(
        context.application, message.chat_id, staff_chat.format_samples(rows)
    )


async def staff_preview_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Director-only: run one check-in's preview now, e.g. /staff_preview
    order. No argument lists the check-ins."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    slot = (context.args[0].strip().lower() if context.args else "")
    if slot not in staff_chat.SLOTS:
        await message.reply_text(
            "Usage: /staff_preview <check-in>\n" + "\n".join(
                f"{name} — {time_} ({shift})"
                for name, (shift, time_, _p) in staff_chat.SLOTS.items()
            )
            + f"\n\nSTAFF_CHAT_STYLE is {staff_chat.style()}."
        )
        return
    await message.reply_text(f"Writing the {slot} check-in for every outlet…")
    await run_staff_preview(context.application, slot, force=True)


async def questions_now_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show the unanswered-question overview on demand (does NOT
    nudge the groups — the scheduled job does that once a day)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    pending = await asyncio.to_thread(supervisor.open_questions_since, supabase)
    summary = supervisor.format_owner_pending(pending)
    await message.reply_text(
        summary or "✅ Every tracked question has been answered — nothing pending."
    )


# === Weekly praise + response scoreboard ====================================
# A supervisor who only ever complains reads as a machine; one who notices
# good work reads as a person. Monday 11:00 MY: every chat that answered ALL
# its questions this week gets Tamil praise, and the owner gets the response
# scoreboard (who answers, how fast) — both built from the question ledger,
# which only ever contains REAL deliveries, so praise can't leak to previews.

def _chat_label(chat_id) -> str:
    """Readable label for a ledger chat: 'SEK20 (Ravi)' for a manager DM,
    otherwise the raw chat id."""
    try:
        for code, row in manager_registration.get_all_managers(supabase).items():
            if row.get("chat_id") == chat_id:
                name = str(row.get("manager_name") or "").strip()
                return f"{code} ({name})" if name else str(code)
    except Exception:
        logger.debug("chat label lookup failed", exc_info=True)
    return f"chat {chat_id}"


async def post_weekly_praise(application: Application, *,
                             notify_chat_id=None) -> None:
    """Monday 11:00 MY job: praise the full-responders, show the owner the
    scoreboard."""
    try:
        rows = await asyncio.to_thread(supervisor.questions_since, supabase)
    except Exception:
        logger.exception("weekly praise: ledger read failed")
        rows = []
    stats = human_touch.engagement_by_chat(rows)

    praised = 0
    for stat in stats:
        text = human_touch.praise_message(stat)
        if not text:
            continue
        if group_reports.blocked(group_reports.PRAISE, stat["chat_id"], ALERT_CHAT_ID):
            continue
        try:
            await application.bot.send_message(
                chat_id=stat["chat_id"], text=text
            )
            praised += 1
        except Exception:
            logger.exception(
                "weekly praise: send failed (chat=%s)", stat.get("chat_id")
            )

    board = human_touch.format_owner_scoreboard(stats, _chat_label)
    if board:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=board)
    if notify_chat_id is not None and not board:
        with contextlib.suppress(Exception):
            await application.bot.send_message(
                chat_id=notify_chat_id,
                text="No tracked questions were asked in the last 7 days — "
                     "no scoreboard yet.",
            )
    logger.info(
        "Weekly praise: %d chat(s) praised of %d with questions",
        praised, len([s for s in stats if s.get("asked")]),
    )


async def scoreboard_now_command(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show the 7-day response scoreboard (praise is NOT sent —
    the Monday job does that)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    rows = await asyncio.to_thread(supervisor.questions_since, supabase)
    board = human_touch.format_owner_scoreboard(
        human_touch.engagement_by_chat(rows), _chat_label
    )
    await message.reply_text(
        board or "No tracked questions were asked in the last 7 days — "
                 "no scoreboard yet."
    )


# === Daily key-stock flag (24h business day) ================================
# The business runs 24h: business day D = two shift emails (D ~19:00 day +
# D+1 ~07:00 overnight), folded onto D at ingest. Each morning, ONLY for
# outlets whose day is COMPLETE (both shifts + D-file — the same gate the
# kitchen comparison uses), the kg of key stock bought that day (ayam,
# kambing, daging, ikan, ...) is checked against what that outlet usually
# buys per RM1000 of sales. Over-bought -> the manager is asked in Tamil,
# with the full-24h framing spelled out. See key_stock_daily.py.

def _gather_key_stock(today=None) -> dict:
    bundle = key_stock_daily.gather_key_stock_flags(supabase, today=today)
    managers = {}
    if bundle["entries"]:
        try:
            managers = manager_registration.get_all_managers(supabase)
        except Exception:
            logger.exception("key stock: manager lookup failed")
    bundle["managers"] = managers
    bundle["enabled"] = wmr.delivery_enabled()
    return bundle


async def post_key_stock_checks(application: Application, *,
                                notify_chat_id=None) -> None:
    """Daily 10:30 MY job (overnight shift email normally lands ~07:05, so
    the just-closed day is complete by then; still-incomplete outlets are
    skipped, never judged on a half day)."""
    try:
        bundle = await asyncio.to_thread(_gather_key_stock)
    except Exception:
        logger.exception("key stock: gather failed")
        if notify_chat_id is not None:
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=notify_chat_id,
                    text="Failed to run the key-stock check.",
                )
        return

    entries = bundle["entries"]
    enabled = bundle["enabled"]
    sent = 0
    for entry in entries:
        text = key_stock_daily.format_manager_key_stock(entry)
        if not text:
            continue
        mgr = bundle["managers"].get(entry["outlet_code"])
        decision = wmr.route_message(
            enabled,
            entry.get("display") or entry["outlet_code"],
            mgr.get("chat_id") if mgr else None,
            ALERT_CHAT_ID,
        )
        # A live outlet gets the natural 10:35 stock check-in instead.
        if _staff_live_now(cashier_names.outlet_for_chat(decision.target_chat_id)):
            continue
        text = human_touch.personalise(
            supervisor.with_reply_footer(text),
            mgr.get("manager_name") if mgr else None,
            decision.target_chat_id,
        )
        try:
            await human_touch.show_typing(application.bot, decision.target_chat_id)
            sent_msg = await application.bot.send_message(
                chat_id=decision.target_chat_id, text=decision.prefix + text
            )
            sent += 1
            if decision.reason == "manager":
                await asyncio.to_thread(
                    supervisor.log_question,
                    supabase, decision.target_chat_id, sent_msg.message_id,
                    "key_stock", text,
                )
        except Exception:
            logger.exception(
                "key stock: send failed (outlet=%s)", entry.get("outlet_code")
            )

    summary = key_stock_daily.format_owner_summary(entries)
    if summary:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
    if notify_chat_id is not None and not entries:
        note = (
            f"✅ Key stock ok for {bundle.get('business_date') or 'yesterday'} — "
            "no outlet bought clearly more than its sales needed."
        )
        if bundle.get("skipped_incomplete"):
            note += (
                f" ({bundle['skipped_incomplete']} outlet(s) skipped — POS day "
                "not complete yet, both shifts not in.)"
            )
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=notify_chat_id, text=note)
    logger.info(
        "Key stock check %s: %d outlet(s) flagged, %d sent, %d incomplete, "
        "delivery_enabled=%s",
        bundle.get("business_date"), len(entries), sent,
        bundle.get("skipped_incomplete", 0), enabled,
    )


async def key_stock_now_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: run the daily key-stock check on demand (for testing)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text(
        "Checking yesterday's key-stock buying vs the full 24h sales…"
    )
    await post_key_stock_checks(
        context.application, notify_chat_id=_command_owner_id(update)
    )


# === Daily slow-item watch (which items sold under the shop's usual) ========
# Combines BOTH shift-close emails of the 24h business day (day + overnight,
# folded onto one business date) into per-item-group quantities, compares each
# group against that shop's own 28-day median, and tells the manager which
# items moved less than usual — push them today. An item slow 3 data-days
# running escalates to the taste/quality question. See item_sales_watch.py.

def _gather_slow_items(today=None) -> dict:
    bundle = item_sales_watch.gather_slow_item_flags(supabase, today=today)
    managers = {}
    if bundle["entries"]:
        try:
            managers = manager_registration.get_all_managers(supabase)
        except Exception:
            logger.exception("slow items: manager lookup failed")
    bundle["managers"] = managers
    bundle["enabled"] = wmr.delivery_enabled()
    return bundle


async def post_slow_item_checks(application: Application, *,
                                notify_chat_id=None) -> None:
    """Daily 10:45 MY job (right after the key-stock check; the overnight
    shift email normally lands ~07:05, so the just-closed day is complete by
    then; still-incomplete outlets are skipped, never judged on a half day)."""
    try:
        bundle = await asyncio.to_thread(_gather_slow_items)
    except Exception:
        logger.exception("slow items: gather failed")
        if notify_chat_id is not None:
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=notify_chat_id,
                    text="Failed to run the slow-item check.",
                )
        return

    entries = bundle["entries"]
    enabled = bundle["enabled"]
    sent = 0
    for entry in entries:
        text = item_sales_watch.format_manager_slow_items(entry)
        if not text:
            continue
        mgr = bundle["managers"].get(entry["outlet_code"])
        decision = wmr.route_message(
            enabled,
            entry.get("display") or entry["outlet_code"],
            mgr.get("chat_id") if mgr else None,
            ALERT_CHAT_ID,
        )
        # A live outlet gets the 16:00 taste check (staff_ops) instead.
        if _staff_live_now(cashier_names.outlet_for_chat(decision.target_chat_id)):
            continue
        text = human_touch.personalise(
            supervisor.with_reply_footer(text),
            mgr.get("manager_name") if mgr else None,
            decision.target_chat_id,
        )
        try:
            await human_touch.show_typing(application.bot, decision.target_chat_id)
            sent_msg = await application.bot.send_message(
                chat_id=decision.target_chat_id, text=decision.prefix + text
            )
            sent += 1
            if decision.reason == "manager":
                await asyncio.to_thread(
                    supervisor.log_question,
                    supabase, decision.target_chat_id, sent_msg.message_id,
                    "slow_items", text,
                )
        except Exception:
            logger.exception(
                "slow items: send failed (outlet=%s)", entry.get("outlet_code")
            )

    summary = item_sales_watch.format_owner_summary(entries)
    if summary:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
    if notify_chat_id is not None and not entries:
        note = (
            f"✅ Item sales ok for {bundle.get('business_date') or 'yesterday'} — "
            "no shop had items clearly under its usual level."
        )
        if bundle.get("skipped_incomplete"):
            note += (
                f" ({bundle['skipped_incomplete']} outlet(s) skipped — POS day "
                "not complete yet, both shift emails not in.)"
            )
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=notify_chat_id, text=note)
    logger.info(
        "Slow-item check %s: %d outlet(s) flagged, %d sent, %d incomplete, "
        "delivery_enabled=%s",
        bundle.get("business_date"), len(entries), sent,
        bundle.get("skipped_incomplete", 0), enabled,
    )


async def slow_items_now_command(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: run the daily slow-item check on demand (for testing)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text(
        "Checking which items sold under each shop's usual yesterday "
        "(full 24h day)…"
    )
    await post_slow_item_checks(
        context.application, notify_chat_id=_command_owner_id(update)
    )


# === Cook-to-demand plan (how much to cook today) ===========================
# Every wastage alert ends with "cook to the sales" — this is the number.
# Forecasts today's demand per outlet per kitchen item from that shop's own
# trailing history (POS dishes sold, or Cooked − Left when POS is absent),
# with weekday seasonality, trend, sell-out censoring and a volatility-sized
# safety buffer, then tells the kitchen what to cook — and flags the items it
# has been over-cooking (wastage) or running dry on (lost sales). Posted in
# the late morning, hours before the 18:00 COOKED form. Yesterday's forecasts
# are scored on the way through, so /forecast_accuracy can answer "should we
# believe this?" with measured numbers. See demand_forecast.py.

def _gather_cook_plans(today=None) -> dict:
    bundle = demand_forecast.gather_cook_plans(supabase, today=today)
    managers = {}
    if bundle["entries"]:
        try:
            managers = manager_registration.get_all_managers(supabase)
        except Exception:
            logger.exception("cook plan: manager lookup failed")
    bundle["managers"] = managers
    # Manager delivery needs BOTH gates: the global one, and this feature's own
    # COOK_PLAN_ENABLED. Until the forecast is backtested to numbers worth
    # acting on, /cook_plan_now stays a preview — every plan routes to the owner
    # with the [TEST] prefix instead of reaching a kitchen.
    bundle["enabled"] = wmr.delivery_enabled() and demand_forecast.cook_plan_enabled()
    return bundle


async def post_cook_plans(application: Application, *,
                          notify_chat_id=None) -> None:
    """Daily 11:00 MY job (after the 10:45 slow-item watch, so yesterday's
    completed day is already folded in and well before anything is cooked).

    The SCHEDULED run no-ops unless COOK_PLAN_ENABLED is truthy — an unproven
    forecast must never reach a kitchen on a timer. ``notify_chat_id`` is set
    only by /cook_plan_now, which is owner-only and always allowed to run so
    the plan can be previewed and measured."""
    if notify_chat_id is None and not demand_forecast.cook_plan_enabled():
        logger.info("cook plan: scheduled run skipped (COOK_PLAN_ENABLED off)")
        return
    try:
        bundle = await asyncio.to_thread(_gather_cook_plans)
    except Exception:
        logger.exception("cook plan: gather failed")
        if notify_chat_id is not None:
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=notify_chat_id,
                    text="Failed to build today's cook plans.",
                )
        return

    entries = bundle["entries"]
    enabled = bundle["enabled"]
    sent = 0
    for entry in entries:
        # A live outlet gets the natural 11:05 cook check-in instead.
        if _staff_live_now(entry.get("outlet_code")):
            continue
        text = demand_forecast.format_cook_plan(entry)
        if not text:
            continue
        mgr = bundle["managers"].get(entry["outlet_code"])
        decision = wmr.route_message(
            enabled,
            entry.get("display") or entry["outlet_code"],
            mgr.get("chat_id") if mgr else None,
            ALERT_CHAT_ID,
        )
        # A live outlet gets the 16:00 taste check (staff_ops) instead.
        if _staff_live_now(cashier_names.outlet_for_chat(decision.target_chat_id)):
            continue
        text = human_touch.personalise(
            supervisor.with_reply_footer(text),
            mgr.get("manager_name") if mgr else None,
            decision.target_chat_id,
        )
        try:
            await human_touch.show_typing(application.bot, decision.target_chat_id)
            sent_msg = await application.bot.send_message(
                chat_id=decision.target_chat_id, text=decision.prefix + text
            )
            sent += 1
            if decision.reason == "manager":
                await asyncio.to_thread(
                    supervisor.log_question,
                    supabase, decision.target_chat_id, sent_msg.message_id,
                    "cook_plan", text,
                )
        except Exception:
            logger.exception(
                "cook plan: send failed (outlet=%s)", entry.get("outlet_code")
            )

    summary = demand_forecast.format_owner_summary(entries)
    if summary:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
    if notify_chat_id is not None and not entries:
        note = (
            f"✅ No cook plan for {bundle.get('business_date') or 'today'} — "
            "no outlet has enough kitchen-log history yet."
        )
        if bundle.get("skipped_thin"):
            note += (
                f" ({bundle['skipped_thin']} outlet(s) skipped — under "
                f"{demand_forecast.MIN_DATA_DAYS} data days per item.)"
            )
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=notify_chat_id, text=note)
    logger.info(
        "Cook plan %s: %d outlet(s) planned, %d sent, %d thin, %d scored, "
        "delivery_enabled=%s",
        bundle.get("business_date"), len(entries), sent,
        bundle.get("skipped_thin", 0), bundle.get("scored", 0), enabled,
    )


async def cook_plan_now_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: build and post today's cook plans on demand."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text(
        "Building today's cook-to-demand plan from each shop's own sales "
        "history…"
    )
    await post_cook_plans(
        context.application, notify_chat_id=_command_owner_id(update)
    )


async def forecast_accuracy_command(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: how close the cook-plan forecasts have been. ``/forecast_accuracy
    [days]`` (default 28)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    days = 28
    if context.args:
        try:
            days = max(1, min(180, int(context.args[0])))
        except (TypeError, ValueError):
            pass
    text = await asyncio.to_thread(
        demand_forecast.accuracy_report, supabase, days
    )
    await message.reply_text(text or "No forecast accuracy data yet.")


# === Overbuying watch (sales down, orders not) ==============================
# Weekly cross-check of the two trends the bot already collects: POS sales
# (from the shift-close emails, reconciled per outlet per day) and purchases
# (from receipts). When an outlet's sales are clearly down but its buying
# barely moved, the manager gets asked in Tamil why the orders haven't come
# down with the sales — item detail names the protein categories still bought
# at the old volume. Monday 09:30 MY, right after the weekly food-cost report.
# Delivery reuses the MANAGER_DELIVERY_ENABLED gate; the owner always gets an
# English summary when anything is flagged. See overbuy_watch.py.

def _gather_overbuy(today=None) -> dict:
    entries = overbuy_watch.gather_overbuy_entries(supabase, today=today)
    # Manager routing needs the registration outlet CODE for each canonical
    # name; the display label doubles as the [TEST]/no-manager prefix text.
    routes = {}
    if entries:
        try:
            outlets = manager_registration.load_active_outlets(supabase)
            managers = manager_registration.get_all_managers(supabase)
            for o in outlets:
                mgr = managers.get(o.code)
                routes[o.canonical] = {
                    "display": o.display,
                    "manager_chat_id": mgr.get("chat_id") if mgr else None,
                    "manager_name": mgr.get("manager_name") if mgr else None,
                }
        except Exception:
            logger.exception("overbuy watch: manager routing lookup failed")
    return {
        "entries": entries,
        "routes": routes,
        "enabled": wmr.delivery_enabled(),
    }


async def post_overbuy_checks(application: Application, *,
                              notify_chat_id=None) -> None:
    """Monday 09:30 MY job. One Tamil question per flagged outlet to its
    manager (via the delivery gate), then the English summary to the owner.
    ``notify_chat_id`` additionally reports the all-clear for the on-demand
    command."""
    try:
        bundle = await asyncio.to_thread(_gather_overbuy)
    except Exception:
        logger.exception("overbuy watch: gather failed")
        if notify_chat_id is not None:
            with contextlib.suppress(Exception):
                await application.bot.send_message(
                    chat_id=notify_chat_id,
                    text="Failed to run the overbuying check.",
                )
        return

    entries = bundle["entries"]
    enabled = bundle["enabled"]
    sent = 0
    for entry in entries:
        text = overbuy_watch.format_manager_overbuy(entry)
        if not text:
            continue
        route = bundle["routes"].get(entry["outlet"]) or {}
        decision = wmr.route_message(
            enabled,
            route.get("display") or entry["outlet"],
            route.get("manager_chat_id"),
            ALERT_CHAT_ID,
        )
        if group_reports.blocked(
            group_reports.OVERBUY, decision.target_chat_id, ALERT_CHAT_ID
        ):
            continue
        text = human_touch.personalise(
            supervisor.with_reply_footer(text),
            route.get("manager_name"),
            decision.target_chat_id,
        )
        try:
            await human_touch.show_typing(application.bot, decision.target_chat_id)
            sent_msg = await application.bot.send_message(
                chat_id=decision.target_chat_id, text=decision.prefix + text
            )
            sent += 1
            if decision.reason == "manager":
                await asyncio.to_thread(
                    supervisor.log_question,
                    supabase, decision.target_chat_id, sent_msg.message_id,
                    "overbuy", text,
                )
        except Exception:
            logger.exception(
                "overbuy watch: send failed (outlet=%s)", entry.get("outlet")
            )

    summary = overbuy_watch.format_owner_summary(entries)
    if summary:
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ALERT_CHAT_ID, text=summary)
    if notify_chat_id is not None and not entries:
        with contextlib.suppress(Exception):
            await application.bot.send_message(
                chat_id=notify_chat_id,
                text="✅ No outlet is overbuying — everywhere sales dropped, "
                     "the ordering came down with it (or sales are steady).",
            )
    logger.info(
        "Overbuy watch: %d outlet(s) flagged, %d message(s) sent, "
        "delivery_enabled=%s",
        len(entries), sent, enabled,
    )


async def overbuy_now_command(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: run the overbuying check on demand (for testing)."""
    message = update.effective_message
    if not message or not is_reviewer(_command_owner_id(update)):
        return
    await message.reply_text("Comparing sales trend vs buying trend…")
    await post_overbuy_checks(
        context.application, notify_chat_id=_command_owner_id(update)
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
        # Too little buying history for a trustworthy draft (order_sanity):
        # ask the shop what it needs instead of sending made-up quantities.
        try:
            verdict = order_sanity.assess(
                order_sanity.fetch_history(supabase, [code], today),
                o.get("items") or [],
            )
        except Exception:
            logger.exception("order drafts: history check failed (%s)", code)
            verdict = {"ok": True, "reason": ""}
        chunks = o["messages"]
        if not verdict["ok"]:
            chunks = [staff_chat.render_template(
                "order", staff_chat.BM_TAMIL, {"ask": True}
            )]
        # One per Telegram-safe chunk; the routing prefix rides on the first.
        for i, chunk in enumerate(chunks):
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
            "thin": "" if verdict["ok"] else verdict["reason"],
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
            if r.get("thin"):
                hq_lines.append(
                    f"{r['display']:<12} no draft sent — asked what to order "
                    f"(thin history: {r['thin']})  {who}"
                )
                continue
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
        # A live outlet gets the natural 20:05 order check-in instead.
        if _staff_live_now(cashier_names.outlet_for_chat(msg["target"])):
            total -= 1
            continue
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
    # A 24h outlet can close its POS day twice (two D-files per business day);
    # fold them so each outlet shows once with the summed day totals.
    return sales_analytics.merge_summary_rows(resp.data or [])


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


def _new_outlet_alert_text(new_codes) -> str:
    codes = ", ".join(new_codes)
    example = new_codes[0]
    return (
        f"🆕 New POS outlet detected: {codes}\n"
        "Auto-registered as INACTIVE — its sales emails are held (marked read, "
        "not counted) until you activate it.\n\n"
        f"To start counting it: /activate_outlet {example} <Shop Name>\n"
        "Past held emails are pulled in automatically on activation."
    )


async def _notify_new_outlets(application, summary) -> None:
    """One-time owner alert when the ingest pass auto-registered a brand-new
    outlet code (e.g. a new shop's POS starts emailing as S-44)."""
    new_codes = (summary or {}).get("new_outlets") or []
    if not new_codes or application is None:
        return
    with contextlib.suppress(Exception):
        await application.bot.send_message(
            chat_id=ALERT_CHAT_ID, text=_new_outlet_alert_text(new_codes)
        )


# Alert the owner ONCE per process (not every 15-min poll) when the database
# still has the pre-0038 unique constraint blocking a 24h outlet's second
# daily-close email.
_migration_0038_alerted = False


async def _notify_migration_0038(application, summary) -> None:
    global _migration_0038_alerted
    if not (summary or {}).get("migration_0038_needed"):
        return
    if _migration_0038_alerted or application is None:
        return
    _migration_0038_alerted = True
    with contextlib.suppress(Exception):
        await application.bot.send_message(
            chat_id=ALERT_CHAT_ID,
            text=(
                "⚠️ Database migration needed: a 24h shop sent its SECOND "
                "daily-close email of the day, but the database still allows "
                "only ONE per shop per day — half that day's sales cannot be "
                "stored and Guna vs POS will keep waiting.\n\n"
                "Fix (one time): run migrations/0038_sales_daily_summary_"
                "multi_close.sql in the Supabase SQL editor. The blocked "
                "emails stay unread and ingest automatically once it's applied."
            ),
        )


_sales_client = None


def _sales_supabase() -> Client:
    """The scheduled sales poll's own Supabase client. The shared ``supabase``
    client is not safe to use from two worker threads at once: at 20:00 the
    poll (every :00/:15/:30/:45) and the order-draft job collided on its
    HTTP/2 connection ("[Errno 11] Resource temporarily unavailable"), which
    failed persist_cadence for the first outlet every evening."""
    global _sales_client
    if _sales_client is None:
        _sales_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sales_client


async def poll_sales_emails(application: Application | None = None) -> None:
    """APScheduler job: ingest unread shift-close emails (every 30 min, 24/7)."""
    if not os.environ.get("GMAIL_INBOX") or not os.environ.get("GMAIL_APP_PASSWORD"):
        logger.info("Sales ingest poll skipped: GMAIL_INBOX/GMAIL_APP_PASSWORD not set")
        return
    try:
        summary = await asyncio.to_thread(run_ingest_once, _sales_supabase())
        logger.info("Sales ingest poll: %s", summary)
        await _notify_new_outlets(application, summary)
        await _notify_migration_0038(application, summary)
    except Exception:
        logger.exception("Sales ingest poll failed")


async def _ingest_then_compare(app, compare_coro) -> None:
    """Run a one-off sales-email ingest IMMEDIATELY BEFORE a kitchen comparison
    pass, so the comparison always reconciles against the freshest POS the inbox
    has right now — not whatever the last */15 poll happened to catch. Best-effort:
    an ingest failure must never block the comparison (it just runs on existing
    data). This is the direct guard against "the 09:00 comparison had stale POS"."""
    try:
        await poll_sales_emails(app)
    except Exception:
        logger.exception("pre-comparison sales ingest failed (continuing)")
    await compare_coro(app)


async def post_kitchen_comparison_0900(app) -> None:
    await _ingest_then_compare(app, kitchen_usage.post_comparison_digests)


async def post_kitchen_comparison_retry(app) -> None:
    await _ingest_then_compare(app, kitchen_usage.post_comparison_digests_retry)


async def post_kitchen_comparison_final(app) -> None:
    await _ingest_then_compare(app, kitchen_usage.post_comparison_digests_final)


async def run_bot() -> None:
    # concurrent_updates(True): process updates as independent tasks so a slow
    # handler (a multi-second OCR on an uploaded receipt) doesn't block other
    # updates. Without it PTB handles updates sequentially, so kitchen numpad
    # taps would queue behind an in-flight OCR and feel laggy.
    # OutletGroupBot: every message to an outlet group opens with the name of
    # the cashier on shift (cashier_names). Load the group/name cache first so
    # the very first sends are already addressed.
    cashier_names.configure(supabase)
    app = (
        Application.builder()
        .bot(OutletGroupBot(token=TELEGRAM_BOT_TOKEN))
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
    app.add_handler(CommandHandler("price_quarantine", price_quarantine_command))
    app.add_handler(CommandHandler("shop_prices", shop_prices_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("tanya", ask_command))
    app.add_handler(CommandHandler("cari", ask_command))
    app.add_handler(CommandHandler("search", ask_command))
    # Aliases: same report, whichever wording comes to mind first.
    app.add_handler(CommandHandler("all_prices", shop_prices_command))
    app.add_handler(CommandHandler("harga", shop_prices_command))
    app.add_handler(CommandHandler("test_digest", test_digest_command))
    app.add_handler(CommandHandler("sales_today", sales_today_command))
    app.add_handler(CommandHandler("sales_yesterday", sales_yesterday_command))
    app.add_handler(CommandHandler("sales_outlet", sales_outlet_command))
    app.add_handler(CommandHandler("sales_ingest_status", sales_ingest_status_command))
    app.add_handler(CommandHandler("sales_ingest_latency", sales_ingest_latency_command))
    app.add_handler(CommandHandler("sales_ingest_manual", sales_ingest_manual_command))
    app.add_handler(CommandHandler("activate_outlet", activate_outlet_command))
    app.add_handler(CommandHandler("food_cost_today", food_cost_today_command))
    app.add_handler(CommandHandler("food_cost_week", food_cost_week_command))
    app.add_handler(CommandHandler("food_cost_month", food_cost_month_command))
    app.add_handler(CommandHandler("monthly_kg", monthly_kg_command))
    app.add_handler(CommandHandler("food_cost_outlet", food_cost_outlet_command))
    app.add_handler(CommandHandler("gen_codes", gen_codes_command))
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("weekly_report_now", weekly_report_now_command))
    app.add_handler(CommandHandler("missing_bills_now", missing_bills_now_command))
    app.add_handler(CommandHandler("bill_analysis_now", bill_analysis_now_command))
    app.add_handler(CommandHandler("outlet_prices", outlet_prices_command))
    app.add_handler(CommandHandler("branch_prices", outlet_prices_command))
    app.add_handler(CommandHandler("overbuy_now", overbuy_now_command))
    app.add_handler(CommandHandler("key_stock_now", key_stock_now_command))
    app.add_handler(CommandHandler("slow_items_now", slow_items_now_command))
    app.add_handler(CommandHandler("cook_plan_now", cook_plan_now_command))
    app.add_handler(CommandHandler("forecast_accuracy", forecast_accuracy_command))
    app.add_handler(CommandHandler("questions_now", questions_now_command))
    app.add_handler(CommandHandler("cashier", cashier_command))
    app.add_handler(CommandHandler("ping_managers", ping_managers_command))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(CommandHandler("staff_preview", staff_preview_command))
    app.add_handler(CommandHandler("staff_samples", staff_samples_command))
    app.add_handler(CommandHandler("draft", draft_command))
    app.add_handler(CommandHandler("order", order_command))
    app.add_handler(CommandHandler("closed", closed_command))
    app.add_handler(CommandHandler("nudge_off", nudge_off_command))
    app.add_handler(CommandHandler("voice_stats", voice_stats_command))
    app.add_handler(CommandHandler("staff_digest_now", staff_digest_now_command))
    app.add_handler(CommandHandler("issues", issues_command))
    app.add_handler(CommandHandler("phrasing_now", phrasing_now_command))
    app.add_handler(CommandHandler("resolve", resolve_command))
    app.add_handler(CommandHandler("form_chase_now", form_chase_now_command))
    app.add_handler(CommandHandler("scoreboard_now", scoreboard_now_command))
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
    # Pinpoint Target: outside purchases + cashier strikes (outside_purchase).
    app.add_handler(CommandHandler("beli_luar", beli_luar_command))
    app.add_handler(CommandHandler("beli_luar_cashier", beli_luar_cashier_command))
    app.add_handler(CommandHandler("izin", izin_command))
    app.add_handler(CommandHandler("bukan_beli_luar", bukan_beli_luar_command))
    app.add_handler(CommandHandler("daftar_cashier", daftar_cashier_command))
    app.add_handler(CommandHandler("tambah_supplier", tambah_supplier_command))
    app.add_handler(CallbackQueryHandler(handle_outside_review, pattern=r"^ob:\d+:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(handle_daftar_callback, pattern=r"^dc:\d+:"))
    app.add_handler(CommandHandler("lebih_beli", lebih_beli_command))
    app.add_handler(CommandHandler("merchant_known", merchant_known_command))
    app.add_handler(CommandHandler("buang_merchant", buang_merchant_command))
    app.add_handler(CommandHandler("pinpoint_shadow", pinpoint_shadow_command))
    app.add_handler(CallbackQueryHandler(handle_overbuy_reason,
                                         pattern=r"^ov:\d+:(stock|order|supplier|other)$"))
    app.add_handler(CallbackQueryHandler(handle_overbuy_decision, pattern=r"^ovm:\d+:(accept|reject)$"))
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
    # Plain-language item search. Registered last in the group so the
    # review-edit conversation and the audit-reply handler above always get
    # first refusal; ~REPLY keeps it off audit replies entirely.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.REPLY, handle_ask_text)
    )
    # Live staff chat: reads staff messages in live outlet groups. Group 1 so
    # it runs IN ADDITION to the handlers above, never instead of them.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
                       handle_staff_reply),
        group=1,
    )
    # Voice notes in live outlet groups: transcribed (when a speech-to-text
    # provider is configured) and read like a typed reply.
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.GROUPS, handle_staff_voice),
                    group=1)
    # Tap-to-answer buttons on live check-ins ("sc:<thread>:<choice>").
    app.add_handler(CallbackQueryHandler(handle_staff_button, pattern=r"^sc:\d+:\w+$"))
    # A typed overbuy reason (reply to the bot's "type your reason" prompt).
    # Group 2: runs in addition to the handlers above and only acts when the
    # replied-to message is one of its own prompts.
    app.add_handler(
        MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND & filters.ChatType.GROUPS,
                       handle_overbuy_reason_text),
        group=2,
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

    # job_defaults: APScheduler's default misfire_grace_time is just 1 SECOND, so
    # in this single-process asyncio bot a cron fire that lands while the event
    # loop is briefly busy (an in-flight OCR, a slow handler) is marked "misfired"
    # and SILENTLY SKIPPED — which can make a */30 poll effectively run far less
    # often than intended. A 5-minute grace + coalesce makes a slightly-late fire
    # still run (and collapses any pile-up into one), so the poll keeps cadence.
    scheduler = AsyncIOScheduler(
        timezone=MALAYSIA_TZ,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    scheduler.add_job(
        post_daily_summary,
        trigger="cron",
        hour=23,
        # 23:59, not 23:00 — the summary window is the full MY calendar day,
        # so receipts logged 23:00-24:00 (shift-close uploads) fell in a
        # permanent daily blind spot: today's run had already fired and
        # tomorrow's covers only tomorrow.
        minute=59,
        args=[app],
        id="daily_summary",
        replace_existing=True,
    )
    # PR #35: poll the master inbox for shift-close emails, 24/7. Every 15 min so
    # an email that lands ~07:00 (the overnight shift report) is in sales_daily
    # well before the 09:00 kitchen comparison. In-process (no separate Render
    # cron) — $0 extra. NOTE: received_at on the row is the email's Date header
    # (when the POS sent it), NOT our ingest time — ingest time is created_at.
    scheduler.add_job(
        poll_sales_emails,
        trigger="cron",
        minute="*/15",
        args=[app],
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
    # Unfilled kitchen-form chaser — runs every 2 hours at :45, but each open
    # COOKED/LEFT form is reminded at most twice per shift, all of a group's
    # open forms go in one message, and nothing goes out 00:00-06:00 unless
    # the form was due then (kitchen_usage.should_remind). The owner is told
    # once when a form has been ignored ~8 hours. No-ops unless
    # KITCHEN_LOG_ENABLED.
    scheduler.add_job(
        kitchen_usage.post_form_reminders,
        trigger="cron",
        hour="*/2",
        minute=45,
        args=[app],
        id="kitchen_form_chase",
        replace_existing=True,
    )
    # Weekly praise + response scoreboard — Monday 11:00 MY, from the
    # question ledger. Full-responders get Tamil praise; the owner gets the
    # who-answers-how-fast scoreboard.
    scheduler.add_job(
        post_weekly_praise,
        trigger="cron",
        day_of_week="mon",
        hour=11,
        minute=0,
        args=[app],
        id="weekly_praise",
        replace_existing=True,
    )
    # Question follow-up — daily 17:00 MY. Nudges yesterday's unanswered
    # manager questions in their own chats (one nudge per question) and shows
    # the owner who has gone quiet. The memory that makes the bot feel human.
    scheduler.add_job(
        post_question_reminders,
        trigger="cron",
        hour=17,
        minute=0,
        args=[app],
        id="question_reminders",
        replace_existing=True,
    )
    # Daily key-stock flag — 10:30 MY, after the ~07:05 overnight shift email
    # completes yesterday's 24h business day. Outlets whose POS day isn't
    # complete are skipped, never judged on a half day.
    scheduler.add_job(
        post_key_stock_checks,
        trigger="cron",
        hour=10,
        minute=30,
        args=[app],
        id="key_stock_daily",
        replace_existing=True,
    )
    # Daily slow-item watch — 10:45 MY, right after the key-stock check.
    # Combines both shift emails of yesterday's 24h day per shop, flags item
    # groups selling under the shop's own 28-day usual, and asks the manager
    # in Tamil to push them today (3-day slumps get the taste/quality
    # question). Gated by MANAGER_DELIVERY_ENABLED.
    scheduler.add_job(
        post_slow_item_checks,
        trigger="cron",
        hour=10,
        minute=45,
        args=[app],
        id="slow_items_daily",
        replace_existing=True,
    )
    # Cook-to-demand plan — 11:00 MY, right after the slow-item watch and
    # hours before the 18:00 COOKED form, so the kitchen has the number while
    # it still matters. Forecasts today's demand per item from the shop's own
    # history and flags what it over-cooks (wastage) or runs dry on (lost
    # sales); also scores yesterday's forecasts. Gated by
    # MANAGER_DELIVERY_ENABLED.
    scheduler.add_job(
        post_cook_plans,
        trigger="cron",
        hour=11,
        minute=0,
        args=[app],
        id="cook_plan_daily",
        replace_existing=True,
    )
    # Overbuying watch — Monday 09:30 MY, right after the 09:00 weekly
    # food-cost report. Flags outlets whose sales dropped over the last 7 days
    # while their buying barely moved, and asks each manager in Tamil why the
    # orders didn't come down. Gated by MANAGER_DELIVERY_ENABLED.
    scheduler.add_job(
        post_overbuy_checks,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=30,
        args=[app],
        id="overbuy_watch",
        replace_existing=True,
    )
    # Missing supplier-bill watch — nightly 21:00 MY, after the day's receipt
    # uploads and ahead of the 23:00 digest. Asks each outlet chat (in Tamil +
    # Malay) about regular suppliers whose bills stopped being uploaded. Gated
    # by MANAGER_DELIVERY_ENABLED: until the owner flips it, every question
    # routes to the owner with a [TEST] prefix.
    scheduler.add_job(
        post_missing_bill_checks,
        trigger="cron",
        hour=21,
        minute=0,
        args=[app],
        id="missing_bill_check",
        replace_existing=True,
    )
    # Bill analysis — nightly 21:30 MY, after the missing-bill check and
    # ahead of the 23:00 digest. Every bill uploaded in the last 24h against
    # the same shop's previous price (increases to owners + the manager whose
    # bill it was), and every item two or more outlets buy compared across
    # branches (owners get the full table; each manager hears which items
    # another branch buys cheaper). Manager notes gated by
    # MANAGER_DELIVERY_ENABLED.
    scheduler.add_job(
        post_bill_analysis,
        trigger="cron",
        hour=21,
        minute=30,
        args=[app],
        id="bill_analysis",
        replace_existing=True,
    )
    # Monthly kg-per-protein purchase report — 1st of the month 09:30 MY,
    # covering the month that just ended, to the alert group. On-demand
    # preview any time via /monthly_kg.
    scheduler.add_job(
        post_monthly_kg_report,
        trigger="cron",
        day=1,
        hour=9,
        minute=30,
        args=[app],
        id="monthly_kg_report",
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
    # STAGE 2 of the kitchen digest — the real Used-vs-POS comparison, gated on POS
    # COMPLETENESS and targeting the most-recent COMPLETE, unreconciled day (NEVER
    # today). A 24h outlet reports its POS in TWO shift-close emails that both fold
    # to the same business_date: the ~7PM day shift and the ~7AM-NEXT-DAY overnight
    # shift, so today is never complete in the morning. This runs at 09:00 (the
    # overnight email is normally in by ~7AM), retries at 11:00 for late POS, and at
    # 14:00 (final) also raises a "⚠️ POS <shift> hilang" alert for any day still
    # missing a shift (ingestion gap). Until complete each outlet shows "⏳ POS belum
    # lengkap" and is never flagged. The 09:00 run notifies; 11:00 is silent.
    # Each pass INGESTS the inbox first (post_kitchen_comparison_*) so it always
    # reconciles against the freshest POS, independent of the */15 poll's timing.
    scheduler.add_job(
        post_kitchen_comparison_0900,
        trigger="cron",
        hour=9,
        minute=0,
        args=[app],
        id="kitchen_comparison",
        replace_existing=True,
    )
    scheduler.add_job(
        post_kitchen_comparison_retry,
        trigger="cron",
        hour=11,
        minute=0,
        args=[app],
        id="kitchen_comparison_retry",
        replace_existing=True,
    )
    scheduler.add_job(
        post_kitchen_comparison_final,
        trigger="cron",
        hour=14,
        minute=0,
        args=[app],
        id="kitchen_comparison_retry_2",
        replace_existing=True,
    )
    # Late catch-up pass at 23:30. The POS appears to dispatch the overnight shift
    # report in a late (~23:00) batch — received_at (the email's Date header, NOT
    # our ingest time) clusters at ~11:00/23:00 — so a day can become POS-complete
    # only late that evening. This pass ingests first, then reconciles, so the day
    # closes the SAME night instead of waiting for next morning's lookback. Silent
    # on still-incomplete days (the 14:00 pass already alerted on real gaps) and
    # idempotent via pos_qty, so it no-ops once a day is already reconciled.
    scheduler.add_job(
        post_kitchen_comparison_retry,
        trigger="cron",
        hour=23,
        minute=30,
        args=[app],
        id="kitchen_comparison_late",
        replace_existing=True,
    )

    # Natural staff chat check-ins (staff_chat.SLOTS). No-ops unless
    # STAFF_CHAT_STYLE=preview; in preview every outlet's message goes to the
    # director chat only, one digest per check-in.
    for _slot, (_shift, _time, _purpose) in staff_chat.SLOTS.items():
        _h, _m = (int(x) for x in _time.split(":"))
        scheduler.add_job(
            run_staff_preview,
            trigger="cron",
            hour=_h,
            minute=_m,
            args=[app, _slot],
            id=f"staff_chat_{_slot}",
            replace_existing=True,
        )

    # Staff questions v2 (staff_ops): leftover, sales note, wastage,
    # afternoon taste check / tip, Monday praise. Off per slot via
    # STAFF_CHAT_SLOTS; only live outlets.
    for _slot, _cron in (("leftover", {"hour": 3, "minute": 0}),
                         ("sales", {"hour": 9, "minute": 0}),
                         ("wastage", {"hour": 10, "minute": 30}),
                         ("afternoon", {"hour": 16, "minute": 0}),
                         ("praise", {"day_of_week": "mon", "hour": 11, "minute": 5})):
        scheduler.add_job(
            run_staff_ops,
            trigger="cron",
            args=[app, _slot],
            id=f"staff_ops_{_slot}",
            replace_existing=True,
            **_cron,
        )

    # Live staff chat: reminders / no-reply / next question every 10 minutes,
    # and the director's morning replies summary at 08:30.
    scheduler.add_job(
        staff_live_tick,
        trigger="cron",
        minute="*/10",
        args=[app],
        id="staff_live_tick",
        replace_existing=True,
    )
    scheduler.add_job(
        post_staff_morning_summary,
        trigger="cron",
        hour=8,
        minute=30,
        args=[app],
        id="staff_morning_summary",
        replace_existing=True,
    )
    # Learning loop — Monday 08:00 MY: the wordings that got the fastest
    # replies last week become the rephrase prompt's examples (staff_learning).
    scheduler.add_job(
        post_phrasing_examples,
        trigger="cron",
        day_of_week="mon",
        hour=8,
        minute=0,
        args=[app],
        id="phrasing_examples",
        replace_existing=True,
    )
    # Pinpoint Target: overbuy questions unanswered for 12h become no_reply
    # strikes (every 30 min); the known-merchant baseline is recounted nightly
    # (03:30, streamed); in shadow mode the director gets the day's
    # would-have-been-sent summary at 21:45.
    scheduler.add_job(
        overbuy_no_reply_tick,
        trigger="cron",
        minute="5,35",
        args=[app],
        id="overbuy_no_reply_tick",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_known_merchants_job,
        trigger="cron",
        hour=3,
        minute=30,
        args=[app],
        id="known_merchants_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        post_shadow_summary,
        trigger="cron",
        hour=21,
        minute=45,
        args=[app],
        id="pinpoint_shadow_summary",
        replace_existing=True,
    )
    # Nightly director digest — 23:30 MY: every reply and non-reply of the
    # day in plain English, ordered by concern (staff_digest).
    scheduler.add_job(
        post_staff_night_digest,
        trigger="cron",
        hour=23,
        minute=30,
        args=[app],
        id="staff_night_digest",
        replace_existing=True,
    )

    async with app:
        await app.start()
        with contextlib.suppress(Exception):
            await app.bot.set_my_commands([
                BotCommand("start", "Greeting"),
                BotCommand("summary", "Today's spending grouped by merchant"),
                BotCommand("compare", "Compare an item's unit price across outlets"),
                BotCommand("ask", "Ask about any item in plain words"),
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
