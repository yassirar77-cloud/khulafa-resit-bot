"""Digest data gathering + delivery logging (PR #34).

The DB glue behind the digest: one ``gather_digest_data`` that the nightly cron
(scripts/send_daily_digest.py) and the /test_digest command both call, plus
``log_digest`` for the digest_log table. Pure content lives in digest.py.

Analytics counts read the clean price_movements view; the "data quality" and
"outlier" sections deliberately query receipts directly to count the rows the
view EXCLUDES.
"""

import logging
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from merchant_resolver import compute_coverage, load_snapshot

logger = logging.getLogger(__name__)

MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")
RECEIPTS_TABLE = "receipts"
PRICE_MOVEMENTS_VIEW = "price_movements"
REPARSE_AUDIT_TABLE = "reparse_audit"
PENDING_REVIEW_TABLE = "pending_review"
DIGEST_LOG_TABLE = "digest_log"

LOW_CONFIDENCE_FLOOR = 60
OUTLIER_TOTAL_MAX = 5000.0
WINDOW_DAYS = 14
NEW_SUPPLIER_WINDOW_DAYS = 7

_PM_COLUMNS = (
    "receipt_id, receipt_date, outlet, merchant_canonical_id, merchant_display_name, "
    "item_canonical_id, item_display_name, item_category, unit_price, line_total, receipt_total"
)


def _rows(resp):
    return resp.data or []


def _today_receipts(client, now_my) -> dict:
    start_local = datetime.combine(now_my.date(), time.min, tzinfo=MALAYSIA_TZ)
    end_local = start_local + timedelta(days=1)
    rows = _rows(
        client.table(RECEIPTS_TABLE)
        .select("total")
        .gte("created_at", start_local.astimezone(timezone.utc).isoformat())
        .lt("created_at", end_local.astimezone(timezone.utc).isoformat())
        .execute()
    )
    total = 0.0
    for r in rows:
        try:
            total += float(r.get("total")) if r.get("total") is not None else 0.0
        except (TypeError, ValueError):
            pass
    pending = _rows(
        client.table(PENDING_REVIEW_TABLE).select("id").eq("status", "pending").execute()
    )
    return {"count": len(rows), "total": total, "pending": len(pending)}


def _pm_window(client, now_my) -> list:
    start = (now_my.date() - timedelta(days=WINDOW_DAYS)).isoformat()
    end = (now_my.date() + timedelta(days=1)).isoformat()
    return _rows(
        client.table(PRICE_MOVEMENTS_VIEW)
        .select(_PM_COLUMNS)
        .gte("receipt_date", start)
        .lte("receipt_date", end)
        .execute()
    )


def _data_quality(client) -> dict:
    low_conf = _rows(
        client.table(RECEIPTS_TABLE).select("id").lt("confidence", LOW_CONFIDENCE_FLOOR).execute()
    )
    reparse_pending = _rows(
        client.table(REPARSE_AUDIT_TABLE).select("id").eq("applied", False).execute()
    )
    unresolved = _unresolved_merchant_count(client)
    return {
        "low_confidence": len(low_conf),
        "reparse_pending": len(reparse_pending),
        "unresolved_merchants": unresolved,
    }


def _unresolved_merchant_count(client) -> int:
    """Same notion as /merchant_coverage: distinct receipt merchant strings that
    don't resolve to a canonical."""
    rows = _rows(client.table(RECEIPTS_TABLE).select("merchant").execute())
    counts: Counter = Counter()
    for r in rows:
        name = (r.get("merchant") or "").strip()
        if name:
            counts[name] += 1
    aliases, canonicals = load_snapshot(client)
    summary = compute_coverage(list(counts.items()), aliases, canonicals)
    return summary["unresolved"]


def _outlier_count(client) -> int:
    rows = _rows(
        client.table(RECEIPTS_TABLE).select("id").gt("total", OUTLIER_TOTAL_MAX).execute()
    )
    return len(rows)


def _new_suppliers(client, now_my, limit=5) -> list:
    """Receipts in the last 7 days with a merchant but no canonical yet,
    grouped by raw merchant, most frequent first."""
    since = (now_my.date() - timedelta(days=NEW_SUPPLIER_WINDOW_DAYS)).isoformat()
    rows = _rows(
        client.table(RECEIPTS_TABLE)
        .select("merchant, merchant_canonical_id, receipt_date")
        .is_("merchant_canonical_id", "null")
        .gte("receipt_date", since)
        .execute()
    )
    counts: Counter = Counter()
    for r in rows:
        name = (r.get("merchant") or "").strip()
        if name:
            counts[name] += 1
    return [{"name": name, "count": n} for name, n in counts.most_common(limit)]


def gather_digest_data(client, now_my) -> dict:
    return {
        "today": _today_receipts(client, now_my),
        "pm_window_rows": _pm_window(client, now_my),
        "data_quality": _data_quality(client),
        "outliers": {"count": _outlier_count(client), "threshold": OUTLIER_TOTAL_MAX},
        "new_suppliers": _new_suppliers(client, now_my),
    }


def log_digest(client, recipient, message_text, status, error_msg=None) -> None:
    try:
        client.table(DIGEST_LOG_TABLE).insert({
            "recipient": recipient,
            "message_text": message_text,
            "status": status,
            "error_msg": error_msg,
        }).execute()
    except Exception:
        logger.warning("digest: could not write digest_log for %s", recipient, exc_info=True)
