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

import reconciliation_service  # noqa: E402

from datetime import timedelta  # noqa: E402

from digest import build_digest_messages, parse_mode_attempts  # noqa: E402
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


def _telegram_send(recipient, text, parse_mode) -> None:
    """Send one message with the given parse_mode (None = plain text). Raises on
    a non-200 Telegram response so the caller can fall back / record failure."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    fields = {
        "chat_id": recipient,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        fields["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(fields).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=20) as resp:  # noqa: S310
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram error: {body.get('description')}")


def _deliver_message(send_fn, recipient, message, plain):
    """Try each parse_mode in turn (Markdown then plain, unless forced plain).
    Returns (delivered, used_fallback, error)."""
    last_error = None
    attempts = parse_mode_attempts(plain)
    for i, parse_mode in enumerate(attempts):
        try:
            send_fn(recipient, message, parse_mode)
            return True, (i > 0), None
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            last_error = str(exc)
            logger.warning(
                "digest send to %s (parse_mode=%s) failed: %s", recipient, parse_mode, last_error
            )
    return False, False, last_error


def run(client, *, recipients, now_my, send_fn, data=None, plain=False) -> dict:
    """Build + deliver the digest to each recipient, logging each outcome.
    Returns ``{recipient: status}``. ``send_fn(recipient, text, parse_mode)``
    must raise on failure; delivery falls back to plain text on a Markdown
    parse error so it always gets through."""
    if data is None:
        data = gather_digest_data(client, now_my)
    messages = build_digest_messages(data, now_my)
    full_text = "\n\n".join(messages)
    message_bytes = len(full_text.encode("utf-8"))

    summary = {}
    for recipient in recipients:
        sent, error, used_fallback = 0, None, False
        for message in messages:
            delivered, fb, err = _deliver_message(send_fn, recipient, message, plain)
            if delivered:
                sent += 1
                used_fallback = used_fallback or fb
            else:
                error = err
                break
        if sent == len(messages):
            status = "success"
        elif sent == 0:
            status = "failed"
        else:
            status = "partial"
        if status == "success" and used_fallback:
            error = "delivered as plain text (markdown parse fallback)"
        log_digest(client, recipient, full_text, status, error, message_bytes=message_bytes)
        summary[recipient] = status
    return summary


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="PR #34 nightly Khulafa digest.")
    parser.add_argument(
        "--plain", action="store_true",
        help="send as plain text (no Markdown) — use if formatting keeps breaking",
    )
    args = parser.parse_args()

    recipients = _recipients()
    if not recipients:
        logger.error("No digest recipients (set DIGEST_RECIPIENTS or YASSIR_CHAT_ID). Aborting.")
        return
    client = _build_client()
    now_my = datetime.now(MALAYSIA_TZ)
    _reconcile_before_digest(client, now_my)
    summary = run(client, recipients=recipients, now_my=now_my, send_fn=_telegram_send, plain=args.plain)
    logger.info("Digest delivery: %s", summary)


def _reconcile_before_digest(client, now_my) -> None:
    """Refresh purchase_reconciliation for today + yesterday so the digest's
    food-cost sections are current. Best-effort: a failure must not block the
    digest itself."""
    today = now_my.date()
    dates = [today.isoformat(), (today - timedelta(days=1)).isoformat()]
    try:
        results = reconciliation_service.run_reconciliation_for_dates(client, dates)
        logger.info(
            "Reconciliation before digest: %s",
            {r["business_date"]: r["outlets_processed"] for r in results},
        )
    except Exception:
        logger.warning("Reconciliation before digest failed", exc_info=True)


if __name__ == "__main__":
    main()
