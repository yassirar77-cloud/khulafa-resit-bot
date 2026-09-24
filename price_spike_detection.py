"""Price spike detection against historical ``item_prices`` data.

After a receipt's line items are persisted via
``price_aggregation.save_item_prices``, this module compares each new
item's unit price against the historical average for the same
canonical item — first scoped to the same merchant, falling back to
global if there isn't enough merchant history.

If a new price exceeds 110% of the historical average (and at least 5
prior samples exist for the chosen scope), a spike record is emitted;
``bot.py`` formats it via ``format_spike_message`` and sends it to the
manager group. The same spike is also rendered in Tamil via
``format_spike_message_tamil`` and sent to the outlet's registered
manager (see ``manager_registration``), telling them to ask the
supplier why the price went up and naming the shops that are still
cheaper — routed through the MANAGER_DELIVERY_ENABLED gate like every
other per-manager message.

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


def _clean_rows(supabase_client, canonical_item: str) -> list[dict]:
    """Comparable price rows for an item, or ``[]``. Never raises.

    Delegates to ``shop_price_comparison.load_price_rows``, which drops
    internal transfers, own-outlet lines, non-supplier receipts and
    corrupt future dates, collapses OCR merchant variants onto one shop,
    and tags each line with its cut. Comparing against the raw table
    instead would average RM1.70 internal-transfer lines together with
    RM54.00 cooked-chicken lines and call the result a chicken price.
    """
    try:
        from shop_price_comparison import load_price_rows

        return load_price_rows(supabase_client, canonical_item)
    except Exception:
        logger.exception(
            "detect_spikes: price row load failed (canonical=%s)", canonical_item
        )
        return []


def _history_from_rows(rows: list[dict], merchant) -> dict | None:
    """Historical stats from cleaned rows: same shop first, then all shops.

    Mirrors ``get_historical_average``'s contract (``_MIN_SAMPLES`` per
    scope, ``None`` when neither has enough) but over rows that are
    already filtered to one cut, so the baseline compares like with like.
    """
    try:
        from shop_price_comparison import shop_key

        if isinstance(merchant, str) and merchant.strip():
            key = shop_key(merchant)
            same_shop = [r["unit_price"] for r in rows if r["shop_key"] == key]
            if len(same_shop) >= _MIN_SAMPLES:
                return _summarize(same_shop, "merchant")

        all_shops = [r["unit_price"] for r in rows]
        if len(all_shops) >= _MIN_SAMPLES:
            return _summarize(all_shops, "global")
        return None
    except Exception:
        logger.exception("_history_from_rows: unexpected failure")
        return None


def detect_spikes(
    supabase_client,
    price_records: list[dict],
    receipt_id,
    merchant,
    include_shop_prices: bool = True,
) -> list[dict]:
    """Return one spike dict per item whose current ``unit_price``
    exceeds the historical average by ``_SPIKE_THRESHOLD``.

    Both the baseline and the cross-shop block are built from cleaned,
    cut-scoped rows (see ``_clean_rows``): leg quarters are compared
    against leg quarters at real suppliers, never against whole birds or
    an internal transfer. When those rows can't be loaded the detector
    falls back to the raw historical average rather than going silent.

    Skips records with no canonical item, non-positive prices, or
    insufficient history. Deduplicates within a single receipt: the
    same ``canonical_item`` only triggers one alert per call even if
    it appears multiple times in ``price_records``. Never raises.
    """
    try:
        if not isinstance(price_records, list):
            return []
        try:
            from shop_price_comparison import item_variant, summarise_shops
        except Exception:
            # Detection still has to run without the comparison layer.
            logger.exception("detect_spikes: comparison layer unavailable")
            item_variant = summarise_shops = None

        spikes: list[dict] = []
        seen: set[str] = set()
        rows_cache: dict[str, list[dict]] = {}
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

                if item_variant is None:
                    variant, variant_rows = "", []
                else:
                    if canonical not in rows_cache:
                        rows_cache[canonical] = _clean_rows(supabase_client, canonical)
                    variant = item_variant(rec.get("raw_item_name"), canonical)
                    variant_rows = [
                        r for r in rows_cache[canonical] if r["variant"] == variant
                    ]

                # The current receipt's own row is already in item_prices;
                # it must not pull its own baseline up.
                hist = _history_from_rows(
                    [r for r in variant_rows if r.get("receipt_id") != receipt_id],
                    merchant,
                )
                if hist is None and not variant_rows:
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
                    # Includes today's row on purpose: the block should show
                    # this shop's new price beside everyone else's.
                    shop_prices = (
                        summarise_shops(variant_rows)
                        if include_shop_prices and summarise_shops
                        else []
                    )
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
                        "shop_prices": shop_prices,
                        "shop_variant": variant if include_shop_prices else "",
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

        # Name the cut when we know it: "Paha Ayam — BESTARI FARM" tells the
        # director what actually moved; a bare "Ayam" does not.
        variant = str(spike.get("shop_variant") or "").strip()
        title = variant.title() if variant else canonical.replace("_", " ").title()
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
                    canonical,
                    shop_prices,
                    current_shop=merchant,
                    variant=spike.get("shop_variant") or None,
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


_MAX_CHEAPER_SHOPS = 3


def cheaper_shops(spike: dict) -> list[dict]:
    """Shops from the spike's comparison block that beat today's price.

    Excludes the shop the spike happened at (its own new price is not an
    alternative), keeps the cheapest-first order ``summarise_shops``
    already established, and caps the list at ``_MAX_CHEAPER_SHOPS`` —
    a manager needs one or two phone calls to make, not a league table.
    Returns ``[]`` on malformed input. Never raises.
    """
    try:
        if not isinstance(spike, dict):
            return []
        current = _to_float(spike.get("current_price"))
        if current is None or current <= 0:
            return []
        shop_prices = spike.get("shop_prices")
        if not isinstance(shop_prices, list):
            return []

        from shop_price_comparison import shop_key

        merchant = str(spike.get("merchant") or "").strip()
        own_key = shop_key(merchant) if merchant else ""

        out: list[dict] = []
        for shop in shop_prices:
            if not isinstance(shop, dict):
                continue
            price = _to_float(shop.get("latest_price"))
            name = str(shop.get("shop") or "").strip()
            if price is None or price <= 0 or not name:
                continue
            if own_key and shop_key(name) == own_key:
                continue
            if price < current:
                out.append({"shop": name, "latest_price": price})
            if len(out) >= _MAX_CHEAPER_SHOPS:
                break
        return out
    except Exception:
        logger.exception("cheaper_shops: unexpected failure")
        return []


def format_spike_message_tamil(spike: dict) -> str:
    """The outlet manager's version of the spike alert, in Tamil.

    Same facts as ``format_spike_message`` but written the way the shop
    managers actually read — spoken Tamil with the English trade words
    kept as-is (same register as the audit warnings in ``bot.py`` and the
    kitchen-usage prompts). Three beats, per the owner's spec:

    1. this item's price went up at your supplier,
    2. ask the supplier WHY,
    3. these other shops are still cheaper — go compare.

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
        n = int(spike["sample_count"])
        current = float(spike["current_price"])
        percent = float(spike["percent_increase"])
        merchant = str(spike.get("merchant") or "").strip()

        variant = str(spike.get("shop_variant") or "").strip()
        title = variant.title() if variant else canonical.replace("_", " ").title()
        merchant_part = f" — {merchant}" if merchant else ""

        body = (
            "⚠️ விலை ஏறிடுச்சு! Price naik!\n"
            "\n"
            f"{title}{merchant_part}\n"
            f"முன்னாடி average: RM{avg:.2f} ({n} resit)\n"
            f"இன்னைக்கு: RM{current:.2f} (+{percent:.1f}%)\n"
            "\n"
            "👉 சப்ளையர்கிட்ட ஏன் விலை ஏறுச்சு-னு கேளுங்க.\n"
        )

        cheaper = cheaper_shops(spike)
        if cheaper:
            body += "\nவேற கடையில இன்னும் cheap-ஆ இருக்கு:\n"
            for shop in cheaper:
                body += f"• {shop['shop']}: RM{shop['latest_price']:.2f}\n"
            body += "\nஅங்க rate-அ compare பண்ணி பாருங்க. 🙏"
        elif isinstance(spike.get("shop_prices"), list) and len(spike["shop_prices"]) >= 2:
            # There IS a comparison and nobody beats today's price — say so,
            # or the manager will assume the bot simply didn't check.
            body += "\nவேற கடையிலயும் இப்போ இதுக்கு cheap-ஆ இல்ல."

        return body.rstrip("\n")
    except Exception:
        logger.exception("format_spike_message_tamil: failed to format")
        return ""
