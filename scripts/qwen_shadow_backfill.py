#!/usr/bin/env python3
"""SHADOW OCR backfill — run Qwen3.6-Plus against existing PROBLEM receipts.

Measurement only. This NEVER touches the live ``receipts`` table and is NOT
wired into any production flow — it is a standalone script, gated behind the
``QWEN_SHADOW_ENABLED`` feature flag (see ``qwen_ocr_shadow``).

For each problem receipt it:
  1. Selects it from ``receipts`` where ANY of:
       - confidence < 60                (the low-confidence backlog)
       - total > 5000                   (RM5,000 RM/Sen split-column outliers)
       - receipt_date in the future / implausibly old (date misreads)
  2. Recovers the original photo. The ``receipts`` table stores NO image, so
     we join ``pending_review`` on (chat_id, telegram_message_id) to get the
     Telegram ``photo_file_id`` and re-download the photo via the bot token.
     Receipts with no recoverable file_id are skipped and reported (these are
     mostly the >=60-confidence auto-saved ones that never hit the review queue).
  3. Runs Qwen shadow OCR on the SAME image and writes a side-by-side row into
     ``ocr_shadow_comparison`` (idempotent: skips receipts already compared).

QUOTA SAFETY: the free Qwen quota is 1M tokens (expires 2026-07-02). The run
is capped two ways — ``--limit`` (default 50) bounds how many receipts are
processed, and ``--token-budget`` (default 900000) stops the run before it
can blow the quota.

Usage:
  QWEN_SHADOW_ENABLED=1 QWEN_API_KEY=... TELEGRAM_BOT_TOKEN=... \\
  SUPABASE_URL=... SUPABASE_KEY=... \\
  python scripts/qwen_shadow_backfill.py --limit 50
  python scripts/qwen_shadow_backfill.py --dry-run     # pick + report, no Qwen calls
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from image_utils import resize_for_ocr  # noqa: E402
from qwen_ocr_shadow import extract_with_qwen_ocr, shadow_enabled  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("qwen_shadow_backfill")

RECEIPTS_TABLE = "receipts"
PENDING_TABLE = "pending_review"
COMPARISON_TABLE = "ocr_shadow_comparison"
SELECT_COLUMNS = "id, total, confidence, receipt_date, raw_text, chat_id, message_id"

OUTLIER_TOTAL = 5000          # RM/Sen split-column misreads land far above this
FUTURE_GRACE_DAYS = 7
MIN_PLAUSIBLE_DATE = "2023-01-01"
IMAGE_MAX_DIM = int(os.environ.get("IMAGE_MAX_DIM", "1600"))
DEFAULT_TOKEN_BUDGET = int(os.environ.get("QWEN_TOKEN_BUDGET", "900000"))


def _build_client():
    from supabase import create_client

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def fetch_problem_receipts(client) -> list:
    """Receipts matching ANY problem signal: low confidence, RM5k outlier, or
    out-of-window date. Ordered by total desc so the worst outliers go first."""
    future_iso = (
        datetime.now(timezone.utc) + timedelta(days=FUTURE_GRACE_DAYS)
    ).date().isoformat()
    or_filter = ",".join(
        [
            "confidence.lt.60",
            f"total.gt.{OUTLIER_TOTAL}",
            f"receipt_date.gt.{future_iso}",
            f"receipt_date.lt.{MIN_PLAUSIBLE_DATE}",
        ]
    )
    resp = (
        client.table(RECEIPTS_TABLE)
        .select(SELECT_COLUMNS)
        .or_(or_filter)
        .order("total", desc=True)
        .order("id", desc=False)
        .execute()
    )
    return resp.data or []


def already_compared_ids(client) -> set:
    resp = client.table(COMPARISON_TABLE).select("receipt_id").execute()
    return {r["receipt_id"] for r in (resp.data or []) if r.get("receipt_id") is not None}


def find_photo_file_id(client, chat_id, message_id) -> str | None:
    """Look up the Telegram photo_file_id for a receipt via pending_review.

    Receipts are linked to their review-queue row by (chat_id, message_id);
    pending_review stores the message id as ``telegram_message_id``. Returns
    the most recent non-null file_id, or None if the receipt never hit review.
    """
    if chat_id is None or message_id is None:
        return None
    resp = (
        client.table(PENDING_TABLE)
        .select("photo_file_id, created_at")
        .eq("chat_id", chat_id)
        .eq("telegram_message_id", message_id)
        .execute()
    )
    rows = [r for r in (resp.data or []) if r.get("photo_file_id")]
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[0]["photo_file_id"]


def download_telegram_photo(file_id: str) -> bytes:
    """Re-download a photo from Telegram by file_id using the bot token.

    Telegram file_ids stay valid for the issuing bot indefinitely, so historical
    receipt photos remain fetchable. Raises on any HTTP/API error.
    """
    import httpx

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    with httpx.Client(timeout=30) as http:
        meta = http.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
        )
        meta.raise_for_status()
        body = meta.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram getFile failed: {body}")
        file_path = body["result"]["file_path"]
        data = http.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
        data.raise_for_status()
        return data.content


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_comparison_row(receipt: dict, qwen: dict) -> dict:
    """Assemble one side-by-side ocr_shadow_comparison row (glm_* | qwen_*)."""
    return {
        "receipt_id": receipt.get("id"),
        # glm_* = the live values already stored on `receipts`.
        "glm_total": _to_float(receipt.get("total")),
        "glm_confidence": receipt.get("confidence"),
        "glm_date": receipt.get("receipt_date"),
        "glm_raw_json": receipt.get("raw_text"),
        # qwen_* = what the shadow run produced for the same image.
        "qwen_total": qwen.get("total"),
        "qwen_confidence": qwen.get("confidence"),
        "qwen_date": qwen.get("receipt_date"),
        "qwen_raw_json": qwen.get("raw_text"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def run(client, *, limit: int, dry_run: bool, token_budget: int) -> dict:
    candidates = fetch_problem_receipts(client)
    done = already_compared_ids(client)
    stats = {
        "candidates": len(candidates),
        "already_compared": 0,
        "no_image": 0,
        "download_failed": 0,
        "ocr_failed": 0,
        "compared": 0,
        "tokens_used": 0,
        "stopped_on_budget": False,
    }

    processed = 0
    for receipt in candidates:
        if processed >= limit:
            logger.info("Reached --limit %d; stopping.", limit)
            break
        rid = receipt.get("id")
        if rid in done:
            stats["already_compared"] += 1
            continue

        file_id = find_photo_file_id(
            client, receipt.get("chat_id"), receipt.get("message_id")
        )
        if not file_id:
            stats["no_image"] += 1
            logger.info("receipt %s: no recoverable photo (never queued) — skipping", rid)
            continue

        if dry_run:
            logger.info(
                "[dry-run] would shadow-OCR receipt %s (glm_total=%s conf=%s) via file_id",
                rid, receipt.get("total"), receipt.get("confidence"),
            )
            processed += 1
            stats["compared"] += 1
            continue

        # Budget guard BEFORE spending more tokens. Assume the next receipt costs
        # roughly as much as the running average (min 1 to avoid div-by-zero).
        avg = stats["tokens_used"] / max(1, processed)
        if stats["tokens_used"] + avg > token_budget:
            logger.warning(
                "Token budget guard: used=%d budget=%d — stopping before next call",
                stats["tokens_used"], token_budget,
            )
            stats["stopped_on_budget"] = True
            break

        try:
            image_bytes = download_telegram_photo(file_id)
        except Exception:
            stats["download_failed"] += 1
            logger.warning("receipt %s: photo download failed", rid, exc_info=True)
            continue

        try:
            image_bytes = resize_for_ocr(image_bytes, IMAGE_MAX_DIM)
            qwen = extract_with_qwen_ocr(image_bytes)
        except Exception:
            stats["ocr_failed"] += 1
            logger.warning("receipt %s: Qwen shadow OCR failed", rid, exc_info=True)
            continue

        stats["tokens_used"] += qwen.get("_total_tokens") or 0
        row = build_comparison_row(receipt, qwen)
        try:
            client.table(COMPARISON_TABLE).insert(row).execute()
        except Exception:
            # Most likely the unique index rejecting a concurrent dup.
            logger.warning("receipt %s: comparison insert failed", rid, exc_info=True)
            continue
        done.add(rid)
        processed += 1
        stats["compared"] += 1
        logger.info(
            "receipt %s compared: glm_total=%s qwen_total=%s glm_conf=%s qwen_conf=%s tokens=%s",
            rid, receipt.get("total"), qwen.get("total"),
            receipt.get("confidence"), qwen.get("confidence"), qwen.get("_total_tokens"),
        )

    _print_report(stats, dry_run)
    return stats


def _print_report(stats: dict, dry_run: bool) -> None:
    head = "DRY RUN — no Qwen calls, no writes" if dry_run else "Qwen shadow backfill complete"
    print(
        f"\n{head}\n"
        f"  candidates (problem receipts): {stats['candidates']}\n"
        f"  already compared (skipped):    {stats['already_compared']}\n"
        f"  no recoverable image (skipped):{stats['no_image']}\n"
        f"  download failed:               {stats['download_failed']}\n"
        f"  Qwen OCR failed:               {stats['ocr_failed']}\n"
        f"  compared (written):            {stats['compared']}\n"
        f"  tokens used:                   {stats['tokens_used']:,}\n"
        f"  stopped on token budget:       {stats['stopped_on_budget']}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=50, metavar="N",
        help="max receipts to shadow-OCR this run (default 50; quota guard)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="select + report problem receipts without calling Qwen or writing",
    )
    parser.add_argument(
        "--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET, metavar="N",
        help=f"stop before exceeding N total tokens (default {DEFAULT_TOKEN_BUDGET}; "
        "keeps the run under the 1M free quota)",
    )
    args = parser.parse_args()

    if not args.dry_run and not shadow_enabled():
        parser.error(
            "Qwen shadow OCR is disabled. Set QWEN_SHADOW_ENABLED=1 to run a real "
            "backfill (or use --dry-run to preview selection without calling Qwen)."
        )

    client = _build_client()
    run(client, limit=args.limit, dry_run=args.dry_run, token_budget=args.token_budget)


if __name__ == "__main__":
    main()
