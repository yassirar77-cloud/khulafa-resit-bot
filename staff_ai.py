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

``complete_json`` never raises: any failure (no key, timeout, bad JSON)
returns ``None`` and the caller sends the plain template instead.
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
    return None
