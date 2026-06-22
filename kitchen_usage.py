"""Daily Kitchen Usage Log — tap-only protein tracking vs POS.

Flow (per outlet group, Asia/Kuala_Lumpur):
  * 18:00  bot posts the COOKED form ("Rekod Masak — Petang"). The assistant
           chef taps each item and keys the cooked quantity on an inline numpad.
  * 02:00  (next calendar day) bot posts the LEFT form ("Rekod Baki — Tutup
           Kedai"). The cashier keys what is left over.
Both submissions belong to the SAME business day — the 18:00 date — so they
reconcile into one ``kitchen_daily_usage`` row per item:

    Used = Cooked − Left

Used is then compared against POS dishes sold for that business_date and a
dual-gate mismatch flag is raised (see ``mismatch_flag``).

This module keeps all the decision logic PURE (no Telegram / Supabase imports
needed to exercise it) so the numpad state machine, the Used arithmetic, the
dual-gate flag, the Bistro-only Ayam Rempah rule and the 18:00→02:00 business
date span are all unit-testable. The Telegram handlers and APScheduler jobs at
the bottom are thin wrappers that persist state to ``kitchen_log_session`` /
``kitchen_daily_usage`` and edit one inline message in place.

callback_data is namespaced ``kdu:{session_id}:{item_code}:{action}`` so it can
never collide with the existing review:/reparse:/backfill: handlers.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

# A submission before this hour (local) belongs to the PREVIOUS calendar day —
# that is how the 02:00 LEFT form folds back onto the 18:00 COOKED business day.
# Anything from noon onward is "today". 18:00 -> today, 02:00 -> yesterday.
BUSINESS_DAY_CUTOFF_HOUR = 12

# Only this outlet sees Ayam Rempah on the form; every other outlet skips it.
BISTRO_OUTLET = "BISTRO7"

CALLBACK_PREFIX = "kdu"
# Pseudo item_code used by the final "Hantar" (submit) button.
FORM_TOKEN = "_form"


# --- item catalogue ---------------------------------------------------------
# (code, BM label, unit). pcs = whole numbers only; kg = one decimal allowed.
# Order here is the order shown on the form.
_ITEMS: list[tuple[str, str, str]] = [
    ("ayam_goreng", "Ayam Goreng", "pcs"),
    ("ayam_bawang", "Ayam Bawang", "pcs"),
    ("ayam_rempah", "Ayam Rempah", "pcs"),   # BISTRO7 only
    ("ayam_kicap", "Ayam Kicap", "pcs"),
    ("ayam_madu", "Ayam Madu", "pcs"),
    ("ayam_tandoori", "Ayam Tandoori", "pcs"),
    ("ikan_goreng", "Ikan Goreng", "pcs"),
    ("ikan_kari", "Ikan Kari", "pcs"),
    ("telur_ikan", "Telur Ikan", "kg"),
    ("kambing", "Kambing", "kg"),
    ("daging", "Daging", "kg"),
]

# item_code -> {"label", "unit"} for O(1) lookups.
ITEM_BY_CODE: dict[str, dict[str, str]] = {
    code: {"label": label, "unit": unit} for code, label, unit in _ITEMS
}

# Items that only ever appear for the Bistro outlet.
BISTRO_ONLY_CODES = frozenset({"ayam_rempah"})


def items_for_outlet(outlet_code) -> list[dict]:
    """Ordered item dicts ({code,label,unit}) for an outlet's form.

    Ayam Rempah is included ONLY for BISTRO7; every other outlet skips it."""
    is_bistro = (outlet_code or "").strip().upper() == BISTRO_OUTLET
    out = []
    for code, label, unit in _ITEMS:
        if code in BISTRO_ONLY_CODES and not is_bistro:
            continue
        out.append({"code": code, "label": label, "unit": unit})
    return out


def required_codes(outlet_code) -> list[str]:
    """The item codes that must be filled before Hantar is allowed."""
    return [it["code"] for it in items_for_outlet(outlet_code)]


# --- business date -----------------------------------------------------------

def business_date_for(dt: datetime, cutoff_hour: int = BUSINESS_DAY_CUTOFF_HOUR) -> date:
    """The business day a timestamp belongs to.

    The business day is the 18:00 date. A COOKED entry at 18:00 on 22 Jun and a
    LEFT entry at 02:00 on 23 Jun both belong to business_date 22 Jun, so any
    local time BEFORE ``cutoff_hour`` (default noon) folds back to the previous
    calendar day; from the cutoff onward it is the current day."""
    local = dt.astimezone(MY_TZ) if dt.tzinfo is not None else dt
    if local.hour < cutoff_hour:
        return (local.date() - timedelta(days=1))
    return local.date()


# --- numpad state machine (pure) --------------------------------------------
# The buffer is just the string of what the user has typed so far ("", "3",
# "12", "3.", "3.5"). apply_key returns the next buffer; commit_value parses it.

DIGITS = frozenset("0123456789")


def apply_key(buffer: str, key: str, unit: str) -> str:
    """Apply one numpad key to the running buffer and return the new buffer.

    Keys: a single digit "0".."9", "." (kg only), or "bs" (backspace). Invalid
    presses are ignored (no-op) rather than raising:
      * "." is rejected for pcs items, and a second "." is rejected for kg.
      * kg allows at most ONE digit after the decimal point.
      * a leading run of zeros is collapsed ("0" then "5" -> "5") so the display
        never shows "05"; "0." is preserved so "0.5" can be typed.
    """
    buffer = buffer or ""
    if key == "bs":
        return buffer[:-1]
    if key == ".":
        if unit != "kg" or "." in buffer:
            return buffer
        return (buffer + ".") if buffer else "0."
    if key in DIGITS:
        if "." in buffer:
            # at most one decimal place
            if len(buffer.split(".", 1)[1]) >= 1:
                return buffer
            return buffer + key
        # integer part: collapse a lone leading zero
        if buffer == "0":
            return key
        return buffer + key
    return buffer


def buffer_display(buffer: str) -> str:
    """What to show for the running buffer ("—" when empty)."""
    return buffer if buffer else "—"


def commit_value(buffer: str, unit: str):
    """Parse a buffer into a stored value, or ``None`` if it isn't a real number.

    pcs -> int, kg -> float rounded to one decimal. Empty / "." / "0." style
    fragments that don't parse to a number return None (Hantar treats the item
    as unfilled)."""
    buffer = (buffer or "").strip()
    if not buffer or buffer == ".":
        return None
    try:
        val = float(buffer)
    except ValueError:
        return None
    if unit == "pcs":
        return int(round(val))
    return round(val, 1)


def format_value(value, unit) -> str:
    """Render a stored numeric value for a button / summary ("—" when missing)."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if unit == "pcs":
        return str(int(round(num)))
    # kg: trim a trailing ".0" so 3.0 shows as "3", 3.5 stays "3.5"
    return f"{num:g}"


def is_form_complete(entries: dict, outlet_code) -> bool:
    """True iff every required item for the outlet has a value in ``entries``."""
    for code in required_codes(outlet_code):
        if entries.get(code) is None:
            return False
    return True


# --- POS matching + Used arithmetic (pure) ----------------------------------

# Each tracked item maps to POS dishes by keyword. POS item names from
# sales_daily_itemwise look like "Ayam Goreng", "Ikan Kari Kepala", "Kambing
# Masak Merah". A dish counts toward an item when its name contains the item's
# base keyword AND the style keyword. This reuses the same "ayam/ikan" base that
# item_canonicalization_v2 keys on, refined by style — the bot already only
# canonicalizes to the bare "ayam", so per-style splitting is done here.
#
# NOTE: Telur Ikan is NOT here — it is not sold as a POS dish, it is BOUGHT by
# weight. It is compared against kg purchased (from receipts), not POS. See
# PURCHASE_COMPARE_CODES / purchased_kg_from_receipts below.
ITEM_POS_KEYWORDS: dict[str, dict] = {
    "ayam_goreng": {"base": "ayam", "style": ["goreng"]},
    "ayam_bawang": {"base": "ayam", "style": ["bawang"]},
    "ayam_rempah": {"base": "ayam", "style": ["rempah"]},
    "ayam_kicap": {"base": "ayam", "style": ["kicap"]},
    "ayam_madu": {"base": "ayam", "style": ["madu"]},
    "ayam_tandoori": {"base": "ayam", "style": ["tandoori"]},
    "ikan_goreng": {"base": "ikan", "style": ["goreng"]},
    "ikan_kari": {"base": "ikan", "style": ["kari", "curry"]},
    "kambing": {"base": "kambing", "style": []},
    "daging": {"base": "daging", "style": []},
}

# Locked portion sizes for the POS-compared kg items: POS sells these by the
# portion, so the POS piece-count is converted to kg before comparing with the
# weighed Used. Kambing 180 g/portion, Daging 60 g/portion (owner-locked).
# Telur Ikan is deliberately ABSENT — it is compared vs kg purchased, not POS,
# so it needs no portion-size guess.
KG_PORTION_GRAMS: dict[str, float] = {
    "kambing": 180.0,
    "daging": 60.0,
}

# Items compared against kg PURCHASED (from receipts) instead of POS sold.
# Telur Ikan (fish roe) is bought by weight; the resit pipeline already stores
# every supplier purchase, so Used (cooked − left) is compared with what was
# bought that day. v1 uses approach (b): show both numbers and only flag when a
# same-day purchase exists (purchases aren't daily, so absence is NOT a flag).
PURCHASE_COMPARE_CODES = frozenset({"telur_ikan"})


def compare_source(item_code: str) -> str:
    """Where an item's comparison quantity comes from: 'purchase' (kg bought,
    from receipts) for Telur Ikan, else 'pos' (dishes sold)."""
    return "purchase" if item_code in PURCHASE_COMPARE_CODES else "pos"


# Dual-gate thresholds (mamak-tuned): a flag needs BOTH the % gate and the
# absolute gate to trip, so tiny outlets don't false-alarm on a few pcs.
PCS_PCT_GATE = 8.0     # > 8 %
PCS_ABS_GATE = 5.0     # AND > 5 pcs
KG_PCT_GATE = 10.0     # > 10 %
KG_ABS_GATE = 1.5      # AND > 1.5 kg


def used_qty(cooked, left):
    """Used = Cooked − Left. ``None`` if either input is missing."""
    if cooked is None or left is None:
        return None
    return cooked - left


def pos_qty_for_item(item_code: str, itemwise_rows: list) -> float:
    """Sum POS quantity sold for a tracked item from sales_daily_itemwise rows.

    Each row is {item_name, qty, ...}. A row counts when its name contains the
    item's base keyword and (if any) one of its style keywords. For kg items the
    summed POS piece-count is converted to kg via ``KG_PORTION_GRAMS``."""
    spec = ITEM_POS_KEYWORDS.get(item_code)
    if not spec:
        return 0.0
    base = spec["base"]
    styles = spec["style"]
    total = 0.0
    for row in itemwise_rows or []:
        name = str(row.get("item_name") or "").lower()
        if base not in name:
            continue
        if styles and not any(s in name for s in styles):
            continue
        try:
            total += float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
    unit = ITEM_BY_CODE.get(item_code, {}).get("unit")
    if unit == "kg":
        grams = KG_PORTION_GRAMS.get(item_code, 0.0)
        return round(total * grams / 1000.0, 2)
    return total


def mismatch_flag(used, pos, unit) -> str | None:
    """Dual-gate mismatch flag for one item, or ``None`` when within tolerance.

    Returns:
      * 'LEAK' when Used > POS past both gates — possible leakage / unrecorded
        sale / over-portioning.
      * 'DATA' when Used < POS past both gates — likely a key-in error or
        carryover.
    Gates (BOTH must trip): pcs -> |Δ| > 8 % AND > 5 pcs; kg -> > 10 % AND
    > 1.5 kg. The % gate is relative to POS; when POS is 0 the % gate is treated
    as exceeded so a non-trivial Used still flags."""
    if used is None or pos is None:
        return None
    delta = used - pos
    abs_delta = abs(delta)
    if pos > 0:
        pct = abs_delta / pos * 100.0
    else:
        pct = float("inf")
    if unit == "pcs":
        pct_gate, abs_gate = PCS_PCT_GATE, PCS_ABS_GATE
    else:
        pct_gate, abs_gate = KG_PCT_GATE, KG_ABS_GATE
    if pct > pct_gate and abs_delta > abs_gate:
        return "LEAK" if delta > 0 else "DATA"
    return None


# Telur Ikan purchase lines: the resit pipeline canonicalises "TELUR IKAN" to the
# coarse "ikan" key, so it can't be separated from other fish by canonical alone.
# Match the raw line name directly: fish roe is always written "telur ikan" (or
# "telur ... ikan" / "roe").
def _is_telur_ikan_line(name) -> bool:
    n = str(name or "").lower()
    if not n:
        return False
    if "telur ikan" in n or "telur" in n and "ikan" in n:
        return True
    return "roe" in n


def purchased_kg_from_receipts(receipts_rows: list, item_code: str = "telur_ikan"):
    """Sum kg PURCHASED for a weight-bought item from receipts.

    Each receipt row carries an ``items`` jsonb list of {name, qty, price}. For a
    weight-priced line the ``qty`` is the kg bought. Returns the summed kg, or
    ``None`` when NO matching purchase line exists at all — the caller shows
    "tiada rekod beli" and does NOT flag (purchases aren't daily, so a missing
    same-day buy is not a mismatch)."""
    if item_code != "telur_ikan":
        return None
    found = False
    total = 0.0
    for r in receipts_rows or []:
        items = r.get("items")
        if not isinstance(items, list):
            continue
        for line in items:
            if not isinstance(line, dict):
                continue
            if not _is_telur_ikan_line(line.get("name")):
                continue
            found = True
            try:
                total += float(line.get("qty") or 0)
            except (TypeError, ValueError):
                continue
    if not found:
        return None
    return round(total, 1)


def evaluate_usage(item_code: str, cooked, left, itemwise_rows: list,
                   purchased_kg=None) -> dict:
    """Full per-item evaluation: Used, the comparison qty, and the dual-gate flag.

    For POS items the comparison qty is POS sold (kg-converted for Kambing/Daging
    via portion sizes). For Telur Ikan (``compare_source`` == 'purchase') it is
    ``purchased_kg`` — kg bought that day from receipts — and the flag is skipped
    when that is ``None`` (no same-day purchase). Returns
    {code,label,unit,cooked,left,used,pos,flag,source}; ``pos`` holds whichever
    comparison qty applies (stored in the kitchen_daily_usage.pos_qty column)."""
    meta = ITEM_BY_CODE.get(item_code, {})
    unit = meta.get("unit", "pcs")
    used = used_qty(cooked, left)
    source = compare_source(item_code)
    if source == "purchase":
        compared = purchased_kg
    else:
        compared = pos_qty_for_item(item_code, itemwise_rows)
    flag = (
        mismatch_flag(used, compared, unit)
        if used is not None and compared is not None
        else None
    )
    return {
        "code": item_code,
        "label": meta.get("label", item_code),
        "unit": unit,
        "cooked": cooked,
        "left": left,
        "used": used,
        "pos": compared,
        "flag": flag,
        "source": source,
    }


# --- form / numpad copy ------------------------------------------------------

PHASE_COOKED = "cooked"
PHASE_LEFT = "left"

_PHASE_COPY = {
    PHASE_COOKED: {
        "title": "🍳 Rekod Masak — Petang",
        "prompt": "berapa dimasak",
    },
    PHASE_LEFT: {
        "title": "🌙 Rekod Baki — Tutup Kedai",
        "prompt": "berapa tinggal",
    },
}


def form_title(phase: str) -> str:
    return _PHASE_COPY.get(phase, _PHASE_COPY[PHASE_COOKED])["title"]


def numpad_prompt(phase: str) -> str:
    return _PHASE_COPY.get(phase, _PHASE_COPY[PHASE_COOKED])["prompt"]


def form_text(phase: str, business_date, outlet_label, entries: dict, outlet_code) -> str:
    """Header text shown above the item-list keyboard."""
    done = sum(1 for c in required_codes(outlet_code) if entries.get(c) is not None)
    total = len(required_codes(outlet_code))
    lines = [
        form_title(phase),
        f"{outlet_label} • {business_date}",
        f"Isi setiap item ({done}/{total} siap). Tap untuk key-in.",
    ]
    return "\n".join(lines)


def numpad_text(phase: str, item_label: str, unit: str, buffer: str) -> str:
    """Header text shown above the numpad."""
    unit_hint = "(kg, boleh 1 titik perpuluhan)" if unit == "kg" else "(pcs, nombor bulat)"
    return "\n".join([
        f"{item_label} — {numpad_prompt(phase)} {unit_hint}",
        f"Nilai: {buffer_display(buffer)}",
    ])


# --- keyboards ---------------------------------------------------------------
# Built lazily so the pure logic above imports with no telegram dependency.

def _cb(session_id, item_code, action) -> str:
    return f"{CALLBACK_PREFIX}:{session_id}:{item_code}:{action}"


def build_item_keyboard(session_id, outlet_code, entries: dict):
    """One button per item (✓ prefix + value when filled, "—" when empty) plus
    a final Hantar button. Returns an InlineKeyboardMarkup."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for it in items_for_outlet(outlet_code):
        code, label, unit = it["code"], it["label"], it["unit"]
        val = entries.get(code)
        if val is not None:
            text = f"✓ {label}: {format_value(val, unit)}"
        else:
            text = f"{label}: —"
        rows.append([InlineKeyboardButton(text, callback_data=_cb(session_id, code, "open"))])

    complete = is_form_complete(entries, outlet_code)
    hantar_label = "📤 Hantar" if complete else "📤 Hantar (isi semua dulu)"
    rows.append([InlineKeyboardButton(hantar_label, callback_data=_cb(session_id, FORM_TOKEN, "send"))])
    return InlineKeyboardMarkup(rows)


def build_numpad_keyboard(session_id, item_code, unit: str):
    """The inline numpad for one item.

    pcs (3×4): [1 2 3] [4 5 6] [7 8 9] [⌫ 0 ✓]
    kg  (+".") : [1 2 3] [4 5 6] [7 8 9] [. 0 ⌫] [✓ Simpan]
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def b(text, action):
        return InlineKeyboardButton(text, callback_data=_cb(session_id, item_code, action))

    rows = [
        [b("1", "d1"), b("2", "d2"), b("3", "d3")],
        [b("4", "d4"), b("5", "d5"), b("6", "d6")],
        [b("7", "d7"), b("8", "d8"), b("9", "d9")],
    ]
    if unit == "kg":
        rows.append([b(".", "dot"), b("0", "d0"), b("⌫", "bs")])
        rows.append([b("✓ Simpan", "ok")])
    else:
        rows.append([b("⌫", "bs"), b("0", "d0"), b("✓", "ok")])
    return InlineKeyboardMarkup(rows)


def parse_callback(data: str) -> dict | None:
    """Split ``kdu:{session_id}:{item_code}:{action}`` into its parts, or None
    if it isn't one of ours."""
    if not isinstance(data, str) or not data.startswith(CALLBACK_PREFIX + ":"):
        return None
    parts = data.split(":", 3)
    if len(parts) != 4:
        return None
    _, session_id, item_code, action = parts
    return {"session_id": session_id, "item_code": item_code, "action": action}


# --- DB layer (impure) -------------------------------------------------------
# A single supabase client is injected once at startup so the handlers/jobs
# below don't need to import bot.py (which would be circular).

SESSION_TABLE = "kitchen_log_session"
USAGE_TABLE = "kitchen_daily_usage"
SALES_SUMMARY_TABLE = "sales_daily_summary"
SALES_ITEMWISE_TABLE = "sales_daily_itemwise"
RECEIPTS_TABLE = "receipts"

_supabase = None


def init_kitchen_usage(supabase_client) -> None:
    """Wire the module to the shared Supabase client (called from bot startup)."""
    global _supabase
    _supabase = supabase_client


def _rows(result):
    return getattr(result, "data", None) or []


def _load_entries(row: dict) -> dict:
    raw = row.get("entries")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def get_or_create_session(client, chat_id, outlet_code, business_date, phase, message_id=None) -> dict:
    """Fetch the open session row for (chat, business_date, phase) or create it."""
    existing = _rows(
        client.table(SESSION_TABLE)
        .select("*")
        .eq("chat_id", chat_id)
        .eq("business_date", str(business_date))
        .eq("phase", phase)
        .limit(1)
        .execute()
    )
    if existing:
        row = existing[0]
        if message_id is not None and row.get("message_id") != message_id:
            client.table(SESSION_TABLE).update({"message_id": message_id}).eq("id", row["id"]).execute()
            row["message_id"] = message_id
        return row
    payload = {
        "chat_id": chat_id,
        "outlet_code": outlet_code,
        "business_date": str(business_date),
        "phase": phase,
        "message_id": message_id,
        "entries": {},
        "buffer": "",
        "status": "open",
    }
    inserted = _rows(client.table(SESSION_TABLE).insert(payload).execute())
    return inserted[0] if inserted else payload


def get_session(client, session_id) -> dict | None:
    rows = _rows(client.table(SESSION_TABLE).select("*").eq("id", session_id).limit(1).execute())
    return rows[0] if rows else None


def _save_session(client, session_id, **fields) -> None:
    fields["updated_at"] = datetime.now(MY_TZ).isoformat()
    client.table(SESSION_TABLE).update(fields).eq("id", session_id).execute()


def _fetch_itemwise(client, outlet_code, business_date) -> list:
    """POS itemwise rows for an outlet's business_date (used to compute pos_qty).

    sales_daily_summary keys on outlet_canonical; kitchen keys on outlet_code.
    We resolve the code to its canonical name and match summaries on either."""
    try:
        from outlet_resolver import canonical_outlet
        canonical = canonical_outlet(outlet_code)
    except Exception:
        canonical = None
    candidates = {c for c in (canonical, outlet_code) if c}
    summaries = _rows(
        client.table(SALES_SUMMARY_TABLE)
        .select("id, outlet_canonical, outlet_code")
        .eq("business_date", str(business_date))
        .execute()
    )
    ids = [
        s["id"] for s in summaries
        if s.get("outlet_canonical") in candidates or s.get("outlet_code") in candidates
    ]
    if not ids:
        return []
    rows = _rows(
        client.table(SALES_ITEMWISE_TABLE)
        .select("item_name, qty, summary_id")
        .in_("summary_id", ids)
        .execute()
    )
    return rows


def _fetch_purchased_kg(client, outlet_code, business_date, item_code="telur_ikan"):
    """kg of a weight-bought item purchased for an outlet's business_date.

    Reads receipts for that calendar date, keeps the ones whose (free-form)
    outlet resolves to the same canonical outlet as ``outlet_code``, and sums the
    matching purchase lines. Returns ``None`` when there is no matching purchase
    that day (Telur Ikan is bought every few days, not daily)."""
    try:
        from outlet_resolver import canonical_outlet
        target = canonical_outlet(outlet_code)
    except Exception:
        target = None
    rows = _rows(
        client.table(RECEIPTS_TABLE)
        .select("outlet, items, receipt_date")
        .eq("receipt_date", str(business_date))
        .execute()
    )
    matched = []
    for r in rows:
        raw = r.get("outlet")
        try:
            from outlet_resolver import canonical_outlet
            rc = canonical_outlet(raw)
        except Exception:
            rc = None
        # Match on canonical outlet; if neither side resolves, fall back to the
        # raw code so a same-named outlet still lines up.
        if (target is not None and rc == target) or (raw == outlet_code):
            matched.append(r)
    return purchased_kg_from_receipts(matched, item_code)


def finalize_submission(client, session: dict, submitter: str):
    """Upsert a completed phase into kitchen_daily_usage. On the LEFT phase also
    compute POS qty + the mismatch flag. Returns the list of per-item evaluation
    dicts (only meaningful for the LEFT phase, else [])."""
    outlet_code = session["outlet_code"]
    business_date = str(session["business_date"])
    phase = session["phase"]
    entries = _load_entries(session)
    now_iso = datetime.now(MY_TZ).isoformat()

    for it in items_for_outlet(outlet_code):
        code, label, unit = it["code"], it["label"], it["unit"]
        value = entries.get(code)
        if value is None:
            continue
        row = {
            "outlet_code": outlet_code,
            "business_date": business_date,
            "item_code": code,
            "item_label": label,
            "unit": unit,
        }
        if phase == PHASE_COOKED:
            row["cooked_qty"] = value
            row["cooked_by"] = submitter
            row["cooked_at"] = now_iso
        else:
            row["left_qty"] = value
            row["left_by"] = submitter
            row["left_at"] = now_iso
        try:
            client.table(USAGE_TABLE).upsert(
                row, on_conflict="outlet_code,business_date,item_code"
            ).execute()
        except Exception:
            logger.exception("kitchen: upsert failed for %s/%s/%s", outlet_code, business_date, code)

    _save_session(client, session["id"], status="submitted", entries=entries)

    if phase != PHASE_LEFT:
        return []

    # LEFT just landed -> compute Used vs comparison qty for the whole day.
    # POS items use sales itemwise; Telur Ikan uses kg purchased from receipts.
    itemwise = _fetch_itemwise(client, outlet_code, business_date)
    purchased = {
        code: _fetch_purchased_kg(client, outlet_code, business_date, code)
        for code in PURCHASE_COMPARE_CODES
    }
    usage_rows = _rows(
        client.table(USAGE_TABLE)
        .select("item_code, cooked_qty, left_qty")
        .eq("outlet_code", outlet_code)
        .eq("business_date", business_date)
        .execute()
    )
    by_code = {r["item_code"]: r for r in usage_rows}
    evaluations = []
    for it in items_for_outlet(outlet_code):
        code = it["code"]
        rec = by_code.get(code)
        if rec is None:
            continue
        ev = evaluate_usage(
            code, rec.get("cooked_qty"), rec.get("left_qty"), itemwise,
            purchased_kg=purchased.get(code),
        )
        evaluations.append(ev)
        try:
            client.table(USAGE_TABLE).update(
                {"pos_qty": ev["pos"], "mismatch_flag": ev["flag"]}
            ).eq("outlet_code", outlet_code).eq("business_date", business_date).eq(
                "item_code", code
            ).execute()
        except Exception:
            logger.exception("kitchen: flag update failed for %s", code)
    return evaluations


# --- summary rendering -------------------------------------------------------

def render_mini_summary(outlet_label, business_date, evaluations: list) -> str:
    """The short Used-vs-POS recap posted in the group right after LEFT."""
    lines = [
        "📊 Ringkasan Guna vs POS",
        f"{outlet_label} • {business_date}",
        "",
    ]
    flagged = 0
    for ev in evaluations:
        used = format_value(ev["used"], ev["unit"])
        unit = ev["unit"]
        if ev["flag"] == "LEAK":
            mark = "🔴"
            flagged += 1
        elif ev["flag"] == "DATA":
            mark = "⚠️"
            flagged += 1
        else:
            mark = "✅"
        if ev.get("source") == "purchase":
            # Telur Ikan: consumption vs purchase, not vs sales.
            if ev["pos"] is None:
                lines.append(f"➖ {ev['label']}: guna {used} {unit} vs tiada rekod beli")
            else:
                beli = format_value(ev["pos"], unit)
                lines.append(f"{mark} {ev['label']}: {used} {unit} guna vs {beli} {unit} beli")
        else:
            pos = format_value(ev["pos"], unit)
            lines.append(f"{mark} {ev['label']}: guna {used} vs POS {pos} {unit}")
    if flagged == 0:
        lines.append("")
        lines.append("Semua padan 👍")
    else:
        lines.append("")
        lines.append("🔴 = guna lebih dari POS (bocor?)  ⚠️ = guna kurang (silap key-in?)")
    return "\n".join(lines)


# --- digest data + section (pure-ish; client only for fetch) ----------------

def gather_digest_usage(client, business_date) -> list:
    """Per-outlet kitchen-usage rollups for the 11PM digest's business_date.

    Returns a list of {outlet_code, complete, items:[...]} — one per outlet that
    has any kitchen_daily_usage row that day. ``complete`` is False when COOKED
    or LEFT is missing for any item (digest then shows "Rekod tak lengkap")."""
    try:
        rows = _rows(
            client.table(USAGE_TABLE)
            .select("outlet_code, item_code, item_label, unit, cooked_qty, left_qty, used_qty, pos_qty, mismatch_flag")
            .eq("business_date", str(business_date))
            .execute()
        )
    except Exception:
        logger.warning("digest: kitchen usage unavailable", exc_info=True)
        return []
    by_outlet: dict = {}
    for r in rows:
        by_outlet.setdefault(r["outlet_code"], []).append(r)
    out = []
    for outlet_code, items in sorted(by_outlet.items()):
        complete = all(
            it.get("cooked_qty") is not None and it.get("left_qty") is not None
            for it in items
        )
        out.append({"outlet_code": outlet_code, "complete": complete, "items": items})
    return out


# --- Telegram handlers + schedulers (impure) --------------------------------

def _submitter_name(user) -> str:
    if user is None:
        return "?"
    name = getattr(user, "full_name", None) or getattr(user, "username", None)
    return name or str(getattr(user, "id", "?"))


async def handle_kitchen_callback(update, context) -> None:
    """Single CallbackQueryHandler for everything under the ``kdu:`` namespace."""
    query = update.callback_query
    if query is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        return
    await query.answer()

    if _supabase is None:
        logger.warning("kitchen callback received but module not initialised")
        return

    session_id = parsed["session_id"]
    item_code = parsed["item_code"]
    action = parsed["action"]

    session = await asyncio.to_thread(get_session, _supabase, session_id)
    if session is None:
        with contextlib.suppress(Exception):
            await query.edit_message_text("Sesi ini dah tamat. Tunggu borang baru.")
        return
    if session.get("status") == "submitted":
        await query.answer()
        return

    outlet_code = session["outlet_code"]
    phase = session["phase"]
    business_date = session["business_date"]
    entries = _load_entries(session)
    try:
        from outlet_mapping import outlet_display_name
        outlet_label = outlet_display_name(outlet_code)
    except Exception:
        outlet_label = outlet_code

    # --- Hantar (submit) ---
    if item_code == FORM_TOKEN and action == "send":
        if not is_form_complete(entries, outlet_code):
            await query.answer("Isi semua item dulu sebelum Hantar.", show_alert=True)
            return
        submitter = _submitter_name(query.from_user)
        evaluations = await asyncio.to_thread(
            finalize_submission, _supabase, session, submitter
        )
        with contextlib.suppress(Exception):
            await query.edit_message_text(f"✅ Tersimpan — {form_title(phase)}\n{outlet_label} • {business_date}")
        if phase == PHASE_LEFT and evaluations:
            summary = render_mini_summary(outlet_label, business_date, evaluations)
            with contextlib.suppress(Exception):
                await context.bot.send_message(chat_id=session["chat_id"], text=summary)
        return

    meta = ITEM_BY_CODE.get(item_code)
    if meta is None:
        return
    unit = meta["unit"]

    # --- open an item's numpad (pre-filled when editing) ---
    if action == "open":
        existing = entries.get(item_code)
        buffer = "" if existing is None else format_value(existing, unit)
        await asyncio.to_thread(
            _save_session, _supabase, session_id, editing_item=item_code, buffer=buffer
        )
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                numpad_text(phase, meta["label"], unit, buffer),
                reply_markup=build_numpad_keyboard(session_id, item_code, unit),
            )
        return

    # --- numpad keypress ---
    buffer = session.get("buffer") or ""
    if action == "ok":
        value = commit_value(buffer, unit)
        if value is not None:
            entries[item_code] = value
        await asyncio.to_thread(
            _save_session, _supabase, session_id,
            entries=entries, editing_item=None, buffer="",
        )
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                form_text(phase, business_date, outlet_label, entries, outlet_code),
                reply_markup=build_item_keyboard(session_id, outlet_code, entries),
            )
        return

    if action == "bs":
        key = "bs"
    elif action == "dot":
        key = "."
    elif action.startswith("d") and len(action) == 2 and action[1] in DIGITS:
        key = action[1]
    else:
        return

    new_buffer = apply_key(buffer, key, unit)
    await asyncio.to_thread(_save_session, _supabase, session_id, buffer=new_buffer)
    with contextlib.suppress(Exception):
        await query.edit_message_text(
            numpad_text(phase, meta["label"], unit, new_buffer),
            reply_markup=build_numpad_keyboard(session_id, item_code, unit),
        )


async def _post_forms(application, phase: str) -> None:
    """Post the COOKED or LEFT form to every configured kitchen group. No-ops
    cleanly when config/kitchen_groups.py has no IDs yet."""
    from config.kitchen_groups import configured_groups

    if _supabase is None:
        logger.warning("kitchen: supabase not initialised — %s post skipped", phase)
        return
    groups = await asyncio.to_thread(configured_groups, _supabase)
    if not groups:
        logger.info("kitchen: no groups resolved — %s post skipped", phase)
        return

    now_my = datetime.now(MY_TZ)
    business_date = business_date_for(now_my)
    for chat_id, outlet_code in groups:
        try:
            from outlet_mapping import outlet_display_name
            outlet_label = outlet_display_name(outlet_code)
        except Exception:
            outlet_label = outlet_code
        session = await asyncio.to_thread(
            get_or_create_session, _supabase, chat_id, outlet_code, business_date, phase
        )
        if session.get("status") == "submitted":
            logger.info("kitchen: %s already submitted for %s %s", phase, outlet_code, business_date)
            continue
        entries = _load_entries(session)
        try:
            msg = await application.bot.send_message(
                chat_id=chat_id,
                text=form_text(phase, business_date, outlet_label, entries, outlet_code),
                reply_markup=build_item_keyboard(session["id"], outlet_code, entries),
            )
            await asyncio.to_thread(
                _save_session, _supabase, session["id"], message_id=msg.message_id
            )
        except Exception:
            logger.exception("kitchen: failed to post %s form to chat %s", phase, chat_id)


async def post_cooked_forms(application) -> None:
    """APScheduler job: 18:00 COOKED form to every kitchen group."""
    await _post_forms(application, PHASE_COOKED)


async def post_left_forms(application) -> None:
    """APScheduler job: 02:00 LEFT form to every kitchen group."""
    await _post_forms(application, PHASE_LEFT)


def register_handlers(app) -> None:
    """Register the single kdu: callback handler on the PTB application."""
    from telegram.ext import CallbackQueryHandler

    app.add_handler(
        CallbackQueryHandler(handle_kitchen_callback, pattern=r"^kdu:")
    )
