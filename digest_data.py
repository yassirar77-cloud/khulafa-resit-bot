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

import food_cost_analytics as fca
from merchant_resolver import compute_coverage, load_snapshot, match_merchant
from outlet_resolver import canonical_outlet

logger = logging.getLogger(__name__)

MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")
RECEIPTS_TABLE = "receipts"
PRICE_MOVEMENTS_VIEW = "price_movements"
REPARSE_AUDIT_TABLE = "reparse_audit"
PENDING_REVIEW_TABLE = "pending_review"
DIGEST_LOG_TABLE = "digest_log"
MERCHANT_CANONICAL_TABLE = "merchant_canonical"
RECONCILIATION_TABLE = "purchase_reconciliation"
MATCH_LOG_TABLE = "purchase_match_log"
SALES_DAILY_SUMMARY_TABLE = "sales_daily_summary"
SALES_DAILY_TOP_ITEMS_TABLE = "sales_daily_top_items"

LOW_CONFIDENCE_FLOOR = 60
OUTLIER_TOTAL_MAX = 5000.0
WINDOW_DAYS = 14
NEW_SUPPLIER_WINDOW_DAYS = 7
FOOD_COST_LOOKBACK_DAYS = 7
TOP_N = 5
SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"

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


def _is_own_outlet(name: str) -> bool:
    """Bug 4: Khulafa's own outlet names ("NASI KANDAR KHULAFA", "RESTORAN
    KHULAFA BISTRO") are internal transfers, never 'new suppliers'."""
    return "khulafa" in (name or "").lower()


def _new_suppliers(client, now_my, limit=5) -> list:
    """Receipts in the last 7 days whose merchant doesn't resolve to a known
    canonical, grouped by raw merchant, most frequent first.

    Bug 4: excludes Khulafa's own outlets. Bug 5: a missing
    ``merchant_canonical_id`` doesn't mean 'new' — the backfill may simply not
    have run — so we re-run the resolver and skip anything that resolves to an
    existing canonical (e.g. EVEREST)."""
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
    aliases, canonicals = load_snapshot(client)
    return filter_new_suppliers(counts, aliases, canonicals, limit)


def filter_new_suppliers(counts, aliases, canonicals, limit=5) -> list:
    """Pure: drop Khulafa own-outlets (bug 4) and already-canonicalisable
    merchants (bug 5) from a merchant -> count map. ``counts`` may be a Counter
    or any iterable of (name, count)."""
    items = counts.most_common() if isinstance(counts, Counter) else list(counts)
    out = []
    for name, n in items:
        if _is_own_outlet(name):
            continue
        cid, conf = match_merchant(name, aliases, canonicals)
        if cid is not None and conf > 0:
            continue
        out.append({"name": name, "count": n})
        if len(out) >= limit:
            break
    return out


def _canonical_name_map(client) -> dict:
    rows = _rows(client.table(MERCHANT_CANONICAL_TABLE).select("id, display_name").execute())
    return {r["id"]: r.get("display_name") for r in rows if r.get("id") is not None}


def _to_float(value):
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _today_suppliers(client, now_my, limit=TOP_N) -> list:
    """Bug 1 fallback: top suppliers from the receipts table directly (not the
    item-resolution-gated price_movements view), for receipts uploaded today in
    MY local time. Returns [{name, amount, line_count}]."""
    start_local = datetime.combine(now_my.date(), time.min, tzinfo=MALAYSIA_TZ)
    end_local = start_local + timedelta(days=1)
    rows = _rows(
        client.table(RECEIPTS_TABLE)
        .select("merchant, merchant_canonical_id, total, receipt_type")
        .gte("created_at", start_local.astimezone(timezone.utc).isoformat())
        .lt("created_at", end_local.astimezone(timezone.utc).isoformat())
        .execute()
    )
    names = _canonical_name_map(client)
    agg: dict = {}
    for r in rows:
        if r.get("receipt_type") not in (None, SUPPLIER_PURCHASE):
            continue
        cid = r.get("merchant_canonical_id")
        name = names.get(cid) if cid is not None else (r.get("merchant") or "").strip()
        key = cid if cid is not None else (name or "?")
        if not name:
            continue
        a = agg.setdefault(key, {"name": name, "amount": 0.0, "line_count": 0})
        a["amount"] += _to_float(r.get("total"))
        a["line_count"] += 1
    kept = [a for a in agg.values() if 0 < a["amount"] <= OUTLIER_TOTAL_MAX]
    return sorted(kept, key=lambda x: (-x["amount"], x["name"] or ""))[:limit]


def _outlet_spending_week(client, now_my, limit=TOP_N) -> list:
    """Bug 3: true weekly outlet spend from receipts (every supplier purchase),
    grouped by canonical outlet — not the resolved-line-item subset in
    price_movements. Returns [{outlet, amount, receipt_count}]."""
    start = (now_my.date() - timedelta(days=6)).isoformat()
    end = now_my.date().isoformat()
    rows = _rows(
        client.table(RECEIPTS_TABLE)
        .select("outlet, total, receipt_date")
        .eq("receipt_type", SUPPLIER_PURCHASE)
        .gte("receipt_date", start)
        .lte("receipt_date", end)
        .execute()
    )
    by: dict = {}
    for r in rows:
        raw = r.get("outlet")
        outlet = canonical_outlet(raw) or (raw if raw else "(no outlet)")
        b = by.setdefault(outlet, {"outlet": outlet, "amount": 0.0, "receipt_count": 0})
        b["amount"] += _to_float(r.get("total"))
        b["receipt_count"] += 1
    return sorted(by.values(), key=lambda x: (-x["amount"], x["outlet"]))[:limit]


def _recon_rows_for(client, date_iso) -> list:
    return _rows(
        client.table(RECONCILIATION_TABLE)
        .select("id, outlet_canonical, business_date, sales_total, "
                "total_food_purchases, food_cost_percent")
        .eq("business_date", date_iso)
        .execute()
    )


def _food_cost_sections(client, now_my) -> dict:
    """Gather the four PR #37 sections: sales today, food cost %, food-cost
    anomalies, cash-no-receipt alerts. Resilient: any failure degrades to an
    empty section rather than breaking the whole digest."""
    out = {
        "sales_today": {}, "food_cost": {}, "food_cost_anomalies": [], "cash_alerts": [],
    }
    today = now_my.date()
    yesterday = today - timedelta(days=1)

    # Reconciliation rows: prefer today, fall back to yesterday (D-files for
    # today's business haven't landed at 23:00, so sales may still be sparse).
    try:
        recon = _recon_rows_for(client, today.isoformat())
        recon_date = today
        if not recon:
            recon = _recon_rows_for(client, yesterday.isoformat())
            recon_date = yesterday
    except Exception:
        logger.warning("digest: purchase_reconciliation unavailable", exc_info=True)
        recon, recon_date = [], today

    if recon:
        out["food_cost"] = {
            "label": recon_date.isoformat(),
            "rows": recon,
            "unclassified": _unclassified_food_cost(client, recon),
        }
        out["food_cost_anomalies"] = _food_cost_anomalies(client, recon, recon_date)
        out["cash_alerts"] = _cash_no_receipt_alerts(client, recon)

    try:
        out["sales_today"] = _sales_today(client, recon_date if recon else today)
    except Exception:
        logger.warning("digest: sales summary unavailable", exc_info=True)
    return out


def _sales_today(client, date) -> dict:
    rows = _rows(
        client.table(SALES_DAILY_SUMMARY_TABLE)
        .select("day_sales, customers, take_away, dine_in")
        .eq("business_date", date.isoformat())
        .execute()
    )
    summary = fca.sales_summary(rows)
    summary["label"] = date.isoformat()
    return summary


def _food_cost_anomalies(client, recon_rows, recon_date) -> list:
    today_by_outlet = {
        r.get("outlet_canonical"): r.get("food_cost_percent")
        for r in recon_rows if r.get("outlet_canonical")
    }
    start = (recon_date - timedelta(days=FOOD_COST_LOOKBACK_DAYS)).isoformat()
    end = (recon_date - timedelta(days=1)).isoformat()
    try:
        history = _rows(
            client.table(RECONCILIATION_TABLE)
            .select("outlet_canonical, food_cost_percent, business_date")
            .gte("business_date", start)
            .lte("business_date", end)
            .execute()
        )
    except Exception:
        logger.warning("digest: food-cost history unavailable", exc_info=True)
        return []
    return fca.compute_anomalies(today_by_outlet, history, FOOD_COST_LOOKBACK_DAYS)


def _cash_no_receipt_alerts(client, recon_rows) -> list:
    """Type B (cash paid, no receipt) entries from purchase_match_log for the
    reconciliation rows being shown."""
    id_to_outlet = {
        r.get("id"): r.get("outlet_canonical") for r in recon_rows if r.get("id") is not None
    }
    if not id_to_outlet:
        return []
    try:
        log_rows = _rows(
            client.table(MATCH_LOG_TABLE)
            .select("reconciliation_id, amount, merchant_or_description, match_type")
            .in_("reconciliation_id", list(id_to_outlet))
            .eq("match_type", "B_cash_no_receipt")
            .execute()
        )
    except Exception:
        logger.warning("digest: cash-no-receipt log unavailable", exc_info=True)
        return []
    alerts = [
        {
            "outlet": id_to_outlet.get(r.get("reconciliation_id")),
            "amount": r.get("amount"),
            "description": r.get("merchant_or_description"),
        }
        for r in log_rows
    ]
    return sorted(alerts, key=lambda a: -(_to_float(a.get("amount"))))


def _unclassified_food_cost(client, recon_rows) -> dict:
    """How much of the food-cost figure came from UNKNOWN-merchant receipts
    (receipt_classification='unknown_included'), so the digest can prompt
    verification of high-value ones. {count, value}."""
    recon_ids = [r.get("id") for r in recon_rows if r.get("id") is not None]
    if not recon_ids:
        return {"count": 0, "value": 0.0}
    try:
        log_rows = _rows(
            client.table(MATCH_LOG_TABLE)
            .select("amount, receipt_classification")
            .in_("reconciliation_id", recon_ids)
            .eq("receipt_classification", "unknown_included")
            .execute()
        )
    except Exception:
        # Column not migrated (0023) yet, or table missing — degrade silently.
        logger.warning("digest: unclassified-merchant stat unavailable", exc_info=True)
        return {"count": 0, "value": 0.0}
    return {
        "count": len(log_rows),
        "value": round(sum(_to_float(r.get("amount")) for r in log_rows), 2),
    }


def _top_items_yesterday(client, now_my) -> dict:
    yesterday = (now_my.date() - timedelta(days=1)).isoformat()
    try:
        summaries = _rows(
            client.table(SALES_DAILY_SUMMARY_TABLE)
            .select("id")
            .eq("business_date", yesterday)
            .execute()
        )
        ids = [s["id"] for s in summaries]
        items = []
        if ids:
            items = _rows(
                client.table(SALES_DAILY_TOP_ITEMS_TABLE)
                .select("item_name, qty, amount")
                .in_("summary_id", ids)
                .execute()
            )
    except Exception:
        logger.warning("digest: top-items-yesterday unavailable", exc_info=True)
        items = []
    return {"label": yesterday, "items": items}


def _safe(fn, default, client, now_my):
    """Run a new-section gatherer; on any failure (e.g. a migration not yet
    applied) degrade to ``default`` rather than breaking the whole digest."""
    try:
        return fn(client, now_my)
    except Exception:
        logger.warning("digest: %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return default


def gather_digest_data(client, now_my) -> dict:
    data = {
        "today": _today_receipts(client, now_my),
        "pm_window_rows": _pm_window(client, now_my),
        "data_quality": _data_quality(client),
        "outliers": {"count": _outlier_count(client), "threshold": OUTLIER_TOTAL_MAX},
        "new_suppliers": _new_suppliers(client, now_my),
        "today_suppliers": _safe(_today_suppliers, [], client, now_my),
        "outlet_spending": _safe(_outlet_spending_week, [], client, now_my),
        "top_items_yesterday": _safe(_top_items_yesterday, {}, client, now_my),
    }
    data.update(_food_cost_sections(client, now_my))
    return data


def log_digest(client, recipient, message_text, status, error_msg=None, message_bytes=None) -> None:
    payload = {
        "recipient": recipient,
        "message_text": message_text,
        "status": status,
        "error_msg": error_msg,
    }
    if message_bytes is not None:
        payload["message_bytes"] = message_bytes
    try:
        client.table(DIGEST_LOG_TABLE).insert(payload).execute()
    except Exception:
        # message_bytes column may not be migrated yet (0014) — retry without it
        # so we still capture the row for diagnosis.
        if "message_bytes" in payload:
            payload.pop("message_bytes")
            try:
                client.table(DIGEST_LOG_TABLE).insert(payload).execute()
                return
            except Exception:
                pass
        logger.warning("digest: could not write digest_log for %s", recipient, exc_info=True)
