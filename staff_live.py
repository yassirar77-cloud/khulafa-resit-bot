"""Live staff chat: the check-in conversation with an outlet group.

Only outlets listed in ``STAFF_CHAT_LIVE_OUTLETS`` (Render env, e.g.
``BISTRO7``) are live; every other outlet stays in director-only preview.

One question at a time, per group. Each check-in is a *thread* in
``staff_chat_thread`` (migrations/0046):

  queued    waiting because another question is still open in that group
  open      sent, waiting for an answer
  reminded  sent + one gentle reminder after 1 hour
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
from datetime import datetime, timedelta

import cashier_names
import staff_chat

logger = logging.getLogger(__name__)

TABLE = "staff_chat_thread"

QUEUED, OPEN, REMINDED = "queued", "open", "reminded"
ANSWERED, NO_REPLY, DROPPED = "answered", "no_reply", "dropped"
ACTIVE = (OPEN, REMINDED)

REMIND_AFTER = timedelta(hours=1)
EXPIRE_AFTER = timedelta(hours=2)
QUIET_START_HOUR, QUIET_END_HOUR = 0, 6
SLOW_REPLY_MINUTES = 30

REPLY_STATUSES = ("ok", "short", "finished", "problem", "order", "other")


def live_outlets() -> set[str]:
    raw = os.environ.get("STAFF_CHAT_LIVE_OUTLETS") or ""
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def is_live(outlet_code) -> bool:
    return str(outlet_code or "").strip().upper() in live_outlets()


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
            elif t.get("status") == OPEN and age >= REMIND_AFTER and not _quiet(now):
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


def reminder_text(language: str) -> str:
    return _in_language(_REMIND, language)


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
    "order (they gave order items/quantities), other.\n"
    'Reply with JSON only: {"is_answer": true|false, "clear": true|false, '
    '"summary_en": "...", "status": "ok|short|finished|problem|order|other"}'
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
    return {
        "is_answer": data.get("is_answer") is True,
        "clear": data.get("clear") is not False,
        "summary_en": str(data.get("summary_en") or "").strip(),
        "status": status if status in REPLY_STATUSES else "other",
    }


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


def format_morning_summary(threads: list[dict], label_for=None) -> str:
    """One director message: the daily replies line per live outlet, the
    unanswered questions, and the problems staff reported."""
    label = label_for or (lambda c: str(c))
    stats = outlet_stats(threads)
    if not stats:
        return ""
    lines = ["🗒️ Staff replies — last 24 hours", ""]
    for code in sorted(stats):
        s = stats[code]
        avg = f" · avg {s['avg_minutes']} min" if s["avg_minutes"] is not None else ""
        lines.append(f"{label(code)}: {verdict(s)} — {s['answered']}/{s['asked']} answered{avg}")
    missed = [t for t in threads if t.get("status") == NO_REPLY]
    if missed:
        lines += ["", "No reply:"]
        for t in sorted(missed, key=lambda t: str(t.get("asked_at"))):
            lines.append(
                f"• {label(t.get('outlet_code'))} {_local_hhmm(t.get('asked_at'))} "
                f"{t.get('slot')} ({t.get('cashier') or 'cashier'})"
            )
    issues = [t for t in threads if t.get("status") == ANSWERED
              and t.get("reply_status") in ("short", "finished", "problem")]
    if issues:
        lines += ["", "What they reported:"]
        for t in sorted(issues, key=lambda t: str(t.get("answered_at"))):
            lines.append(
                f"• {label(t.get('outlet_code'))} {_local_hhmm(t.get('answered_at'))} "
                f"{t.get('slot')}: {t.get('reply_en') or t.get('reply_text')}"
            )
    return "\n".join(lines)
