#!/usr/bin/env python3
"""PR #34 — nightly Khulafa Telegram digest.

Entry point for the Render cron job (23:00 Malaysia = 15:00 UTC). Gathers the
day's data, builds the 8-section digest, DMs it to each recipient, and logs
delivery to digest_log.

  Render cron:
    Type:     Cron Job
    Schedule: 0 15 * * *          # 15:00 UTC = 23:00 Asia/Kuala_Lumpur
    Command:  python scripts/send_daily_digest.py

Recipients: DIGEST_RECIPIENTS (comma-separated chat ids) if set, else
YASSIR_CHAT_ID. Requires TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY.
"""

import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digest import build_digest_messages  # noqa: E402
from digest_data import MALAYSIA_TZ, gather_digest_data, log_digest  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("send_daily_digest")


def _build_client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _recipients() -> list:
    raw = os.environ.get("DIGEST_RECIPIENTS") or os.environ.get("YASSIR_CHAT_ID") or ""
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("Ignoring non-numeric digest recipient %r", part)
    return ids


def _telegram_send(recipient, text) -> None:
    """Send one Markdown message. Raises on a non-200 Telegram response so the
    caller records failed/partial."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode({
        "chat_id": recipient,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=20) as resp:  # noqa: S310
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram error: {body.get('description')}")


def run(client, *, recipients, now_my, send_fn, data=None) -> dict:
    """Build + deliver the digest to each recipient, logging each outcome.
    Returns ``{recipient: status}``. ``send_fn(recipient, text)`` must raise on
    failure."""
    if data is None:
        data = gather_digest_data(client, now_my)
    messages = build_digest_messages(data, now_my)
    full_text = "\n\n".join(messages)

    summary = {}
    for recipient in recipients:
        sent, error = 0, None
        for message in messages:
            try:
                send_fn(recipient, message)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                error = str(exc)
                logger.warning("digest send to %s failed: %s", recipient, error)
                break
        if sent == len(messages):
            status = "success"
        elif sent == 0:
            status = "failed"
        else:
            status = "partial"
        log_digest(client, recipient, full_text, status, error)
        summary[recipient] = status
    return summary


def main() -> None:
    recipients = _recipients()
    if not recipients:
        logger.error("No digest recipients (set DIGEST_RECIPIENTS or YASSIR_CHAT_ID). Aborting.")
        return
    client = _build_client()
    now_my = datetime.now(MALAYSIA_TZ)
    summary = run(client, recipients=recipients, now_my=now_my, send_fn=_telegram_send)
    logger.info("Digest delivery: %s", summary)


if __name__ == "__main__":
    main()
