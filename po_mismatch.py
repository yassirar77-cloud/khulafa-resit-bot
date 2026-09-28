"""Receipt-vs-order mismatch: ask the cashier about the lines that differ.

When a supplier bill is read (GLM OCR) in a live outlet group, its lines
are compared with the purchase order for that day — what the cashier
confirmed at the 20:05 check-in (``staff_order_items``), else the saved
order draft — and with the outlet's usual paid price per item:

  missing   an ordered item is not on the bill
  qty       the quantity differs from the order (beyond ``QTY_TOL``)
  price     the unit price is more than ``PO_PRICE_PCT`` (default 10%)
            off the usual price
  extra     a line on the bill that was not ordered

The cashier gets ONE question in their language listing only those lines
(fixed wording — the numbers are the point). The reply reader gives it the
status ``mismatch_explained`` with an English ``explanation_en``, which is
saved on the receipt row (migrations/0053). A mismatch still unexplained
after the two nudges goes in the 23:30 digest.

Pure: comparison and wording only. bot.py fetches the order and sends.
"""
from __future__ import annotations

import os

import cashier_names
import staff_chat

SLOT = "po_mismatch"
STATUS_EXPLAINED = "mismatch_explained"
DEFAULT_PRICE_PCT = 10
QTY_TOL = 0.05          # a 5% rounding difference is not a mismatch
MAX_LINES = 6           # more than this and the bill is simply wrong — list the first six


def price_pct() -> float:
    raw = (os.environ.get("PO_PRICE_PCT") or "").strip().rstrip("%")
    try:
        pct = float(raw) if raw else DEFAULT_PRICE_PCT
    except ValueError:
        pct = DEFAULT_PRICE_PCT
    return max(0.5, pct) / 100.0


def receipt_lines(items) -> list[dict]:
    """The bill's lines by canonical item: ``[{item, qty, price, name}]``
    (quantities of repeated items add up; price is the unit price)."""
    import item_canonicalization_v2 as icv2
    by_item: dict[str, dict] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("description") or "").strip()
        if not name:
            continue
        canon = icv2.canonicalize_item(name).get("canonical")
        if not canon:
            continue
        try:
            qty = float(it.get("qty") if it.get("qty") is not None else it.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            price = float(it.get("price")) if it.get("price") not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        line = by_item.setdefault(canon, {"item": canon, "qty": 0.0, "price": None, "name": name})
        line["qty"] += max(qty, 0.0)
        if price is not None and price > 0:
            line["price"] = price
    return list(by_item.values())


def compare(bill: list[dict], order: list[dict], usual_prices: dict | None = None, *,
            pct: float | None = None) -> list[dict]:
    """The mismatched lines. ``bill``: ``receipt_lines``; ``order``:
    ``[{item, qty, unit}]``; ``usual_prices``: ``{item: unit_price}``."""
    pct = price_pct() if pct is None else pct
    usual_prices = usual_prices or {}
    got = {b["item"]: b for b in bill or []}
    out: list[dict] = []
    for o in order or []:
        item = str(o.get("item") or "").lower()
        try:
            ordered = float(o.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if not item or ordered <= 0:
            continue
        unit = str(o.get("unit") or "")
        b = got.pop(item, None)
        if b is None:
            out.append({"kind": "missing", "item": item, "label": staff_chat.item_label(item),
                        "ordered": staff_chat._qty_pack(staff_chat.fmt_qty(ordered, unit or None), unit)})
            continue
        if b["qty"] > 0 and abs(b["qty"] - ordered) / ordered > QTY_TOL:
            out.append({"kind": "qty", "item": item, "label": staff_chat.item_label(item),
                        "ordered": staff_chat._qty_pack(staff_chat.fmt_qty(ordered, unit or None), unit),
                        "received": staff_chat._qty_pack(staff_chat.fmt_qty(b["qty"], unit or None), unit)})
        usual = usual_prices.get(item)
        if b.get("price") and usual and abs(b["price"] / float(usual) - 1) > pct:
            out.append({"kind": "price", "item": item, "label": staff_chat.item_label(item),
                        "usual": f"{float(usual):.2f}", "paid": f"{b['price']:.2f}",
                        "pct": int(round((b["price"] / float(usual) - 1) * 100))})
    for item, b in got.items():
        if b["qty"] > 0:
            out.append({"kind": "extra", "item": item, "label": staff_chat.item_label(item),
                        "received": staff_chat.fmt_qty(b["qty"])})
    return out[:MAX_LINES]


_HEAD = {
    "bm": "Bil {supplier} tak sama dengan order semalam:",
    "tamil": "{supplier} bill நேற்று போட்ட order-ஓட ஒத்துப்போகல:",
    "english": "The {supplier} bill doesn't match yesterday's order:",
    "indonesian": "Nota {supplier} tidak sama dengan order kemarin:",
    "bengali": "{supplier} bill gotokaler order-er shathe mile na:",
}
_LINE = {
    "missing": {"bm": "• {label}: order {ordered}, tak ada dalam bil",
                "tamil": "• {label}: order {ordered}, bill-ல இல்ல",
                "english": "• {label}: ordered {ordered}, not on the bill",
                "indonesian": "• {label}: order {ordered}, tidak ada di nota",
                "bengali": "• {label}: order {ordered}, bill-e nai"},
    "qty": {"bm": "• {label}: order {ordered}, bil {received}",
            "tamil": "• {label}: order {ordered}, bill-ல {received}",
            "english": "• {label}: ordered {ordered}, bill says {received}",
            "indonesian": "• {label}: order {ordered}, nota {received}",
            "bengali": "• {label}: order {ordered}, bill-e {received}"},
    "price": {"bm": "• {label}: harga RM{paid}, biasa RM{usual}",
              "tamil": "• {label}: விலை RM{paid}, வழக்கமா RM{usual}",
              "english": "• {label}: price RM{paid}, usually RM{usual}",
              "indonesian": "• {label}: harga RM{paid}, biasanya RM{usual}",
              "bengali": "• {label}: dam RM{paid}, shadharon RM{usual}"},
    "extra": {"bm": "• {label} {received}: tak order",
              "tamil": "• {label} {received}: order பண்ணல",
              "english": "• {label} {received}: not ordered",
              "indonesian": "• {label} {received}: tidak diorder",
              "bengali": "• {label} {received}: order kora hoy nai"},
}
_TAIL = {
    "bm": "Kenapa ya? Taip sikit.",
    "tamil": "ஏன்னு சொல்லுங்க? கொஞ்சம் type பண்ணுங்க.",
    "english": "Why is that? Please type a few words.",
    "indonesian": "Kenapa ya? Tolong ketik sedikit.",
    "bengali": "Keno, bolben? Ektu likhe din.",
}


def question(mismatches: list[dict], supplier: str, language: str) -> str:
    """The cashier's question, listing only the mismatched lines."""
    def one(lang):
        lines = [_HEAD[lang].format(supplier=supplier)]
        lines += [_LINE[m["kind"]][lang].format(**m) for m in mismatches]
        lines.append(_TAIL[lang])
        return "\n".join(lines)
    if language == staff_chat.BM_TAMIL:
        return f"{one('bm')}\n\n{one('tamil')}"
    return one(language if language in _HEAD else "bm")


def facts(receipt_id, supplier: str, mismatches: list[dict]) -> dict:
    return {"receipt_id": receipt_id, "supplier": supplier, "mismatches": mismatches,
            "lines": str(len(mismatches))}


def explanation(parsed: dict | None, text) -> str:
    """What to save on the receipt: the parser's English explanation, else
    its summary, else the raw reply."""
    p = parsed or {}
    return (str(p.get("explanation_en") or "").strip() or str(p.get("summary_en") or "").strip()
            or str(text or "").strip())[:500]


def digest_lines(threads: list[dict], label=str) -> list[dict]:
    """Mismatches still unexplained (no reply after the nudges) for the
    23:30 digest: ``[{outlet, supplier, lines}]``."""
    out = []
    for t in threads or []:
        if t.get("slot") != SLOT or t.get("status") != "no_reply":
            continue
        f = t.get("facts") or {}
        out.append({"outlet": label(t.get("outlet_code")), "supplier": f.get("supplier") or "supplier",
                    "lines": str(f.get("lines") or len(f.get("mismatches") or []))})
    return out


def language_for(chat_id) -> str:
    return cashier_names.language_for_chat(chat_id)
