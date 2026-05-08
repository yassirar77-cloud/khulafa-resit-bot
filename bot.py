import asyncio
import base64
import json
import logging
import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template
from openai import OpenAI
from supabase import create_client, Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
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
ZHIPU_API_KEY = os.environ["ZHIPU_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
ALERT_CHAT_ID = int(os.environ["ALERT_CHAT_ID"])
HEALTH_PORT = int(os.environ.get("PORT", "10000"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
ZHIPU_MODEL = "glm-4.6v-flash"
RECEIPTS_TABLE = "receipts"

zhipu_client = OpenAI(api_key=ZHIPU_API_KEY, base_url=ZHIPU_BASE_URL)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

OCR_PROMPT = (
    "You are a receipt OCR assistant. Extract the receipt fields and respond "
    "ONLY with a compact JSON object using these keys: "
    "merchant (string), date (YYYY-MM-DD or null), total (number or null), "
    "currency (string or null), items (array of {name, price}), "
    "raw_text (full transcription). No markdown, no commentary."
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
        zhipu_client.chat.completions.create,
        model=ZHIPU_MODEL,
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
        return json.loads(content)
    except json.JSONDecodeError:
        return {"raw_text": content}


def store_receipt(record: dict) -> dict:
    result = supabase.table(RECEIPTS_TABLE).insert(record).execute()
    return result.data[0] if result.data else record


def format_alert(record: dict, parsed: dict) -> str:
    merchant = parsed.get("merchant") or "Unknown merchant"
    total = parsed.get("total")
    currency = parsed.get("currency") or ""
    date = parsed.get("date") or "—"
    user = record.get("telegram_username") or record.get("telegram_user_id")
    total_str = f"{total} {currency}".strip() if total is not None else "n/a"
    return (
        "New receipt logged\n"
        f"From: {user}\n"
        f"Merchant: {merchant}\n"
        f"Date: {date}\n"
        f"Total: {total_str}"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.photo:
        return

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

    alert = format_alert(stored, parsed)
    await message.reply_text(alert)
    try:
        await context.bot.send_message(chat_id=ALERT_CHAT_ID, text=alert)
    except Exception:
        logger.exception("Failed to send alert to ALERT_CHAT_ID")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Send a receipt photo and I'll log it. Use /dashboard to view stats."
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


def main() -> None:
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot starting (health on :%d)", HEALTH_PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
