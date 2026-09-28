"""Voice replies in the outlet groups.

A cashier may answer a check-in with a Telegram voice note. The bot
downloads the .ogg, asks the speech-to-text provider (``staff_ai.
transcribe``) for the words, and feeds the transcript into the SAME reply
reader as a typed message — nothing downstream knows the difference. The
transcript and the audio ``file_id`` are kept in ``staff_chat_log`` with
``kind = 'voice'`` (migrations/0054).

When transcription is not available, fails, or is not confident enough
(``VOICE_MIN_CONFIDENCE``, default 0.6), the bot answers in the cashier's
language asking them to type it instead. No provider is wired yet, so
today every voice note gets that answer.

Pure: wording and decisions. bot.py downloads and dispatches.
"""
from __future__ import annotations

import os

import cashier_names
import staff_ai
import staff_chat

KIND = "voice"
DEFAULT_MIN_CONFIDENCE = 0.6
MAX_SECONDS = 120          # longer than this is not a reply
LANGUAGE_HINTS = {"tamil": ("ta",), "bm": ("ms",), "bengali": ("bn",), "english": ("en",),
                  "indonesian": ("id",), staff_chat.BM_TAMIL: ("ms", "ta")}


def min_confidence() -> float:
    raw = (os.environ.get("VOICE_MIN_CONFIDENCE") or "").strip()
    try:
        return min(1.0, max(0.0, float(raw))) if raw else DEFAULT_MIN_CONFIDENCE
    except ValueError:
        return DEFAULT_MIN_CONFIDENCE


def accept(transcript: dict | None) -> str | None:
    """The text to feed the reply reader, or None when the cashier should
    type instead (no transcript, empty, or below the confidence floor)."""
    if not transcript:
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


def transcribe(audio_bytes: bytes, language: str) -> dict | None:
    """Provider call with the cashier's language as a hint. Never raises."""
    try:
        return staff_ai.transcribe(audio_bytes, languages=LANGUAGE_HINTS.get(language, ()))
    except Exception:
        return None


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
            text, accepted: bool) -> dict:
    """``staff_chat_log`` row for one voice note (kind = 'voice')."""
    facts = {"file_id": file_id, "duration_ok": True, "accepted": accepted,
             "confidence": (transcript or {}).get("confidence"),
             "slot": (thread or {}).get("slot"), "thread_id": (thread or {}).get("id")}
    return {
        "kind": KIND, "mode": "natural",
        "slot": (thread or {}).get("slot") or "voice",
        "outlet_code": outlet_code, "chat_id": chat_id,
        "cashier": (thread or {}).get("cashier"), "language": language,
        "facts": facts, "template_text": None, "ai_text": text, "final_text": text,
        "source": "voice" if accepted else "type_instead", "problems": [] if accepted else ["not transcribed"],
        "provider": (transcript or {}).get("provider") or staff_ai.voice_provider() or None,
        "model": (transcript or {}).get("model"), "tokens_in": None, "tokens_out": None,
        "voice_file_id": file_id, "transcript": text,
    }
