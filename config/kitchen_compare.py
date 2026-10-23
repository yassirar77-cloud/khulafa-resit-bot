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
# A kg field keyed at/above this is grams ("500" for 0.5 kg): ✓ saves it as
# kg (÷1000) and says so. It no longer asks — the "500 kg (betul)" answer was
# being tapped as "500, correct" and 697 rows went in as hundreds of kg
# (Jun–Oct 2026). Daily record for either item is 6.25 kg (KLANG).
KG_GRAMS_SUSPECT_AT = 50

# --- POS pieces -> kg ---------------------------------------------------------
# POS sells these by the portion; the summed POS count is converted to kg
# before comparing with the weighed guna. Owner-locked values.
KG_PORTION_GRAMS: dict[str, float] = {
    "kambing": 180.0,
    "daging": 60.0,
}

# Telur Ikan is keyed in pcs and every POS size (B, M, Ikut Saiz, Rm 12,
# "Nasi Telur Ikan") counts as 1 pc — owner decision, Oct 2026.

# POS items that are curry GRAVY only (no meat) and must count toward no
# button. Normalised names (pos_names.normalize_pos_name). Owner rule (Oct
# 2026): a "Kari Ayam/Kambing" item counts toward Ayam Kari/Rendang only when
# its POS price is RM4 or more (i.e. it includes meat); under RM4 it is gravy.
GRAVY_ONLY_NAMES: frozenset = frozenset({
    "kari bungkus",
    "kua kari",              # KLANG
    "nasi putih ( kari )",   # SBESI, SEK6
    "kari ayam/kambing",     # SEK15 — avg RM2.59 (Aug–Oct 2026) < RM4 -> gravy
})

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
# Ayam dishes that count toward NO kitchen item, listed under the summary as
# "POS tiada butang: X n" so the owner can decide. Empty since Oct 2026: the
# former Rendang/Kari/Kurma/Masak Merah buckets are the "Ayam Kari/Rendang"
# button and Ayam 65 has its own button.
# label -> regex patterns (searched in the normalised name).
UNMAPPED_AYAM_BUCKETS: dict[str, tuple[str, ...]] = {}
