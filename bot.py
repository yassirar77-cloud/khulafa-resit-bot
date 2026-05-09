import asyncio
import base64
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from flask import Flask, jsonify
from openai import OpenAI
from supabase import Client, create_client
from telegram import Update
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
ALERT_CHAT_ID = int(os.environ["ALERT_CHAT_ID"])
HEALTH_PORT = int(os.environ.get("PORT", "10000"))

ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
ZHIPU_MODEL = "glm-4.6v-flash"
RECEIPTS_TABLE = "receipts"
AUDIT_TABLE = "audit_responses"
MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")

BIG_PURCHASE_MULTIPLIER = 2.0
BIG_PURCHASE_LOOKBACK_DAYS = 14
NEW_SUPPLIER_THRESHOLD = 200.0
SUSPICIOUS_PRICE_RATIO = 1.20
SUSPICIOUS_ITEM_LOOKBACK_DAYS = 7
DUPLICATE_TOTAL_TOLERANCE = 0.05

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
        return (
            "வாங்கினது அதிகம்! ஏன் இவ்வளவு வாங்கினீங்க? / "
            "Belian banyak hari ni! Kenapa beli lebih dari biasa? "
            f"(purata 14 hari RM{avg:.2f}, hari ni RM{total:.2f})"
        )
    return None


def _check_new_supplier(chat_id: int, merchant: str, total: float, current_id) -> str | None:
    if not merchant or total <= NEW_SUPPLIER_THRESHOLD:
        return None
    query = (
        supabase.table(RECEIPTS_TABLE)
        .select("id")
        .eq("chat_id", chat_id)
        .eq("merchant", merchant)
        .limit(2)
    )
    res = query.execute()
    rows = res.data or []
    if current_id is not None:
        rows = [r for r in rows if r.get("id") != current_id]
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
        for prev in row.get("items") or []:
            name = (prev.get("name") or "").strip().lower()
            price = _to_float(prev.get("price"))
            if name and price is not None:
                history.setdefault(name, []).append(price)

    flagged = []
    for it in items:
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


def run_audit_checks(stored: dict, parsed: dict) -> list[tuple[str, str]]:
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Send a receipt photo and I'll log it."
    )


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
    by_outlet: dict[int, dict] = {}
    by_supplier: dict[str, float] = {}
    failed = 0

    for r in rows:
        cid = r.get("chat_id")
        total = _to_float(r.get("total"))
        merchant = r.get("merchant")

        outlet = by_outlet.setdefault(cid, {"total": 0.0, "count": 0})
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
        for cid, data in sorted_outlets:
            lines.append(f"  • Outlet {cid}: RM{data['total']:.2f} ({data['count']} resit)")

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


async def post_init(application: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=MALAYSIA_TZ)
    scheduler.add_job(
        post_daily_summary,
        trigger="cron",
        hour=23,
        minute=0,
        args=[application],
        id="daily_summary",
        replace_existing=True,
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started: daily summary at 23:00 Asia/Kuala_Lumpur")


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)


def main() -> None:
    threading.Thread(target=run_health_server, daemon=True).start()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, handle_audit_reply))
    logger.info("Bot starting (health on :%d)", HEALTH_PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
