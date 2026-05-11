import asyncio
import base64
import contextlib
import json
import logging
import os
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
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from audit_messages import build_big_purchase_message
from date_utils import normalize_date
from image_utils import resize_for_ocr
from items_utils import normalize_items
from money_utils import normalize_total

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
_VERIFICATION_KEYS = ("verification_status", "verification_notes", "confidence")


def store_receipt(record: dict) -> dict:
    global _outlet_column_available, _verification_columns_available, _bill_to_column_available
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
        else:
            raise
    return result.data[0] if result.data else record


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
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

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


async def run_bot() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("compare", compare_command))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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
