"""Cross-shop price comparison for ANY item (not just ayam/chicken).

The director alert used to say only "this item got more expensive at this
shop". That answers "did the price move?" but not the question the
director actually asks next: *"who else sells it, and for how much?"*

This module answers that — and the honest version of it. A naive
"group item_prices by merchant" produces garbage, because the raw table
mixes four different things together:

1. **Internal transfers.** Khulafa's own outlets appear as "merchants"
   when stock moves between them. RM1.70 "ayam" from RESTORAN KHULAFA is
   an internal transfer line, not a shop price. Rows are kept only when
   the receipt is a ``SUPPLIER_PURCHASE`` and the merchant's canonical
   category is a real supplier.
2. **OCR name variants.** MYMOON'S / MYMOOK'S / MIMOON'S KITCHEN, or
   "BESTARI FARM" vs "BESTARI FARM (M) SDN BHD", are one shop each.
   Merchants are collapsed through the canonical merchant layer
   (``receipts.merchant_canonical_id``, falling back to the same tiered
   matcher the resolver uses) before grouping.
3. **Different cuts and pack sizes.** Whole chicken, leg quarters and a
   30kg carton all canonicalise to "ayam", so their unit prices are not
   comparable. Rows are grouped by ITEM VARIANT (derived from the raw
   receipt line) and only ever compared within a variant.
4. **Corrupt dates.** The table carries receipts stamped months into the
   future. Anything dated after today is dropped.

What is left is a short, comparable answer: for each cut, what each
supplier last charged, cheapest first. Used by the price-spike alert
(``price_spike_detection``) and by ``/shop_prices <item>`` in ``bot.py``.

Hard rules (same as the rest of the alert pipeline):
- Nothing here ever raises. Every entry point swallows exceptions and
  returns a safe default ([] or "") — a comparison failure must never
  block a price alert or crash the receipt pipeline.
- Non-positive prices carry no signal and are dropped.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_ITEM_PRICES_TABLE = "item_prices"
_RECEIPTS_TABLE = "receipts"
_MERCHANT_CANONICAL_TABLE = "merchant_canonical"

# How far back a comparison looks. Supplier prices drift, so a two-year-old
# quote is noise; 90 days keeps "who is cheapest right now" honest.
DEFAULT_LOOKBACK_DAYS = 90

# Alerts go to a phone. More than a handful of shops turns the alert into a
# spreadsheet, so the tail is summarised as "+N more shops".
DEFAULT_MAX_SHOPS = 6

# Likewise for cuts: show the few that actually get bought.
DEFAULT_MAX_VARIANTS = 4

# PostgREST chokes on very long IN lists; look receipts up in batches.
_ID_CHUNK = 200

# Only real purchases are shop prices. Internal transfers, staff advances
# and petty cash all land in item_prices too.
SUPPLIER_RECEIPT_TYPE = "SUPPLIER_PURCHASE"
_NON_SHOP_CATEGORIES = frozenset({
    "internal_transfer", "staff_advance", "petty_cash", "utility",
    "rent_license",
})

# Khulafa's own outlets, including the OCR spellings ("KHULAPA"). Belt and
# braces for rows whose merchant was never canonicalised.
_OWN_OUTLET_MARKERS = ("khulafa", "khulapa", "khulafah")

_UNKNOWN_SHOP = "Unknown shop"

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Size / packaging / quantity tokens: they describe the pack, not the cut,
# so they are stripped before two receipt lines are called the same item.
_UNITS = (
    "kg|kgm|kgs|g|gm|gms|gram|grams|mg|ml|l|ltr|litre|liter|pkt|pck|pack|"
    "packs|pkg|pcs|pc|pec|btl|botol|bottle|tin|can|ctn|carton|kotak|box|"
    "bag|beg|btg|batang|ekor|biji|dozen|dzn|dz|set|unit|units|slab|tray|"
    "roll|bks|bks"
)
_QTY_TOKEN_RE = re.compile(
    r"^(?:"
    r"\d+(?:[.,]\d+)?"                       # 30, 2.5
    r"|x\d+(?:[.,]\d+)?"                     # x2
    r"|\d+(?:[.,]\d+)?(?:" + _UNITS + r")"   # 30kg
    r"|(?:" + _UNITS + r")\d+(?:[.,]\d+)?"   # kg30
    r"|(?:" + _UNITS + r")"                  # kg
    r"|rm"
    r")$",
    re.IGNORECASE,
)

# Legal / trading suffixes. Two receipts naming the same shop with and
# without them are the same shop.
_SHOP_NOISE_WORDS = frozenset({
    "sdn", "bhd", "berhad", "sdnbhd", "enterprise", "enterprises", "trading",
    "resources", "holdings", "group", "marketing", "supply", "supplies",
    "services", "service", "company", "co", "m", "and",
})

# Only the legal form is dropped for DISPLAY — "BALAJI ENTERPRISE" reads
# like the shop the director knows, while "BALAJI" (the grouping key)
# does not.
_LEGAL_FORM_WORDS = frozenset({
    "sdn", "bhd", "berhad", "sdnbhd", "co", "company", "m",
})


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


def _normalise(value: Any) -> str:
    """Uppercase, punctuation to space, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", value.upper())).strip()


def _format_date(value: Any) -> str:
    """``2026-08-02`` -> ``02 Aug``. Falls back to the raw value."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return value.strip()


def _iso(value: Any) -> str:
    """The date part of a stored value, or "" when unusable."""
    if isinstance(value, str) and len(value.strip()) >= 10:
        return value.strip()[:10]
    return ""


def shop_key(name: Any) -> str:
    """Grouping key for a shop name: legal suffixes and punctuation gone.

    ``BESTARI FARM (M) SDN BHD`` and ``BESTARI FARM`` share a key; two
    genuinely different shops do not.
    """
    words = [w for w in _normalise(name).split() if w.lower() not in _SHOP_NOISE_WORDS]
    return " ".join(words) or _normalise(name)


def shop_display(name: Any) -> str:
    """Readable shop name: legal form dropped, trade name kept."""
    words = [w for w in _normalise(name).split() if w.lower() not in _LEGAL_FORM_WORDS]
    return " ".join(words) or _normalise(name)


def item_variant(raw_item_name: Any, canonical_item: Any = None) -> str:
    """The cut / grade of an item, with pack size stripped.

    ``AYAM BERSIH 30KG`` and ``AYAM BERSIH 1 KG`` are both ``AYAM BERSIH``;
    ``PAHA AYAM`` stays distinct from both. Returns the canonical item's
    display name when the line carries nothing else (a bare ``AYAM 30KG``).
    """
    tokens = [t for t in _normalise(raw_item_name).split() if not _QTY_TOKEN_RE.match(t)]
    variant = " ".join(tokens).strip()
    return variant or _normalise(canonical_item).replace("_", " ") or "Item"


def _is_own_outlet(name: Any) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in _OWN_OUTLET_MARKERS)


def _cutoff_iso(lookback_days: int | None, today: date | None) -> str | None:
    if not lookback_days or lookback_days <= 0:
        return None
    base = today or date.today()
    try:
        return (base - timedelta(days=int(lookback_days))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_price_rows(
    supabase_client,
    canonical_item: str,
    cutoff: str | None,
    exclude_receipt_id,
) -> list[dict]:
    """Read the item's price rows. Never raises; returns [] on failure.

    Tries the paginated read first (hosted PostgREST clamps every response
    to 1000 rows, so a busy item would silently truncate), and falls back
    to a single plain query for clients without ``.order``/``.range``.
    """

    def _build():
        query = (
            supabase_client.table(_ITEM_PRICES_TABLE)
            .select(
                "merchant, receipt_date, unit_price, qty, receipt_id, "
                "raw_item_name, outlet_code"
            )
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
            "shop prices: paginated read unavailable, falling back (canonical=%s)",
            canonical_item,
            exc_info=True,
        )

    try:
        result = _build().execute()
    except Exception:
        logger.exception("shop prices: query failed (canonical=%s)", canonical_item)
        return []
    return getattr(result, "data", None) or []


def _receipt_meta(supabase_client, receipt_ids: list) -> dict:
    """``{receipt_id: {'receipt_type', 'merchant_canonical_id'}}``.

    Returns ``{}`` when the lookup isn't possible — callers then treat
    every receipt as "type unknown" and fall back to name-based filtering
    rather than dropping everything.
    """
    meta: dict = {}
    ids = [i for i in receipt_ids if i is not None]
    if not ids:
        return meta
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        try:
            result = (
                supabase_client.table(_RECEIPTS_TABLE)
                .select("id, receipt_type, merchant_canonical_id")
                .in_("id", chunk)
                .execute()
            )
        except Exception:
            logger.warning(
                "shop prices: receipt lookup failed for %d id(s); "
                "falling back to name-based filtering",
                len(chunk),
                exc_info=True,
            )
            continue
        for row in getattr(result, "data", None) or []:
            if isinstance(row, dict) and row.get("id") is not None:
                meta[row["id"]] = row
    return meta


def _merchant_canonicals(supabase_client) -> dict:
    """``{canonical_id: {'display_name', 'category'}}``; ``{}`` on failure."""
    try:
        result = (
            supabase_client.table(_MERCHANT_CANONICAL_TABLE)
            .select("id, display_name, category")
            .execute()
        )
    except Exception:
        logger.warning("shop prices: merchant_canonical read failed", exc_info=True)
        return {}
    out: dict = {}
    for row in getattr(result, "data", None) or []:
        if isinstance(row, dict) and row.get("id") is not None:
            out[row["id"]] = row
    return out


class _NameResolver:
    """Lazy, read-only merchant matcher for rows the backfill never tagged.

    Reuses ``merchant_resolver``'s tiered matcher so "MYMOOK'S KITCHEN"
    collapses onto the same canonical as "MYMOON'S KITCHEN". Read-only:
    unlike ``resolve_merchant`` it never writes fuzzy aliases — a price
    lookup shouldn't mutate the alias table.
    """

    def __init__(self, client):
        self._client = client
        self._snapshot = None
        self._cache: dict = {}

    def _load(self):
        if self._snapshot is None:
            try:
                from merchant_resolver import load_snapshot

                self._snapshot = load_snapshot(self._client)
            except Exception:
                logger.warning("shop prices: merchant snapshot failed", exc_info=True)
                self._snapshot = ([], [])
        return self._snapshot

    def canonical_id(self, raw_name):
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None
        key = raw_name.strip()
        if key in self._cache:
            return self._cache[key]
        aliases, canonicals = self._load()
        canonical_id = None
        if aliases or canonicals:
            try:
                from merchant_resolver import match_merchant

                canonical_id, _confidence = match_merchant(key, aliases, canonicals)
            except Exception:
                logger.warning(
                    "shop prices: merchant match failed for %r", key, exc_info=True
                )
                canonical_id = None
        self._cache[key] = canonical_id
        return canonical_id


def load_price_rows(
    supabase_client,
    canonical_item,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    exclude_receipt_id=None,
    today: date | None = None,
) -> list[dict]:
    """Cleaned, comparable price rows for one canonical item.

    Applies every filter the raw table needs: supplier receipts only, no
    own-outlet transfers, no future-dated rows, merchant names collapsed
    onto their canonical shop, and each line tagged with its item variant.

    Each row: ``{'shop', 'shop_key', 'variant', 'unit_price',
    'receipt_date', 'receipt_id'}``. Never raises.
    """
    try:
        if not isinstance(canonical_item, str) or not canonical_item.strip():
            return []
        canon = canonical_item.strip()
        base_today = today or date.today()
        cutoff = _cutoff_iso(lookback_days, base_today)
        today_iso = base_today.isoformat()

        raw_rows = _fetch_price_rows(
            supabase_client, canon, cutoff, exclude_receipt_id
        )
        if not raw_rows:
            return []

        dated: list[dict] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            price = _to_float(row.get("unit_price"))
            if price is None or price <= 0:
                continue
            when = _iso(row.get("receipt_date"))
            # Corrupt future dates ("last 26 Dec" on a receipt logged in
            # August) are stale OCR, not a price. Re-check the cutoff here
            # too: the fallback query path may not have filtered it.
            if not when or when > today_iso:
                continue
            if cutoff and when < cutoff:
                continue
            dated.append((row, price, when))

        if not dated:
            return []

        meta = _receipt_meta(supabase_client, [r.get("receipt_id") for r, _, _ in dated])
        canonicals = _merchant_canonicals(supabase_client)
        resolver = _NameResolver(supabase_client)

        out: list[dict] = []
        for row, price, when in dated:
            receipt = meta.get(row.get("receipt_id")) or {}

            # Real purchases only. An unknown type (receipt not found) is
            # kept — better a slightly noisy comparison than an empty one.
            receipt_type = receipt.get("receipt_type")
            if receipt_type and receipt_type != SUPPLIER_RECEIPT_TYPE:
                continue

            raw_merchant = row.get("merchant")
            canonical_id = receipt.get("merchant_canonical_id")
            if canonical_id is None and canonicals:
                canonical_id = resolver.canonical_id(raw_merchant)

            canonical_row = canonicals.get(canonical_id) if canonical_id else None
            if canonical_row:
                category = (canonical_row.get("category") or "").strip().lower()
                if category in _NON_SHOP_CATEGORIES:
                    continue
                shop = (canonical_row.get("display_name") or "").strip()
            else:
                shop = ""

            if not shop:
                # No canonical row: fall back to the suffix-stripped name, so
                # the same shop reads identically everywhere in the report
                # ("BESTARI FARM", never "BESTARI FARM (M) SDN BHD" in one
                # block and "BESTARI FARM" in the next).
                raw = raw_merchant.strip() if isinstance(raw_merchant, str) else ""
                shop = shop_display(raw) or raw
            if _is_own_outlet(shop):
                continue
            if not shop:
                shop = _UNKNOWN_SHOP

            out.append({
                "shop": shop,
                "shop_key": shop_key(shop) or shop,
                "variant": item_variant(row.get("raw_item_name"), canon),
                "unit_price": price,
                "receipt_date": when,
                "receipt_id": row.get("receipt_id"),
            })
        return out
    except Exception:
        logger.exception("load_price_rows: unexpected failure")
        return []


def summarise_shops(rows: list[dict]) -> list[dict]:
    """One entry per shop, cheapest latest price first.

    Takes rows from ``load_price_rows`` (already cleaned and variant
    tagged) — public so callers holding rows don't re-query. Never raises.
    """
    try:
        return _summarise_by_shop([r for r in rows if isinstance(r, dict)])
    except Exception:
        logger.exception("summarise_shops: unexpected failure")
        return []


def _summarise_by_shop(rows: list[dict]) -> list[dict]:
    """One entry per shop, cheapest latest price first."""
    by_shop: dict[str, dict] = {}
    for row in rows:
        bucket = by_shop.setdefault(row["shop_key"], {
            "shop": row["shop"],
            "prices": [],
            "latest_price": row["unit_price"],
            "latest_date": row["receipt_date"],
            "_latest_key": None,
            "_names": {},
        })
        bucket["prices"].append(row["unit_price"])
        bucket["_names"][row["shop"]] = bucket["_names"].get(row["shop"], 0) + 1

        # "Latest" = newest receipt_date, with receipt_id as the tiebreaker
        # so two receipts on the same day resolve to the one logged last.
        receipt_id = row.get("receipt_id")
        sort_key = (
            row["receipt_date"],
            receipt_id if isinstance(receipt_id, int) else -1,
        )
        if bucket["_latest_key"] is None or sort_key > bucket["_latest_key"]:
            bucket["_latest_key"] = sort_key
            bucket["latest_price"] = row["unit_price"]
            bucket["latest_date"] = row["receipt_date"]

    shops: list[dict] = []
    for bucket in by_shop.values():
        prices = bucket["prices"]
        if not prices:
            continue
        # Label with the spelling that appears most often; shortest wins a
        # tie, so the tidy "BESTARI FARM" beats a one-off OCR mangling.
        label = sorted(bucket["_names"].items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
        shops.append({
            "shop": label,
            "latest_price": bucket["latest_price"],
            "latest_date": bucket["latest_date"],
            "avg_price": sum(prices) / len(prices),
            "min_price": min(prices),
            "max_price": max(prices),
            "sample_count": len(prices),
        })
    shops.sort(key=lambda s: (s["latest_price"], s["shop"]))
    return shops


def get_shop_prices(
    supabase_client,
    canonical_item,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    exclude_receipt_id=None,
    today: date | None = None,
    variant: str | None = None,
) -> list[dict]:
    """Return one price summary per shop for ``canonical_item``.

    ``variant`` restricts the comparison to one cut (e.g. ``PAHA AYAM``)
    so pack sizes and cuts are never averaged together — pass the raw
    receipt line and it is normalised for you.

    Each entry: ``{'shop', 'latest_price', 'latest_date', 'avg_price',
    'min_price', 'max_price', 'sample_count'}``, cheapest latest price
    first. Never raises — returns ``[]`` on bad input or any failure.
    """
    try:
        rows = load_price_rows(
            supabase_client,
            canonical_item,
            lookback_days=lookback_days,
            exclude_receipt_id=exclude_receipt_id,
            today=today,
        )
        if variant:
            wanted = item_variant(variant, canonical_item)
            rows = [r for r in rows if r["variant"] == wanted]
        return _summarise_by_shop(rows)
    except Exception:
        logger.exception("get_shop_prices: unexpected failure")
        return []


def get_variant_prices(
    supabase_client,
    canonical_item,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    today: date | None = None,
) -> list[dict]:
    """Per-cut breakdown: ``[{'variant', 'shops', 'sample_count',
    'latest_date'}]``, most recently bought cut first. Never raises.
    """
    try:
        rows = load_price_rows(
            supabase_client, canonical_item, lookback_days=lookback_days, today=today
        )
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["variant"], []).append(row)

        variants = []
        for variant, variant_rows in grouped.items():
            variants.append({
                "variant": variant,
                "shops": _summarise_by_shop(variant_rows),
                "sample_count": len(variant_rows),
                "latest_date": max(r["receipt_date"] for r in variant_rows),
            })
        variants.sort(key=lambda v: (v["latest_date"], v["sample_count"]), reverse=True)
        return variants
    except Exception:
        logger.exception("get_variant_prices: unexpected failure")
        return []


# --- rendering --------------------------------------------------------------

def _shop_line(shop: dict, marker: str) -> str:
    latest = float(shop["latest_price"])
    when = _format_date(shop.get("latest_date"))
    when_part = f" · {when}" if when else ""
    n = int(shop.get("sample_count") or 0)
    n_part = f" ({n}x)" if n > 1 else ""
    return f"{marker} {shop['shop']} — RM{latest:.2f}{when_part}{n_part}"


def _window_label(lookback_days: int | None) -> str:
    try:
        return (
            f" (last {int(lookback_days)} days)"
            if lookback_days and int(lookback_days) > 0
            else ""
        )
    except (TypeError, ValueError):
        return ""


def format_shop_comparison(
    canonical_item,
    shops: list[dict],
    current_shop=None,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    max_shops: int = DEFAULT_MAX_SHOPS,
    variant: str | None = None,
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
        current_key = shop_key(current) if current else ""
        limit = max(1, int(max_shops or DEFAULT_MAX_SHOPS))

        shown = clean[:limit]
        hidden = clean[limit:]
        current_row = next(
            (s for s in clean if current_key and shop_key(s["shop"]) == current_key),
            None,
        )
        if current_row is not None and current_row not in shown:
            shown = shown + [current_row]
            hidden = [s for s in hidden if s is not current_row]

        title = str(variant).strip().title() if variant else _display_name(canonical_item)
        lines = [f"🏪 {title} — price at all shops{_window_label(lookback_days)}:"]
        for i, shop in enumerate(shown):
            is_current = shop is current_row
            marker = "👉" if is_current else ("🥇" if i == 0 else "•")
            suffix = "  ← this receipt" if is_current else ""
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
        elif current_row is not None:
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


def pick_variant(query: Any, variants: list[dict]) -> str | None:
    """Which cut, if any, the director asked for.

    ``/shop_prices paha ayam`` should show leg quarters only, not every
    ayam line. Matches the typed text against the known variants (exact
    first, then containment) and returns ``None`` when it is ambiguous or
    the query was just the item name.
    """
    try:
        wanted = _normalise(query)
        if not wanted or not variants:
            return None
        names = [v.get("variant") for v in variants if v.get("variant")]
        for name in names:
            if name == wanted:
                return name
        hits = [n for n in names if wanted in n or n in wanted]
        return hits[0] if len(hits) == 1 else None
    except Exception:
        logger.exception("pick_variant: unexpected failure")
        return None


def build_shop_price_report(
    supabase_client,
    query,
    lookback_days: int | None = DEFAULT_LOOKBACK_DAYS,
    max_shops: int = 5,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    today: date | None = None,
) -> str:
    """Full text reply for ``/shop_prices <item>``.

    One short block per cut (whole, leg, …) with each supplier's last
    price, cheapest first — never one lump of unrelated lines. Blocking
    (Supabase I/O), so call it from a worker thread. Always returns a
    user-facing string; never raises.
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

        variants = get_variant_prices(
            supabase_client, canonical, lookback_days=lookback_days, today=today
        )
        title = _display_name(canonical)
        window = _window_label(lookback_days)
        if not variants:
            return f"No supplier prices for {title}{window}."

        asked = pick_variant(raw, variants)
        if asked:
            variants = [v for v in variants if v["variant"] == asked]

        shop_limit = max(1, int(max_shops or 5))
        variant_limit = max(1, int(max_variants or DEFAULT_MAX_VARIANTS))
        shown = variants[:variant_limit]
        hidden = variants[variant_limit:]

        lines = [f"🏪 {title} — supplier prices{window}"]
        for entry in shown:
            shops = entry["shops"][:shop_limit]
            extra = len(entry["shops"]) - len(shops)
            lines.append("")
            lines.append(str(entry["variant"]).title())
            for i, shop in enumerate(shops):
                lines.append(_shop_line(shop, "🥇" if i == 0 else "•"))
            if extra > 0:
                lines.append(f"… +{extra} more shop(s)")

        if hidden:
            lines.append("")
            lines.append(
                f"… +{len(hidden)} more type(s): "
                + ", ".join(str(v["variant"]).title() for v in hidden[:5])
            )
        return "\n".join(lines)
    except Exception:
        logger.exception("build_shop_price_report: unexpected failure")
        return "Failed to build the shop price comparison."
