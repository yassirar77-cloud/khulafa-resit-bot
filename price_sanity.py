"""Ingestion-side sanity gate for ``item_prices`` rows (issue #79).

The order-draft layer caps absurd forecast quantities downstream, but the
garbage still lived in ``item_prices`` and poisoned every consumer
(forecasting, price-spike detection, averages, shop comparison). This
module rejects implausible rows BEFORE they enter the corpus:

- absolute ceilings on qty / unit_price / line_total (the receipt-2254
  OCR column merge: qty=40250, line_total=RM4,025,000);
- a line total that dwarfs its own receipt's total (a single line can
  never legitimately exceed the whole receipt);
- future-dated ``receipt_date`` (never valid for a purchase receipt,
  corrupts cadence/forecast windows);
- non-positive qty/price (no usable price signal);
- orders-of-magnitude deviation from the item's own recent history
  (median-based, so one poisoned historical row can't drag the bound).

Rejected rows are NOT silently dropped: ``price_aggregation`` stores them
in ``item_price_quarantine`` (migration 0042) with the reject reasons, so
thresholds can be tuned and each row traced back to the OCR misread.

Everything here is fire-and-forget safe: evaluation is pure, the history
fetch swallows all errors, and nothing in this module raises into the
receipt pipeline.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")
_ITEM_PRICES_TABLE = "item_prices"

# Absolute ceilings. Genuine lines across the corpus sit orders of
# magnitude below these (typical qty 1-200, unit price under RM500, the
# receipt-total outlier filter already treats RM5,000+ receipts as
# suspect) — anything above is an OCR column merge, not a purchase.
QTY_MAX = 1_000.0
UNIT_PRICE_MAX = 5_000.0
LINE_TOTAL_MAX = 10_000.0

# A line total may not exceed RECEIPT_TOTAL_FACTOR x the receipt's own
# total — but only once it also clears RECEIPT_TOTAL_CHECK_FLOOR, so a
# receipt whose *total* was misread low doesn't quarantine every normal
# line on it.
RECEIPT_TOTAL_FACTOR = 2.0
RECEIPT_TOTAL_CHECK_FLOOR = 1_000.0

# History-relative bounds: reject only on orders-of-magnitude deviation
# from the item's recent median, and only with enough samples to trust
# the median. Generous multipliers — a genuine price hike is 1.2-2x, a
# bulk order 2-5x; a column merge is 100-1000x.
HISTORY_MIN_SAMPLES = 3
HISTORY_PRICE_FACTOR = 10.0
HISTORY_QTY_FACTOR = 20.0
HISTORY_WINDOW_DAYS = 180

# Reject reason codes (stable strings — stored in the quarantine table).
REASON_FUTURE_DATE = "future_receipt_date"
REASON_NON_POSITIVE = "non_positive_qty_or_price"
REASON_QTY_CEILING = "qty_above_ceiling"
REASON_PRICE_CEILING = "unit_price_above_ceiling"
REASON_LINE_TOTAL_CEILING = "line_total_above_ceiling"
REASON_EXCEEDS_RECEIPT = "line_total_exceeds_receipt_total"
REASON_PRICE_VS_HISTORY = "unit_price_vs_history_median"
REASON_QTY_VS_HISTORY = "qty_vs_history_median"


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    """Best-effort ISO date parse; None on anything unparseable."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def today_my() -> date:
    return datetime.now(_MY_TZ).date()


def evaluate_record(
    record: dict,
    receipt_date: Any = None,
    receipt_total: Any = None,
    history: dict | None = None,
    today: date | None = None,
) -> list[str]:
    """Return the list of reject reasons for one price record.

    Empty list = the row is plausible and may enter ``item_prices``.
    ``history`` is the per-item stats dict from ``fetch_history_stats``
    (or None when no history is available). Pure — never raises.
    """
    reasons: list[str] = []
    qty = _to_float(record.get("qty"))
    unit_price = _to_float(record.get("unit_price"))
    line_total = _to_float(record.get("line_total"))

    rd = _parse_date(receipt_date)
    if rd is not None and rd > (today or today_my()):
        reasons.append(REASON_FUTURE_DATE)

    if (qty is not None and qty <= 0) or (unit_price is not None and unit_price <= 0):
        reasons.append(REASON_NON_POSITIVE)

    if qty is not None and qty > QTY_MAX:
        reasons.append(REASON_QTY_CEILING)
    if unit_price is not None and unit_price > UNIT_PRICE_MAX:
        reasons.append(REASON_PRICE_CEILING)
    if line_total is not None and line_total > LINE_TOTAL_MAX:
        reasons.append(REASON_LINE_TOTAL_CEILING)

    total = _to_float(receipt_total)
    if (
        line_total is not None
        and total is not None
        and total > 0
        and line_total > RECEIPT_TOTAL_CHECK_FLOOR
        and line_total > RECEIPT_TOTAL_FACTOR * total
    ):
        reasons.append(REASON_EXCEEDS_RECEIPT)

    if isinstance(history, dict):
        median_price = _to_float(history.get("median_price"))
        price_samples = history.get("price_samples") or 0
        if (
            unit_price is not None
            and median_price is not None
            and median_price > 0
            and price_samples >= HISTORY_MIN_SAMPLES
            and unit_price > HISTORY_PRICE_FACTOR * median_price
        ):
            reasons.append(REASON_PRICE_VS_HISTORY)

        median_qty = _to_float(history.get("median_qty"))
        qty_samples = history.get("qty_samples") or 0
        if (
            qty is not None
            and median_qty is not None
            and median_qty > 0
            and qty_samples >= HISTORY_MIN_SAMPLES
            and qty > HISTORY_QTY_FACTOR * median_qty
        ):
            reasons.append(REASON_QTY_VS_HISTORY)

    return reasons


def partition_records(
    records: list[dict],
    receipt_date: Any = None,
    receipt_total: Any = None,
    history_by_item: dict | None = None,
    today: date | None = None,
) -> tuple[list[dict], list[tuple[dict, list[str]]]]:
    """Split records into (clean, rejected) where rejected pairs each
    record with its reasons. Never raises."""
    clean: list[dict] = []
    rejected: list[tuple[dict, list[str]]] = []
    history_by_item = history_by_item or {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        history = history_by_item.get(rec.get("canonical_item"))
        try:
            reasons = evaluate_record(
                rec,
                receipt_date=receipt_date,
                receipt_total=receipt_total,
                history=history,
                today=today,
            )
        except Exception:  # pragma: no cover - safety net
            logger.exception("price sanity evaluation failed; passing row through")
            reasons = []
        if reasons:
            rejected.append((rec, reasons))
        else:
            clean.append(rec)
    return clean, rejected


def median_stats(rows: list[dict]) -> dict:
    """Median qty/unit_price stats over ``rows``, counting only values
    inside the absolute ceilings — historical garbage must not stretch
    the very bound that is supposed to catch it."""
    prices = []
    qtys = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        p = _to_float(row.get("unit_price"))
        if p is not None and 0 < p <= UNIT_PRICE_MAX:
            prices.append(p)
        q = _to_float(row.get("qty"))
        if q is not None and 0 < q <= QTY_MAX:
            qtys.append(q)
    return {
        "median_price": statistics.median(prices) if prices else None,
        "price_samples": len(prices),
        "median_qty": statistics.median(qtys) if qtys else None,
        "qty_samples": len(qtys),
    }


def fetch_history_stats(
    supabase_client,
    canonical_items,
    window_days: int = HISTORY_WINDOW_DAYS,
    today: date | None = None,
) -> dict:
    """Per-canonical-item median stats over the recent window.

    Only rows already inside the absolute ceilings feed the medians, so
    historical garbage (pre-gate rows) can't stretch the bound and let
    new garbage through. Returns ``{canonical_item: stats_dict}``;
    items with no usable history are simply absent. Never raises.
    """
    stats: dict = {}
    cutoff = None
    try:
        from datetime import timedelta

        cutoff = ((today or today_my()) - timedelta(days=window_days)).isoformat()
    except Exception:  # pragma: no cover - defensive
        pass

    for item in sorted(
        {c for c in (canonical_items or []) if isinstance(c, str) and c.strip()}
    ):
        try:
            query = (
                supabase_client.table(_ITEM_PRICES_TABLE)
                .select("qty, unit_price")
                .eq("canonical_item", item)
            )
            if cutoff:
                query = query.gte("receipt_date", cutoff)
            result = query.limit(1000).execute()
            rows = getattr(result, "data", None) or []
            if isinstance(rows, list) and rows:
                stats[item] = median_stats(rows)
        except Exception:
            logger.exception("fetch_history_stats: query failed (item=%s)", item)
            continue
    return stats


_QUARANTINE_TABLE = "item_price_quarantine"


def fetch_recent_quarantine(supabase_client, limit: int = 10) -> list[dict]:
    """Most recent quarantined rows, newest first. Raises on query
    failure — the command handler owns the user-facing error."""
    result = (
        supabase_client.table(_QUARANTINE_TABLE)
        .select(
            "id, receipt_id, receipt_date, outlet_code, merchant, "
            "canonical_item, raw_item_name, qty, unit_price, line_total, "
            "reasons, source, created_at"
        )
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return getattr(result, "data", None) or []


def _fmt_num(value: Any) -> str:
    f = _to_float(value)
    if f is None:
        return "—"
    return f"{f:,.2f}".rstrip("0").rstrip(".") if f % 1 else f"{f:,.0f}"


def format_quarantine_rows(rows: list[dict]) -> str:
    """Telegram-friendly plain-text listing for ``/price_quarantine``."""
    if not rows:
        return (
            "✅ Quarantine kosong — tiada garbage row kena tolak.\n"
            "(Gate issue #79 aktif: qty/harga mustahil, tarikh masa depan, "
            "outlier 10x+ vs sejarah semua masuk sini, bukan item_prices.)"
        )
    lines = [f"🧯 ITEM PRICE QUARANTINE — {len(rows)} latest"]
    for row in rows:
        date_s = str(row.get("receipt_date") or "?")
        lines.append(
            f"\n#{row.get('id')} resit {row.get('receipt_id') or '—'} "
            f"({date_s}, {row.get('outlet_code') or '—'})"
        )
        lines.append(
            f"  {row.get('raw_item_name') or '?'} @ {row.get('merchant') or '—'}"
        )
        lines.append(
            f"  qty {_fmt_num(row.get('qty'))} x RM{_fmt_num(row.get('unit_price'))} "
            f"= RM{_fmt_num(row.get('line_total'))}"
        )
        lines.append(
            f"  sebab: {row.get('reasons') or '?'} [{row.get('source') or 'ingest'}]"
        )
    return "\n".join(lines)


def count_quarantined_since(supabase_client, since_iso: str) -> int:
    """Count quarantine rows created at/after ``since_iso``. Returns 0 on
    any failure — feeds the digest data-quality section, which must not
    break the digest."""
    try:
        result = (
            supabase_client.table(_QUARANTINE_TABLE)
            .select("id")
            .gte("created_at", since_iso)
            .limit(1000)
            .execute()
        )
    except Exception:
        logger.exception("count_quarantined_since failed")
        return 0
    return len(getattr(result, "data", None) or [])
