"""The human-supervisor layer — every trigger becomes a remembered question.

The managers are not report readers. What works on them is what a good
human supervisor does: ask ONE simple question in their own language,
wait for the answer, say thanks when it comes, nudge once when it
doesn't, and tell the boss who answered what. This module gives every
alert in the bot that behaviour, on top of the ``audit_responses``
ledger that already powers the receipt audit questions:

  * ``with_reply_footer`` puts the same simple Tamil instruction on
    every question — "REPLY to this message" — because an uneducated
    manager will not guess Telegram mechanics on his own;
  * ``log_question`` records what was asked, where, and when (the
    existing ``handle_audit_reply`` capture then matches ANY reply to
    the question by (chat_id, message_id) — no new plumbing);
  * ``open_questions_between`` feeds the once-per-question reminder: the
    daily job reminds only questions aged 20–44 h, so each question gets
    exactly one nudge and nobody is nagged twice — stateless;
  * the formatters keep the tone human: a thank-you in Tamil when a
    reply lands, a gentle "still waiting" nudge, and an English overview
    for the owner of who has answered and who has gone quiet.

Hard rule (same as the rest of the alert layer): nothing here ever
raises — a supervision failure must never break the message that was
being sent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_AUDIT_TABLE = "audit_responses"

# One nudge per question, the day after it was asked: the daily reminder
# job picks up questions aged 18-42 hours. The window is 24 h wide (so a
# daily run can only match a question once) and its edges sit away from
# every scheduled ask time — the nightly 21:00 missing-bill questions are
# 20 h old at the 17:00 reminder, comfortably inside [18, 42] once and
# outside it (44 h) the next day, so scheduler jitter can't double- or
# zero-nudge them.
REMINDER_MIN_AGE_HOURS = 18
REMINDER_MAX_AGE_HOURS = 42
PENDING_OVERVIEW_DAYS = 3

# Ledger rows of this type are the bot's own nudges, not questions — they
# exist so a reply TO THE NUDGE is captured too, and are excluded from
# the reminder window and the pending overview.
REMINDER_TYPE = "reminder"
_RE_LINK_PREFIX = "[re:"

# ~25 lines x ~130 chars stays safely under Telegram's 4096-char cap.
_MAX_OVERVIEW_LINES = 25

# The standing instruction under every question. Simple spoken Tamil with
# the actual Telegram gesture spelled out — a low-literacy manager will
# not guess that "reply" means long-press → Reply, and a plain typed
# message in the group is NOT captured.
REPLY_FOOTER = (
    "\n\n👉 பதில் சொல்ல: இந்த message-அ அழுத்தி பிடிங்க (long press), "
    "'Reply' தட்டுங்க, அப்புறம் பதில் type பண்ணுங்க 🙏"
)

# The nudge invites a reply TO ITSELF (the newest message — the natural
# tap target), and the nudge's own message_id is logged linked to the
# original question, so either reply closes the loop.
REMINDER_TEXT = (
    "👋 Boss, நேத்து ஒரு கேள்வி கேட்டேன் — இன்னும் பதில் வரல.\n"
    "இந்த message-அ அழுத்தி பிடிங்க (long press), 'Reply' தட்டுங்க, "
    "பதில் சொல்லுங்க 🙏"
)


def with_reply_footer(text) -> str:
    """Append the REPLY instruction to a question. Idempotent, and a
    no-op on empty input so a failed formatter stays failed."""
    if not isinstance(text, str) or not text.strip():
        return ""
    if REPLY_FOOTER.strip() in text:
        return text
    return text + REPLY_FOOTER


def log_question(supabase, chat_id, message_id, question_type,
                 question_text, receipt_id=None) -> bool:
    """Record an asked question in the ledger so the reply capture and the
    reminder loop can see it. Returns False (never raises) on failure —
    the question was already sent; losing the ledger row must not undo
    that."""
    try:
        if chat_id is None or message_id is None:
            return False
        supabase.table(_AUDIT_TABLE).insert({
            "receipt_id": receipt_id,
            "chat_id": chat_id,
            "question_type": str(question_type or "question"),
            "question_text": str(question_text or "")[:2000],
            "question_message_id": message_id,
        }).execute()
        return True
    except Exception:
        logger.exception(
            "supervisor: failed to log question (chat=%s, type=%s)",
            chat_id, question_type,
        )
        return False


def log_reminder(supabase, chat_id, nudge_message_id, original_row: dict) -> bool:
    """Record a sent nudge, linked to its original question via the
    ``[re:<id>]`` prefix in question_text, so a manager who replies to the
    NUDGE (the newest message — the natural tap target) still closes the
    original question. Never raises."""
    try:
        if chat_id is None or nudge_message_id is None:
            return False
        original_id = (original_row or {}).get("id")
        if original_id is None:
            return False
        supabase.table(_AUDIT_TABLE).insert({
            "receipt_id": None,
            "chat_id": chat_id,
            "question_type": REMINDER_TYPE,
            "question_text": (
                f"{_RE_LINK_PREFIX}{original_id}] "
                f"{_first_line((original_row or {}).get('question_text'))}"
            ),
            "question_message_id": nudge_message_id,
        }).execute()
        return True
    except Exception:
        logger.exception(
            "supervisor: failed to log reminder (chat=%s)", chat_id
        )
        return False


def linked_original_id(question_row: dict):
    """The original question's ledger id a reminder row points at, or
    ``None`` for ordinary questions. Never raises."""
    try:
        if not isinstance(question_row, dict):
            return None
        if question_row.get("question_type") != REMINDER_TYPE:
            return None
        text = str(question_row.get("question_text") or "")
        if not text.startswith(_RE_LINK_PREFIX):
            return None
        return int(text[len(_RE_LINK_PREFIX):].split("]")[0])
    except Exception:
        return None


def close_question(supabase, row_id, manager_reply, replied_at_iso) -> dict | None:
    """Mark a ledger row answered and return it (for the owner note).
    ``None`` on failure; never raises."""
    try:
        res = (
            supabase.table(_AUDIT_TABLE)
            .select("id, chat_id, question_type, question_text")
            .eq("id", row_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        supabase.table(_AUDIT_TABLE).update({
            "manager_reply": manager_reply,
            "replied_at": replied_at_iso,
        }).eq("id", row_id).execute()
        return rows[0]
    except Exception:
        logger.exception("supervisor: close_question failed (id=%s)", row_id)
        return None


def _open_questions(supabase, newest_iso: str | None, oldest_iso: str) -> list[dict]:
    try:
        query = (
            supabase.table(_AUDIT_TABLE)
            .select(
                "id, chat_id, question_type, question_text, "
                "question_message_id, asked_at"
            )
            .is_("replied_at", "null")
            .neq("question_type", REMINDER_TYPE)
            .gte("asked_at", oldest_iso)
        )
        if newest_iso:
            query = query.lte("asked_at", newest_iso)
        rows = getattr(query.execute(), "data", None) or []
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        logger.exception("supervisor: open-question read failed")
        return []


def open_questions_between(supabase, *, now: datetime | None = None) -> list[dict]:
    """The questions due their one nudge: unanswered, asked 20-44 h ago."""
    base = now or datetime.now(timezone.utc)
    return _open_questions(
        supabase,
        (base - timedelta(hours=REMINDER_MIN_AGE_HOURS)).isoformat(),
        (base - timedelta(hours=REMINDER_MAX_AGE_HOURS)).isoformat(),
    )


def open_questions_since(supabase, *, now: datetime | None = None,
                         days: int = PENDING_OVERVIEW_DAYS) -> list[dict]:
    """Every still-unanswered question of the last ``days`` days — the
    owner's who-went-quiet overview."""
    base = now or datetime.now(timezone.utc)
    return _open_questions(
        supabase, None, (base - timedelta(days=days)).isoformat()
    )


def questions_since(supabase, *, now: datetime | None = None,
                    days: int = 7) -> list[dict]:
    """ALL questions of the last ``days`` days — answered and not — with
    their reply timestamps, excluding the bot's own nudge rows. Feeds the
    weekly response scoreboard. ``[]`` on failure; never raises."""
    try:
        base = now or datetime.now(timezone.utc)
        rows = (
            supabase.table(_AUDIT_TABLE)
            .select(
                "id, chat_id, question_type, question_text, asked_at, replied_at"
            )
            .neq("question_type", REMINDER_TYPE)
            .gte("asked_at", (base - timedelta(days=days)).isoformat())
            .execute()
        )
        return [r for r in (getattr(rows, "data", None) or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("supervisor: questions_since read failed")
        return []


def format_reply_ack() -> str:
    """The thank-you a manager sees the moment his answer is captured —
    the loop must FEEL closed or they stop answering."""
    return (
        "சரி boss, பதில் note பண்ணிட்டேன் ✅ நன்றி!\n"
        "Terima kasih, jawapan disimpan ✅"
    )


def _first_line(text, limit: int = 90) -> str:
    line = str(text or "").strip().split("\n")[0]
    return line[:limit] + ("…" if len(line) > limit else "")


def format_owner_reply_note(question_row: dict, reply_text) -> str:
    """What the owner sees when a manager answers — the supervisor
    reporting upward. ``""`` on malformed input."""
    try:
        if not isinstance(question_row, dict):
            return ""
        qtype = str(question_row.get("question_type") or "question")
        chat = question_row.get("chat_id")
        return (
            f"💬 Reply received ({qtype}, chat {chat}):\n"
            f"Q: {_first_line(question_row.get('question_text'))}\n"
            f"A: {str(reply_text or '').strip()[:400]}"
        )
    except Exception:
        logger.exception("supervisor: owner reply note failed")
        return ""


def format_owner_pending(rows: list[dict]) -> str:
    """English overview of unanswered questions for the alert group.
    ``""`` when everything has been answered. Never raises."""
    try:
        if not rows:
            return ""
        by_chat: dict = {}
        for r in rows:
            if isinstance(r, dict):
                by_chat.setdefault(r.get("chat_id"), []).append(r)
        lines = [
            f"⏳ Unanswered questions (last {PENDING_OVERVIEW_DAYS} days):"
        ]
        for chat, qs in sorted(by_chat.items(), key=lambda kv: str(kv[0])):
            for q in qs:
                lines.append(
                    f"• chat {chat} — {q.get('question_type') or 'question'}: "
                    f"{_first_line(q.get('question_text'))}"
                )
        if len(lines) == 1:
            return ""
        # Hard cap: a 3-day backlog across 10 outlets can outgrow Telegram's
        # 4096-char message limit, and a too-long summary would be dropped
        # exactly when the backlog matters most.
        if len(lines) > _MAX_OVERVIEW_LINES + 1:
            hidden = len(lines) - 1 - _MAX_OVERVIEW_LINES
            lines = lines[:_MAX_OVERVIEW_LINES + 1]
            lines.append(f"…and {hidden} more (see /questions_now).")
        lines.append("Reminders sent where due. Silence twice = call them.")
        return "\n".join(lines)
    except Exception:
        logger.exception("supervisor: pending overview failed")
        return ""
