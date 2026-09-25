"""Live staff chat: the check-in conversation with an outlet group.

Only outlets listed in ``STAFF_CHAT_LIVE_OUTLETS`` (Render env, e.g.
``BISTRO7``) are live; every other outlet stays in director-only preview.

One question at a time, per group. Each check-in is a *thread* in
``staff_chat_thread`` (migrations/0046):

  queued    waiting because another question is still open in that group
  open      sent, waiting for an answer
  reminded  sent + one reminder after 30 minutes (money questions only)
  answered  a reply was understood (and saved)
  no_reply  2 hours without an answer — goes in the director's morning
            summary, and the group's next queued question is released
  dropped   was still queued when its shift ended (a 15:00 lunch question
            is pointless at 21:00)

Replies are read by the AI provider (any language) and saved as a short
English summary with a status (ok / short / finished / problem / order /
other). An unclear answer gets ONE simpler follow-up; after that, whatever
they said is saved.

No reminders 00:00-06:00 (the thread can still expire). The director gets
one morning summary: per outlet answered / slow / no reply, the questions
nobody answered, and the problems staff reported.

Pure planning and formatting live here; Telegram and DB glue is in bot.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

import cashier_names
import staff_chat
import staff_ops

logger = logging.getLogger(__name__)

TABLE = "staff_chat_thread"

QUEUED, OPEN, REMINDED = "queued", "open", "reminded"
ANSWERED, NO_REPLY, DROPPED = "answered", "no_reply", "dropped"
INFO = "info"            # sent, no reply expected (staff_ops sales note, tip, praise)
ACTIVE = (OPEN, REMINDED)

# A money question gets one reminder after 30 minutes; every question expires after an hour,
# so it never holds the next check-in back (nothing drifts past midnight).
REMIND_AFTER = timedelta(minutes=30)
EXPIRE_AFTER = timedelta(hours=1)
LATE_TAP_WINDOW = timedelta(hours=12)   # a button tap on an expired question still counts
DETAIL_WINDOW = timedelta(hours=1)      # typed details after "Change" / "Problem"
QUIET_START_HOUR, QUIET_END_HOUR = 0, 6
# Only the money questions get the 30-minute reminder. Wastage, leftover,
# taste check, sales note (and the paused check-ins) just expire and show as
# "no reply" in the director's morning summary.
REMIND_SLOTS = ("bills", "minimarket", "invoice")
SLOW_REPLY_MINUTES = 30

REPLY_STATUSES = ("ok", "short", "finished", "problem", "order", "handed_in", "other")
HANDED_IN = "handed_in"


def live_outlets() -> set[str]:
    raw = os.environ.get("STAFF_CHAT_LIVE_OUTLETS") or ""
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def is_live(outlet_code) -> bool:
    return str(outlet_code or "").strip().upper() in live_outlets()


def enabled_slots() -> set[str]:
    """Check-ins that run, from ``STAFF_CHAT_SLOTS`` (e.g. ``order,bills``).
    Unset or empty = all of them."""
    raw = os.environ.get("STAFF_CHAT_SLOTS") or ""
    chosen = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return chosen or set(staff_chat.SLOTS) | set(staff_ops.OPS_SLOTS)


def slot_enabled(slot) -> bool:
    return str(slot or "").lower() in enabled_slots()


def _ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _quiet(now: datetime) -> bool:
    local = now.astimezone(cashier_names.MALAYSIA_TZ)
    return QUIET_START_HOUR <= local.hour < QUIET_END_HOUR


# --- planning ------------------------------------------------------------------

def plan_tick(threads: list[dict], now: datetime) -> list[tuple[str, dict]]:
    """What to do now, per group: ``[(action, thread)]`` with action one of
    remind / expire / drop / release. Pure."""
    actions: list[tuple[str, dict]] = []
    by_chat: dict = {}
    for t in threads or []:
        by_chat.setdefault(t.get("chat_id"), []).append(t)
    shift_now = cashier_names.shift_at(now)
    for _chat, items in by_chat.items():
        active = [t for t in items if t.get("status") in ACTIVE]
        still_active = False
        for t in active:
            asked = _ts(t.get("asked_at"))
            if asked is None:
                continue
            age = now - asked
            if age >= EXPIRE_AFTER:
                actions.append(("expire", t))
            elif (t.get("status") == OPEN and age >= REMIND_AFTER and not _quiet(now)
                  and t.get("slot") in REMIND_SLOTS):
                actions.append(("remind", t))
                still_active = True
            else:
                still_active = True
        queued = sorted(
            (t for t in items if t.get("status") == QUEUED),
            key=lambda t: str(t.get("created_at") or ""),
        )
        released = False
        for t in queued:
            same_shift = (t.get("shift"), str(t.get("shift_date"))) == (
                shift_now[0], shift_now[1].isoformat()
            )
            if not same_shift:
                actions.append(("drop", t))
            elif not still_active and not released:
                actions.append(("release", t))
                released = True
    return actions


def thread_row(*, outlet_code, chat_id, slot, text, question_en, facts,
               language, cashier, now: datetime, status: str,
               message_id=None) -> dict:
    shift, shift_date = cashier_names.shift_at(now)
    return {
        "outlet_code": outlet_code,
        "chat_id": chat_id,
        "slot": slot,
        "status": status,
        "question_text": text,
        "question_en": question_en,
        "facts": facts or {},
        "language": language,
        "cashier": cashier,
        "message_id": message_id,
        "asked_at": now.isoformat() if status in ACTIVE else None,
        "shift": shift,
        "shift_date": shift_date.isoformat(),
    }


# --- wording for reminders / clarifications ----------------------------------------

_REMIND = {
    "bm": "Tadi saya ada tanya — boleh jawab sikit? 🙏",
    "tamil": "முன்னாடி ஒரு கேள்வி கேட்டேன் — கொஞ்சம் பதில் சொல்லுங்க 🙏",
    "english": "Just checking on my earlier question — a quick reply please 🙏",
    "indonesian": "Tadi saya tanya — tolong dibalas sebentar ya 🙏",
    "bengali": "Age ekta proshno korechilam — ektu uttor diben 🙏",
}
_CLARIFY = {
    "bm": "Maaf, saya kurang faham. Boleh cakap sikit lagi, senang-senang?",
    "tamil": "மன்னிக்கணும், சரியா புரியல. கொஞ்சம் சுலபமா சொல்லுங்க?",
    "english": "Sorry, I didn't quite get that. Could you say it a bit more simply?",
    "indonesian": "Maaf, saya kurang paham. Bisa dijelaskan sedikit lagi?",
    "bengali": "Sorry, thik bujhte parini. Ektu shohoj kore bolben?",
}


def _in_language(table: dict, language: str) -> str:
    if language == staff_chat.BM_TAMIL:
        return f"{table['bm']}\n{table['tamil']}"
    return table.get(language) or table["bm"]


_REMIND_MANY = {
    "bm": "Ada {n} soalan tadi belum dijawab — boleh tekan butang pada soalan tu? 🙏",
    "tamil": "முன்னாடி கேட்ட {n} கேள்விக்கு இன்னும் பதில் வரல — அந்த கேள்வியில இருக்கற button-அ தட்டுங்க 🙏",
    "english": "{n} earlier questions still need an answer — a quick tap on their buttons please 🙏",
    "indonesian": "Ada {n} pertanyaan tadi yang belum dijawab — tolong tekan tombol di pertanyaan itu ya 🙏",
    "bengali": "Age-r {n}-ta proshner uttor ekhono ashe nai — proshner button-e ektu tap korun 🙏",
}


def reminder_text(language: str, count: int = 1) -> str:
    """One reminder per group: several unanswered questions share one
    message instead of one each."""
    if count > 1:
        table = {k: v.format(n=count) for k, v in _REMIND_MANY.items()}
        return _in_language(table, language)
    return _in_language(_REMIND, language)


def group_reminders(actions: list[tuple[str, dict]]) -> dict:
    """``{chat_id: [threads]}`` due for their one reminder, from plan_tick's
    actions — sent as one message per group."""
    out: dict = {}
    for action, t in actions:
        if action == "remind":
            out.setdefault(t.get("chat_id"), []).append(t)
    return out


def clarify_text(language: str) -> str:
    return _in_language(_CLARIFY, language)


# --- reading replies -------------------------------------------------------------

REPLY_PROMPT = (
    "A restaurant office asked the cashier of one outlet a question in the "
    "outlet's staff group chat. Then a staff member wrote the message below "
    "(Tamil, Malay, Bengali, English or a mix; may be in English letters). "
    "Decide:\n"
    "- is_answer: is it a reply to that question (not unrelated chatter, "
    "not a bill photo caption, not talk between staff)?\n"
    "- clear: if it is an answer, is it clear enough to act on?\n"
    "- summary_en: one short English sentence of what they said.\n"
    "- status: ok (all fine / confirmed), short (something running low), "
    "finished (something sold out / finished), problem (broken, issue), "
    "order (they gave order items/quantities), handed_in (for a bill "
    "question: they gave the paper bill to the boss / office instead of "
    "uploading it), other.\n"
    "- items: ONLY when they list things to order with quantities, each "
    "{item, qty, unit}: item = the usual Malay name in English letters "
    "(ayam, ikan, sotong, udang, kambing, daging, telur, santan, roti, gas "
    "...); keep the exact kind when they say it: bawang besar (onion), bawang "
    "merah (shallot), bawang putih (garlic), cili kering (dry chilli), "
    "kentang, tomato, halia, daun kari. qty = a number exactly as they wrote it, unit = kg, "
    "pcs, ekor, kotak, biji, tin, pack, guni or liter (empty if none). Never "
    "invent an item or quantity they did not write. Otherwise [].\n"
    "- asks_if_bot: true if they ask whether they are talking to a person, "
    "a bot, a robot or a machine.\n"
    'Reply with JSON only: {"is_answer": true|false, "clear": true|false, '
    '"summary_en": "...", "status": "ok|short|finished|problem|order|other", '
    '"items": [], "asks_if_bot": false}'
)


def parse_reply(question_en, question_text, reply_text, complete) -> dict | None:
    """The AI's reading of a reply, or None when it couldn't be read."""
    try:
        result = complete(REPLY_PROMPT, json.dumps(
            {"question_in_english": question_en, "question_as_sent": question_text,
             "staff_message": reply_text},
            ensure_ascii=False,
        ))
    except Exception:
        logger.exception("staff live: reply parse failed")
        return None
    data = (result or {}).get("data")
    if not isinstance(data, dict) or "is_answer" not in data:
        return None
    status = str(data.get("status") or "other").lower()
    items = data.get("items")
    return {
        "is_answer": data.get("is_answer") is True,
        "clear": data.get("clear") is not False,
        "summary_en": str(data.get("summary_en") or "").strip(),
        "status": status if status in REPLY_STATUSES else "other",
        "items": items if isinstance(items, list) else [],
        "asks_if_bot": data.get("asks_if_bot") is True,
    }


# --- honesty: never pretend to be a person ---------------------------------------

_HONEST = {
    "english": "This is the Khulafa office system; the boss reads every reply every morning.",
    "bm": "Ini sistem pejabat Khulafa; bos baca setiap jawapan setiap pagi.",
    "tamil": "இது Khulafa office system; ஒவ்வொரு பதிலையும் boss தினமும் காலையில படிப்பாங்க.",
    "indonesian": "Ini sistem kantor Khulafa; bos membaca setiap balasan setiap pagi.",
    "bengali": "Eta Khulafa office er system; boss protidin shokale shob uttor poren.",
}

# "Are you a bot / a person?" in the languages staff write. Matched on the
# raw text so it works with no open question and no AI call.
_BOT_Q = re.compile(
    r"(?:\b(?:bot|robot|chatbot|machine|mesin|ai)\b[^\n]{0,20}\?"
    r"|\b(?:are|r)\s+(?:you|u)\s+(?:a\s+)?(?:bot|robot|human|real|person|machine)\b"
    r"|\b(?:is\s+this|this\s+is)\s+(?:a\s+)?(?:bot|robot|real person|human)\b"
    r"|\b(?:ni|ini|awak|kamu|anda|you)\s+(?:ni\s+)?(?:bot|robot)\b"
    r"|\b(?:bot|robot)\s*(?:ke|kah|ka|ah|aa?|na)\b"
    r"|\b(?:manusia|orang|human|manush)\s+(?:ke|kah|atau|or|na)\b"
    r"|\bapni\s+ki\s+(?:bot|manush|robot)\b"
    r"|(?:bot|robot)[-\s]?(?:ஆ|ஆ\?)"
    r"|மனுஷ(?:ன|ர)?(?:ா|ஆ)"
    r"|ஆளா\?)",
    re.IGNORECASE,
)


def asks_if_bot(text) -> bool:
    return bool(_BOT_Q.search(str(text or "")))


def honest_reply(language: str) -> str:
    """The one honest answer to "am I talking to a person?"."""
    if language == staff_chat.BM_TAMIL:
        return f"{_HONEST['bm']}\n{_HONEST['tamil']}"
    return _HONEST.get(language) or _HONEST["bm"]


def decide_reply(thread: dict, parsed: dict | None, *, is_reply_to_question: bool) -> str:
    """What to do with a staff message while ``thread`` is active:
    'answer' (save + move on), 'clarify' (ask once more), or 'ignore'."""
    if parsed is None:
        # AI unreadable: only a direct reply to the question counts.
        return "answer" if is_reply_to_question else "ignore"
    if not parsed["is_answer"] and not is_reply_to_question:
        return "ignore"
    if not parsed["clear"] and not thread.get("clarify_sent_at"):
        return "clarify"
    return "answer"


# --- tap-to-answer buttons -------------------------------------------------------
#
# Every question carries buttons in the cashier's language. A tap counts as
# the answer; "Change" / "Problem" / "Something ran out" also ask them to
# type the details, which are added to the same answer.

# code -> (reply_status, English summary, asks for typed details)
CHOICES = {
    "ok": ("ok", "OK", False),
    "change": ("order", "Wants to change it", True),
    "allok": ("ok", "All OK", False),
    "problem": ("problem", "Has a problem", True),
    "upload": ("ok", "Uploading the bill now", False),
    "gave": (HANDED_IN, "Gave the bill to the boss", False),
    "nobill": ("other", "No bill", False),
    "ranout": ("finished", "Something ran out", True),
    "enough": ("ok", "Enough", False),
    "short": ("short", "Not enough", True),
}

# button set -> the choices it offers, in order
BUTTON_SETS = {
    "order": ("ok", "change"),
    "status": ("allok", "problem"),
    "bills": ("upload", "gave", "nobill"),
    "lunch": ("ok", "ranout"),
    "stock": ("enough", "short"),
    "cook": ("ok", "change"),
}

_LABELS = {
    "english": {"ok": "✅ OK", "change": "✏️ Change", "allok": "✅ All OK",
                "problem": "⚠️ Problem", "upload": "✅ Uploading now",
                "gave": "📦 Gave to boss", "nobill": "❌ No bill",
                "ranout": "⚠️ Something ran out", "enough": "✅ Enough",
                "short": "⚠️ Not enough"},
    "bm": {"ok": "✅ OK", "change": "✏️ Tukar", "allok": "✅ Semua OK",
           "problem": "⚠️ Ada masalah", "upload": "✅ Upload sekarang",
           "gave": "📦 Dah bagi bos", "nobill": "❌ Tiada bil",
           "ranout": "⚠️ Ada yang habis", "enough": "✅ Cukup",
           "short": "⚠️ Tak cukup"},
    "tamil": {"ok": "✅ சரி", "change": "✏️ மாத்தணும்", "allok": "✅ எல்லாம் சரி",
              "problem": "⚠️ பிரச்சனை இருக்கு", "upload": "✅ இப்போ upload பண்றேன்",
              "gave": "📦 Boss-கிட்ட கொடுத்தேன்", "nobill": "❌ Bill இல்ல",
              "ranout": "⚠️ ஏதோ தீர்ந்துடுச்சு", "enough": "✅ போதும்",
              "short": "⚠️ போதாது"},
    "bengali": {"ok": "✅ Thik ache", "change": "✏️ Bodlabo", "allok": "✅ Shob thik",
                "problem": "⚠️ Shomossha ache", "upload": "✅ Ekhon upload korchi",
                "gave": "📦 Boss ke diyechi", "nobill": "❌ Bill nai",
                "ranout": "⚠️ Kichu shesh", "enough": "✅ Jothesto",
                "short": "⚠️ Kom ache"},
    "indonesian": {"ok": "✅ Oke", "change": "✏️ Ganti", "allok": "✅ Semua aman",
                   "problem": "⚠️ Ada masalah", "upload": "✅ Upload sekarang",
                   "gave": "📦 Sudah kasih bos", "nobill": "❌ Tidak ada nota",
                   "ranout": "⚠️ Ada yang habis", "enough": "✅ Cukup",
                   "short": "⚠️ Kurang"},
}

_DETAIL_PROMPT = {
    "change": {
        "english": "OK, please type the changes (item and quantity).",
        "bm": "Ok, taip apa nak tukar (barang & berapa).",
        "tamil": "சரி, என்ன மாத்தணும்னு type பண்ணுங்க (சாமான், அளவு).",
        "bengali": "Thik ache, ki bodlaben likhe din (jinish ar koto).",
        "indonesian": "Oke, ketik apa yang mau diganti (barang & jumlah).",
    },
    "problem": {
        "english": "What's the problem? Please type a few words.",
        "bm": "Apa masalahnya? Taip sikit ya.",
        "tamil": "என்ன பிரச்சனை? கொஞ்சம் type பண்ணுங்க.",
        "bengali": "Ki shomossha? Ektu likhe din.",
        "indonesian": "Masalahnya apa? Tolong ketik ya.",
    },
    "ranout": {
        "english": "What ran out? Please type it.",
        "bm": "Apa yang habis? Taip ya.",
        "tamil": "என்ன தீர்ந்துச்சு? Type பண்ணுங்க.",
        "bengali": "Ki shesh hoyeche? Likhe din.",
        "indonesian": "Apa yang habis? Tolong ketik ya.",
    },
    "short": {
        "english": "What is not enough? Please type it.",
        "bm": "Apa yang tak cukup? Taip ya.",
        "tamil": "எது போதாது? Type பண்ணுங்க.",
        "bengali": "Ki kom ache? Likhe din.",
        "indonesian": "Apa yang kurang? Tolong ketik ya.",
    },
}

_THANKS = {"english": "Noted, thank you 🙏", "bm": "Baik, terima kasih 🙏",
           "tamil": "சரி, நன்றி 🙏", "bengali": "Thik ache, dhonnobad 🙏",
           "indonesian": "Oke, terima kasih 🙏"}


# Staff questions v2 (staff_ops): invoice, mini market, taste, leftover and
# wastage buttons share the same tap handling.
CHOICES.update(staff_ops.CHOICES)
BUTTON_SETS.update(staff_ops.BUTTON_SETS)
for _l, _labels in staff_ops.LABELS.items():
    _LABELS[_l].update(_labels)
_DETAIL_PROMPT.update(staff_ops.DETAIL_PROMPTS)


def _lang(language: str) -> str:
    """Buttons are short: BM+Tamil cashiers get the BM labels."""
    return language if language in _LABELS else "bm"


def button_set(slot: str, facts: dict | None) -> str | None:
    """Which buttons a question gets. The open "what do you need tomorrow?"
    order question has none — the answer has to be typed."""
    if slot in staff_ops.OPS_SLOTS:
        return staff_ops.button_set(slot, facts)
    if slot == "order":
        return None if (facts or {}).get("ask") else "order"
    if slot in ("open", "night"):
        return "status"
    return slot if slot in BUTTON_SETS else None


def keyboard(thread_id, set_key: str | None, language: str) -> list[list[tuple[str, str]]]:
    """Rows of ``(label, callback_data)``; ``[]`` for no buttons. Kept free of
    Telegram types so it can be tested; bot.py builds the markup."""
    if not set_key or thread_id is None:
        return []
    labels = _LABELS[_lang(language)]
    buttons = [(labels[c], f"sc:{thread_id}:{c}") for c in BUTTON_SETS[set_key]]
    # Two sit side by side, four make a 2x2 grid, three stack.
    if len(buttons) == 4:
        return [buttons[:2], buttons[2:]]
    return [[b] for b in buttons] if len(buttons) > 2 else [buttons]


def parse_callback(data) -> tuple[int, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "sc" or parts[2] not in CHOICES:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def tap_outcome(thread: dict, code: str, now: datetime) -> str:
    """What a tap on ``thread`` does: 'answer', 'already' (answered before)
    or 'stale' (dropped, or expired too long ago)."""
    status = thread.get("status")
    if status in ACTIVE:
        return "answer"
    if status == ANSWERED:
        return "already"
    asked = _ts(thread.get("asked_at"))
    if status == NO_REPLY and asked and now - asked <= LATE_TAP_WINDOW:
        return "answer"
    return "stale"


def tap_fields(code: str, label: str, now: datetime) -> dict:
    """The thread update for a tap."""
    reply_status, summary, detail = CHOICES[code]
    return {
        "status": ANSWERED,
        "answered_at": now.isoformat(),
        "reply_text": f"[button] {label}",
        "reply_en": summary,
        "reply_status": reply_status,
        "answer_source": "button",
        "awaiting_detail": detail,
    }


def detail_prompt(code: str, language: str) -> str | None:
    table = _DETAIL_PROMPT.get(code)
    if not table:
        return None
    if language == staff_chat.BM_TAMIL:
        return f"{table['bm']}\n{table['tamil']}"
    return table.get(language) or table["bm"]


def thanks_text(language: str) -> str:
    return _THANKS.get(_lang(language), _THANKS["bm"])


def detail_fields(thread: dict, text: str, summary_en: str | None) -> dict:
    """Typed details after a Change / Problem tap, added to the answer."""
    base = thread.get("reply_en") or ""
    detail = (summary_en or text or "").strip()
    return {
        "reply_text": f"{thread.get('reply_text') or ''}\n{text}".strip(),
        "reply_en": f"{base}: {detail}" if base else detail,
        "awaiting_detail": False,
    }


def awaiting_detail(thread: dict | None, now: datetime) -> bool:
    if not thread or not thread.get("awaiting_detail"):
        return False
    answered = _ts(thread.get("answered_at"))
    return bool(answered and now - answered <= DETAIL_WINDOW)


def handin_row(thread: dict) -> dict | None:
    """``staff_bill_handins`` row for a bill question answered "gave it to
    the boss"; None when the thread isn't one."""
    if thread.get("slot") != "bills":
        return None
    facts = thread.get("facts") or {}
    supplier = facts.get("supplier_full") or facts.get("supplier")
    if not supplier:
        return None
    return {
        "outlet_code": thread.get("outlet_code"),
        "supplier": supplier,
        "last_bill": facts.get("last_iso"),
        "days_missing": int(facts["days"]) if str(facts.get("days") or "").isdigit() else None,
        "cashier": thread.get("cashier"),
        "thread_id": thread.get("id"),
    }


# --- director's morning summary --------------------------------------------------

def outlet_stats(threads: list[dict]) -> dict:
    """Per outlet: asked, answered, no_reply, avg reply minutes."""
    stats: dict = {}
    for t in threads or []:
        if t.get("status") not in (ANSWERED, NO_REPLY, OPEN, REMINDED):
            continue
        s = stats.setdefault(t.get("outlet_code"), {
            "asked": 0, "answered": 0, "no_reply": 0, "minutes": []})
        s["asked"] += 1
        if t.get("status") == ANSWERED:
            s["answered"] += 1
            asked, answered = _ts(t.get("asked_at")), _ts(t.get("answered_at"))
            if asked and answered:
                s["minutes"].append(max(0.0, (answered - asked).total_seconds() / 60))
        elif t.get("status") == NO_REPLY:
            s["no_reply"] += 1
    for s in stats.values():
        m = s.pop("minutes")
        s["avg_minutes"] = round(sum(m) / len(m)) if m else None
    return stats


def verdict(s: dict) -> str:
    if not s["asked"]:
        return "—"
    rate = s["answered"] / s["asked"]
    if rate < 0.5:
        return "❌ no reply"
    if rate < 0.8 or (s["avg_minutes"] or 0) > SLOW_REPLY_MINUTES:
        return "🐢 slow"
    return "✅ answered"


def _local_hhmm(value) -> str:
    ts = _ts(value)
    return ts.astimezone(cashier_names.MALAYSIA_TZ).strftime("%H:%M") if ts else "?"


_SENT = (ANSWERED, NO_REPLY, OPEN, REMINDED)


def _mark(t: dict) -> str:
    """✅ 12m (answered, minutes to reply) · ✗ (no reply) · ⏳ (still open)."""
    status = t.get("status")
    if status == ANSWERED:
        asked, answered = _ts(t.get("asked_at")), _ts(t.get("answered_at"))
        if asked and answered:
            return f"✅ {max(0, round((answered - asked).total_seconds() / 60))}m"
        return "✅"
    return "✗" if status == NO_REPLY else "⏳"


def _timeline(items: list[dict]) -> str:
    """Every question sent to one outlet, in order: its send time, slot and
    outcome — so the times that get no replies stand out."""
    sent = sorted((t for t in items if t.get("status") in _SENT and t.get("asked_at")),
                  key=lambda t: str(t.get("asked_at")))
    return " · ".join(f"{_local_hhmm(t['asked_at'])} {t.get('slot')} {_mark(t)}"
                      for t in sent)


def _by_slot(threads: list[dict]) -> list[str]:
    """Answer rate per check-in across outlets, in schedule order."""
    counts: dict[str, list[int]] = {}
    for t in threads:
        if t.get("status") in _SENT and t.get("asked_at"):
            c = counts.setdefault(t.get("slot"), [0, 0])
            c[1] += 1
            c[0] += t.get("status") == ANSWERED
    order = sorted(set(staff_chat.SLOTS) | set(staff_ops.SLOT_TIMES),
                   key=lambda s: (staff_chat.SLOTS[s][1] if s in staff_chat.SLOTS
                                  else staff_ops.SLOT_TIMES[s]))
    rows = []
    for slot in sorted(counts, key=lambda s: order.index(s) if s in order else 99):
        answered, sent = counts[slot]
        time_ = (staff_chat.SLOTS[slot][1] if slot in staff_chat.SLOTS
                 else staff_ops.SLOT_TIMES.get(slot, "?"))
        rows.append(f"{time_} {slot}: {answered}/{sent} answered")
    return rows


def format_morning_summary(threads: list[dict], label_for=None) -> str:
    """One director message: per live outlet the replies line and every
    question's send time with its outcome, the answer rate per check-in
    time, and the problems staff reported."""
    label = label_for or (lambda c: str(c))
    stats = outlet_stats(threads)
    if not stats:
        return ""
    lines = ["🗒️ Staff replies — last 24 hours",
             "✅ answered (minutes to reply) · ✗ no reply · ⏳ still open", ""]
    for code in sorted(stats):
        s = stats[code]
        avg = f" · avg {s['avg_minutes']} min" if s["avg_minutes"] is not None else ""
        lines.append(f"{label(code)}: {verdict(s)} — {s['answered']}/{s['asked']} answered{avg}")
        timeline = _timeline([t for t in threads if t.get("outlet_code") == code])
        if timeline:
            lines.append(f"   {timeline}")
    by_slot = _by_slot(threads)
    if by_slot:
        lines += ["", "By check-in time (all outlets):"] + [f"• {r}" for r in by_slot]
    handed = [t for t in threads if t.get("status") == ANSWERED
              and t.get("slot") == "bills" and t.get("reply_status") == HANDED_IN]
    if handed:
        lines += ["", "📦 Bills handed in, not uploaded — please check the paper bills:"]
        for t in sorted(handed, key=lambda t: str(t.get("answered_at"))):
            f = t.get("facts") or {}
            last = f" (last upload {f['last']})" if f.get("last") else ""
            lines.append(
                f"• {label(t.get('outlet_code'))}: {f.get('supplier') or 'supplier?'}{last}"
                f" — {t.get('cashier') or 'cashier'}, {_local_hhmm(t.get('answered_at'))}"
            )
    issues = [t for t in threads if t.get("status") == ANSWERED
              and t.get("slot") not in staff_ops.OPS_SLOTS
              and t.get("reply_status") in ("short", "finished", "problem")]
    if issues:
        lines += ["", "What they reported:"]
        for t in sorted(issues, key=lambda t: str(t.get("answered_at"))):
            lines.append(
                f"• {label(t.get('outlet_code'))} {_local_hhmm(t.get('answered_at'))} "
                f"{t.get('slot')}: {t.get('reply_en') or t.get('reply_text')}"
            )
    lines += staff_ops.summary_sections(threads, label)
    return "\n".join(lines)
