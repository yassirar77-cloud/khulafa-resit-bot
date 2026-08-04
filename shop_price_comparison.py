"""Cross-shop price comparison for ANY item (not just ayam/chicken).

The director alert used to say only "this item got more expensive at this
shop". That answers "did the price move?" but not the question the
director actually asks next: *"who else sells it, and for how much?"*

This module answers that. Given a canonical item it reads every
``item_prices`` row for that item inside a lookback window, groups by
``merchant`` (the shop / supplier), and returns one row per shop with its
latest price, average, range and sample count — cheapest shop first. The
same block is:

- appended to every price-spike alert (``price_spike_detection``), so the
  director sees the alternatives in the alert itself, and
- available on demand via ``/shop_prices <item>`` in ``bot.py``.

It is item-agnostic: any canonical item with price history is supported.
``resolve_item_query`` maps free text ("ayam", "AYAM BERSIH 30KG",
"chicken") onto a canonical key so the command works with whatever the
director types.

Hard rules (same as the rest of the alert pipeline):
- Nothing here ever raises. Every entry point swallows exceptions and
  returns a safe default ([] or "") — a comparison failure must never
  block a price alert or crash the receipt pipeline.
- Non-positive prices carry no signal and are dropped.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_ITEM_PRICES_TABLE = "item_prices"

# How far back a comparison looks. Supplier prices drift, so a two-year-old
# quote is noise; 90 days keeps "who is cheapest right now" honest.
DEFAULT_LOOKBACK_DAYS = 90

# Alerts go to a phone. More than a handful of shops turns the alert into a
# spreadsheet, so the tail is summarised as "+N more shops".
DEFAULT_MAX_SHOPS = 6

_UNKNOWN_SHOP = "Unknown shop"


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_name(canonical: Any) -> str:
    """``ais_batu`` -> ``Ais Batu``. Canonical keys are snake_case."""
    text = str(canonical or "").strip()
    if not text:
        return "Item"
    return text.replace("_", " ").title()


def _date_key(value: Any) -> str:
    """Sortable date string; unparseable/missing dates sort oldest."""
    return value if isinstance(value, str) else ""


def _format_date(value: Any) -> str:
    """``2026-08-02`` -> ``02 Aug``. Falls back to the raw value."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return value.strip()


def _cutoff_iso(lookback_days: int | None, today: date | None) -> str | None:
    if not lookback_days or lookback_days <= 0:
        return None
    base = today or date.today()
    try:
        return (base - timedelta(days=int(lookback_days))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_rows(
    supabase_client,
    canonical_item: str,
    cutoff: str | None,
    exclude_receipt_id,
) -> list[dict]:
    """Read the item's price rows. Never raises; returns [] on failure.

    Tries the paginated read first (hosted PostgREST clamps every response
    to 1000 rows, so a busy item would silently truncate), and falls back
    to a single plain query for clients without ``.order``/``.range``.

    Rows with a NULL ``receipt_date`` drop out whenever a lookback window
    is in force — an undated row can't be placed in a window. Pass
    ``lookback_days=None`` to see them.
    """

    def _build():
        query = (
            supabase_client.table(_ITEM_PRICES_TABLE)
            .select("merchant, receipt_date, unit_price, qty, receipt_id, outlet_code")
            .eq("canonical_item", canonical_item)
        )
        if cutoff:
            query = query.gte("receipt_date", cutoff)
        if exclude_receipt_id is not None:
            query = query.neq("receipt_id", exclude_receipt_id)
        return query

    try:
        from db_pagination import fetch_all_pages

        return fetch_all_pages(lambda: _build().order("receipt_id", desc=False))
    except Exception:
        logger.debug(
            "get_shop_prices: paginated read unavailable, falling back "
            "(canonical=%s)",
            canonical_item,
            exc_info=True,
        )

    try:
        result = _build().execute()
    except Exception:
        logger.exception(
            "get_shop_prices: query failed (canonical=%s)", canonical_item
        )
        return []
    return getattr(result, "data", None) or []


def get_shop_prices(
    supabase_client,
    canonical_item,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    exclude_receipt_id=None,
    today: date | None = None,
) -> list[dict]:
    """Return one price summary per shop for ``canonical_item``.

    Each entry: ``{'shop', 'latest_price', 'latest_date', 'avg_price',
    'min_price', 'max_price', 'sample_count'}``. Sorted by ``latest_price``
    ascending (cheapest shop first), ties broken by shop name so the
    output is stable.

    ``lookback_days=None`` (or 0) reads the full history. Never raises —
    returns ``[]`` on bad input or any failure.
    """
    try:
        if not isinstance(canonical_item, str) or not canonical_item.strip():
            return []
        canon = canonical_item.strip()

        rows = _fetch_rows(
            supabase_client,
            canon,
            _cutoff_iso(lookback_days, today),
            exclude_receipt_id,
        )

        by_shop: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = _to_float(row.get("unit_price"))
            if price is None or price <= 0:
                continue
            shop = row.get("merchant")
            shop = shop.strip() if isinstance(shop, str) and shop.strip() else _UNKNOWN_SHOP

            bucket = by_shop.setdefault(
                shop,
                {
                    "shop": shop,
                    "prices": [],
                    "latest_price": price,
                    "latest_date": None,
                    "_latest_key": None,
                },
            )
            bucket["prices"].append(price)

            # "Latest" = newest receipt_date, with receipt_id as the
            # tiebreaker so two receipts on the same day resolve to the
            # one logged last rather than to whichever row came back first.
            receipt_id = row.get("receipt_id")
            sort_key = (
                _date_key(row.get("receipt_date")),
                receipt_id if isinstance(receipt_id, int) else -1,
            )
            if bucket["_latest_key"] is None or sort_key > bucket["_latest_key"]:
                bucket["_latest_key"] = sort_key
                bucket["latest_price"] = price
                bucket["latest_date"] = row.get("receipt_date")

        shops: list[dict] = []
        for bucket in by_shop.values():
            prices = bucket["prices"]
            if not prices:
                continue
            shops.append({
                "shop": bucket["shop"],
                "latest_price": bucket["latest_price"],
                "latest_date": bucket["latest_date"],
                "avg_price": sum(prices) / len(prices),
                "min_price": min(prices),
                "max_price": max(prices),
                "sample_count": len(prices),
            })

        shops.sort(key=lambda s: (s["latest_price"], s["shop"]))
        return shops
    except Exception:
        logger.exception("get_shop_prices: unexpected failure")
        return []


def _shop_line(shop: dict, marker: str) -> str:
    latest = float(shop["latest_price"])
    avg = float(shop["avg_price"])
    n = int(shop["sample_count"])
    when = _format_date(shop.get("latest_date"))
    when_part = f", last {when}" if when else ""
    receipts = "receipt" if n == 1 else "receipts"
    return (
        f"{marker} {shop['shop']} — RM{latest:.2f} "
        f"(avg RM{avg:.2f}, {n} {receipts}{when_part})"
    )


def format_shop_comparison(
    canonical_item,
    shops: list[dict],
    current_shop=None,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    max_shops: int = DEFAULT_MAX_SHOPS,
) -> str:
    """Render the "price at all shops" block for an alert.

    Returns ``""`` when there is nothing worth comparing (fewer than two
    shops with history) or on malformed input — the caller then simply
    omits the block. Never raises.

    The current shop is always shown even if it falls outside the top
    ``max_shops``: an alert that hides the shop it is complaining about
    would be useless.
    """
    try:
        if not isinstance(shops, list) or len(shops) < 2:
            return ""
        clean = [s for s in shops if isinstance(s, dict) and _to_float(s.get("latest_price"))]
        if len(clean) < 2:
            return ""

        current = (current_shop or "").strip() if isinstance(current_shop, str) else ""
        limit = max(1, int(max_shops or DEFAULT_MAX_SHOPS))

        shown = clean[:limit]
        hidden = clean[limit:]
        current_row = next((s for s in clean if s["shop"] == current), None)
        if current_row is not None and current_row not in shown:
            shown = shown + [current_row]
            hidden = [s for s in hidden if s is not current_row]

        title = _display_name(canonical_item)
        window = (
            f" (last {int(lookback_days)} days)"
            if lookback_days and int(lookback_days) > 0
            else ""
        )
        lines = [f"🏪 Price at all shops — {title}{window}:"]
        for i, shop in enumerate(shown):
            if shop["shop"] == current and current:
                marker = "👉"
            elif i == 0:
                marker = "🥇"
            else:
                marker = "•"
            suffix = "  ← this receipt" if shop["shop"] == current and current else ""
            lines.append(_shop_line(shop, marker) + suffix)

        if hidden:
            lines.append(f"… +{len(hidden)} more shop(s)")

        cheapest = clean[0]
        if current_row is not None and current_row is not cheapest:
            gap = float(current_row["latest_price"]) - float(cheapest["latest_price"])
            if gap > 0:
                pct = gap / float(current_row["latest_price"]) * 100.0
                lines.append(
                    f"💡 Cheapest: {cheapest['shop']} at "
                    f"RM{float(cheapest['latest_price']):.2f} — RM{gap:.2f} "
                    f"({pct:.0f}%) below {current_row['shop']}."
                )
        elif current_row is not None and current_row is cheapest:
            lines.append(f"✅ {current_row['shop']} is still the cheapest shop.")
        else:
            lines.append(
                f"💡 Cheapest: {cheapest['shop']} at "
                f"RM{float(cheapest['latest_price']):.2f}."
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("format_shop_comparison: failed to format")
        return ""


# --- free-text item lookup (drives /shop_prices <anything>) -----------------

# English terms for items whose canonical key is Malay. Only needed where
# the canonical vocabulary has no English variation; anything else already
# resolves through ``canonicalize_item``. Unknown targets are ignored at
# lookup time, so this list is safe to extend ahead of the vocabulary.
_QUERY_SYNONYMS = {
    "chicken": "ayam",
    "beef": "daging",
    "meat": "daging",
    "mutton": "kambing",
    "lamb": "kambing",
    "goat": "kambing",
    "fish": "ikan",
    "anchovies": "ikan_bilis",
    "anchovy": "ikan_bilis",
    "prawn": "udang",
    "prawns": "udang",
    "shrimp": "udang",
    "squid": "sotong",
    "ice": "ais_batu",
    "ice cube": "ais_batu",
    "ice cubes": "ais_batu",
    "coffee": "kopi",
    "bread": "roti",
    "coconut": "kelapa",
    "coconut milk": "santan",
    "crackers": "keropok",
    "peanut": "kacang",
    "peanuts": "kacang",
    "nuts": "kacang",
    "soy sauce": "kicap",
    "chilli sauce": "sos_cili",
    "chili sauce": "sos_cili",
    "tomato sauce": "sos_tomato",
    "ketchup": "sos_tomato",
    "oyster sauce": "sos_tiram",
    "tamarind": "asam_jawa",
    "vinegar": "cuka",
    "fried onion": "bawang_goreng",
    "fried onions": "bawang_goreng",
    "breadcrumbs": "tepung_roti",
    "yoghurt": "yogurt",
}


def resolve_item_query(query: Any) -> dict:
    """Map free text to a canonical item key.

    Returns ``{'canonical': str | None, 'suggestions': list[str]}``.
    ``suggestions`` is populated when the text is ambiguous (matches
    several canonical keys) or unknown, so the caller can reply with
    "did you mean…". Never raises.
    """
    out: dict = {"canonical": None, "suggestions": []}
    try:
        if not isinstance(query, str) or not query.strip():
            return out
        text = " ".join(query.split()).strip()
        lowered = text.lower()

        from item_canonicalization_v2 import canonicalize_item, list_canonical_items

        known = list_canonical_items()

        # Canonical keys are snake_case ("ais_batu"); nobody types the
        # underscore, so "ais batu" and "ais-batu" resolve too.
        keyed = lowered.replace("-", " ").replace(" ", "_")
        if lowered in known:
            out["canonical"] = lowered
            return out
        if keyed in known:
            out["canonical"] = keyed
            return out

        synonym = _QUERY_SYNONYMS.get(lowered)
        if synonym and synonym in known:
            out["canonical"] = synonym
            return out

        result = canonicalize_item(text)
        if result.get("matched") and result.get("canonical"):
            out["canonical"] = result["canonical"]
            return out

        partial = [k for k in known if keyed in k]
        if len(partial) == 1:
            out["canonical"] = partial[0]
            return out
        if partial:
            out["suggestions"] = sorted(partial)[:10]
            return out

        # Last resort: token overlap, so "harga ayam" still finds "ayam".
        tokens = [t for t in lowered.replace("-", " ").split() if len(t) > 2]
        overlap = sorted({k for k in known for t in tokens if t in k})
        out["suggestions"] = overlap[:10]
        return out
    except Exception:
        logger.exception("resolve_item_query: unexpected failure")
        return out


def build_shop_price_report(
    supabase_client,
    query,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    max_shops: int = 12,
    today: date | None = None,
) -> str:
    """Full text reply for ``/shop_prices <item>``.

    Blocking (Supabase I/O) — call it from a worker thread. Always returns
    a user-facing string; never raises.
    """
    try:
        raw = (query or "").strip() if isinstance(query, str) else ""
        if not raw:
            return "Usage: /shop_prices <item>\nExample: /shop_prices ayam"

        resolved = resolve_item_query(raw)
        canonical = resolved.get("canonical")
        if not canonical:
            suggestions = resolved.get("suggestions") or []
            if suggestions:
                return (
                    f'No item called "{raw}". Did you mean: '
                    + ", ".join(_display_name(s) for s in suggestions)
                    + "?"
                )
            return (
                f'No price history for "{raw}". '
                "Try an item name as it appears on receipts, e.g. "
                "/shop_prices ayam"
            )

        shops = get_shop_prices(
            supabase_client,
            canonical,
            lookback_days=lookback_days,
            today=today,
        )
        title = _display_name(canonical)
        window = (
            f" (last {int(lookback_days)} days)"
            if lookback_days and int(lookback_days) > 0
            else ""
        )
        if not shops:
            return f"No price history for {title}{window}."

        limit = max(1, int(max_shops or 12))
        shown = shops[:limit]
        hidden = shops[limit:]

        lines = [f"🏪 {title} — price at all shops{window}:"]
        for i, shop in enumerate(shown):
            lines.append(_shop_line(shop, "🥇" if i == 0 else "•"))
        if hidden:
            lines.append(f"… +{len(hidden)} more shop(s)")

        if len(shops) >= 2:
            cheapest, dearest = shops[0], shops[-1]
            gap = float(dearest["latest_price"]) - float(cheapest["latest_price"])
            if gap > 0:
                pct = gap / float(dearest["latest_price"]) * 100.0
                lines.append(
                    f"💡 Cheapest: {cheapest['shop']} "
                    f"RM{float(cheapest['latest_price']):.2f} — RM{gap:.2f} "
                    f"({pct:.0f}%) below {dearest['shop']} "
                    f"RM{float(dearest['latest_price']):.2f}."
                )
        return "\n".join(lines)
    except Exception:
        logger.exception("build_shop_price_report: unexpected failure")
        return "Failed to build the shop price comparison."
