"""Plain-language item search — the director asks, the bot answers.

Every number the director wants about an item is already in the database,
but until now it was only reachable by remembering the right slash command
and its exact spelling (``/shop_prices``, ``/outlet_prices``,
``/monthly_kg``). On a phone, mid-conversation, that is a wall. The
question that actually gets typed is::

    beras beli kat mana
    where we buy beras
    bila last beli ayam
    berapa kita belanja untuk telur bulan ni
    which branch pays most for minyak

This module turns that free text into an answer. It does two things and
nothing else:

1. **Understand the question** (``parse_question``) — pure, no I/O. Pick
   the intent out of the wording (English, Malay, or the mix everybody
   actually types), pull the time window out of it ("bulan ni", "last 30
   days"), strip the question away and resolve what is left to a canonical
   item through the same resolver ``/shop_prices`` uses.
2. **Answer it** (``answer_question``) — route to the report that already
   exists where there is one (``bill_analysis`` for "which branch"), and
   build the three that did not: the whole picture for one item
   (``build_item_report``), its last purchases, and what it cost us over a
   window.

Why ``build_item_report`` exists rather than reusing
``shop_price_comparison.build_shop_price_report``: that report groups by
``item_variant``, which is the raw receipt line with the pack size
stripped. Every OCR spelling and brand prefix therefore becomes its own
"cut" — ayam alone produced 41 of them ("I AYAM", "1ST AYAM", "MR CM
AYAM", "LAI AYAM"). Capped at a few blocks, that splits one item into
one-shop blocks, hides most of the real suppliers behind "+38 more
type(s)", and then reports "only one supplier has priced this item" to a
director who buys it from five. The answer to "where do we buy this" is
one list of shops; the per-cut comparison is a second, narrower question,
and is kept for where it is honest (a like-for-like cut two shops both
sold).

Intents, in the order they are matched (first hit wins):

===========  =====================================  =====================
Intent       Typical wording                        Answered by
===========  =====================================  =====================
``items``    "what items can I ask"                 the canonical vocabulary
``branch``   "which branch", "outlet mana"          ``bill_analysis``
``spend``    "berapa banyak", "how much we spend"   ``build_spend_summary``
``last``     "bila last beli", "when did we buy"    ``build_last_purchases``
``shop``     "beli kat mana", "harga", "cheapest"   ``shop_price_comparison``
===========  =====================================  =====================

``shop`` is also the fallback: a bare item name ("beras") is read as
"where do we buy this, and for how much" — the question that is asked most.

Hard rules, same as the rest of the reporting layer:

- **Nothing here ever raises.** Every entry point swallows exceptions and
  returns a safe default, because a bad question must never crash the bot.
- **Never guess an item.** If the text does not resolve to a canonical
  item, say so and offer the near misses — a confident answer about the
  wrong item is worse than no answer.
- **Only spend what the receipt shows.** ``item_prices`` carries
  ``unit_price`` and ``qty`` but no line total, so a line with no quantity
  is counted in the purchase count and excluded from the money, and the
  report says how many lines that was.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

INTENT_ITEMS = "items"
INTENT_BRANCH = "branch"
INTENT_SPEND = "spend"
INTENT_LAST = "last"
INTENT_SHOP = "shop"
INTENT_NONE = "none"

# A spend question with no window named means "recently", and a month is
# the unit the director thinks in.
DEFAULT_SPEND_DAYS = 30

# "When did we last buy this" has to reach back further than a price
# comparison: a supplier we last used in March is still the answer.
DEFAULT_LAST_LOOKBACK_DAYS = 365
DEFAULT_LAST_LIMIT = 5

# Alerts and answers go to a phone. Past a handful of rows the answer
# stops being readable.
MAX_SHOP_ROWS = 6
MAX_OUTLET_ROWS = 6

# "Where do we buy this" looks at a quarter by default — long enough to
# catch a supplier used monthly, short enough that the prices still mean
# something.
DEFAULT_ITEM_LOOKBACK_DAYS = 90

# The shop list is the answer to the question, so it is generous. The
# purchase log underneath is a sample, not a ledger.
MAX_SHOPS_LISTED = 12
MAX_PURCHASE_LINES = 10
MAX_COMPARE_GROUPS = 3

_UNKNOWN = "Unknown shop"

_MONTHS_EN = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


# --- text normalisation -----------------------------------------------------

def _norm(text: Any) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed."""
    lowered = str(text or "").lower()
    return " ".join(_PUNCT_RE.sub(" ", lowered).split())


def _has_phrase(text: str, phrase: str) -> bool:
    """Whole-word containment on already-normalised text."""
    return f" {phrase} " in f" {text} "


def _first_phrase(text: str, phrases: tuple) -> str | None:
    for phrase in phrases:
        if _has_phrase(text, phrase):
            return phrase
    return None


# --- intent vocabulary ------------------------------------------------------
#
# Multi-word phrases come before the bare words they contain, so the most
# specific wording wins and shows up in the debug trace.

_ITEMS_PHRASES = (
    "what items", "what item", "which items", "list items", "list item",
    "senarai item", "senarai barang", "item list", "apa item",
    "what can i ask", "what can you tell", "apa boleh tanya", "apa saya boleh tanya",
)

_BRANCH_PHRASES = (
    "which branch", "which branches", "which outlet", "which outlets",
    "branch mana", "outlet mana", "cawangan mana", "kedai kita mana",
    "per branch", "per outlet", "each branch", "each outlet",
    "every branch", "every outlet", "semua outlet", "semua cawangan",
    "between branches", "between outlets", "compare branch", "compare outlet",
    "branch", "branches", "outlet", "outlets", "cawangan",
)

_SPEND_PHRASES = (
    "how much do we spend", "how much we spend", "how much did we spend",
    "how much do we buy", "how much we buy", "how much did we buy",
    "how many kg", "how much kg", "how many times",
    "berapa banyak", "berapa kg", "berapa kilo", "berapa duit",
    "berapa ringgit", "berapa kali", "habis berapa", "guna berapa",
    "pakai berapa", "total spend", "total belian",
    "spend", "spent", "spending", "belanja", "perbelanjaan", "belian",
    "jumlah", "usage", "consumption",
)

_LAST_PHRASES = (
    "last beli", "last buy", "last bought", "last purchase", "last order",
    "kali terakhir", "baru beli", "when did we", "when do we", "when we",
    "last time", "latest", "terakhir", "recent", "recently", "bila", "when",
    "last",
)

_SHOP_PHRASES = (
    "beli kat mana", "beli dari mana", "beli di mana", "mana beli",
    "where do we buy", "where we buy", "where to buy", "buy from where",
    "from where", "from which shop", "which shop", "which supplier",
    "who sells", "siapa jual", "paling murah", "kedai mana", "shop mana",
    "supplier mana", "berapa harga", "price at", "compare price",
    "cheapest", "termurah", "murah", "harga", "price", "prices", "cost",
    "supplier", "suppliers", "vendor", "kedai", "shop", "shops",
    "where", "mana", "compare",
)

# Everything the question itself is made of. Stripped before what is left
# is read as the item name.
_FILLER_WORDS = frozenset("""
a an the is are was were am be do does did done can could would will shall
should may might must have has had we us our ours you your i me my mine
they them their it its
please tolong boleh nak minta sila
tell show give list check find search cari tanya ask ada tau bagitau
bot khulafa sir boss
for of from at in on to into by with and or dan atau dgn dengan untuk
this that these those ini itu ni tu yang yg tersebut
what apa apakah how macam mana which where when why who siapa bila kenapa
buy beli membeli belian bought purchase purchased purchasing order orders
sell sold jual jualan stock stok
much many banyak berapa lebih paling
now sekarang currently semasa tak ke lah la ah kan pun je saja sahaja
still lagi ada punya kita kami saya aku dia
pay pays paid bayar membayar mahal expensive dear top best worst
most least terbanyak paling average avg per each
""".split())

# Window words — parsed for the date range first, then dropped so they are
# never mistaken for part of the item name.
_WINDOW_WORDS = frozenset("""
day days hari week weeks minggu month months bulan year years tahun
today yesterday semalam ago lepas lalu sudah last past previous recent
this current ini ni
""".split())

_INTENT_TOKENS = frozenset(
    token
    for group in (_ITEMS_PHRASES, _BRANCH_PHRASES, _SPEND_PHRASES,
                  _LAST_PHRASES, _SHOP_PHRASES)
    for phrase in group
    for token in phrase.split()
)

_STRIP_TOKENS = _FILLER_WORDS | _WINDOW_WORDS | _INTENT_TOKENS


# --- time window ------------------------------------------------------------

def _month_start(day: date) -> date:
    return day.replace(day=1)


def _prev_month_bounds(day: date) -> tuple[date, date]:
    first_this = _month_start(day)
    last_prev = first_this - timedelta(days=1)
    return _month_start(last_prev), last_prev


def _fmt_day(value: date) -> str:
    return f"{value.day} {_MONTHS_EN[value.month - 1]}"


def _range_label(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{_fmt_day(start)} – {_fmt_day(end)} {end.year}"
    return f"{_fmt_day(start)} {start.year} – {_fmt_day(end)} {end.year}"


def parse_window(text: Any, today: date | None = None,
                 default_days: int = DEFAULT_SPEND_DAYS) -> dict:
    """Pull a date range out of the question.

    Understands "bulan ni" / "this month", "bulan lepas" / "last month",
    "minggu ni" / "this week", "N hari" / "N days" / "N weeks" / "N months",
    "tahun ni" / "this year", and "hari ni" / "today". Falls back to the
    last ``default_days`` days. Returns ``{'start', 'end', 'label',
    'explicit'}``; ``explicit`` is False when nothing was named. Never
    raises.
    """
    base = today or date.today()
    out = {
        "start": base - timedelta(days=max(1, int(default_days)) - 1),
        "end": base,
        "label": f"last {int(default_days)} days",
        "explicit": False,
    }
    try:
        normalised = _norm(text)
        if not normalised:
            return out

        if _first_phrase(normalised, ("hari ini", "hari ni", "today")):
            return {"start": base, "end": base,
                    "label": f"today ({_fmt_day(base)})", "explicit": True}

        if _first_phrase(normalised, ("semalam", "yesterday")):
            day = base - timedelta(days=1)
            return {"start": day, "end": day,
                    "label": f"yesterday ({_fmt_day(day)})", "explicit": True}

        if _first_phrase(normalised, ("bulan lepas", "bulan lalu", "last month",
                                      "previous month")):
            start, end = _prev_month_bounds(base)
            return {"start": start, "end": end,
                    "label": f"{_MONTHS_EN[start.month - 1]} {start.year}",
                    "explicit": True}

        if _first_phrase(normalised, ("bulan ini", "bulan ni", "this month",
                                      "month to date")):
            start = _month_start(base)
            return {"start": start, "end": base,
                    "label": f"{_MONTHS_EN[base.month - 1]} {base.year} "
                             f"(to {_fmt_day(base)})",
                    "explicit": True}

        if _first_phrase(normalised, ("minggu ini", "minggu ni", "this week",
                                      "last week", "minggu lepas")):
            return {"start": base - timedelta(days=6), "end": base,
                    "label": "last 7 days", "explicit": True}

        if _first_phrase(normalised, ("tahun ini", "tahun ni", "this year")):
            return {"start": date(base.year, 1, 1), "end": base,
                    "label": f"{base.year} (to {_fmt_day(base)})",
                    "explicit": True}

        # "30 hari", "last 6 months", "2 weeks", "3 bulan"
        match = re.search(
            r"(\d{1,4})\s*(hari|days?|minggu|weeks?|bulan|months?|tahun|years?)",
            normalised,
        )
        if match:
            count = max(1, min(int(match.group(1)), 3650))
            unit = match.group(2)
            if unit.startswith(("hari", "day")):
                days = count
            elif unit.startswith(("minggu", "week")):
                days = count * 7
            elif unit.startswith(("bulan", "month")):
                days = count * 30
            else:
                days = count * 365
            days = max(1, min(days, 3650))
            return {"start": base - timedelta(days=days - 1), "end": base,
                    "label": f"last {days} days", "explicit": True}
        return out
    except Exception:
        logger.exception("parse_window: unexpected failure")
        return out


# --- question parsing -------------------------------------------------------

def detect_intent(text: Any) -> tuple[str, str | None]:
    """``(intent, matched_phrase)`` for already-arbitrary free text.

    The order is deliberate. ``branch`` is checked before ``spend`` so
    "which branch spends most" is a branch comparison; ``spend`` before
    ``last`` so "how much did we spend last month" is money, not a
    purchase list. Anything that names no intent at all falls through to
    ``INTENT_NONE`` — the caller decides whether a bare item name means
    "where do we buy this" (it does) or nothing.
    """
    try:
        normalised = _norm(text)
        if not normalised:
            return INTENT_NONE, None
        if normalised in ("items", "item", "barang"):
            return INTENT_ITEMS, normalised
        for intent, phrases in (
            (INTENT_ITEMS, _ITEMS_PHRASES),
            (INTENT_BRANCH, _BRANCH_PHRASES),
            (INTENT_SPEND, _SPEND_PHRASES),
            (INTENT_LAST, _LAST_PHRASES),
            (INTENT_SHOP, _SHOP_PHRASES),
        ):
            hit = _first_phrase(normalised, phrases)
            if hit:
                return intent, hit
        return INTENT_NONE, None
    except Exception:
        logger.exception("detect_intent: unexpected failure")
        return INTENT_NONE, None


def extract_item_text(text: Any) -> str:
    """What is left once the question is taken away.

    "bila last beli paha ayam?" -> "paha ayam". Numbers and window words go
    too, so "beras 30 hari" is still "beras".
    """
    try:
        tokens = _norm(text).split()
        kept = [
            t for t in tokens
            if t not in _STRIP_TOKENS and not t.isdigit()
        ]
        return " ".join(kept)
    except Exception:
        logger.exception("extract_item_text: unexpected failure")
        return ""


def _resolve_item(item_text: str) -> dict:
    """Canonical item for the leftover text, with a per-token retry.

    "paha ayam sejuk" may not resolve whole while "ayam" does, so each
    token is tried in turn before giving up. Suggestions always come from
    the full phrase — they are what the director actually typed.
    """
    from shop_price_comparison import resolve_item_query

    resolved = resolve_item_query(item_text) or {}
    canonical = resolved.get("canonical")
    suggestions = list(resolved.get("suggestions") or [])
    if canonical:
        return {"canonical": canonical, "suggestions": suggestions}

    for token in item_text.split():
        if len(token) < 3:
            continue
        again = resolve_item_query(token) or {}
        if again.get("canonical"):
            return {"canonical": again["canonical"], "suggestions": suggestions}
        if not suggestions:
            suggestions = list(again.get("suggestions") or [])
    if not suggestions:
        suggestions = _near_names(item_text)
    return {"canonical": None, "suggestions": suggestions}


def _near_names(item_text: str) -> list[str]:
    """Spelling-error rescue for the "did you mean" line only.

    A typo ("ayamm", "tellur") shares no substring with the canonical key,
    so the resolver's containment fallback finds nothing and the director
    gets a dead end. Close matches are OFFERED, never auto-selected — a
    confident answer about the wrong item is the one failure mode this
    feature cannot have.
    """
    try:
        import difflib

        from item_canonicalization_v2 import list_canonical_items

        known = list_canonical_items() or []
        hits: list[str] = []
        for token in item_text.split():
            if len(token) < 3:
                continue
            for match in difflib.get_close_matches(token, known, n=3, cutoff=0.7):
                if match not in hits:
                    hits.append(match)
        return hits[:5]
    except Exception:
        logger.exception("_near_names: unexpected failure (%r)", item_text)
        return []


def parse_question(text: Any, today: date | None = None) -> dict:
    """Free text -> ``{'intent', 'item_text', 'canonical', 'suggestions',
    'window', 'matched', 'confident', 'raw'}``.

    Pure: the item vocabulary is a local JSON file, so nothing here
    touches the database. ``confident`` is the gate for answering
    un-prompted chatter — True only when the text resolved to a real item
    AND either named an intent or was punctuated as a question. A bare
    "ok" or "sudah hantar" never trips it. Never raises.
    """
    out = {
        "raw": str(text or "").strip(),
        "intent": INTENT_NONE,
        "matched": None,
        "item_text": "",
        "canonical": None,
        "suggestions": [],
        "window": parse_window("", today=today),
        "confident": False,
    }
    try:
        raw = out["raw"]
        if not raw:
            return out
        is_question = "?" in raw
        intent, matched = detect_intent(raw)
        item_text = extract_item_text(raw)
        resolved = _resolve_item(item_text) if item_text else {
            "canonical": None, "suggestions": []
        }

        default_days = (
            DEFAULT_LAST_LOOKBACK_DAYS if intent == INTENT_LAST
            else DEFAULT_SPEND_DAYS
        )
        out.update({
            "intent": intent,
            "matched": matched,
            "item_text": item_text,
            "canonical": resolved.get("canonical"),
            "suggestions": resolved.get("suggestions") or [],
            "window": parse_window(raw, today=today, default_days=default_days),
        })
        out["confident"] = bool(
            out["canonical"] and (intent != INTENT_NONE or is_question)
        )
        return out
    except Exception:
        logger.exception("parse_question: unexpected failure (%r)", text)
        return out


# --- shared row helpers -----------------------------------------------------

def _display_name(canonical: Any) -> str:
    text = str(canonical or "").strip()
    return text.replace("_", " ").title() if text else "Item"


def _money(value: float) -> str:
    return f"RM{float(value):,.2f}"


def _qty(value: float) -> str:
    rounded = round(float(value), 2)
    return f"{rounded:,.0f}" if abs(rounded - round(rounded)) < 0.005 else f"{rounded:,.2f}"


def _short_date(iso: Any) -> str:
    try:
        parsed = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return str(iso or "")
    return _fmt_day(parsed)


def _row_sort_key(row: dict) -> tuple:
    receipt_id = row.get("receipt_id")
    return (row.get("receipt_date") or "", receipt_id if isinstance(receipt_id, int) else -1)


def _load_rows(supabase_client, canonical: str, lookback_days: int,
               today: date | None) -> list[dict]:
    """Cleaned price rows for one item. ``[]`` on any failure."""
    try:
        from shop_price_comparison import load_price_rows

        rows = load_price_rows(
            supabase_client, canonical,
            lookback_days=max(1, int(lookback_days)), today=today,
        )
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        logger.exception("director_ask: row load failed (%s)", canonical)
        return []


def _window_rows(rows: list[dict], window: dict) -> list[dict]:
    """Keep the rows inside the asked-for range (the loader only bounds
    the start, so an explicit "last month" still needs its end clipped)."""
    start = window.get("start")
    end = window.get("end")
    start_iso = start.isoformat() if isinstance(start, date) else None
    end_iso = end.isoformat() if isinstance(end, date) else None
    out = []
    for row in rows:
        when = str(row.get("receipt_date") or "")
        if not when:
            continue
        if start_iso and when < start_iso:
            continue
        if end_iso and when > end_iso:
            continue
        out.append(row)
    return out


def _lookback_for(window: dict, today: date) -> int:
    start = window.get("start")
    if not isinstance(start, date):
        return DEFAULT_SPEND_DAYS
    return max(1, (today - start).days + 1)


# --- answers that did not exist yet -----------------------------------------

def build_last_purchases(
    supabase_client,
    canonical: str,
    limit: int = DEFAULT_LAST_LIMIT,
    lookback_days: int = DEFAULT_LAST_LOOKBACK_DAYS,
    today: date | None = None,
    window: dict | None = None,
) -> str:
    """"When did we last buy this, from whom, and for how much."

    The most recent purchases newest first — date, shop, unit price,
    quantity, the outlet that bought it and the cut, with the shops that
    have priced it recently listed underneath so the next call is obvious.

    ``window`` clips both ends: "bila last beli beras bulan lepas" must not
    answer with a September receipt. Without one the search simply reaches
    back ``lookback_days``. Blocking (Supabase I/O). Always returns a
    string; never raises.
    """
    try:
        title = _display_name(canonical)
        rows = _load_rows(supabase_client, canonical, lookback_days, today)
        period = f"the last {int(lookback_days)} days"
        if window:
            rows = _window_rows(rows, window)
            period = str(window.get("label") or period)
        if not rows:
            return f"No purchase of {title} in {period}."

        rows.sort(key=_row_sort_key, reverse=True)
        shown = rows[:max(1, int(limit))]
        item_key = _norm(canonical.replace("_", " "))

        scope = f" ({period})" if window else ""
        lines = [f"🧾 {title} — last {len(shown)} purchase(s){scope}"]
        for row in shown:
            price = float(row.get("unit_price") or 0.0)
            parts = [f"• {_short_date(row.get('receipt_date'))} · {row.get('shop')}",
                     f"— {_money(price)}"]
            qty = row.get("qty")
            if isinstance(qty, (int, float)) and float(qty) > 0:
                parts.append(f"× {_qty(qty)}")
            outlet = row.get("outlet_code")
            if outlet:
                parts.append(f"· {outlet}")
            line = " ".join(parts)
            variant = str(row.get("variant") or "")
            if variant and _norm(variant) != item_key:
                line += f"\n   {variant.title()}"
            lines.append(line)

        newest = rows[0]
        days_ago = None
        try:
            days_ago = ((today or date.today())
                        - date.fromisoformat(str(newest["receipt_date"])[:10])).days
        except (KeyError, TypeError, ValueError):
            days_ago = None
        if days_ago is not None and days_ago >= 0:
            when = "today" if days_ago == 0 else (
                "yesterday" if days_ago == 1 else f"{days_ago} days ago"
            )
            lines.append("")
            lines.append(f"📅 Last bought {when}, from {newest.get('shop')}.")

        shops = sorted({str(r.get("shop")) for r in rows if r.get("shop")})
        if len(shops) > 1:
            listed = ", ".join(shops[:MAX_SHOP_ROWS])
            extra = len(shops) - min(len(shops), MAX_SHOP_ROWS)
            more = f" +{extra} more" if extra > 0 else ""
            lines.append(f"🏪 Shops that sell it: {listed}{more}")
        lines.append(f"→ Ask \"{canonical.replace('_', ' ')} cheapest\" to compare prices.")
        return "\n".join(lines)
    except Exception:
        logger.exception("build_last_purchases failed (%s)", canonical)
        return "Failed to read the purchase history."


def build_spend_summary(
    supabase_client,
    canonical: str,
    window: dict | None = None,
    today: date | None = None,
) -> str:
    """"How much of this did we buy, and what did it cost."

    Total spend and quantity over the window, split by shop and by outlet.
    ``item_prices`` stores no line total, so money is ``unit_price × qty``
    and a line with no quantity is counted as a purchase but left out of
    the money — the report says how many, rather than quietly under- or
    over-reporting. Blocking. Always returns a string; never raises.
    """
    try:
        base_today = today or date.today()
        window = window or parse_window("", today=base_today)
        title = _display_name(canonical)
        label = window.get("label") or "recent"

        rows = _window_rows(
            _load_rows(
                supabase_client, canonical,
                _lookback_for(window, base_today), base_today,
            ),
            window,
        )
        if not rows:
            return f"No {title} bought — {label}."

        total_spend = 0.0
        total_qty = 0.0
        priced_lines = 0
        by_shop: dict[str, dict] = {}
        by_outlet: dict[str, float] = {}
        for row in rows:
            price = float(row.get("unit_price") or 0.0)
            qty = row.get("qty")
            qty = float(qty) if isinstance(qty, (int, float)) and float(qty) > 0 else None
            spend = price * qty if qty else None
            shop = str(row.get("shop") or "Unknown shop")
            bucket = by_shop.setdefault(shop, {"spend": 0.0, "qty": 0.0, "lines": 0})
            bucket["lines"] += 1
            if spend is not None:
                priced_lines += 1
                total_spend += spend
                total_qty += qty
                bucket["spend"] += spend
                bucket["qty"] += qty
                outlet = row.get("outlet_code") or "—"
                by_outlet[outlet] = by_outlet.get(outlet, 0.0) + spend

        lines = [f"💰 {title} — {label}"]
        if priced_lines:
            lines.append(
                f"Spend: {_money(total_spend)} · Qty: {_qty(total_qty)} "
                f"· {len(rows)} purchase line(s)"
            )
            avg = total_spend / total_qty if total_qty else 0.0
            if avg > 0:
                lines.append(f"Average paid: {_money(avg)} per unit")
        else:
            lines.append(
                f"{len(rows)} purchase line(s), but none carried a quantity — "
                "can't total the spend."
            )
        skipped = len(rows) - priced_lines
        if priced_lines and skipped:
            lines.append(f"({skipped} line(s) had no quantity — not in the total.)")

        if by_shop:
            ranked = sorted(by_shop.items(), key=lambda kv: -kv[1]["spend"])
            lines.append("")
            lines.append("By shop:")
            for shop, stats in ranked[:MAX_SHOP_ROWS]:
                if stats["spend"] > 0:
                    unit = stats["spend"] / stats["qty"] if stats["qty"] else 0.0
                    lines.append(
                        f"• {shop} — {_money(stats['spend'])} "
                        f"· {_qty(stats['qty'])} · avg {_money(unit)}"
                    )
                else:
                    lines.append(f"• {shop} — {stats['lines']} line(s), no quantity")
            if len(ranked) > MAX_SHOP_ROWS:
                lines.append(f"… +{len(ranked) - MAX_SHOP_ROWS} more shop(s)")

        if len(by_outlet) > 1:
            ranked_outlets = sorted(by_outlet.items(), key=lambda kv: -kv[1])
            lines.append("")
            lines.append("By outlet:")
            for outlet, spend in ranked_outlets[:MAX_OUTLET_ROWS]:
                lines.append(f"• {outlet} — {_money(spend)}")
            if len(ranked_outlets) > MAX_OUTLET_ROWS:
                lines.append(f"… +{len(ranked_outlets) - MAX_OUTLET_ROWS} more outlet(s)")
        return "\n".join(lines)
    except Exception:
        logger.exception("build_spend_summary failed (%s)", canonical)
        return "Failed to total the purchases."


def _shop_summaries(rows: list[dict]) -> list[dict]:
    """One entry per shop, most recently bought first.

    Deliberately NOT grouped by variant. ``item_variant`` is the raw
    receipt line with the pack size stripped, so every OCR spelling and
    brand prefix becomes its own "cut" — ayam alone produced 41 of them
    ("I AYAM", "1ST AYAM", "MR CM AYAM"). Grouping the answer by that
    splits one item into dozens of one-shop blocks, and a report capped at
    a few blocks then hides most of the suppliers behind them. Whoever
    sold us the item belongs in the same list.
    """
    by_shop: dict[str, dict] = {}
    for row in rows:
        key = row.get("shop_key") or row.get("shop") or _UNKNOWN
        bucket = by_shop.setdefault(key, {
            "shop": row.get("shop") or _UNKNOWN,
            "times": 0,
            "prices": [],
            "outlets": set(),
            "last_key": None,
            "last_date": None,
            "last_price": None,
            "last_variant": "",
        })
        price = float(row.get("unit_price") or 0.0)
        bucket["times"] += 1
        bucket["prices"].append(price)
        if row.get("outlet_code"):
            bucket["outlets"].add(str(row["outlet_code"]))
        sort_key = _row_sort_key(row)
        if bucket["last_key"] is None or sort_key > bucket["last_key"]:
            bucket["last_key"] = sort_key
            bucket["last_date"] = row.get("receipt_date")
            bucket["last_price"] = price
            bucket["last_variant"] = str(row.get("variant") or "")
    shops = list(by_shop.values())
    shops.sort(key=lambda s: (s["last_key"] or ("", -1)), reverse=True)
    return shops


def _comparable_groups(rows: list[dict]) -> list[dict]:
    """The cuts where a price comparison is actually honest.

    A RM110 sack of beras idly and a RM33.90 bag of basmati are not two
    quotes for the same thing, so "cheapest" is only ever claimed inside
    one variant, and only when two or more shops sold it.
    """
    from shop_price_comparison import summarise_shops

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("variant") or ""), []).append(row)

    out = []
    for variant, variant_rows in grouped.items():
        shops = summarise_shops(variant_rows)
        if len(shops) < 2:
            continue
        out.append({
            "variant": variant,
            "shops": shops,
            "latest_date": max(r.get("receipt_date") or "" for r in variant_rows),
        })
    out.sort(key=lambda g: (len(g["shops"]), g["latest_date"]), reverse=True)
    return out


# The drop reasons, in the order a director cares about them, with the
# wording they read on the receipt rather than the column name.
_DROP_REASONS = (
    ("own_outlet", "internal transfer / own outlet"),
    ("non_shop_merchant", "merchant not categorised as a shop"),
    ("non_supplier_receipt", "receipt not a supplier purchase"),
    ("bad_date", "bad date (missing or in the future)"),
    ("bad_price", "bad price (zero or negative)"),
    ("no_item", "line not matched to an item"),
)


def _dropped_note(stats: dict) -> str:
    """One line naming what never reached the answer, so a missing
    supplier is visible instead of silent."""
    parts = [
        f"{int(stats.get(key) or 0)} {label}"
        for key, label in _DROP_REASONS
        if int(stats.get(key) or 0)
    ]
    if not parts:
        return ""
    return "⚠️ Left out: " + ", ".join(parts) + "."


def _dropped_detail(stats: dict) -> list[str]:
    """Which SHOP each dropped row belonged to.

    "3 merchant not categorised as a shop" cannot tell the director which
    of his suppliers went missing. The names can: a mini market filed
    under the wrong canonical category shows up here by name, which is
    the difference between "the data is wrong somewhere" and a one-line
    fix with /merchant_show.
    """
    names = stats.get("dropped_names")
    if not isinstance(names, dict) or not names:
        return []
    lines = ["", "🔍 Which shop each dropped row belonged to"]
    for key, label in _DROP_REASONS:
        bucket = names.get(key)
        if not bucket:
            continue
        ranked = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))
        listed = ", ".join(
            f"{shop} ({n}x)" if n > 1 else str(shop) for shop, n in ranked
        )
        lines.append(f"• {label}: {listed}")
    return lines if len(lines) > 2 else []


def build_item_report(
    supabase_client,
    query,
    lookback_days: int | None = DEFAULT_ITEM_LOOKBACK_DAYS,
    today: date | None = None,
    debug: bool = False,
) -> str:
    """"Which shops do we buy this from, when, and for how much."

    The full picture for one item in one message: every supplier that sold
    it, the most recent purchases with date, shop, line, price, quantity
    and the outlet that bought it, which outlets buy it at all, and — only
    where it is honest — the cheapest shop for a like-for-like cut.

    Blocking (Supabase I/O). Always returns a string; never raises.
    """
    try:
        from shop_price_comparison import load_price_rows_with_stats

        raw = str(query or "").strip()
        parts = raw.split()
        if len(parts) > 1 and parts[-1].lower() in ("debug", "why"):
            debug = True
            raw = " ".join(parts[:-1])
        if not raw:
            return HELP_TEXT

        resolved = _resolve_item(raw)
        canonical = resolved.get("canonical")
        if not canonical:
            return _miss_text({
                "raw": raw, "item_text": raw,
                "suggestions": resolved.get("suggestions") or [],
            })

        base_today = today or date.today()
        window_days = int(lookback_days or DEFAULT_ITEM_LOOKBACK_DAYS)
        rows, stats = load_price_rows_with_stats(
            supabase_client, canonical, lookback_days=window_days, today=base_today
        )
        rows = [r for r in rows if isinstance(r, dict)]

        # One shop is not an answer to "who sells it". Before saying so,
        # look back a year — a supplier last used in March is still a
        # supplier worth calling.
        widened = False
        if window_days < 365 and len({r.get("shop_key") for r in rows}) < 2:
            wide_rows, wide_stats = load_price_rows_with_stats(
                supabase_client, canonical, lookback_days=365, today=base_today
            )
            wide_rows = [r for r in wide_rows if isinstance(r, dict)]
            if len({r.get("shop_key") for r in wide_rows}) > len(
                {r.get("shop_key") for r in rows}
            ):
                rows, stats, window_days, widened = wide_rows, wide_stats, 365, True

        title = _display_name(canonical)
        if not rows:
            out = f"No supplier bought {title} in the last {window_days} days."
            note = _dropped_note(stats)
            if note:
                out += "\n" + note
            return out

        shops = _shop_summaries(rows)
        outlets: dict[str, int] = {}
        for row in rows:
            if row.get("outlet_code"):
                code = str(row["outlet_code"])
                outlets[code] = outlets.get(code, 0) + 1

        lines = [
            f"🔎 {title} — last {window_days} days",
            f"{len(rows)} purchase(s) · {len(shops)} shop(s)"
            + (f" · {len(outlets)} outlet(s)" if outlets else ""),
            "",
            "🏪 Shops we buy it from (most recent first)",
        ]
        for shop in shops[:MAX_SHOPS_LISTED]:
            prices = shop["prices"]
            line = (
                f"• {shop['shop']} — {_short_date(shop['last_date'])} · "
                f"{_money(shop['last_price'])} · {shop['times']}x"
            )
            if len(prices) > 1 and max(prices) - min(prices) > 0.005:
                line += f" · range {_money(min(prices))}–{_money(max(prices))}"
            lines.append(line)
            if shop["last_variant"]:
                lines.append(f"   last: {shop['last_variant'].title()}")
        if len(shops) > MAX_SHOPS_LISTED:
            lines.append(f"… +{len(shops) - MAX_SHOPS_LISTED} more shop(s)")

        recent = sorted(rows, key=_row_sort_key, reverse=True)[:MAX_PURCHASE_LINES]
        lines.append("")
        lines.append("🧾 Recent purchases")
        for row in recent:
            bits = [
                f"• {_short_date(row.get('receipt_date'))}",
                f"· {row.get('shop')}",
                f"— {_money(float(row.get('unit_price') or 0.0))}",
            ]
            qty = row.get("qty")
            if isinstance(qty, (int, float)) and float(qty) > 0:
                bits.append(f"× {_qty(qty)}")
            if row.get("outlet_code"):
                bits.append(f"· {row['outlet_code']}")
            lines.append(" ".join(bits))
            variant = str(row.get("variant") or "")
            if variant:
                lines.append(f"   {variant.title()}")
        if len(rows) > len(recent):
            lines.append(f"… +{len(rows) - len(recent)} more purchase(s)")

        if outlets:
            ranked = sorted(outlets.items(), key=lambda kv: -kv[1])
            listed = ", ".join(f"{code} ({n}x)" for code, n in ranked[:MAX_OUTLET_ROWS])
            lines.append("")
            lines.append(f"🏬 Outlets buying it: {listed}")

        groups = _comparable_groups(rows)
        lines.append("")
        if groups:
            lines.append("💡 Same type, more than one shop — cheapest first")
            for group in groups[:MAX_COMPARE_GROUPS]:
                lines.append("")
                lines.append(str(group["variant"]).title())
                for i, shop in enumerate(group["shops"][:MAX_SHOP_ROWS]):
                    marker = "🥇" if i == 0 else "•"
                    times = int(shop.get("sample_count") or 0)
                    suffix = f" ({times}x)" if times > 1 else ""
                    lines.append(
                        f"{marker} {shop['shop']} — "
                        f"{_money(float(shop['latest_price']))} · "
                        f"{_short_date(shop.get('latest_date'))}{suffix}"
                    )
        else:
            lines.append(
                "💡 Every type came from one shop only, so there is nothing "
                "like-for-like to compare — pack sizes and grades differ. The "
                "shop list above is everyone who sold it."
            )

        if widened:
            lines.append("")
            lines.append(
                f"(Fewer than two shops in the last "
                f"{int(lookback_days or DEFAULT_ITEM_LOOKBACK_DAYS)} days — "
                "widened to a year.)"
            )
        note = _dropped_note(stats)
        if note:
            lines.append("")
            lines.append(note)
        if debug:
            lines.append("")
            lines.append(
                f"🔍 read {stats.get('fetched', 0)} row(s) from item_prices, "
                f"kept {stats.get('kept', 0)}"
            )
            lines.extend(_dropped_detail(stats))
        return "\n".join(lines)
    except Exception:
        logger.exception("build_item_report failed (%r)", query)
        return "Failed to build the item report."


def build_item_index() -> str:
    """Every item the director can ask about, so "what can I ask" has a
    real answer instead of a guess. Never raises."""
    try:
        from item_canonicalization_v2 import list_canonical_items

        items = sorted(list_canonical_items() or [])
        if not items:
            return "No item vocabulary is loaded."
        names = ", ".join(_display_name(i) for i in items)
        return (
            f"🔎 {len(items)} items I can answer on:\n{names}\n\n"
            "Ask in plain words, e.g. \"beras beli kat mana\", "
            "\"bila last beli ayam\", \"berapa belanja telur bulan ni\"."
        )
    except Exception:
        logger.exception("build_item_index failed")
        return "Failed to list the item vocabulary."


# --- help / misses ----------------------------------------------------------

HELP_TEXT = (
    "🔎 Ask me about any item — plain words, English or Malay:\n"
    "• \"beras beli kat mana\" — which shops sell it, cheapest first\n"
    "• \"harga ayam\" — every shop's latest price, per cut\n"
    "• \"bila last beli telur\" — the last purchases, shop and price\n"
    "• \"berapa belanja minyak bulan ni\" — spend and quantity for a period\n"
    "• \"which branch pays most for gula\" — branch-by-branch comparison\n"
    "• \"what items can I ask\" — the full item list\n"
    "\nPeriods I understand: hari ni, minggu ni, bulan ni, bulan lepas, "
    "30 hari / last 30 days, tahun ni."
)


def _query_text(parsed: dict) -> str:
    """The item string to hand a downstream report.

    The leftover text is preferred because it carries the CUT — "paha
    ayam" must stay "paha ayam", not collapse onto every ayam line. But it
    only survives when it resolves to the same item on its own; words the
    stripper missed ("beras mahal") would make the downstream resolver
    miss, so those fall back to the canonical key we already resolved.
    """
    canonical = parsed.get("canonical") or ""
    item_text = (parsed.get("item_text") or "").strip()
    if not item_text:
        return canonical.replace("_", " ")
    try:
        from shop_price_comparison import resolve_item_query

        if (resolve_item_query(item_text) or {}).get("canonical") == canonical:
            return item_text
    except Exception:
        logger.exception("_query_text: resolve failed (%r)", item_text)
    return canonical.replace("_", " ")


def _miss_text(parsed: dict) -> str:
    """What to say when the question named no item we know."""
    raw = parsed.get("raw") or ""
    item_text = parsed.get("item_text") or ""
    suggestions = parsed.get("suggestions") or []
    if suggestions:
        names = ", ".join(_display_name(s) for s in suggestions[:8])
        subject = item_text or raw
        return f'No item called "{subject}". Did you mean: {names}?'
    if not item_text:
        return HELP_TEXT
    return (
        f'I don\'t track an item called "{item_text}".\n\n' + HELP_TEXT
    )


# --- the entry point --------------------------------------------------------

def answer_question(supabase_client, text: Any, today: date | None = None) -> str:
    """Answer a free-text question about an item. Blocking; never raises.

    Always returns something a director can read: the report when the
    question resolved, the near misses when it named an item we don't
    track, and the examples when it named nothing at all.
    """
    try:
        base_today = today or date.today()
        parsed = parse_question(text, today=base_today)
        if not parsed["raw"]:
            return HELP_TEXT
        if parsed["intent"] == INTENT_ITEMS:
            return build_item_index()

        canonical = parsed.get("canonical")
        if not canonical:
            return _miss_text(parsed)

        intent = parsed["intent"]
        item_text = _query_text(parsed)
        window = parsed.get("window") or parse_window("", today=base_today)

        if intent == INTENT_BRANCH:
            from bill_analysis import build_outlet_price_report

            return build_outlet_price_report(
                supabase_client, item_text, today=base_today
            )
        if intent == INTENT_SPEND:
            return build_spend_summary(
                supabase_client, canonical, window=window, today=base_today
            )
        if intent == INTENT_LAST:
            explicit = bool(window.get("explicit"))
            return build_last_purchases(
                supabase_client, canonical,
                lookback_days=(
                    _lookback_for(window, base_today) if explicit
                    else DEFAULT_LAST_LOOKBACK_DAYS
                ),
                today=base_today,
                window=window if explicit else None,
            )

        # INTENT_SHOP and the bare-item fallback: "where do we buy this,
        # and for how much" is the question that gets asked most, and it
        # wants every shop in one list — not one block per OCR spelling.
        return build_item_report(
            supabase_client, item_text, today=base_today,
            lookback_days=(
                _lookback_for(window, base_today) if window.get("explicit")
                else DEFAULT_ITEM_LOOKBACK_DAYS
            ),
        )
    except Exception:
        logger.exception("answer_question failed (%r)", text)
        return "Failed to answer that. Try /help for the commands."
