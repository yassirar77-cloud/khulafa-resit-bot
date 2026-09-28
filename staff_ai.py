"""Wording provider for natural staff messages.

The bot's code decides WHAT to ask and WHICH facts go in; this module only
asks a language model to phrase it. ``STAFF_CHAT_AI`` picks the provider so
another can be added later; today the only one is DeepSeek (OpenAI-compatible
API, ``DEEPSEEK_API_KEY``).

Settings (Render env):
  STAFF_CHAT_AI        provider name, default "deepseek"
  DEEPSEEK_API_KEY     required for deepseek
  DEEPSEEK_MODEL       default "deepseek-flash"
  DEEPSEEK_BASE_URL    default "https://api.deepseek.com"
  STAFF_VOICE_AI       speech provider, default "groq"
  GROQ_API_KEY         required for voice notes (else "please type it")
  GROQ_STT_MODEL       default "whisper-large-v3-turbo"
  GROQ_BASE_URL        default "https://api.groq.com/openai/v1"

``complete_json`` never raises: any failure (no key, timeout, bad JSON)
returns ``None`` and the caller sends the plain template instead.

``transcribe`` is the speech-to-text call for voice notes (staff_voice):
Groq-hosted Whisper (``GROQ_API_KEY``, ``GROQ_STT_MODEL`` default
whisper-large-v3-turbo, OpenAI-compatible audio endpoint). The cashier's
language is always passed — never auto-detected. Any failure returns
``None`` and the cashier is asked to type instead. Provider code for
speech lives here and nowhere else.

``status`` reports the last successful call and today's token spend for
/health.
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

DEEPSEEK = "deepseek"
PROVIDERS = (DEEPSEEK,)

DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
TIMEOUT_SECONDS = 20.0
MAX_TOKENS = 800

_clients: dict = {}

# For /health: when the provider last answered, and today's token spend.
_status: dict = {"last_ok_at": None, "tokens_in_today": 0, "tokens_out_today": 0,
                 "calls_today": 0, "failures_today": 0, "day": None}
GROQ = "groq"
_VOICE_PROVIDERS = (GROQ,)
DEFAULT_GROQ_STT_MODEL = "whisper-large-v3-turbo"
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
VOICE_TIMEOUT_SECONDS = 30.0
# Whisper language codes per /lang setting. The Malay+Tamil mix is sent as
# Malay: most of those cashiers speak Malay to the office.
VOICE_LANGUAGE_CODES = {"tamil": "ta", "bm": "ms", "bengali": "bn", "english": "en",
                        "indonesian": "id", "bm_tamil": "ms"}
DEFAULT_VOICE_LANGUAGE = "ms"


def provider() -> str:
    return (os.environ.get("STAFF_CHAT_AI") or DEEPSEEK).strip().lower()


def model() -> str:
    if provider() == DEEPSEEK:
        return (os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip()
    return ""


def _deepseek_client():
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        return None
    base_url = (os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).strip()
    cache_key = (key, base_url)
    if cache_key not in _clients:
        from openai import OpenAI
        _clients[cache_key] = OpenAI(
            api_key=key, base_url=base_url, timeout=TIMEOUT_SECONDS, max_retries=1
        )
    return _clients[cache_key]


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _parse(content):
    """The JSON object in a reply, tolerating code fences. None if absent."""
    text = _FENCE.sub("", content or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def complete_json(system: str, user: str, *, attempts: int = 2) -> dict | None:
    """One chat completion that must return a JSON object, retried once.

    DeepSeek's flash model reasons by default and those hidden tokens count
    against ``max_tokens``: with a small cap the visible content came back
    empty (json char 0). Reasoning is switched off — this is short
    rephrasing, not a problem to think through.

    Returns ``{"data": <parsed object>, "provider", "model", "tokens_in",
    "tokens_out"}`` or ``None``."""
    name = provider()
    if name != DEEPSEEK:
        logger.warning("staff ai: unknown provider %r", name)
        return None
    try:
        client = _deepseek_client()
    except Exception:
        logger.exception("staff ai: client setup failed")
        return None
    if client is None:
        logger.warning("staff ai: DEEPSEEK_API_KEY not set")
        return None
    for attempt in range(1, attempts + 1):
        try:
            resp = client.chat.completions.create(
                model=model(),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=1.0,
                max_tokens=MAX_TOKENS,
                extra_body={"thinking": {"type": "disabled"}},
            )
            choice = resp.choices[0]
            data = _parse(choice.message.content)
            usage = getattr(resp, "usage", None)
            if data is not None:
                _record(getattr(usage, "prompt_tokens", None),
                        getattr(usage, "completion_tokens", None))
                return {
                    "data": data,
                    "provider": name,
                    "model": model(),
                    "tokens_in": getattr(usage, "prompt_tokens", None),
                    "tokens_out": getattr(usage, "completion_tokens", None),
                }
            logger.warning(
                "staff ai: no JSON in reply (attempt %d/%d, finish=%s, "
                "content_len=%d, tokens_out=%s)",
                attempt, attempts, getattr(choice, "finish_reason", None),
                len(choice.message.content or ""),
                getattr(usage, "completion_tokens", None),
            )
        except Exception:
            logger.exception(
                "staff ai: completion failed (provider=%s, attempt %d/%d)",
                name, attempt, attempts,
            )
    _record(None, None, ok=False)
    return None


# --- health -----------------------------------------------------------------------

def _today() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date().isoformat()


def _record(tokens_in, tokens_out, *, ok: bool = True) -> None:
    """Count one call for /health; the counters reset each Malaysian day."""
    from datetime import datetime, timezone
    day = _today()
    if _status["day"] != day:
        _status.update(day=day, tokens_in_today=0, tokens_out_today=0, calls_today=0,
                       failures_today=0)
    if ok:
        _status["last_ok_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _status["calls_today"] += 1
        _status["tokens_in_today"] += int(tokens_in or 0)
        _status["tokens_out_today"] += int(tokens_out or 0)
    else:
        _status["failures_today"] += 1


def status() -> dict:
    """``{provider, model, last_ok_at, tokens_today, tokens_in_today,
    tokens_out_today, calls_today, failures_today}`` for /health."""
    if _status["day"] != _today():
        _record(None, None, ok=False)
        _status["failures_today"] -= 1
    return {
        "provider": provider(), "model": model(), "configured": _deepseek_client() is not None
        if provider() == DEEPSEEK else False,
        "last_ok_at": _status["last_ok_at"],
        "tokens_today": _status["tokens_in_today"] + _status["tokens_out_today"],
        "tokens_in_today": _status["tokens_in_today"],
        "tokens_out_today": _status["tokens_out_today"],
        "calls_today": _status["calls_today"],
        "failures_today": _status["failures_today"],
    }


def reset_status() -> None:
    """Tests only."""
    _status.update(last_ok_at=None, tokens_in_today=0, tokens_out_today=0, calls_today=0,
                   failures_today=0, day=None)


# --- speech to text ------------------------------------------------------------------

def voice_provider() -> str:
    return (os.environ.get("STAFF_VOICE_AI") or GROQ).strip().lower()


def voice_model() -> str:
    return (os.environ.get("GROQ_STT_MODEL") or DEFAULT_GROQ_STT_MODEL).strip()


def voice_language_code(language) -> str:
    """The Whisper language code for a cashier's /lang setting. Never empty:
    auto-detect is not used (it guesses Hindi or Indonesian for our staff)."""
    return VOICE_LANGUAGE_CODES.get(str(language or "").strip().lower(), DEFAULT_VOICE_LANGUAGE)


def _groq_client():
    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        return None
    base_url = (os.environ.get("GROQ_BASE_URL") or DEFAULT_GROQ_BASE_URL).strip()
    cache_key = ("groq", key, base_url)
    if cache_key not in _clients:
        from openai import OpenAI
        _clients[cache_key] = OpenAI(
            api_key=key, base_url=base_url, timeout=VOICE_TIMEOUT_SECONDS, max_retries=1
        )
    return _clients[cache_key]


def _segments_confidence(segments) -> float | None:
    """Whisper gives no single score. Per segment it reports the average
    log-probability of its tokens and the probability that it is not speech;
    the confidence is the duration-weighted mean of exp(avg_logprob) scaled
    by (1 - no_speech_prob). None when there are no segments."""
    import math
    total, weight = 0.0, 0.0
    for seg in segments or []:
        get = seg.get if isinstance(seg, dict) else (lambda k, d=None: getattr(seg, k, d))
        try:
            lp = float(get("avg_logprob", 0.0) or 0.0)
            ns = float(get("no_speech_prob", 0.0) or 0.0)
            dur = max(0.1, float(get("end", 0.0) or 0.0) - float(get("start", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
        total += dur * math.exp(min(0.0, lp)) * (1.0 - min(1.0, max(0.0, ns)))
        weight += dur
    if weight <= 0:
        return None
    return round(max(0.0, min(1.0, total / weight)), 3)


def _failure(reason: str, code: str, *, status=None, message: str = "") -> dict:
    return {"ok": False, "text": "", "reason": reason, "status": status,
            "message": str(message or "")[:300], "provider": voice_provider(),
            "model": voice_model(), "language": code}


def transcribe(audio_bytes: bytes, *, language, mime: str = "audio/ogg",
               filename: str = "voice.ogg") -> dict:
    """Speech to text for a staff voice note. ``language`` is the cashier's
    /lang setting (or already a Whisper code); it is ALWAYS sent.

    Success: ``{"ok": True, "text", "confidence", "provider", "model",
    "language", "duration"}``. Failure: ``{"ok": False, "reason", "status",
    "message", ...}`` with reason one of ``no_key`` (GROQ_API_KEY unset),
    ``unknown_provider``, ``no_audio``, ``api_error`` (status = HTTP code
    when the client gave one, message = the error text) or ``empty_text``
    (the model heard nothing). API errors are logged at WARNING. Never
    raises."""
    name = voice_provider()
    code = (language if language in VOICE_LANGUAGE_CODES.values()
            else voice_language_code(language))
    if name not in _VOICE_PROVIDERS:
        logger.warning("staff ai: voice provider %r not available", name)
        return _failure("unknown_provider", code, message=name)
    try:
        client = _groq_client()
    except Exception as exc:
        logger.exception("staff ai: voice client setup failed")
        return _failure("api_error", code, message=f"client setup: {exc}")
    if client is None:
        logger.warning("staff ai: GROQ_API_KEY not set — voice notes get 'please type it'")
        return _failure("no_key", code)
    if not audio_bytes:
        return _failure("no_audio", code)
    try:
        resp = client.audio.transcriptions.create(
            model=voice_model(),
            file=(filename, audio_bytes, mime),
            language=code,
            response_format="verbose_json",
            temperature=0,
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        message = getattr(exc, "message", None) or str(exc)
        logger.warning("staff ai: transcription api_error (provider=%s, model=%s, lang=%s, "
                       "status=%s): %s", name, voice_model(), code, status, str(message)[:300])
        return _failure("api_error", code, status=status, message=message)
    get = resp.get if isinstance(resp, dict) else (lambda k, d=None: getattr(resp, k, d))
    text = str(get("text") or "").strip()
    if not text:
        return _failure("empty_text", code)
    duration = get("duration")
    try:
        duration = round(float(duration), 1) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "ok": True,
        "text": text,
        "confidence": _segments_confidence(get("segments")),
        "provider": name,
        "model": voice_model(),
        "language": code,
        "duration": duration,
    }
