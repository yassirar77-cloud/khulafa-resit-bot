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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reconciliation_service  # noqa: E402

from datetime import timedelta  # noqa: E402

import merchant_auto_resolve  # noqa: E402
from digest import build_digest_messages, parse_mode_attempts  # noqa: E402
from digest_data import (  # noqa: E402
    FOOD_COST_LOOKBACK_DAYS,
    MALAYSIA_TZ,
    digest_already_sent,
    gather_digest_data,
    log_digest,
)

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


def _telegram_send_once(recipient, text, parse_mode) -> None:
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


_SEND_RETRIES = 3


def _telegram_send(recipient, text, parse_mode) -> None:
    """Send one message with the given parse_mode (None = plain text), retrying
    TRANSIENT failures — network errors/timeouts, HTTP 429 (honouring
    Retry-After) and 5xx — with backoff. There is exactly one 23:00 cron fire a
    night, so a 20-second Telegram blip must not cost the whole digest.
    Permanent errors (400 parse errors etc.) raise immediately so the caller's
    HTML→plain fallback still engages."""
    for attempt in range(_SEND_RETRIES + 1):
        try:
            _telegram_send_once(recipient, text, parse_mode)
            return
        except urllib.error.HTTPError as exc:
            transient = exc.code == 429 or exc.code >= 500
            if not transient or attempt == _SEND_RETRIES:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 2.0 * (attempt + 1)
            logger.warning(
                "digest send to %s got HTTP %s — retrying in %.0fs (%d/%d)",
                recipient, exc.code, min(delay, 60), attempt + 1, _SEND_RETRIES,
            )
            time.sleep(min(delay, 60))
        except OSError as exc:  # URLError / timeout / connection reset
            if attempt == _SEND_RETRIES:
                raise
            delay = 2.0 * (attempt + 1)
            logger.warning(
                "digest send to %s failed (%s) — retrying in %.0fs (%d/%d)",
                recipient, exc, delay, attempt + 1, _SEND_RETRIES,
            )
            time.sleep(delay)


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


def _merchant_review_digest_line(client):
    """One-line nudge if the merchant review queue is non-empty, else None.
    Best-effort: a failure here must never block the digest."""
    try:
        queue = merchant_auto_resolve.fetch_review_queue(client)
        return merchant_auto_resolve.format_review_digest_line(queue)
    except Exception:
        logger.warning("merchant review digest line failed", exc_info=True)
        return None


def run(client, *, recipients, now_my, send_fn, data=None, plain=False, force=False) -> dict:
    """Build + deliver the digest to each recipient, logging each outcome.
    Returns ``{recipient: status}``. ``send_fn(recipient, text, parse_mode)``
    must raise on failure; delivery falls back to plain text on a Markdown
    parse error so it always gets through.

    Idempotent per MY-day: a recipient who already has a ``success``
    digest_log row today is skipped (status ``skipped_duplicate``), so a
    cron re-run / manual re-invocation can't double-send. ``force=True``
    bypasses the guard for a deliberate corrected re-send."""
    if not force:
        pending, skipped = [], []
        for recipient in recipients:
            (skipped if digest_already_sent(client, recipient, now_my) else pending).append(recipient)
        if skipped:
            logger.info("Digest already sent today to %s — skipping (use --force to re-send)", skipped)
        recipients = pending
        if not recipients:
            return {r: "skipped_duplicate" for r in skipped}
    else:
        skipped = []
    if data is None:
        data = gather_digest_data(client, now_my)
    messages = build_digest_messages(data, now_my)
    review_line = _merchant_review_digest_line(client)
    if review_line:
        # One line a day (the digest itself is once-nightly), only when the
        # owner queue is non-empty — so it nudges without spamming.
        messages = [*messages, review_line]
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
    for recipient in skipped:
        summary[recipient] = "skipped_duplicate"
    return summary


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="PR #34 nightly Khulafa digest.")
    parser.add_argument(
        "--plain", action="store_true",
        help="send as plain text (no Markdown) — use if formatting keeps breaking",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="re-send even if today's digest was already delivered (corrected re-send)",
    )
    args = parser.parse_args()

    recipients = _recipients()
    if not recipients:
        logger.error("No digest recipients (set DIGEST_RECIPIENTS or YASSIR_CHAT_ID). Aborting.")
        return
    client = _build_client()
    now_my = datetime.now(MALAYSIA_TZ)
    _reconcile_before_digest(client, now_my)
    try:
        summary = run(client, recipients=recipients, now_my=now_my, send_fn=_telegram_send,
                      plain=args.plain, force=args.force)
    except Exception as exc:  # noqa: BLE001
        # A build/deliver crash must not vanish into the cron log: record a
        # failed digest_log row per recipient (best-effort — the DB itself may
        # be the thing that's down) and exit non-zero so Render shows the run
        # as failed.
        logger.exception("Digest run crashed before delivery")
        for recipient in recipients:
            log_digest(client, recipient, "", "failed", f"digest crashed: {exc}")
        sys.exit(1)
    logger.info("Digest delivery: %s", summary)


def _reconcile_before_digest(client, now_my) -> None:
    """Refresh purchase_reconciliation across the full food-cost rolling window
    so the digest's food-cost sections are current. Best-effort: a failure must
    not block the digest itself.

    The window matches FOOD_COST_LOOKBACK_DAYS (the rolling read), not just
    today + yesterday: a day's sales D-file (sales_daily_summary) lands the next
    morning, so a 2-day window froze older days at sales_total=NULL once they
    aged past 'yesterday'. Re-running the whole window every night re-pulls
    sales_total from sales_daily_summary for every day still on screen."""
    today = now_my.date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(FOOD_COST_LOOKBACK_DAYS)]
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
