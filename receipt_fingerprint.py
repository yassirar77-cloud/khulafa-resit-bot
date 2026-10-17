"""Receipt fingerprints: one bill can only be claimed once.

Every saved receipt photo gets a 64-bit difference hash (dHash — Pillow only,
no new dependency, a few milliseconds) plus a content key: merchant, receipt
date, total and the invoice / DO number read from the OCR text. On each new
upload the last ``FINGERPRINT_LOOKBACK_DAYS`` of fingerprints are compared
and three kinds of match come out:

* ``same_photo``   — Hamming distance <= ``FINGERPRINT_MAX_DISTANCE``: the same
  picture sent again. Telegram recompresses every photo, so the bytes are
  never identical, but the gradient structure dHash reads survives that.
* ``same_invoice`` — a different picture of a bill with the same merchant and
  the same invoice / DO number (both bills must carry one).
* ``same_bill``    — a different picture, no invoice number to compare, but the
  same merchant + receipt date + a total within ``TOTAL_TOLERANCE``. Only
  counted at or above ``FINGERPRINT_MIN_TOTAL``, because a standing daily
  order (roti, gas) legitimately produces identical small bills at two
  outlets on the same day.

What matters is a match in ANOTHER chat (another outlet): one supplier bill
submitted at two outlets is either a mix-up or a double claim, and the
director gets both receipt ids with the outlets, dates and totals. A match in
the SAME chat is a re-send: one short line under the photo (no money) and
nothing else — two genuinely different bills from one merchant on one day are
already covered by the "same shop 2 kali" audit question. The content checks
never run within one chat; only the picture check does.

Pure helpers take plain dicts so they are testable without a database; the
``receipt_fingerprints`` table is ``migrations/0071_receipt_fingerprints.sql``.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

logger = logging.getLogger(__name__)

TABLE = "receipt_fingerprints"
HASH_SIZE = 8                       # 8x8 = 64 bits
TOTAL_TOLERANCE = 0.02              # same_bill: totals within 2%


def enabled() -> bool:
    return os.environ.get("FINGERPRINT_ENABLED", "true").strip().lower() not in ("0", "false", "off", "no")


def lookback_days() -> int:
    return _env_int("FINGERPRINT_LOOKBACK_DAYS", 45, 1, 365)


def max_distance() -> int:
    return _env_int("FINGERPRINT_MAX_DISTANCE", 6, 0, 20)


def min_total() -> float:
    try:
        return max(0.0, float(os.environ.get("FINGERPRINT_MIN_TOTAL", "100")))
    except ValueError:
        return 100.0


def _env_int(name, default, lo, hi) -> int:
    try:
        return max(lo, min(int(os.environ.get(name, str(default))), hi))
    except ValueError:
        return default


# --- image hash -----------------------------------------------------------------------

def dhash(image_bytes, hash_size: int = HASH_SIZE) -> str | None:
    """Difference hash of a photo as ``hash_size*hash_size`` bits in hex
    (16 hex chars for the default 8). ``None`` when the bytes are not an
    image Pillow can open — never raises."""
    if not image_bytes:
        return None
    try:
        from PIL import Image, ImageOps
        with Image.open(BytesIO(image_bytes)) as img:
            try:
                img = ImageOps.exif_transpose(img) or img
            except Exception:
                pass
            grey = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
            px = grey.tobytes()          # mode L: one byte per pixel, row-major
    except Exception:
        logger.warning("dhash: could not read image", exc_info=True)
        return None
    bits = 0
    w = hash_size + 1
    for row in range(hash_size):
        for col in range(hash_size):
            left = px[row * w + col]
            right = px[row * w + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:0{hash_size * hash_size // 4}x}"


def hamming(a, b) -> int | None:
    """Bits that differ between two hex hashes; ``None`` when either is
    missing or malformed."""
    if not a or not b:
        return None
    try:
        x, y = int(str(a), 16), int(str(b), 16)
    except ValueError:
        return None
    if len(str(a)) != len(str(b)):
        return None
    return bin(x ^ y).count("1")


# --- content key ----------------------------------------------------------------------

# "INVOICE NO: INV-2026/0042", "D.O. # 88123", "Resit No 004512", "Bill: 7781"
_INVOICE_RE = re.compile(
    r"\b(?:TAX\s+)?(?:INV(?:OICE)?|D\.?\s?O\.?|DELIVERY\s+ORDER|CASH\s+BILL|BILL|BIL|RESIT|RECEIPT|REF(?:ERENCE)?|DOC(?:UMENT)?)"
    r"\s*(?:NO|NUM(?:BER)?|#)?\s*[.:#]?\s*"
    r"([A-Z]{0,4}[-/]?\d[\dA-Z\-/]{2,})\b",
    re.IGNORECASE,
)
_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def invoice_no(raw_text) -> str | None:
    """The first invoice / DO / receipt number in the OCR text, normalised to
    letters+digits upper-cased (``INV-2026/0042`` -> ``INV20260042``). Needs
    at least three digits, so "Bill 2" or a date fragment never counts."""
    if not isinstance(raw_text, str) or not raw_text:
        return None
    for m in _INVOICE_RE.finditer(raw_text):
        token = _ALNUM_RE.sub("", m.group(1).upper())
        digits = sum(ch.isdigit() for ch in token)
        if digits >= 3 and len(token) <= 24:
            return token
    return None


def _total(value) -> float | None:
    if value is None:
        return None
    try:
        s = str(value).upper().replace("RM", "").replace(",", "").strip()
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def totals_match(a, b, tolerance: float = TOTAL_TOLERANCE) -> bool:
    x, y = _total(a), _total(b)
    if x is None or y is None:
        return False
    return abs(x - y) / max(x, y) <= tolerance


def _merchant(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def _day(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    return s[:10] if len(s) >= 10 else None


def fingerprint_row(stored: dict, parsed: dict | None, image_hash, *, now=None) -> dict:
    """The row saved for a receipt: ``stored`` is the saved receipts row,
    ``parsed`` the OCR output (for ``raw_text``)."""
    raw = (parsed or {}).get("raw_text") or stored.get("raw_text")
    return {
        "receipt_id": stored.get("id"),
        "chat_id": stored.get("chat_id"),
        "outlet": stored.get("outlet"),
        "dhash": image_hash,
        "merchant": _merchant(stored.get("merchant")) or None,
        "receipt_date": _day(stored.get("receipt_date")),
        "total": _total(stored.get("total")),
        "invoice_no": invoice_no(raw),
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


# --- matching -------------------------------------------------------------------------

def find_matches(new: dict, candidates, *, max_dist: int | None = None,
                 min_content_total: float | None = None) -> list[dict]:
    """Earlier fingerprints that look like the same bill as ``new``.

    Each match: ``{kind, receipt_id, chat_id, outlet, receipt_date, total,
    distance, cross_chat}``. Cross-chat matches first, then by kind
    (same_photo, same_invoice, same_bill), then the smallest distance."""
    max_dist = max_distance() if max_dist is None else max_dist
    min_content_total = min_total() if min_content_total is None else min_content_total
    new_id = new.get("receipt_id")
    new_chat = new.get("chat_id")
    new_hash = new.get("dhash")
    new_merchant = _merchant(new.get("merchant"))
    new_inv = new.get("invoice_no")
    new_day = _day(new.get("receipt_date"))
    new_total = _total(new.get("total"))
    out = []
    for c in candidates or []:
        if new_id is not None and c.get("receipt_id") == new_id:
            continue
        cross = c.get("chat_id") != new_chat
        dist = hamming(new_hash, c.get("dhash"))
        kind = None
        if dist is not None and dist <= max_dist:
            kind = "same_photo"
        elif cross and new_merchant and _merchant(c.get("merchant")) == new_merchant:
            c_inv = c.get("invoice_no")
            if new_inv and c_inv:
                if new_inv == c_inv:
                    kind = "same_invoice"
            elif (new_day and _day(c.get("receipt_date")) == new_day
                  and new_total is not None and new_total >= min_content_total
                  and totals_match(new_total, c.get("total"))):
                kind = "same_bill"
        if kind is None:
            continue
        out.append({
            "kind": kind,
            "receipt_id": c.get("receipt_id"),
            "chat_id": c.get("chat_id"),
            "outlet": c.get("outlet"),
            "receipt_date": _day(c.get("receipt_date")),
            "total": _total(c.get("total")),
            "distance": dist,
            "cross_chat": cross,
            "created_at": c.get("created_at"),
        })
    rank = {"same_photo": 0, "same_invoice": 1, "same_bill": 2}
    out.sort(key=lambda m: (not m["cross_chat"], rank[m["kind"]],
                            m["distance"] if m["distance"] is not None else 99))
    return out


def split_matches(matches) -> tuple[list[dict], list[dict]]:
    """``(for_director, for_group)``: cross-chat matches go to the director;
    a same-chat re-send of the same picture gets one line in the group."""
    director = [m for m in matches if m["cross_chat"]]
    group = [m for m in matches if not m["cross_chat"] and m["kind"] == "same_photo"]
    return director, group


# --- texts ----------------------------------------------------------------------------

_KIND_LABEL = {
    "same_photo": "same photo",
    "same_invoice": "same invoice number",
    "same_bill": "same merchant, date and total",
}


def _rm(value) -> str:
    v = _total(value)
    return f"RM{v:,.2f}" if v is not None else "RM —"


def _nice_day(value) -> str:
    d = _day(value)
    if not d:
        return "undated"
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%-d %b %Y")
    except ValueError:
        return d


def format_director_alert(new: dict, matches) -> str:
    """One message for the director: the new bill and every earlier bill it
    matches, with outlets, dates, totals and how they matched."""
    head = (f"⚠️ Same bill at two outlets? {new.get('merchant') or 'UNKNOWN MERCHANT'} "
            f"{_rm(new.get('total'))}, {_nice_day(new.get('receipt_date'))}")
    lines = [head,
             f"• #{new.get('receipt_id')} {new.get('outlet') or '—'} — this upload"]
    for m in matches:
        how = _KIND_LABEL[m["kind"]]
        if m["kind"] == "same_photo" and m["distance"] is not None:
            how += f", distance {m['distance']}"
        lines.append(f"• #{m['receipt_id']} {m.get('outlet') or '—'}, "
                     f"{_nice_day(m.get('receipt_date'))}, {_rm(m.get('total'))} — {how}")
    if any(m["kind"] == "same_bill" for m in matches):
        lines.append("same_bill = no invoice number to compare; check the two photos before acting.")
    lines.append("One supplier bill should be logged at one outlet only.")
    return "\n".join(lines)


def format_group_reply(match: dict) -> str:
    """Under a re-sent photo in the outlet group. No money figures."""
    return (f"Resit ini sudah direkod (#{match['receipt_id']}, {_nice_day(match.get('receipt_date'))}). "
            "Tak perlu hantar lagi.\n"
            f"This bill was already logged as #{match['receipt_id']}.")


# --- I/O ------------------------------------------------------------------------------

def recent(client, since) -> list[dict]:
    """Fingerprints created since ``since`` (ISO timestamp / datetime)."""
    from db_pagination import fetch_all_pages
    since_iso = since.isoformat() if hasattr(since, "isoformat") else str(since)
    return fetch_all_pages(
        lambda: client.table(TABLE)
        .select("receipt_id, chat_id, outlet, dhash, merchant, receipt_date, total, invoice_no, created_at")
        .gte("created_at", since_iso).order("id", desc=False))


def record(client, row: dict) -> dict | None:
    payload = {k: v for k, v in row.items() if v is not None}
    res = client.table(TABLE).insert(payload).execute()
    data = res.data or []
    return data[0] if data else None


def check_and_record(client, stored: dict, parsed: dict | None, image_hash,
                     *, now: datetime | None = None) -> dict:
    """Compare a just-saved receipt against the recent fingerprints, then save
    its own. Returns ``{"row", "director", "group"}``; the caller decides who
    to tell. Nothing is saved for a receipt without an id (failed insert)."""
    moment = now or datetime.now(timezone.utc)
    row = fingerprint_row(stored, parsed, image_hash, now=moment)
    if row.get("receipt_id") is None:
        return {"row": row, "director": [], "group": []}
    candidates = recent(client, moment - timedelta(days=lookback_days()))
    matches = find_matches(row, candidates)
    director, group = split_matches(matches)
    if director:
        # The cross-chat match is kept on the row so /resit_sama can list it.
        row["matched_receipt_id"] = director[0]["receipt_id"]
        row["match_kind"] = director[0]["kind"]
        row["match_outlet"] = director[0].get("outlet")
    record(client, row)
    return {"row": row, "director": director, "group": group}


def fetch_matched(client, since) -> list[dict]:
    """Fingerprint rows that matched a bill in another chat, newest first."""
    since_iso = since.isoformat() if hasattr(since, "isoformat") else str(since)
    res = (client.table(TABLE)
           .select("receipt_id, outlet, merchant, total, receipt_date, "
                   "matched_receipt_id, match_kind, match_outlet, created_at")
           .gte("created_at", since_iso).order("id", desc=True).limit(200).execute())
    return [r for r in (res.data or []) if r.get("matched_receipt_id") is not None]


def format_stats(rows, days: int) -> str:
    """``/resit_sama [days]`` — the bills that matched one in another outlet."""
    if not rows:
        return f"No bill submitted at two outlets in the last {days} days."
    lines = [f"Bills matched across outlets — last {days} days ({len(rows)}):"]
    for r in rows:
        lines.append(f"• #{r.get('receipt_id')} {r.get('outlet') or '—'} ↔ "
                     f"#{r.get('matched_receipt_id')} {r.get('match_outlet') or '—'}: "
                     f"{r.get('merchant') or '—'} {_rm(r.get('total'))}, "
                     f"{_nice_day(r.get('receipt_date'))} "
                     f"({_KIND_LABEL.get(r.get('match_kind'), r.get('match_kind'))})")
    return "\n".join(lines)
