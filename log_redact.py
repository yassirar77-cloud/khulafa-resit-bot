"""Keep secrets out of the logs.

The Telegram Bot API puts the bot token in every request URL
(``https://api.telegram.org/bot<token>/sendMessage``), and httpx/httpx2 log
each request URL at INFO — so every Render log line for Telegram carried the
full token. ``install()``:

* drops the per-request loggers (httpx, httpx2, httpcore) to WARNING, and
* wraps every root handler's formatter so the final text of every record —
  message, args and tracebacks included — is masked: a Telegram token shows
  as ``bot***``, API keys and JWTs as ``***``, plus the exact values of the
  secret env vars wherever they appear.
"""
from __future__ import annotations

import logging
import os
import re

SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN", "DEEPSEEK_API_KEY", "SUPABASE_KEY", "ZAI_API_KEY",
    "GMAIL_APP_PASSWORD", "CLOUDINARY_API_SECRET", "CLOUDINARY_URL",
    "ANTHROPIC_API_KEY",
)
QUIET_LOGGERS = ("httpx", "httpx2", "httpcore")

_PATTERNS = (
    # Telegram bot token in a Bot API URL, or bare.
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot***"),
    (re.compile(r"(?<![A-Za-z0-9])\d{6,}:[A-Za-z0-9_-]{30,}"), "***"),
    # OpenAI-style keys (DeepSeek, ZAI) and JWTs (Supabase keys).
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "***"),
)
_MIN_SECRET_LEN = 8


def redact(text: str, secrets=()) -> str:
    if not text:
        return text
    for value in secrets:
        if value and len(value) >= _MIN_SECRET_LEN:
            text = text.replace(value, "***")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _env_secrets() -> tuple[str, ...]:
    return tuple(v for v in (os.environ.get(k) for k in SECRET_ENV_VARS) if v)


class RedactingFormatter(logging.Formatter):
    """Wraps another formatter and masks secrets in its output."""

    def __init__(self, inner: logging.Formatter | None, secrets=()):
        super().__init__()
        self._inner = inner or logging.Formatter()
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._inner.format(record), self._secrets)


def install(root: logging.Logger | None = None) -> None:
    """Call once after ``logging.basicConfig``. Safe to call again."""
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    root = root or logging.getLogger()
    secrets = _env_secrets()
    for handler in root.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter, secrets))
