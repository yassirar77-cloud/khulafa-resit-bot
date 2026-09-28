"""Voice replies in the outlet groups.

A cashier may answer a check-in with a Telegram voice note. The bot
downloads the .ogg, asks the speech-to-text provider (``staff_ai.
transcribe`` — Groq-hosted Whisper) for the words, always with the
cashier's /lang language (ta, ms, bn, en, id; the Malay+Tamil mix as ms,
never auto-detect), and feeds the transcript into the SAME reply reader
as a typed message — nothing downstream knows the difference. The
transcript, the language used, the duration and the audio ``file_id``
are kept in ``staff_chat_log`` with ``kind = 'voice'`` (migrations/0054).

When transcription fails, or is not confident enough
(``VOICE_MIN_CONFIDENCE``, default 0.6), the bot answers in the cashier's
language asking them to type it instead.

Pure: wording and decisions. bot.py downloads and dispatches.
"""
from __future__ import annotations

import logging
import os

import cashier_names
import staff_ai
import staff_chat

logger = logging.getLogger(__name__)

KIND = "voice"
DEFAULT_MIN_CONFIDENCE = 0.6
MAX_SECONDS = 120          # longer than this is not a reply


def language_code(language: str) -> str:
    """Whisper code for the cashier's /lang setting (bm_tamil -> ms)."""
    return staff_ai.voice_language_code(language)


def min_confidence() -> float:
    raw = (os.environ.get("VOICE_MIN_CONFIDENCE") or "").strip()
    try:
        return min(1.0, max(0.0, float(raw))) if raw else DEFAULT_MIN_CONFIDENCE
    except ValueError:
        return DEFAULT_MIN_CONFIDENCE


def accept(transcript: dict | None) -> str | None:
    """The text to feed the reply reader, or None when the cashier should
    type instead (failed call, empty text, or below the confidence floor)."""
    if not transcript or transcript.get("ok") is False:
        return None
    text = str(transcript.get("text") or "").strip()
    if not text:
        return None
    conf = transcript.get("confidence")
    if conf is not None:
        try:
            if float(conf) < min_confidence():
                return None
        except (TypeError, ValueError):
            pass
    return text


# Reasons a voice note bounced (asked to type), for the log line and /voice_stats.
API_REASONS = ("api_error", "download_error")       # logged at WARNING


def bounce(transcript: dict | None) -> dict | None:
    """Why the note was bounced: ``{"reason", "detail", "level"}`` or None
    when it was accepted. reason: api_error (with status and message),
    empty_text, low_confidence (with the score and the floor), no_key,
    unknown_provider, no_audio, too_long, download_error, no_transcript."""
    if accept(transcript) is not None:
        return None
    if not transcript:
        return {"reason": "no_transcript", "detail": "", "level": "info"}
    if transcript.get("ok") is False:
        reason = str(transcript.get("reason") or "api_error")
        detail = ""
        if reason == "api_error":
            status = transcript.get("status")
            detail = f"status={status if status is not None else '?'} {transcript.get('message') or ''}".strip()
        elif transcript.get("detail") or transcript.get("message"):
            detail = str(transcript.get("detail") or transcript.get("message"))
        return {"reason": reason, "detail": detail[:300],
                "level": "warning" if reason in API_REASONS else "info"}
    if not str(transcript.get("text") or "").strip():
        return {"reason": "empty_text", "detail": "", "level": "info"}
    conf = transcript.get("confidence")
    return {"reason": "low_confidence", "detail": f"score={conf} floor={min_confidence()}",
            "level": "info"}


def transcribe(audio_bytes: bytes, language: str) -> dict:
    """Provider call with the cashier's language, always given. Never raises:
    an unexpected exception becomes an api_error result."""
    try:
        return staff_ai.transcribe(audio_bytes, language=language_code(language))
    except Exception as exc:
        logger.warning("staff voice: transcription raised: %s", exc)
        return {"ok": False, "reason": "api_error", "status": None, "message": str(exc)[:300],
                "language": language_code(language)}


def failed(reason: str, detail: str = "", language: str = "") -> dict:
    """A bounce the bot decided itself (too_long, download_error)."""
    return {"ok": False, "reason": reason, "detail": detail, "message": detail,
            "language": language_code(language) if language else None}


_TYPE_INSTEAD = {
    "bm": "Maaf, saya tak dapat dengar voice note tu dengan jelas — boleh taip jawapan? 🙏",
    "tamil": "மன்னிக்கணும், voice note தெளிவா கேக்கல — பதிலை type பண்ணி அனுப்புங்க 🙏",
    "english": "Sorry, I couldn't make out that voice note — could you type the reply? 🙏",
    "indonesian": "Maaf, voice note-nya kurang jelas — bisa diketik jawabannya? 🙏",
    "bengali": "Sorry, voice note-ta bhalo bujhte parlam na — uttor-ta likhe diben? 🙏",
}


def type_instead_text(language: str) -> str:
    return cashier_names.pick(_TYPE_INSTEAD, language)


def too_long(duration) -> bool:
    try:
        return float(duration or 0) > MAX_SECONDS
    except (TypeError, ValueError):
        return False


def log_row(thread: dict | None, *, outlet_code, chat_id, language, file_id, transcript,
            text, accepted: bool, duration=None) -> dict:
    """``staff_chat_log`` row for one voice note (kind = 'voice'): the
    transcript, the language sent to the provider, the note's duration."""
    t = transcript or {}
    why = None if accepted else bounce(transcript)
    facts = {"file_id": file_id, "accepted": accepted,
             "confidence": t.get("confidence"),
             "stt_language": t.get("language") or language_code(language),
             "duration_s": t.get("duration") if t.get("duration") is not None else duration,
             "reason": why["reason"] if why else None,
             "reason_detail": why["detail"] if why else None,
             "slot": (thread or {}).get("slot"), "thread_id": (thread or {}).get("id")}
    return {
        "kind": KIND, "mode": "natural",
        "slot": (thread or {}).get("slot") or "voice",
        "outlet_code": outlet_code, "chat_id": chat_id,
        "cashier": (thread or {}).get("cashier"), "language": language,
        "facts": facts, "template_text": None, "ai_text": text, "final_text": text,
        "source": "voice" if accepted else "type_instead",
        "problems": [] if accepted else [why["reason"] if why else "not transcribed"],
        "provider": t.get("provider") or staff_ai.voice_provider() or None,
        "model": t.get("model") or staff_ai.voice_model(), "tokens_in": None, "tokens_out": None,
        "voice_file_id": file_id, "transcript": text,
    }


# --- /voice_stats -----------------------------------------------------------------

def stats(rows: list[dict]) -> dict:
    """Per outlet from ``staff_chat_log`` voice rows: ``{code: {transcribed,
    bounced, reasons: {reason: n}}}``."""
    out: dict = {}
    for r in rows or []:
        code = str(r.get("outlet_code") or "?")
        s = out.setdefault(code, {"transcribed": 0, "bounced": 0, "reasons": {}})
        if r.get("source") == "voice":
            s["transcribed"] += 1
            continue
        s["bounced"] += 1
        facts = r.get("facts") or {}
        reason = facts.get("reason") or (r.get("problems") or ["unknown"])[0] or "unknown"
        if reason == "not transcribed":
            reason = "unknown"
        s["reasons"][reason] = s["reasons"].get(reason, 0) + 1
    return out


def format_stats(rows: list[dict], label=str, *, since=None) -> str:
    """``/voice_stats``: transcribed vs bounced per outlet this week, with
    the bounce reasons."""
    per = stats(rows)
    head = "🎤 Voice notes this week" + (f" (since {since.isoformat()})" if since else "")
    if not per:
        return head + "\n\nNo voice notes yet."
    total_t = sum(s["transcribed"] for s in per.values())
    total_b = sum(s["bounced"] for s in per.values())
    lines = [head, f"{total_t} transcribed · {total_b} bounced (asked to type)", ""]
    for code in sorted(per, key=lambda c: (-(per[c]["transcribed"] + per[c]["bounced"]), c)):
        s = per[code]
        line = f"• {label(code)}: {s['transcribed']} transcribed, {s['bounced']} bounced"
        if s["reasons"]:
            reasons = ", ".join(f"{r} ×{n}" for r, n in
                                sorted(s["reasons"].items(), key=lambda kv: (-kv[1], kv[0])))
            line += f" — {reasons}"
        lines.append(line)
    all_reasons: dict = {}
    for s in per.values():
        for r, n in s["reasons"].items():
            all_reasons[r] = all_reasons.get(r, 0) + n
    if all_reasons:
        lines += ["", "Bounce reasons: " + ", ".join(
            f"{r} ×{n}" for r, n in sorted(all_reasons.items(), key=lambda kv: (-kv[1], kv[0])))]
        if "api_error" in all_reasons:
            lines.append("api_error = Groq refused or failed (status and message are in the "
                         "Render log at WARNING)")
        if "no_key" in all_reasons:
            lines.append("no_key = GROQ_API_KEY was not set when the note came in")
        if "low_confidence" in all_reasons:
            lines.append(f"low_confidence = below VOICE_MIN_CONFIDENCE ({min_confidence()})")
    return "\n".join(lines)
