"""Learn tomorrow's order from what cashiers tell us.

When a cashier answers the 20:05 order check-in with items and quantities
("esok ayam 40kg, ikan 10kg"), the reply reader (staff_live.parse_reply)
pulls out ``[{item, qty, unit}]``. Those are saved to ``staff_order_items``
(migrations/0047) and merged into the order history the drafts are built
from, as buying days for tomorrow. Outlets with thin receipt history
(Sungai Besi, Damansara, and the ones asked "what do you need?") then get
real drafts sooner.

Rules:
  * only items the canonicaliser recognises feed the drafts (unknown words
    are kept for the record, never guessed);
  * a receipt for the same item within a day of ``order_for`` wins — the
    staff row is then skipped, so nothing is counted twice;
  * staff rows carry no price or supplier, so price alerts and supplier
    picks still come from receipts only.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import item_canonicalization_v2 as icv2

logger = logging.getLogger(__name__)

TABLE = "staff_order_items"
MAX_QTY = 5000          # anything bigger is a typo, not an order
MAX_ITEMS = 30
_UNITS = {
    "kg": "kg", "kilo": "kg", "kilogram": "kg", "g": "g", "gram": "g",
    "pcs": "pcs", "pc": "pcs", "pieces": "pcs", "piece": "pcs", "biji": "biji",
    "ekor": "ekor", "kotak": "kotak", "box": "kotak", "karton": "kotak",
    "ctn": "kotak", "tin": "tin", "botol": "botol", "bottle": "botol",
    "pack": "pack", "paket": "pack", "bungkus": "pack", "l": "liter",
    "liter": "liter", "litre": "liter", "tong": "tong", "guni": "guni",
    "bag": "guni", "sack": "guni",
}


def clean_items(raw) -> list[dict]:
    """Validate the reply reader's items: ``[{item, qty, unit}]`` with a
    positive number under MAX_QTY. Anything else is dropped."""
    out: list[dict] = []
    for it in (raw or [])[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("item") or "").strip()
        try:
            qty = float(str(it.get("qty")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if not name or not (0 < qty <= MAX_QTY):
            continue
        unit = str(it.get("unit") or "").strip().lower()
        out.append({"item": name[:80], "qty": round(qty, 3),
                    "unit": _UNITS.get(unit, unit[:12] or None)})
    return out


_SYNONYMS_PATH = Path(__file__).resolve().parent / "data" / "staff_item_synonyms.json"
# Letters of the scripts staff write in: a synonym must not sit inside a
# longer word ("dim" in "dimsum", "tel" in "hotel").
_LETTER = r"A-Za-z஀-௿ঀ-৿"


def _load_synonyms() -> list[tuple[re.Pattern, str, bool]]:
    """``[(pattern, item, is_phrase)]``, longest word first."""
    raw = json.loads(_SYNONYMS_PATH.read_text(encoding="utf-8"))
    pairs = [(word.strip().lower(), item)
             for item, words in raw.items() if not item.startswith("_")
             for word in words if word.strip()]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)     # longest match wins
    return [(re.compile(rf"(?<![{_LETTER}]){re.escape(w)}(?![{_LETTER}])"), item, " " in w)
            for w, item in pairs]


SYNONYMS = _load_synonyms()

# Processed goods named after a raw item ("chicken nugget", "Knorr chicken
# stock", "Maggi mee ayam") are not that item — Klang's "Ayam 2kg" draft
# came from exactly this. Such names are kept unmatched, never guessed.
_PROCESSED = re.compile(
    rf"(?<![{_LETTER}])(?:nugget|nuggets|sosej|sausage|sausages|burger|frankfurter|"
    rf"ball|balls|bebola|stock|kiub|cube|cubes|knorr|maggi|perisa|flavou?r|"
    rf"cooker|periuk|mesin|machine)"
    rf"(?![{_LETTER}])"
)


def canonical(name: str) -> str | None:
    """The order-history item for a word a cashier wrote, in any of their
    languages ("கோழி", "murgi", "chicken" -> ayam). Order: multi-word staff
    phrases ("coconut milk", "தேங்காய் பால்"), then the receipt
    canonicaliser (it knows "chilli sauce", "ikan bilis"), then single staff
    words. Unknown -> None, never guessed."""
    text = str(name or "").strip().lower()
    if not text or _PROCESSED.search(text):
        return None
    for pattern, item, phrase in SYNONYMS:
        if phrase and pattern.search(text):
            return item
    res = icv2.canonicalize_item(name)
    if res.get("matched"):
        return res["canonical"]
    for pattern, item, phrase in SYNONYMS:
        if not phrase and pattern.search(text):
            return item
    return None


def rows_for_reply(thread: dict, items: list[dict], reply_text: str) -> list[dict]:
    """``staff_order_items`` rows for one answered order check-in. The goods
    are for the day after the shift that was asked."""
    try:
        shift_day = date.fromisoformat(str(thread.get("shift_date"))[:10])
    except ValueError:
        return []
    order_for = (shift_day + timedelta(days=1)).isoformat()
    return [
        {
            "outlet_code": thread.get("outlet_code"),
            "order_for": order_for,
            "raw_item": it["item"],
            "canonical_item": canonical(it["item"]),
            "qty": it["qty"],
            "unit": it.get("unit"),
            "cashier": thread.get("cashier"),
            "thread_id": thread.get("id"),
            "reply_text": (reply_text or "")[:1000],
        }
        for it in clean_items(items)
    ]


def save(supabase, rows: list[dict]) -> int:
    if not rows:
        return 0
    try:
        supabase.table(TABLE).insert(rows).execute()
        return len(rows)
    except Exception:
        logger.exception("staff orders: save failed (%d rows)", len(rows))
        return 0


def fetch_history_rows(supabase, *, today: date, lookback: int, codes=None) -> list[dict]:
    """Saved staff orders in the window, shaped like ``item_prices`` rows
    (receipt_date = order_for; no price, no supplier). ``[]`` on failure."""
    try:
        q = (supabase.table(TABLE)
             .select("outlet_code, canonical_item, qty, raw_item, order_for, created_at")
             .gte("order_for", (today - timedelta(days=lookback)).isoformat())
             .lte("order_for", (today + timedelta(days=1)).isoformat())
             .not_.is_("canonical_item", "null"))
        if codes:
            q = q.in_("outlet_code", list(codes))
        data = q.limit(5000).execute().data or []
    except Exception:
        logger.exception("staff orders: history read failed")
        return []
    return [
        {"outlet_code": r.get("outlet_code"), "canonical_item": r.get("canonical_item"),
         "qty": r.get("qty"), "unit_price": None, "merchant": None,
         "raw_item_name": r.get("raw_item"), "receipt_date": r.get("order_for"),
         "created_at": r.get("created_at"), "source": "staff"}
        for r in data
    ]


def merge(price_rows: list[dict], staff_rows: list[dict]) -> list[dict]:
    """Receipt rows plus the staff rows no receipt already covers (same
    outlet + item within a day)."""
    seen: set[tuple[str, str, date]] = set()
    for r in price_rows or []:
        d = _day(r.get("receipt_date"))
        if d is not None:
            seen.add((str(r.get("outlet_code") or ""),
                      str(r.get("canonical_item") or "").lower(), d))
    out = list(price_rows or [])
    for r in staff_rows or []:
        d = _day(r.get("receipt_date"))
        key = (str(r.get("outlet_code") or ""), str(r.get("canonical_item") or "").lower())
        if d is None or not key[1]:
            continue
        if any((key[0], key[1], d + timedelta(days=k)) in seen for k in (-1, 0, 1)):
            continue
        out.append(r)
    return out


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
