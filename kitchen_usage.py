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
import os
import re
import time
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

# Form phases. Three entries per business day, all keyed to the 18:00 date:
#   18:00 COOKED (evening), 00:00 COOKED_NIGHT (optional, additive), 02:00 LEFT.
PHASE_COOKED = "cooked"
PHASE_COOKED_NIGHT = "cooked_night"
PHASE_LEFT = "left"
# Phases that write/extend cooked_qty (vs LEFT which writes left_qty).
COOKED_PHASES = (PHASE_COOKED, PHASE_COOKED_NIGHT)

# Master kill-switch for the scheduled forms. The 18:00 COOKED / 02:00 LEFT
# posters NO-OP unless KITCHEN_LOG_ENABLED is truthy. Default OFF so the bot can
# ship (and /kitchen_groups_debug can verify the chat->outlet mapping) WITHOUT
# blasting a possibly-mis-mapped form to 10 groups. Flip the env var to 'true'
# once the mapping is confirmed.
_ENABLED_TRUTHY = {"1", "true", "yes", "on", "y"}


def kitchen_log_enabled() -> bool:
    """True only when KITCHEN_LOG_ENABLED is explicitly set truthy. Default OFF."""
    return os.environ.get("KITCHEN_LOG_ENABLED", "").strip().lower() in _ENABLED_TRUTHY


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


def can_submit(entries: dict, outlet_code, phase: str = PHASE_COOKED) -> bool:
    """Hantar is allowed once AT LEAST ONE item is filled — for every phase.

    Outlets don't cook every item every day, so forcing all 11 is too slow.
    Untouched items are saved as 0 on submit (COOKED/LEFT) — see
    ``finalize_submission`` — or simply skipped for the additive night form.
    The day is only "incomplete" in the digest when a whole COOKED or LEFT
    session was never submitted, never because some items were 0."""
    return any(entries.get(c) is not None for c in required_codes(outlet_code))


# --- bulk free-text parsing (one message, all items) ------------------------
# Staff reply to a posted form with one message, one item per line OR
# comma-separated ("ayam goreng 50, kambing 8"). Each segment is matched to an
# item by normalized name + curated variants, and the trailing number is the
# value (kg items allow a decimal, incl. comma-decimal).

# Curated name variants per item. Single-style words (bawang/rempah/kicap/madu/
# tandoori/kari) are unique to one item so they're safe; "goreng" is NOT listed
# alone because it's ambiguous between ayam_goreng and ikan_goreng.
_ITEM_VARIANTS: dict[str, list[str]] = {
    "ayam_goreng": ["ayam goreng", "a goreng", "ag", "goreng ayam"],
    "ayam_bawang": ["ayam bawang", "a bawang", "ab", "bawang"],
    "ayam_rempah": ["ayam rempah", "a rempah", "ar", "rempah"],
    "ayam_kicap": ["ayam kicap", "a kicap", "ak", "kicap"],
    "ayam_madu": ["ayam madu", "a madu", "am", "madu"],
    "ayam_tandoori": ["ayam tandoori", "a tandoori", "at", "tandoori", "tandori"],
    "ikan_goreng": ["ikan goreng", "i goreng", "ig"],
    "ikan_kari": ["ikan kari", "i kari", "ik", "kari ikan", "ikan curry", "kari", "curry"],
    "telur_ikan": ["telur ikan", "telurikan", "telur", "roe", "fish roe"],
    "kambing": ["kambing", "mutton", "goat"],
    "daging": ["daging", "beef"],
}


def _normalize_name(s) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_NAME_TO_CODE: dict[str, str] = {}
_NOSPACE_TO_CODE: dict[str, str] = {}
for _code, _variants in _ITEM_VARIANTS.items():
    for _v in _variants:
        _n = _normalize_name(_v)
        if _n:
            _NAME_TO_CODE.setdefault(_n, _code)
            _NOSPACE_TO_CODE.setdefault(_n.replace(" ", ""), _code)


def match_item_name(name) -> str | None:
    """Resolve a free-text item name to an item_code, or None when it's
    unrecognised / ambiguous (e.g. bare "goreng")."""
    n = _normalize_name(name)
    if not n:
        return None
    if n in _NAME_TO_CODE:
        return _NAME_TO_CODE[n]
    ns = n.replace(" ", "")
    if ns in _NOSPACE_TO_CODE:
        return _NOSPACE_TO_CODE[ns]
    # Substring fallback — only when it resolves to exactly ONE item.
    cands = set()
    for vname, code in _NAME_TO_CODE.items():
        vns = vname.replace(" ", "")
        if vname in n or n in vname or vns in ns or ns in vns:
            cands.add(code)
    return next(iter(cands)) if len(cands) == 1 else None


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _value_from_str(num_str: str, unit: str):
    return commit_value(num_str.replace(",", "."), unit)


def _split_segments(text) -> list[str]:
    # A comma is both a decimal mark ("3,5") and an item separator
    # ("ayam goreng 50, kambing 8"). Protect decimal commas (digit,digit -> .)
    # first, then split on newlines and the remaining (separator) commas.
    protected = re.sub(r"(?<=\d),(?=\d)", ".", text or "")
    return [s.strip() for s in re.split(r"[\n,]+", protected) if s.strip()]


def parse_bulk_entry(text, outlet_code) -> dict:
    """Parse a free-text bulk message into {matched: {code: value}, unmatched:
    [segment, ...]}. Untouched items aren't included (they become 0 on submit).
    Ayam Rempah is only accepted for BISTRO7 (otherwise the segment is reported
    as unmatched). The number is the trailing numeric token in each segment."""
    allowed = set(required_codes(outlet_code))
    matched: dict[str, float] = {}
    unmatched: list[str] = []
    for seg in _split_segments(text):
        nums = list(_NUM_RE.finditer(seg))
        if not nums:
            unmatched.append(seg)
            continue
        m = nums[-1]
        name = (seg[: m.start()] + " " + seg[m.end():]).strip()
        code = match_item_name(name)
        if code is None or code not in allowed:
            unmatched.append(seg)
            continue
        unit = ITEM_BY_CODE[code]["unit"]
        val = _value_from_str(m.group(0), unit)
        if val is None:
            unmatched.append(seg)
            continue
        matched[code] = val
    return {"matched": matched, "unmatched": unmatched}


# --- outlet-code normalisation ----------------------------------------------
# The POS daily-summary keys every outlet with a one-letter shift/day prefix
# ("S-KLANG" per shift, "D-KLANG" per day) while the kitchen keys the SAME
# outlet bare ("KLANG"). A raw string compare therefore never matches and POS
# read 0 for every outlet. Stripping the S-/D- prefix and resolving the rest to
# its canonical outlet name lands both sides on one join key (all 10 outlets,
# including the two whose names differ: kitchen "D" <-> POS "D-DAMANSARA" both
# -> "D.U", kitchen "KLRAZAK" <-> POS "D-RAZAK" both -> "K.L Razak").
_OUTLET_CODE_PREFIX_RE = re.compile(r"^[sd]-", re.IGNORECASE)


def normalize_outlet_code(code):
    """Common join key shared by a kitchen outlet_code and a POS outlet_code.

    Strips the POS S-/D- prefix, then resolves the remainder to its canonical
    outlet name (via ``outlet_resolver.canonical_outlet``) so kitchen ``KLANG``
    and POS ``S-KLANG`` / ``D-KLANG`` all collapse to ``"Klang B.Emas"``.
    Falls back to the upper-cased prefix-stripped code when the outlet is not in
    the canonical registry; returns ``None`` for empty / non-string input."""
    if not isinstance(code, str):
        return None
    raw = code.strip()
    if not raw:
        return None
    stripped = _OUTLET_CODE_PREFIX_RE.sub("", raw).strip()
    if not stripped:
        return None
    try:
        from outlet_resolver import canonical_outlet
        resolved = canonical_outlet(stripped)
    except Exception:
        resolved = None
    return resolved or stripped.upper()


# --- POS matching + Used arithmetic (pure) ----------------------------------

# Each tracked item maps to POS dishes by keyword, kept on its OWN comparison
# line (whole-leg items are NOT combined — each item's mismatch stays visible by
# name). POS item names from sales_daily_itemwise look like "Ayam Goreng",
# "Nasi Ayam Bawang", "Kambing Masak Merah". A dish counts toward an item when
# its name contains the item's base keyword AND matches the item's rule, and is
# NOT on the exclusion list (Thai-chef isi-ayam, staff meals — see
# ``_pos_dish_excluded``).
#
# A rule is one of:
#   * "phrases" — the listed words must appear ADJACENT (whole-cut only). Used
#     for the *goreng* items so "Ayam Goreng" / "Nasi Ayam Goreng Besar" count
#     but carb-fried "Nasi Goreng Ayam" / "Maggi Goreng Ayam" / "Mee Goreng
#     Ayam" (ayam trailing) do NOT.
#   * "styles" — any listed style keyword anywhere in the name. So "Ayam
#     Bawang", "Nasi Ayam Bawang", "Nasi Separuh Ayam Bawang" and "Briyani Ayam
#     Bawang Set" all count for ayam_bawang. Plain "Nasi Ayam" / "Nasi Separuh
#     Ayam" carry no style, so they match nothing (correctly excluded).
#   * neither (kambing/daging) — base match alone: ALL kambing / daging dishes.
# "not" lists words that disqualify a dish even when the rule otherwise matches
# (a fried "...berempah" dish is a goreng dish, not a rempah dish).
#
# NOTE: Telur Ikan is NOT here — it is not sold as a POS dish, it is BOUGHT by
# weight. It is compared against kg purchased (from receipts), not POS. See
# PURCHASE_COMPARE_CODES / purchased_kg_from_receipts below.
ITEM_POS_KEYWORDS: dict[str, dict] = {
    # Whole-cut fried only — "ayam goreng" must be adjacent.
    "ayam_goreng": {"base": "ayam", "phrases": ["ayam goreng"]},
    "ayam_bawang": {"base": "ayam", "styles": ["bawang"]},
    # BISTRO7 only; other outlets have no kitchen line so POS shows 0 quietly.
    "ayam_rempah": {"base": "ayam", "styles": ["rempah"], "not": ["goreng"]},
    "ayam_kicap": {"base": "ayam", "styles": ["kicap"]},
    "ayam_madu": {"base": "ayam", "styles": ["madu"]},
    # "tandori" is the common POS misspelling; "...Staff" handled by exclusion.
    "ayam_tandoori": {"base": "ayam", "styles": ["tandoori", "tandori"]},
    "ikan_goreng": {"base": "ikan", "phrases": ["ikan goreng"]},
    "ikan_kari": {"base": "ikan", "styles": ["kari", "curry"]},
    "kambing": {"base": "kambing", "styles": []},
    "daging": {"base": "daging", "styles": []},
}

# Dishes excluded from EVERY item: staff meals and anything in the POS "THAI
# FOOD" category (matched on the itemwise row's category field). These are the
# Thai-chef / staff meals that never appear in the kitchen log, so counting
# their sales would manufacture false mismatches.
_POS_STAFF_SUBSTR = "staff"

# Ayam-only exclusions: Thai-chef isi-ayam noodle/rice dishes and the
# rendang/kurma styles that are not tracked in the kitchen log. NOT applied to
# kambing/daging — those count ALL dishes (minus staff / Thai category).
AYAM_EXCLUDE_SUBSTRINGS = (
    "paprik", "tomyam", "tom yam", "maggi",
    "indomee", "indo mee",
    "kuey teow", "kuey tiaw", "koay teow", "kuetiau", "kuey tiau",
    "mee ", "rendang", "kurma",
)


def _pos_dish_excluded(name: str, category, base: str) -> bool:
    """True when a POS dish must not count toward ANY kitchen item.

    Staff meals and the THAI FOOD category are excluded for every item; the
    isi-ayam noodle/rice + rendang/kurma exclusions apply only to ayam items."""
    if "thai" in str(category or "").lower():
        return True
    if _POS_STAFF_SUBSTR in name:
        return True
    if base == "ayam" and any(s in name for s in AYAM_EXCLUDE_SUBSTRINGS):
        return True
    return False


def _pos_dish_matches(spec: dict, name: str) -> bool:
    """True when a dish name satisfies an item's match rule (phrases / styles /
    base-only), and is not disqualified by the rule's ``not`` list."""
    if any(n in name for n in spec.get("not", ())):
        return False
    phrases = spec.get("phrases")
    if phrases is not None:
        return any(p in name for p in phrases)
    styles = spec.get("styles")
    if styles:
        return any(s in name for s in styles)
    return True  # base-only (kambing / daging): all dishes


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

    Each row is {item_name, qty, category, ...}. A row counts when its name
    contains the item's base keyword, satisfies the item's match rule (phrases /
    styles / base-only), and is not excluded (Thai-chef isi-ayam, staff meals;
    see ``_pos_dish_excluded``). For kg items the summed POS piece-count is
    converted to kg via ``KG_PORTION_GRAMS``."""
    spec = ITEM_POS_KEYWORDS.get(item_code)
    if not spec:
        return 0.0
    base = spec["base"]
    total = 0.0
    for row in itemwise_rows or []:
        name = str(row.get("item_name") or "").lower()
        if base not in name:
            continue
        if _pos_dish_excluded(name, row.get("category"), base):
            continue
        if not _pos_dish_matches(spec, name):
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

_PHASE_COPY = {
    PHASE_COOKED: {
        "title": "🍳 Rekod Masak — Petang",
        "prompt": "berapa dimasak",
    },
    PHASE_COOKED_NIGHT: {
        "title": "🌙 Rekod Masak Malam — Tambahan",
        # The night form captures only the EXTRA cooked at night, added on top of
        # the 6PM amount.
        "prompt": "berapa tambah masak malam",
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
    """Header text above the per-item tap buttons. Items are entered by tapping
    a button (then the numpad), not by typing. Bilingual (BM + Tamil)."""
    done = sum(1 for c in required_codes(outlet_code) if entries.get(c) is not None)
    total = len(required_codes(outlet_code))
    lines = [
        form_title(phase),
        f"{outlet_label} • {business_date}",
    ]
    if phase == PHASE_COOKED_NIGHT:
        lines.append(f"Tap item untuk key-in tambahan masak malam ({done} item). Skip jika tiada.")
        lines.append("தமிழ்: item-ஐ தட்டி இரவு கூடுதலை மட்டும் போடுங்க.")
    else:
        lines.append(f"Tap untuk key-in ({done}/{total}). Yang tak isi = 0.")
        lines.append("தமிழ்: item-ஐ தட்டி எண் போடுங்க. போடாதது = 0.")
    # Mistake-fixing before Hantar: re-tap an item to change it, 🗑 to clear.
    lines.append("Tekan balik barang untuk betulkan sebelum Hantar.")
    return "\n".join(lines)


def numpad_text(phase: str, item_label: str, unit: str, current: str = "—") -> str:
    """Header above the numpad. The running value shows in a pop-up toast as you
    tap (the message isn't re-edited per digit). ``current`` is the value already
    saved for this item (shown as a reference) — typing a new number REPLACES it,
    or tap 🗑 Kosongkan to unset it."""
    unit_hint = "(kg, boleh 1 titik perpuluhan)" if unit == "kg" else "(pcs, nombor bulat)"
    return "\n".join([
        f"{item_label} — {numpad_prompt(phase)} {unit_hint}",
        f"Sekarang: {current}. Taip nilai baru → ✓ (papar di pop-up atas). 🗑 Kosongkan untuk buang.",
    ])


# --- keyboards ---------------------------------------------------------------
# Built lazily so the pure logic above imports with no telegram dependency.

def _cb(session_id, item_code, action) -> str:
    return f"{CALLBACK_PREFIX}:{session_id}:{item_code}:{action}"


def build_item_keyboard(session_id, outlet_code, entries: dict, phase: str = PHASE_COOKED):
    """One button per item (✓ prefix + value when filled, "—" when empty) plus
    a final Hantar button. Returns an InlineKeyboardMarkup. ``phase`` controls
    the Hantar gating label (the night form only needs one item)."""
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

    if can_submit(entries, outlet_code, phase):
        hantar_label = "📤 Hantar"
    else:
        hantar_label = "📤 Hantar (key sekurang-kurangnya 1 item)"
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
    # Per-item clear — reset this item back to "—" if the wrong item was tapped.
    rows.append([b("🗑 Kosongkan", "clr")])
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


class KitchenPromotionError(Exception):
    """Raised when a submitted form's entries could not be written to
    kitchen_daily_usage at all (so the session is left OPEN for retry)."""


def _rows(result):
    return getattr(result, "data", None) or []


def _upsert_usage_row(client, row: dict) -> None:
    """Write one kitchen_daily_usage row, keyed on
    (outlet_code, business_date, item_code).

    Prefers a native ON CONFLICT upsert (atomic). If that fails — most commonly
    because a hand-created table lacks the UNIQUE(outlet_code, business_date,
    item_code) constraint that ON CONFLICT requires (Postgres 42P10) — it falls
    back to a manual select-then-update/insert so promotion still works. Only a
    genuinely missing table (PGRST205) makes both paths fail, which propagates so
    the caller can surface it."""
    try:
        client.table(USAGE_TABLE).upsert(
            row, on_conflict="outlet_code,business_date,item_code"
        ).execute()
        return
    except Exception as exc:
        if _is_missing_table_error(exc):
            raise
        logger.warning(
            "kitchen: native upsert failed for %s/%s/%s (%s) — falling back to "
            "manual select+update/insert (check the UNIQUE constraint on "
            "kitchen_daily_usage)",
            row.get("outlet_code"), row.get("business_date"), row.get("item_code"), exc,
        )

    existing = _rows(
        client.table(USAGE_TABLE)
        .select("id")
        .eq("outlet_code", row["outlet_code"])
        .eq("business_date", row["business_date"])
        .eq("item_code", row["item_code"])
        .limit(1)
        .execute()
    )
    if existing:
        client.table(USAGE_TABLE).update(row).eq("id", existing[0]["id"]).execute()
    else:
        client.table(USAGE_TABLE).insert(row).execute()


def _is_missing_table_error(exc: Exception) -> bool:
    """True when an error is PostgREST's "table not in the schema cache"
    (PGRST205) for a kitchen table — i.e. migration 0032 isn't applied or the
    schema cache wasn't reloaded. Detected by code/message so it works whether
    the client raises APIError or a plain Exception."""
    code = getattr(exc, "code", None)
    if code == "PGRST205":
        return True
    text = str(getattr(exc, "message", "") or exc).lower()
    return "pgrst205" in text or (
        "schema cache" in text
        and ("kitchen_log_session" in text or "kitchen_daily_usage" in text)
    )


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

    sales_daily_summary keys outlets with a POS S-/D- prefix (e.g. "D-KLANG")
    while the kitchen keys them bare ("KLANG"). Both sides are reduced to one
    join key via ``normalize_outlet_code`` so the join lands for all 10 outlets
    (this is why POS previously read 0). Matches a summary when EITHER its
    outlet_code or its outlet_canonical normalises to the same key."""
    target = normalize_outlet_code(outlet_code)
    if target is None:
        return []
    summaries = _rows(
        client.table(SALES_SUMMARY_TABLE)
        .select("id, outlet_canonical, outlet_code")
        .eq("business_date", str(business_date))
        .execute()
    )
    ids = [
        s["id"] for s in summaries
        if normalize_outlet_code(s.get("outlet_code")) == target
        or normalize_outlet_code(s.get("outlet_canonical")) == target
    ]
    if not ids:
        return []
    rows = _rows(
        client.table(SALES_ITEMWISE_TABLE)
        .select("item_name, qty, category, summary_id")
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
    """Promote a completed phase's entries into kitchen_daily_usage (one row per
    item, upserted on (outlet_code, business_date, item_code)). COOKED writes
    cooked_qty/cooked_by/cooked_at and leaves left_qty NULL; LEFT writes
    left_qty/left_by/left_at and then computes pos_qty + the mismatch flag.

    Raises KitchenPromotionError when NOT A SINGLE row could be written (e.g. the
    table is missing its unique constraint, a schema mismatch, or PGRST205) so
    the caller can tell the user and the session is left OPEN for retry instead
    of being marked submitted with no usage rows. Returns the per-item
    evaluation dicts (LEFT phase only, else [])."""
    outlet_code = session["outlet_code"]
    business_date = str(session["business_date"])
    phase = session["phase"]
    entries = _load_entries(session)
    now_iso = datetime.now(MY_TZ).isoformat()

    # COOKED / LEFT write EVERY item (untouched -> 0) so a submitted form is a
    # complete record for the day. The optional additive night form only writes
    # the items the chef actually keyed (adding 0 would be a no-op anyway).
    if phase == PHASE_COOKED_NIGHT:
        pending = [it for it in items_for_outlet(outlet_code) if entries.get(it["code"]) is not None]
    else:
        pending = items_for_outlet(outlet_code)
    logger.info(
        "kitchen: promoting %d entries to kitchen_daily_usage for %s %s %s",
        len(pending), outlet_code, business_date, phase,
    )

    # The night phase ADDS to the existing cooked_qty, so pre-load the current
    # cooked_qty per item (base 0 when no 6PM row exists yet). The double-add
    # guard is the session itself: a cooked_night session is marked submitted at
    # the end, and the handler/scheduler never re-run a submitted session.
    existing_cooked = {}
    if phase == PHASE_COOKED_NIGHT:
        try:
            for r in _rows(
                client.table(USAGE_TABLE)
                .select("item_code, cooked_qty")
                .eq("outlet_code", outlet_code)
                .eq("business_date", business_date)
                .execute()
            ):
                existing_cooked[r.get("item_code")] = r.get("cooked_qty")
        except Exception:
            logger.warning("kitchen: could not read existing cooked_qty for night add", exc_info=True)

    written = 0
    last_error = None
    for it in pending:
        code, label, unit = it["code"], it["label"], it["unit"]
        value = entries.get(code)
        row = {
            "outlet_code": outlet_code,
            "business_date": business_date,
            "item_code": code,
            "item_label": label,
            "unit": unit,
        }
        if phase == PHASE_COOKED:
            # Untouched items default to 0 (staff key only what they cooked).
            row["cooked_qty"] = value if value is not None else 0
            row["cooked_by"] = submitter
            row["cooked_at"] = now_iso
        elif phase == PHASE_COOKED_NIGHT:
            # Additive: cooked_qty = (existing 6PM cooked, or 0) + night value.
            base = existing_cooked.get(code) or 0
            row["cooked_qty"] = base + value
            row["cooked_by"] = submitter
            row["cooked_at"] = now_iso
        else:
            # Untouched items default to 0 leftover.
            row["left_qty"] = value if value is not None else 0
            row["left_by"] = submitter
            row["left_at"] = now_iso
        try:
            _upsert_usage_row(client, row)
            written += 1
        except Exception as exc:
            last_error = exc
            logger.exception(
                "kitchen: usage write FAILED for %s/%s/%s (%s): %s",
                outlet_code, business_date, code, phase, exc,
            )

    logger.info(
        "kitchen: promotion done — %d/%d rows written to kitchen_daily_usage for %s %s %s",
        written, len(pending), outlet_code, business_date, phase,
    )

    if pending and written == 0:
        # Nothing landed — leave the session OPEN so the data isn't lost behind a
        # "submitted" no-op and the user can simply tap Hantar again after the
        # table is fixed.
        raise KitchenPromotionError(
            "0/%d kitchen_daily_usage rows written for %s %s %s: %s"
            % (len(pending), outlet_code, business_date, phase, last_error)
        )

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


# In-memory numpad buffers so a digit tap never touches the DB. Keyed by
# (chat_id, user_id, session_id, item_code) -> {"buffer": str, "phase": str}.
# Set when an item's numpad is opened, mutated on each digit (memory only),
# written to kitchen_log_session only on ✓ commit. A process restart loses an
# uncommitted half-typed buffer (acceptable) — committed values are persisted.
_numpad_state: dict = {}

_DIGIT_ACTIONS = frozenset({"bs", "dot"})


def _is_numpad_key(action: str) -> bool:
    return (
        action in _DIGIT_ACTIONS
        or (len(action) == 2 and action[0] == "d" and action[1] in DIGITS)
    )


def _action_to_key(action: str) -> str:
    if action == "bs":
        return "bs"
    if action == "dot":
        return "."
    return action[1]  # "d5" -> "5"


def _numpad_key(chat_id, user_id, session_id, item_code):
    return (chat_id, user_id, str(session_id), item_code)


async def _handle_numpad_key(query, session_id, item_code, action) -> None:
    """Fast path for a numpad digit/backspace/dot.

    The running value is shown via the callback ANSWER TOAST (one lightweight
    answerCallbackQuery), NOT by editing the message — a per-digit
    editMessageText is a 300-800ms Telegram round-trip and was the visible lag.
    The message (and its keyboard) are edited only on ✓ commit. The buffer lives
    in memory (no DB per digit). On the common path answer() is the FIRST await,
    so Telegram's spinner clears instantly."""
    t0 = time.monotonic()
    meta = ITEM_BY_CODE.get(item_code)
    if meta is None:
        with contextlib.suppress(Exception):
            await query.answer()
        return
    unit = meta["unit"]
    chat_id = query.message.chat_id if query.message else None
    user_id = query.from_user.id if query.from_user else None
    key = _numpad_key(chat_id, user_id, session_id, item_code)

    st = _numpad_state.get(key)
    if st is None:
        # Memory lost (e.g. restart) — recover the buffer from the DB once (rare),
        # then stay in memory for subsequent taps.
        session = await asyncio.to_thread(get_session, _supabase, session_id)
        if session is None or session.get("status") == "submitted":
            with contextlib.suppress(Exception):
                await query.answer("Sesi dah tamat — tunggu borang baru.")
            return
        st = {"buffer": session.get("buffer") or "", "phase": session.get("phase") or PHASE_COOKED}
        _numpad_state[key] = st

    # In-memory mutation (synchronous, no await) — atomic within this task.
    st["buffer"] = apply_key(st["buffer"], _action_to_key(action), unit)
    display = buffer_display(st["buffer"])

    t_ans = time.monotonic()
    with contextlib.suppress(Exception):
        # Toast shows the running value AND clears the spinner in one call.
        await query.answer(text=f"{meta['label']}: {display}", cache_time=0)
    t_done = time.monotonic()
    logger.info(
        "kitchen numpad %s -> %r: recv->answer_start %.0fms, answer %.0fms (toast, no edit)",
        action, st["buffer"], (t_ans - t0) * 1000, (t_done - t_ans) * 1000,
    )


def _clear_numpad_state(session_id) -> None:
    """Drop any in-memory numpad buffers for a session (on commit / submit)."""
    sid = str(session_id)
    for k in [k for k in _numpad_state if k[2] == sid]:
        _numpad_state.pop(k, None)


async def handle_kitchen_callback(update, context) -> None:
    """Single CallbackQueryHandler for everything under the ``kdu:`` namespace."""
    query = update.callback_query
    if query is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        return

    session_id = parsed["session_id"]
    item_code = parsed["item_code"]
    action = parsed["action"]

    if _supabase is None:
        with contextlib.suppress(Exception):
            await query.answer()
        logger.warning("kitchen callback received but module not initialised")
        return

    # FAST PATH: numpad digit / backspace / dot — answer-toast shows the running
    # value (no message edit, no DB). answer() is the first await here.
    if _is_numpad_key(action):
        await _handle_numpad_key(query, session_id, item_code, action)
        return

    # Non-numpad (open / ✓ commit / Hantar): clear the spinner, then do the work.
    with contextlib.suppress(Exception):
        await query.answer()

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
        if not can_submit(entries, outlet_code, phase):
            if phase == PHASE_COOKED_NIGHT:
                await query.answer("Key sekurang-kurangnya 1 item tambahan dulu.", show_alert=True)
            else:
                await query.answer("Isi semua item dulu sebelum Hantar.", show_alert=True)
            return
        submitter = _submitter_name(query.from_user)
        try:
            evaluations = await asyncio.to_thread(
                finalize_submission, _supabase, session, submitter
            )
        except Exception as exc:
            # Promotion to kitchen_daily_usage failed (table/constraint/schema
            # issue). Leave the form OPEN so they can retry once it's fixed.
            logger.exception("kitchen: finalize_submission failed for session %s", session_id)
            if _is_missing_table_error(exc):
                note = ("Jadual kitchen belum siap dalam DB (PGRST205). "
                        "Cuba tekan Hantar sekali lagi nanti.")
            else:
                note = "Gagal simpan ke pangkalan data. Cuba tekan Hantar sekali lagi."
            with contextlib.suppress(Exception):
                await query.message.reply_text(f"⚠️ {note}")
            return
        _clear_numpad_state(session_id)
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

    # --- open an item's numpad ---
    # Start with an EMPTY buffer (no DB write) and show the already-saved value as
    # a reference; typing a new number REPLACES it, ⌫ edits, 🗑 Kosongkan unsets.
    if action == "open":
        existing = entries.get(item_code)
        current = format_value(existing, unit) if existing is not None else "—"
        chat_id = query.message.chat_id if query.message else None
        user_id = query.from_user.id if query.from_user else None
        _numpad_state[_numpad_key(chat_id, user_id, session_id, item_code)] = {
            "buffer": "", "phase": phase,
        }
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                numpad_text(phase, meta["label"], unit, current),
                reply_markup=build_numpad_keyboard(session_id, item_code, unit),
            )
        return

    # --- 🗑 Kosongkan: unset this item (wrong item tapped) and return to list ---
    if action == "clr":
        entries.pop(item_code, None)
        _clear_numpad_state(session_id)
        await asyncio.to_thread(
            _save_session, _supabase, session_id,
            entries=entries, editing_item=None, buffer="",
        )
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                form_text(phase, business_date, outlet_label, entries, outlet_code),
                reply_markup=build_item_keyboard(session_id, outlet_code, entries, phase),
            )
        return

    # --- ✓ commit: read the in-memory buffer, persist the value to the DB ---
    if action == "ok":
        chat_id = query.message.chat_id if query.message else None
        user_id = query.from_user.id if query.from_user else None
        st = _numpad_state.get(_numpad_key(chat_id, user_id, session_id, item_code))
        buffer = st["buffer"] if st else (session.get("buffer") or "")
        value = commit_value(buffer, unit)
        if value is not None:
            entries[item_code] = value
        _clear_numpad_state(session_id)
        await asyncio.to_thread(
            _save_session, _supabase, session_id,
            entries=entries, editing_item=None, buffer="",
        )
        # The ONLY message edit in the numpad flow — back to the item list.
        t_edit = time.monotonic()
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                form_text(phase, business_date, outlet_label, entries, outlet_code),
                reply_markup=build_item_keyboard(session_id, outlet_code, entries, phase),
            )
        logger.info("kitchen numpad ✓ commit %s=%r: edit %.0fms",
                    item_code, value, (time.monotonic() - t_edit) * 1000)
        return


async def _post_one(application, chat_id, outlet_code, business_date, phase) -> bool:
    """Post a single form to one group. Returns True if a form was sent, False if
    it was skipped because the phase is already submitted. Raises on error (the
    caller decides whether to swallow it)."""
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
        return False
    entries = _load_entries(session)
    # Original tap-button layout: one button per item + Hantar. Entry is via the
    # (fast, in-memory) numpad when an item is tapped.
    msg = await application.bot.send_message(
        chat_id=chat_id,
        text=form_text(phase, business_date, outlet_label, entries, outlet_code),
        reply_markup=build_item_keyboard(session["id"], outlet_code, entries, phase),
    )
    await asyncio.to_thread(_save_session, _supabase, session["id"], message_id=msg.message_id)
    return True


async def post_one_form(application, chat_id, outlet_code, phase=PHASE_COOKED) -> bool:
    """Manually post one COOKED/LEFT form to a single group (used by the owner
    /kitchen_post_now test command). Bypasses the KITCHEN_LOG_ENABLED gate — it
    is an explicit, owner-triggered single post. Raises on DB/Telegram error so
    the command can report it."""
    if _supabase is None:
        raise RuntimeError("supabase not initialised")
    business_date = business_date_for(datetime.now(MY_TZ))
    return await _post_one(application, chat_id, outlet_code, business_date, phase)


async def _post_forms(application, phase: str) -> None:
    """Post the COOKED or LEFT form to every configured kitchen group. No-ops
    cleanly when KITCHEN_LOG_ENABLED is off or no groups resolve, and never lets
    one bad group/DB error abort the whole run (or the scheduler)."""
    from config.kitchen_groups import configured_groups

    enabled = kitchen_log_enabled()
    # Always log that the job fired, so the logs distinguish "flag off" from
    # "table missing" from "nothing resolved".
    logger.info("kitchen %s poster fired, enabled=%s", phase.upper(), enabled)
    if not enabled:
        logger.info(
            "kitchen: KITCHEN_LOG_ENABLED not set — %s post skipped (safety gate)", phase
        )
        return
    if _supabase is None:
        logger.warning("kitchen: supabase not initialised — %s post skipped", phase)
        return
    groups = await asyncio.to_thread(configured_groups, _supabase)
    if not groups:
        logger.info("kitchen: no groups resolved — %s post skipped", phase)
        return

    now_my = datetime.now(MY_TZ)
    business_date = business_date_for(now_my)
    posted = 0
    for chat_id, outlet_code in groups:
        try:
            if await _post_one(application, chat_id, outlet_code, business_date, phase):
                posted += 1
        except Exception as exc:
            if _is_missing_table_error(exc):
                # kitchen_log_session / kitchen_daily_usage not in PostgREST's
                # schema cache (migration 0032 not applied, or schema not
                # reloaded). Log loudly and skip — don't crash the scheduler.
                logger.error(
                    "kitchen: %s post skipped for chat %s (%s) — kitchen tables "
                    "unavailable in PostgREST schema cache. Apply migration 0032 "
                    "and run NOTIFY pgrst, 'reload schema'. (%s)",
                    phase, chat_id, outlet_code, exc,
                )
            else:
                logger.exception("kitchen: failed to post %s form to chat %s", phase, chat_id)
    logger.info("kitchen %s poster done: posted %d/%d group(s)", phase.upper(), posted, len(groups))


async def post_cooked_forms(application) -> None:
    """APScheduler job: 18:00 COOKED form to every kitchen group."""
    await _post_forms(application, PHASE_COOKED)


async def post_night_forms(application) -> None:
    """APScheduler job: 00:00 optional night-cook (additive) form to every group."""
    await _post_forms(application, PHASE_COOKED_NIGHT)


async def post_left_forms(application) -> None:
    """APScheduler job: 02:00 LEFT form to every kitchen group."""
    await _post_forms(application, PHASE_LEFT)


def register_handlers(app) -> None:
    """Register the kdu: callback handler — the per-item tap buttons, the numpad,
    and Hantar all route through it. (The bulk free-text and ForceReply input
    experiments were removed; entry is tap-only.)"""
    from telegram.ext import CallbackQueryHandler

    app.add_handler(
        CallbackQueryHandler(handle_kitchen_callback, pattern=r"^kdu:")
    )
