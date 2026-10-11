"""Tunables for the daily "📊 Ringkasan Guna vs POS" comparison.

Everything the owner may want to adjust lives here, not in kitchen_usage.py.
"""
from __future__ import annotations

# --- status icons -------------------------------------------------------------
# diff = guna − POS, pct = diff / POS.
#   POS = 0 and guna > 0                     -> 🔴 always
#   guna = 0 and POS > 0                     -> ⚠️ always
#   pct > +STATUS_PCT and diff ≥ floor       -> 🔴
#   pct < −STATUS_PCT and |diff| ≥ floor     -> ⚠️
#   otherwise                                -> ✅
# guna < 0 is never given a status: it is a key-in error ("❌ Data salah").
STATUS_PCT = 0.15
STATUS_FLOOR = {"pcs": 2.0, "kg": 0.3}

# --- scheduled posting -------------------------------------------------------
# The 09:00/11:00/14:00 job only auto-posts business days on/after this date
# (8–10 Oct are held for the owner's review of the corrected summaries).
# Days before it were compared under the old (wrong) rules and dates; they are
# re-posted only on the owner's say-so with /kitchen_recheck YYYY-MM-DD post.
AUTO_POST_FROM = "2026-10-11"

# --- key-in validation --------------------------------------------------------
# A kg field keyed at/above this is almost certainly grams ("500" for 0.5 kg):
# the bot asks "Maksud 500 g = 0.5 kg?" before saving.
KG_GRAMS_SUSPECT_AT = 50

# --- POS pieces -> kg ---------------------------------------------------------
# POS sells these by the portion; the summed POS count is converted to kg
# before comparing with the weighed guna. Owner-locked values.
KG_PORTION_GRAMS: dict[str, float] = {
    "kambing": 180.0,
    "daging": 60.0,
}

# Telur Ikan is sold on the POS in sized portions. Grams per portion, keyed by
# the normalised POS name. NOT YET SET — no weight has been agreed, so until
# every variant an outlet sells has a weight here, the summary shows the POS
# piece count next to guna kg without a status (never a guessed conversion).
# Variants seen in the last 30 days (all outlets):
#   "telur ikan b", "telur ikan (b)", "telur ikan m", "telur ikan (m)",
#   "telur ikan ikut saiz", "telur ikan rm 12", "nasi telur ikan"
TELUR_IKAN_PORTION_GRAMS: dict[str, float] = {}

# --- POS name normalisation ---------------------------------------------------
# Whole-word typo fixes applied to every POS item name before matching (after
# lower-casing, "_"/"+" -> space and whitespace collapsing). Every entry was
# seen on a real POS in the 30-day audit (docs/pos_item_audit.md).
POS_NAME_TYPOS: dict[str, str] = {
    "awam": "ayam",        # "Briyani Awam Bawang" (SEK14)
    "gorng": "goreng",     # "Mee Gorng Tampa"
    "goring": "goreng",    # "Bihun Goring Daging"
    "sepuruh": "separuh",  # "Nasi Sepuruh Ayam Besar" (KLANG)
    "bunkus": "bungkus",   # "Telur Bunkus Ayam"
    "ayambawang": "ayam bawang",  # "Briyani Ayambawang Telur" (BISTRO7)
    "ayamkari": "ayam kari",      # "AyamKari/Kurma" (KLANG)
    "ayam65": "ayam 65",          # "Nasiputih+ Ayam65" (SEK14)
}

# --- chicken dishes with no lauk button ----------------------------------------
# Ayam dishes that count toward NO kitchen item. They are listed under the
# summary ("POS tiada butang: Rendang 3, Kari 9") so the owner can decide
# whether they belong to a button. Deliberately NOT mapped to any item.
# label -> regex patterns (searched in the normalised name).
UNMAPPED_AYAM_BUCKETS: dict[str, tuple[str, ...]] = {
    "Rendang": ("rendang",),
    "Kari": ("kari", "curry"),
    "Kurma": ("kurma",),
    "Ayam 65": (r"\b65\b",),
    "Masak Merah": ("merah",),
}
