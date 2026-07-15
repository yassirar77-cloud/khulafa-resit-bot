"""Telegram WebApp initData verification (no Telegram/Supabase deps).

initData is trusted only after verifying its HMAC signature against the bot
token (https://core.telegram.org/bots/webapps#validating-data-received-via-
the-mini-app). Anything unsigned could be forged by any visitor who found the
/webapp URL — it is posted as a plain link in group chats.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

INITDATA_MAX_AGE_S = 24 * 3600
_INITDATA_MAX_LEN = 8192


def verify_init_data(init_data: str, bot_token: str, *, now: float | None = None) -> int | None:
    """Return the authenticated Telegram user id, or None if the initData is
    missing, forged, malformed, or older than INITDATA_MAX_AGE_S."""
    if not init_data or not bot_token or len(init_data) > _INITDATA_MAX_LEN:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
    except ValueError:
        return None
    data = dict(pairs)
    supplied_hash = data.pop("hash", None)
    if not supplied_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if abs((now if now is not None else time.time()) - auth_date) > INITDATA_MAX_AGE_S:
        return None
    try:
        user = json.loads(data.get("user") or "{}")
        return int(user["id"])
    except (ValueError, TypeError, KeyError):
        return None
