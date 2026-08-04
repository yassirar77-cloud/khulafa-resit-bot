"""Price spike detection against historical ``item_prices`` data.

After a receipt's line items are persisted via
``price_aggregation.save_item_prices``, this module compares each new
item's unit price against the historical average for the same
canonical item — first scoped to the same merchant, falling back to
global if there isn't enough merchant history.

If a new price exceeds 110% of the historical average (and at least 5
prior samples exist for the chosen scope), a spike record is emitted;
``bot.py`` formats it via ``format_spike_message`` and sends it to the
manager group.

Every spike also carries a cross-shop price comparison (see
``shop_price_comparison``): for the SAME item, what every other shop
charged recently, cheapest first. The director's next question after
"the price went up" is always "who else sells it cheaper?", so the alert
answers both at once. This applies to every item with price history, not
just the ones that happen to be watched closely.

Hard rules:
- Minimum 5 historical samples per scope before any alert fires.
- Strict ``>`` 110% threshold (exactly 110% does not trigger).
- Failure NEVER raises — every entry point swallows exceptions and
  returns a safe default. Detection is fire-and-forget and must never
  crash the receipt pipeline.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ITEM_PRICES_TABLE = "item_prices"
_MIN_SAMPLES = 5
_SPIKE_THRESHOLD = 1.10  # alert when current > 110% of historical avg


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _query_prices(
    supabase_client,
    canonical_item: str,
    merchant: str | None,
    exclude_receipt_id,
) -> list[float]:
    """Fetch ``unit_price`` values for the given filter scope.

    Returns ``[]`` on any error or empty result. Never raises.
    Filters out non-positive prices (zero or negative carry no signal).
    """
    try:
        query = (
            supabase_client.table(_ITEM_PRICES_TABLE)
            .select("unit_price")
            .eq("canonical_item", canonical_item)
        )
        if merchant:
            query = query.eq("merchant", merchant)
        if exclude_receipt_id is not None:
            query = query.neq("receipt_id", exclude_receipt_id)
        result = query.execute()
    except Exception:
        logger.exception(
            "get_historical_average: query failed (canonical=%s, merchant=%s)",
            canonical_item,
            merchant,
        )
        return []

    rows = getattr(result, "data", None) or []
    prices: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _to_float(row.get("unit_price"))
        if price is None or price <= 0:
            continue
        prices.append(price)
    return prices


def _summarize(prices: list[float], scope: str) -> dict:
    return {
        "avg_price": sum(prices) / len(prices),
        "min_price": min(prices),
        "max_price": max(prices),
        "sample_count": len(prices),
        "scope": scope,
    }


def get_historical_average(
    supabase_client,
    canonical_item,
    merchant=None,
    exclude_receipt_id=None,
) -> dict | None:
    """Return historical price stats for ``canonical_item``.

    Tries merchant-scoped first; falls back to global if the
    merchant-scoped sample is below ``_MIN_SAMPLES``. Returns ``None``
    if neither scope has enough data, or on any failure.

    Output: ``{'avg_price', 'min_price', 'max_price', 'sample_count',
    'scope'}`` where ``scope`` is ``'merchant'`` or ``'global'``.
    """
    try:
        if not isinstance(canonical_item, str) or not canonical_item.strip():
            return None
        canon = canonical_item.strip()

        if isinstance(merchant, str) and merchant.strip():
            merchant_prices = _query_prices(
                supabase_client, canon, merchant, exclude_receipt_id
            )
            if len(merchant_prices) >= _MIN_SAMPLES:
                return _summarize(merchant_prices, "merchant")

        global_prices = _query_prices(
            supabase_client, canon, None, exclude_receipt_id
        )
        if len(global_prices) >= _MIN_SAMPLES:
            return _summarize(global_prices, "global")

        return None
    except Exception:
        logger.exception("get_historical_average: unexpected failure")
        return None


def _shop_prices_for(supabase_client, canonical_item: str) -> list[dict]:
    """Per-shop price summary for the alert block. Never raises.

    Deliberately does NOT exclude the current receipt: its row is already
    in ``item_prices`` by the time detection runs, and the whole point of
    the block is to show today's price alongside what the other shops
    charge.
    """
    try:
        from shop_price_comparison import get_shop_prices

        return get_shop_prices(supabase_client, canonical_item)
    except Exception:
        logger.exception(
            "detect_spikes: shop comparison failed (canonical=%s)",
            canonical_item,
        )
        return []


def detect_spikes(
    supabase_client,
    price_records: list[dict],
    receipt_id,
    merchant,
    include_shop_prices: bool = True,
) -> list[dict]:
    """Return one spike dict per item whose current ``unit_price``
    exceeds the historical average by ``_SPIKE_THRESHOLD``.

    Skips records with no canonical item, non-positive prices, or
    insufficient history. Deduplicates within a single receipt: the
    same ``canonical_item`` only triggers one alert per call even if
    it appears multiple times in ``price_records``. Never raises.
    """
    try:
        if not isinstance(price_records, list):
            return []
        spikes: list[dict] = []
        seen: set[str] = set()
        for rec in price_records:
            try:
                if not isinstance(rec, dict):
                    continue
                canonical = rec.get("canonical_item")
                if not isinstance(canonical, str) or not canonical.strip():
                    continue
                if canonical in seen:
                    continue
                current = _to_float(rec.get("unit_price"))
                if current is None or current <= 0:
                    continue

                hist = get_historical_average(
                    supabase_client,
                    canonical,
                    merchant=merchant,
                    exclude_receipt_id=receipt_id,
                )
                if hist is None:
                    continue

                avg = hist["avg_price"]
                if avg <= 0:
                    continue

                if current > _SPIKE_THRESHOLD * avg:
                    percent_increase = ((current - avg) / avg) * 100.0
                    spikes.append({
                        "canonical_item": canonical,
                        "raw_item_name": rec.get("raw_item_name") or "",
                        "current_price": current,
                        "historical_avg": avg,
                        "min_price": hist["min_price"],
                        "max_price": hist["max_price"],
                        "sample_count": hist["sample_count"],
                        "scope": hist["scope"],
                        "percent_increase": percent_increase,
                        "merchant": merchant or "",
                        "shop_prices": (
                            _shop_prices_for(supabase_client, canonical)
                            if include_shop_prices
                            else []
                        ),
                    })
                    seen.add(canonical)
            except Exception:
                logger.exception("detect_spikes: per-item failure (skipping)")
                continue
        return spikes
    except Exception:
        logger.exception("detect_spikes: unexpected failure")
        return []


def format_spike_message(spike: dict) -> str:
    """Render a spike dict as the Style A manager-group alert.

    Returns ``""`` on malformed input so the caller can skip silently.
    Never raises.
    """
    try:
        if not isinstance(spike, dict):
            return ""
        canonical = str(spike.get("canonical_item") or "").strip()
        if not canonical:
            return ""
        avg = float(spike["historical_avg"])
        min_p = float(spike["min_price"])
        max_p = float(spike["max_price"])
        n = int(spike["sample_count"])
        scope = str(spike.get("scope") or "").strip()
        current = float(spike["current_price"])
        percent = float(spike["percent_increase"])
        merchant = str(spike.get("merchant") or "").strip()

        title = canonical.title()
        merchant_part = f" — {merchant}" if merchant else ""
        body = (
            "⚠️ Price increase detected\n"
            "\n"
            f"{title}{merchant_part}\n"
            f"Previous average: RM{avg:.2f} (from {n} receipts, {scope} scope)\n"
            f"Range: RM{min_p:.2f} - RM{max_p:.2f}\n"
            f"Today: RM{current:.2f} (+{percent:.1f}%)\n"
        )

        # Cross-shop block. Absent/empty ``shop_prices`` (or a single shop,
        # where there is nothing to compare) leaves the alert exactly as it
        # was before this feature.
        comparison = ""
        shop_prices = spike.get("shop_prices")
        if shop_prices:
            try:
                from shop_price_comparison import format_shop_comparison

                comparison = format_shop_comparison(
                    canonical, shop_prices, current_shop=merchant
                )
            except Exception:
                logger.exception(
                    "format_spike_message: shop comparison block failed"
                )
                comparison = ""
        if comparison:
            body += "\n" + comparison + "\n"

        return body + "\nDid you ask supplier?"
    except Exception:
        logger.exception("format_spike_message: failed to format")
        return ""
