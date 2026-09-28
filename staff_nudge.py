"""Follow-up nudges for unanswered check-ins.

After a scheduled check-in (staff_chat.SLOTS) an outlet gets
``NUDGE_AFTER_MIN`` minutes (Render env, default 40) to reply. Then ONE
reminder goes out; after another ``NUDGE_AFTER_MIN`` without a reply a
second, firmer one. Never more than two per check-in, none before 07:00
or after 23:30, none for an outlet marked closed for the day
(``/closed <OUTLET>``), and none for an outlet the director silenced for
the day with ``/nudge_off <OUTLET> today`` (``outlet_nudge_off``,
migrations/0057 — the outlet is NOT closed: its check-ins still go out).
The question expires (no_reply) one more window after the second nudge.

The AI provider (``staff_ai``) only rephrases the nudge in the cashier's
language. It is given three facts — the outlet's name, which check-in and
how many minutes have passed — and nothing else, so the fact check is
simple: every number in the wording must be one of those, no items, no
money, no other names. Any failure falls back to the plain template.

Every nudge is logged to ``staff_chat_log`` with ``kind = 'nudge'`` and
``nudge_no = 1 | 2`` (migrations/0050).

Pure: no Telegram, no database. bot.py does the sending.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta

import staff_ai
import staff_chat

logger = logging.getLogger(__name__)

DEFAULT_AFTER_MIN = 40
MAX_NUDGES = 2
WINDOW_START = time(7, 0)
WINDOW_END = time(23, 30)
KIND = "nudge"

# Human names of the check-ins, for the facts (the slot key alone means
# nothing to the writer or the cashier).
CHECKIN_NAMES = {
    "open": "morning opening", "stock": "stock check", "cook": "cook plan",
    "lunch": "lunch crowd", "order": "tomorrow's order", "bills": "supplier bill",
    "night": "night check", "invoice": "supplier invoice", "minimarket": "mini market buy",
    "leftover": "leftover food", "wastage": "wastage", "afternoon": "taste check",
    "po_mismatch": "bill vs order",
}


def after() -> timedelta:
    """The nudge interval (``NUDGE_AFTER_MIN``, default 40 minutes)."""
    raw = (os.environ.get("NUDGE_AFTER_MIN") or "").strip()
    try:
        minutes = int(raw) if raw else DEFAULT_AFTER_MIN
    except ValueError:
        minutes = DEFAULT_AFTER_MIN
    return timedelta(minutes=max(1, minutes))


def slots() -> set[str]:
    """Check-ins that get nudges: ``NUDGE_SLOTS`` (e.g. ``order,bills``),
    default every scheduled check-in plus the money questions."""
    raw = os.environ.get("NUDGE_SLOTS") or ""
    chosen = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return chosen or set(staff_chat.SLOTS) | {"invoice", "minimarket", "po_mismatch"}


def in_window(now: datetime) -> bool:
    """07:00–23:30 Malaysia time."""
    local = now.astimezone(staff_chat.cashier_names.MALAYSIA_TZ).time()
    return WINDOW_START <= local <= WINDOW_END


def due(thread: dict, now: datetime, *, asked: datetime | None = None) -> int | None:
    """Which nudge (1 or 2) ``thread`` is due for now, else None. ``asked``
    overrides the thread's asked_at (parsed by the caller)."""
    if thread.get("slot") not in slots():
        return None
    count = int(thread.get("nudge_count") or 0)
    if count >= MAX_NUDGES:
        return None
    asked = asked or _ts(thread.get("asked_at"))
    if asked is None:
        return None
    if now - asked < after() * (count + 1):
        return None
    return count + 1


def expire_after(slot: str) -> timedelta:
    """A nudged check-in lives one window past its last nudge."""
    if slot in slots():
        return after() * (MAX_NUDGES + 1)
    return timedelta(hours=1)


def _ts(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# --- wording ---------------------------------------------------------------------

_T = {
    1: {
        "bm": "Tadi saya tanya pasal {checkin} {minutes} minit lepas — boleh jawab sikit? 🙏",
        "tamil": "{minutes} நிமிஷம் முன்னாடி {checkin} பத்தி கேட்டேன் — கொஞ்சம் பதில் சொல்லுங்க 🙏",
        "english": "I asked about the {checkin} {minutes} minutes ago — a quick reply please 🙏",
        "indonesian": "Tadi saya tanya soal {checkin} {minutes} menit lalu — tolong dibalas ya 🙏",
        "bengali": "{minutes} minute age {checkin} niye proshno korechilam — ektu uttor diben 🙏",
    },
    2: {
        "bm": "{outlet}, soalan {checkin} dah {minutes} minit tak berjawab. Tolong jawab sekarang ya.",
        "tamil": "{outlet}, {checkin} கேள்விக்கு {minutes} நிமிஷமா பதில் இல்ல. இப்பவே பதில் சொல்லுங்க.",
        "english": "{outlet}, the {checkin} question has waited {minutes} minutes. Please answer now.",
        "indonesian": "{outlet}, pertanyaan {checkin} sudah {minutes} menit belum dijawab. Tolong jawab sekarang ya.",
        "bengali": "{outlet}, {checkin} proshno {minutes} minute dhore uttor chara. Ekhon uttor din please.",
    },
}

_CHECKIN_WORDS = {
    "bm": {"open": "kedai buka", "stock": "stok", "cook": "masak hari ni", "lunch": "lunch",
           "order": "order esok", "bills": "bil supplier", "night": "malam ni",
           "invoice": "invois", "minimarket": "mini market", "leftover": "baki makanan",
           "wastage": "buangan", "afternoon": "rasa lauk", "po_mismatch": "bil vs order"},
    "tamil": {"open": "கடை திறப்பு", "stock": "stock", "cook": "இன்னைக்கு சமையல்", "lunch": "lunch",
              "order": "நாளைய order", "bills": "supplier bill", "night": "ராத்திரி",
              "invoice": "invoice", "minimarket": "mini market", "leftover": "மீதி சாப்பாடு",
              "wastage": "wastage", "afternoon": "ருசி", "po_mismatch": "bill vs order"},
    "indonesian": {"open": "toko buka", "stock": "stok", "cook": "masak hari ini", "lunch": "makan siang",
                   "order": "order besok", "bills": "nota supplier", "night": "malam ini",
                   "invoice": "nota", "minimarket": "mini market", "leftover": "sisa makanan",
                   "wastage": "buangan", "afternoon": "rasa lauk", "po_mismatch": "nota vs order"},
    "bengali": {"open": "dokan khola", "stock": "stock", "cook": "aajker ranna", "lunch": "lunch",
                "order": "kalker order", "bills": "supplier bill", "night": "raat",
                "invoice": "invoice", "minimarket": "mini market", "leftover": "baki khabar",
                "wastage": "wastage", "afternoon": "swad", "po_mismatch": "bill vs order"},
}


def facts_for(thread: dict, outlet_label: str, now: datetime, nudge_no: int) -> dict:
    """The ONLY facts the writer gets: outlet name, check-in, minutes."""
    asked = _ts(thread.get("asked_at")) or now
    minutes = max(1, int((now - asked).total_seconds() // 60))
    slot = str(thread.get("slot") or "")
    return {"outlet": outlet_label, "checkin": CHECKIN_NAMES.get(slot, slot),
            "slot": slot, "minutes": str(minutes), "nudge_no": nudge_no}


def template(facts: dict, language: str) -> str:
    """The plain nudge in the cashier's language."""
    no = 2 if int(facts.get("nudge_no") or 1) >= 2 else 1

    def one(lang):
        words = _CHECKIN_WORDS.get(lang, {})
        checkin = words.get(facts.get("slot"), facts.get("checkin") or "")
        return _T[no][lang].format(outlet=facts.get("outlet", ""), checkin=checkin,
                                   minutes=facts.get("minutes", ""))
    if language == staff_chat.BM_TAMIL:
        return f"{one('bm')}\n{one('tamil')}"
    return one(language if language in _T[no] else "bm")


SYSTEM_PROMPT = (
    "You write a short Telegram reminder from Khulafa HQ (a Malaysian "
    "restaurant office) to the cashier of one outlet who has not answered a "
    "question the office asked earlier. Sound like a boss on WhatsApp: "
    "polite the first time, firmer the second time, never rude.\n"
    "Rules:\n"
    "- 1 or 2 short lines. No name at the start (it is added for you), no "
    "signature, never claim to be a person.\n"
    "- Use ONLY the facts given: the outlet name, which question it was and "
    "how many minutes have passed. Do not add items, numbers, suppliers, "
    "money or anything else. Write the minutes with 0-9 digits exactly as given.\n"
    "- Say the same thing as the reference message, in your own words, in "
    "the language asked for.\n"
    'Reply with JSON only: {"text": "<the reminder>", "english": "<the same '
    'in plain English>"}'
)


def check(text: str, facts: dict, language: str, other_names=()) -> list[str]:
    """Why the AI wording can't go: anything beyond the three facts."""
    problems = staff_chat.fact_check(
        text, {k: facts.get(k) for k in ("outlet", "minutes")},
        other_names=other_names, language=language, slot=None,
    )
    if str(facts.get("minutes") or "") not in str(text or ""):
        problems.append("minutes not written as in the data")
    return problems


def build(thread: dict, outlet_label: str, now: datetime, nudge_no: int, *,
          complete=None, other_names=()) -> dict:
    """Word one nudge. Same shape as staff_chat.build_message: ``{text,
    english, source, problems, template, ai_text, provider, model,
    tokens_in, tokens_out, facts}``. Never raises."""
    language = thread.get("language") or staff_chat.DEFAULT_LANGUAGE
    facts = facts_for(thread, outlet_label, now, nudge_no)
    plain = template(facts, language)
    out = {
        "text": plain, "english": "", "source": "template", "problems": [],
        "template": plain, "ai_text": None, "provider": staff_ai.provider(),
        "model": staff_ai.model(), "tokens_in": None, "tokens_out": None,
        "facts": facts,
    }
    complete = complete or staff_ai.complete_json
    user = staff_chat.build_user_prompt("night", language, facts, plain, f"nudge-{nudge_no}")
    try:
        result = complete(SYSTEM_PROMPT, user)
    except Exception:
        logger.exception("staff nudge: provider call failed")
        result = None
    if not result:
        out["problems"] = ["ai unavailable"]
        return out
    data = result.get("data") or {}
    ai_text = str(data.get("text") or "").strip()
    out.update(ai_text=ai_text, english=str(data.get("english") or "").strip(),
               provider=result.get("provider") or out["provider"],
               model=result.get("model") or out["model"],
               tokens_in=result.get("tokens_in"), tokens_out=result.get("tokens_out"))
    problems = check(ai_text, facts, language, other_names)
    if problems:
        out["problems"] = problems
        out["english"] = ""
        return out
    out.update(text=ai_text, source="ai")
    return out


def log_row(thread: dict, result: dict, mode: str) -> dict:
    """``staff_chat_log`` row for a nudge (kind = 'nudge')."""
    row = staff_chat.log_row(
        thread.get("slot"), thread.get("outlet_code"), thread.get("chat_id"),
        thread.get("cashier"), thread.get("language"), result.get("facts"), result, mode,
    )
    row["kind"] = KIND
    row["nudge_no"] = int((result.get("facts") or {}).get("nudge_no") or 1)
    return row


# --- /nudge_off <OUTLET> today ---------------------------------------------------

OFF_TABLE = "outlet_nudge_off"
OFF_KIND = "nudge_off"


def off_row(outlet_code: str, day, marked_by) -> dict:
    """``outlet_nudge_off`` row: no nudges to this outlet for ``day``."""
    return {"outlet_code": str(outlet_code).upper(), "day": day.isoformat(), "marked_by": marked_by}


def off_log_row(outlet_code: str, day, marked_by, chat_id=None) -> dict:
    """``staff_chat_log`` row recording who silenced the nudges (kind = 'nudge_off')."""
    return {
        "kind": OFF_KIND, "mode": "natural", "slot": OFF_KIND,
        "outlet_code": str(outlet_code).upper(), "chat_id": chat_id, "cashier": None,
        "language": None,
        "facts": {"day": day.isoformat(), "marked_by": marked_by, "scope": "today"},
        "template_text": None, "ai_text": None,
        "final_text": f"nudges off for {str(outlet_code).upper()} on {day.isoformat()}",
        "source": "command", "problems": [], "provider": None, "model": None,
        "tokens_in": None, "tokens_out": None,
    }


def parse_off_args(args, known: set[str]) -> tuple[str | None, str]:
    """``/nudge_off <OUTLET> today`` -> ``(code, "")`` or ``(None, reason)``.
    Only "today" is accepted as the scope (it may be left out)."""
    args = [a.strip() for a in (args or []) if a.strip()]
    if not args:
        return None, "usage"
    code = args[0].upper()
    if code not in known:
        return None, f"Unknown outlet {code}. Known: " + ", ".join(sorted(known))
    scope = args[1].lower() if len(args) > 1 else "today"
    if scope != "today":
        return None, "Only 'today' is supported: /nudge_off <OUTLET> today"
    return code, ""
