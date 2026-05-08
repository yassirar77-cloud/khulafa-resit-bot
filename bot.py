import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template
from openai import OpenAI
from supabase import create_client, Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
RECEIPTS_TABLE = "receipts"
MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")
MIN_PLAUSIBLE_YEAR = 2024
FALLBACK_YEAR = 2026

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
    "items (array of {name, qty, price} where qty is a number or null), "
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
    "field is unreadable, use null. No markdown, no commentary, JSON only."
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


async def extract_receipt(image_bytes: bytes) -> dict:
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
    content = response.choices[0].message.content or "{}"
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"raw_text": content}
    parsed["date"] = normalize_date(parsed.get("date"))
    return parsed


def normalize_date(value) -> str | None:
    if not isinstance(value, str) or len(value) < 4:
        return value if isinstance(value, str) else None
    try:
        year = int(value[:4])
    except ValueError:
        return value
    if year < MIN_PLAUSIBLE_YEAR:
        return f"{FALLBACK_YEAR}{value[4:]}"
    return value


def store_receipt(record: dict) -> dict:
    result = supabase.table(RECEIPTS_TABLE).insert(record).execute()
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
    date = parsed.get("date") or "—"
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


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.photo:
        return

    chat = message.chat
    chat_title = chat.title if chat else None
    outlet = derive_outlet(message.chat_id, chat_title)
    logger.info(
        "Receipt photo received: chat_id=%s chat_title=%r outlet=%s",
        message.chat_id,
        chat_title,
        outlet,
    )

    await message.reply_text("Processing receipt…")

    photo = message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    try:
        parsed = await extract_receipt(image_bytes)
    except Exception:
        logger.exception("OCR failed")
        await message.reply_text("Failed to read receipt. Try a clearer photo.")
        return

    user = update.effective_user
    record = {
        "telegram_user_id": user.id if user else None,
        "telegram_username": user.username if user else None,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "merchant": parsed.get("merchant"),
        "receipt_date": parsed.get("date"),
        "total": parsed.get("total"),
        "currency": parsed.get("currency"),
        "items": parsed.get("items"),
        "raw_text": parsed.get("raw_text"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        stored = await asyncio.to_thread(store_receipt, record)
    except Exception:
        logger.exception("Supabase insert failed")
        await message.reply_text("Saved OCR locally but database write failed.")
        stored = record

    user_alert = format_alert(stored, parsed)
    ops_alert = format_alert(stored, parsed, outlet=outlet)
    await message.reply_text(user_alert)
    try:
        await context.bot.send_message(chat_id=ALERT_CHAT_ID, text=ops_alert)
    except Exception:
        logger.exception("Failed to send alert to ALERT_CHAT_ID")


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
    if not WEBAPP_URL:
        await message.reply_text(
            "Dashboard URL not configured. Set WEBAPP_URL to the public /webapp endpoint."
        )
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open dashboard", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )
    await message.reply_text(
        "Tap below to open the Khulafa Resit Monitor dashboard.",
        reply_markup=keyboard,
    )


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


async def run_bot() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("compare", compare_command))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

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

    async with app:
        await app.start()
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            error_callback=on_polling_error,
        )
        logger.info("Bot started (health on :%d)", HEALTH_PORT)
        try:
            await stop.wait()
        finally:
            await app.updater.stop()
            await app.stop()


def main() -> None:
    threading.Thread(target=run_health_server, daemon=True).start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
