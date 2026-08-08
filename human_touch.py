"""Human-touch layer — the bot's supervision feels like a person, honestly.

The managers should feel a real person behind every question — and they
can, without a single lie, because a real person IS behind it: the owner
receives every answer, every silence, every flag. This module makes that
presence felt:

  * questions greet the manager BY NAME, with wording that changes day to
    day (deterministically — hash of chat + date — so tests stay exact
    and no two days read like the same template);
  * the thank-you ack varies too, and always says — truthfully — that
    the answer went to the boss;
  * shops that answer everything get PRAISED weekly. A supervisor who
    only ever complains reads as a machine; one who notices good work
    reads as a person;
  * the owner gets a weekly response scoreboard (who answers, how fast)
    built from the same question ledger;
  * a "typing…" chat action before a question makes the message land
    like someone composing it.

Deliberately absent: fake human names, "our audit team reviewed this",
or any claim a person did something no person did. The day a manager
catches one fake human, every real message loses its power.

Hard rule: nothing here ever raises.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# Spoken-register Tamil, one thought per line, no literary words.
_GREETINGS = [
    "வணக்கம் {name} 🙏",
    "{name}, ஒரு நிமிஷம் 🙏",
    "{name}, கொஞ்சம் இத பாருங்க 👇",
    "{name}, ஒரு விஷயம் சொல்லணும்",
]

# Every variant truthfully reports the upward step: the reply handler
# forwards the answer to the owner right after this ack is sent.
_ACKS = [
    "சரி {name}, note பண்ணிட்டேன் ✅ Boss-க்கும் அனுப்பிட்டேன். நன்றி!",
    "Ok {name} 👍 பதிவு ஆச்சு ✅ Boss பாப்பாரு. நன்றி!",
    "நல்லது {name} 🙏 எழுதி வெச்சுட்டேன் ✅ Boss-க்கு காட்டிட்டேன்.",
]

_DEFAULT_NAME = "boss"


def _pick(variants: list[str], chat_id, on_date: date) -> str:
    """Deterministic daily rotation: same chat + same day = same line,
    next day a different one. No randomness — replayable in tests."""
    try:
        idx = (abs(int(chat_id or 0)) + on_date.toordinal()) % len(variants)
    except (TypeError, ValueError):
        idx = 0
    return variants[idx]


def _clean_name(manager_name) -> str:
    name = str(manager_name or "").strip()
    if not name:
        return _DEFAULT_NAME
    # First name only — "Ravi boss" reads warmer than a full registry name.
    return f"{name.split()[0]} boss"


def greeting(manager_name, chat_id, on_date: date | None = None) -> str:
    try:
        base = on_date or date.today()
        return _pick(_GREETINGS, chat_id, base).format(
            name=_clean_name(manager_name)
        )
    except Exception:
        logger.exception("human touch: greeting failed")
        return ""


def personalise(text, manager_name, chat_id, on_date: date | None = None) -> str:
    """Prepend the daily greeting to a question. Empty text stays empty —
    a failed formatter must stay failed."""
    try:
        if not isinstance(text, str) or not text.strip():
            return ""
        greet = greeting(manager_name, chat_id, on_date)
        return f"{greet}\n\n{text}" if greet else text
    except Exception:
        logger.exception("human touch: personalise failed")
        return text if isinstance(text, str) else ""


def ack(manager_name, chat_id, on_date: date | None = None) -> str:
    """The varied thank-you for a captured reply. Falls back to a plain
    constant on any failure — the ack must always exist."""
    try:
        base = on_date or date.today()
        return _pick(_ACKS, chat_id, base).format(name=_clean_name(manager_name))
    except Exception:
        logger.exception("human touch: ack failed")
        return "சரி boss, பதில் note பண்ணிட்டேன் ✅ நன்றி!"


async def show_typing(bot, chat_id) -> None:
    """Best-effort 'typing…' before a question. Never raises."""
    try:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        logger.debug("human touch: typing action failed", exc_info=True)


# --- weekly praise + owner scoreboard ---------------------------------------

def engagement_by_chat(question_rows: list[dict]) -> list[dict]:
    """Per-chat response stats from ledger rows (reminder rows excluded by
    the caller's query): asked, answered, avg_response_minutes. Never
    raises."""
    try:
        by_chat: dict = {}
        for row in question_rows or []:
            if not isinstance(row, dict):
                continue
            chat = row.get("chat_id")
            if chat is None:
                continue
            bucket = by_chat.setdefault(
                chat, {"chat_id": chat, "asked": 0, "answered": 0, "_mins": []}
            )
            bucket["asked"] += 1
            if row.get("replied_at"):
                bucket["answered"] += 1
                try:
                    asked = datetime.fromisoformat(str(row["asked_at"]))
                    replied = datetime.fromisoformat(str(row["replied_at"]))
                    if asked.tzinfo is None:
                        asked = asked.replace(tzinfo=timezone.utc)
                    if replied.tzinfo is None:
                        replied = replied.replace(tzinfo=timezone.utc)
                    mins = (replied - asked).total_seconds() / 60.0
                    if mins >= 0:
                        bucket["_mins"].append(mins)
                except (TypeError, ValueError):
                    pass
        out = []
        for bucket in by_chat.values():
            mins = bucket.pop("_mins")
            bucket["avg_response_minutes"] = (
                sum(mins) / len(mins) if mins else None
            )
            out.append(bucket)
        out.sort(key=lambda b: (-(b["answered"]), b["asked"]))
        return out
    except Exception:
        logger.exception("human touch: engagement stats failed")
        return []


def praise_message(stat: dict) -> str:
    """Tamil praise for a chat that answered EVERY question this week.
    ``""`` when the record doesn't earn it. Never raises."""
    try:
        if not isinstance(stat, dict):
            return ""
        asked = int(stat.get("asked") or 0)
        answered = int(stat.get("answered") or 0)
        if asked == 0 or answered < asked:
            return ""
        return (
            "🌟 இந்த வாரம் சூப்பர்!\n"
            f"கேட்ட {asked} கேள்விக்கும் பதில் சொன்னீங்க 👏\n"
            "Boss-க்கும் தெரியும். இப்படியே continue பண்ணுங்க 🙏"
        )
    except Exception:
        logger.exception("human touch: praise failed")
        return ""


def _fmt_minutes(mins) -> str:
    if mins is None:
        return "—"
    mins = float(mins)
    if mins < 90:
        return f"{mins:.0f} min"
    return f"{mins / 60.0:.1f} h"


def format_owner_scoreboard(stats: list[dict], label_for=None) -> str:
    """English weekly response scoreboard for the alert group. ``""`` when
    there were no questions at all. Never raises."""
    try:
        rows = [s for s in stats or [] if isinstance(s, dict) and s.get("asked")]
        if not rows:
            return ""
        label = label_for if callable(label_for) else (
            lambda c: f"chat {c}"
        )
        lines = ["📊 Question scoreboard (last 7 days):"]
        for s in rows:
            mark = " 🌟" if s["answered"] == s["asked"] else ""
            avg = (
                f", avg {_fmt_minutes(s['avg_response_minutes'])}"
                if s.get("avg_response_minutes") is not None
                else ""
            )
            lines.append(
                f"• {label(s['chat_id'])}: {s['answered']}/{s['asked']} "
                f"answered{avg}{mark}"
            )
        full = [s for s in rows if s["answered"] == s["asked"]]
        if full:
            lines.append(
                f"{len(full)} chat(s) answered everything — praised in Tamil."
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("human touch: scoreboard failed")
        return ""
