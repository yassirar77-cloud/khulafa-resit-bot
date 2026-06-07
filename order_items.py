"""Static classification of the v2 canonical items for the order generator.

``item_prices.canonical_item`` carries the 34 v2 canonical keys produced by
``item_canonicalization_v2`` (``ayam``, ``daging``, ``spices_saida`` …). Those
keys have NO unit/category metadata of their own, so the order generator needs a
small, explicit map for three things the spec asks for:

  * KIND — perishable (consumption-replacement, no stockpiling) vs dry (cadence
    cycle quantity) vs exclude (not an orderable supply line). Drives §3.4.
  * DISPLAY — a human label + the unit/pack noun shown on the draft line.
  * ALTERNATES — the cheaper-supplier hints named in the spec (Shree Map Jaya
    for Saida/Balaji spices, Quiwave Oceanic for Fook Leong udang/sotong).

Pure data + tiny pure helpers — no DB, no I/O. Anything not listed here falls
back to dry/NEEDS_REVIEW rather than being silently dropped, matching the
"erratic data → flag, don't guess" guardrail.
"""
from __future__ import annotations

PERISHABLE = "perishable"
DRY = "dry"
EXCLUDE = "exclude"

# Kind per canonical key. Perishable = bought to replace what was used, never
# stockpiled (fresh proteins, seafood, coconut, ice). Dry = bought in cycles
# that cover the whole gap to the next purchase (spices, sauces, gas, packaging).
# Exclude = service/prepared lines that are not a supplier order item.
_KIND: dict[str, str] = {
    # perishable / fresh
    "ayam": PERISHABLE,
    "daging": PERISHABLE,
    "kambing": PERISHABLE,
    "ikan": PERISHABLE,
    "sotong": PERISHABLE,
    "udang": PERISHABLE,
    "kelapa": PERISHABLE,
    "santan": PERISHABLE,
    "bawang_goreng": PERISHABLE,
    "roti": PERISHABLE,
    "capati": PERISHABLE,
    "ais_batu": PERISHABLE,
    # dry goods / cycle-bought
    "ikan_bilis": DRY,
    "kicap": DRY,
    "sos_cili": DRY,
    "sos_tomato": DRY,
    "sos_tiram": DRY,
    "cuka": DRY,
    "asam_jawa": DRY,
    "kerisik": DRY,
    "spices_babas": DRY,
    "spices_saida": DRY,
    "tea_masala": DRY,
    "kopi": DRY,
    "extra_juss": DRY,
    "yogurt": DRY,
    "keropok": DRY,
    "kacang": DRY,
    "tepung_roti": DRY,
    "drinks": DRY,
    "gas": DRY,
    "cleaning": DRY,
    # not an order line
    "nasi_lemak": EXCLUDE,
    "transport": EXCLUDE,
}

# Human label + the pack/unit noun for the draft line. Unit is best-effort: the
# receipt data does not store pack size, so this is the *display* noun only and
# the manager still confirms the real pack (see DEFAULT_PACK).
_DISPLAY: dict[str, tuple[str, str]] = {
    "ayam": ("Ayam", "kg"),
    "daging": ("Daging", "kg"),
    "kambing": ("Kambing", "kg"),
    "ikan": ("Ikan", "kg"),
    "sotong": ("Sotong", "kg"),
    "udang": ("Udang", "kg"),
    "kelapa": ("Kelapa", "biji"),
    "santan": ("Santan", "kg"),
    "bawang_goreng": ("Bawang Goreng", "kg"),
    "roti": ("Roti", "pack"),
    "capati": ("Capati", "pack"),
    "ais_batu": ("Ais", "bag"),
    "ikan_bilis": ("Ikan Bilis", "kg"),
    "kicap": ("Kicap", "btl"),
    "sos_cili": ("Sos Cili", "btl"),
    "sos_tomato": ("Sos Tomato", "btl"),
    "sos_tiram": ("Sos Tiram", "btl"),
    "cuka": ("Cuka", "btl"),
    "asam_jawa": ("Asam Jawa", "pack"),
    "kerisik": ("Kerisik", "pack"),
    "spices_babas": ("Rempah (Babas)", "pack"),
    "spices_saida": ("Rempah (Saida)", "pack"),
    "tea_masala": ("Tea Masala", "pack"),
    "kopi": ("Kopi", "kg"),
    "extra_juss": ("Extra Joss", "pack"),
    "yogurt": ("Yogurt", "pack"),
    "keropok": ("Keropok", "pack"),
    "kacang": ("Kacang", "kg"),
    "tepung_roti": ("Tepung", "kg"),
    "drinks": ("Minuman", "unit"),
    "gas": ("Gas", "tong"),
    "cleaning": ("Pencuci", "btl"),
}

# Known pack/round-up sizes. The receipt data has no unit column, so we keep
# this deliberately empty: every item rounds to whole units and is tagged for
# the manager to confirm the real pack (sack/carton/tin). Populate as pack data
# becomes known — never guess a sack size the kitchen didn't tell us.
DEFAULT_PACK: dict[str, float] = {}

# Cheaper-supplier alternates from the spec. Each rule fires when the item's
# current (most-used) supplier matches one of ``from_suppliers`` — a
# case-insensitive substring match on the canonical/raw merchant string.
_ALTERNATES: list[dict] = [
    {
        "items": {"spices_saida", "spices_babas"},
        "from_suppliers": ("saida", "balaji", "sayidah", "rahman"),
        "alternate": "Shree Map Jaya",
        "note": "Beli dari Shree Map Jaya cycle ni — rempah lagi murah.",
    },
    {
        "items": {"udang", "sotong"},
        "from_suppliers": ("fook leong", "fook leong seafood"),
        "alternate": "Quiwave Oceanic",
        "note": "Cuba Quiwave Oceanic untuk udang/sotong — selalunya lagi murah.",
    },
]


def kind_of(canonical_item) -> str:
    """perishable / dry / exclude for a canonical key. Unknown -> dry (so it is
    still surfaced for review, never silently dropped)."""
    if not isinstance(canonical_item, str):
        return DRY
    return _KIND.get(canonical_item.strip().lower(), DRY)


def is_orderable(canonical_item) -> bool:
    """False for service/prepared lines that should never appear on a supplier
    order draft (transport, nasi lemak)."""
    return kind_of(canonical_item) != EXCLUDE


def display_name(canonical_item) -> str:
    """Human label for the draft line. Falls back to a title-cased key."""
    key = (canonical_item or "").strip().lower()
    if key in _DISPLAY:
        return _DISPLAY[key][0]
    return key.replace("_", " ").title() if key else "?"


def unit_noun(canonical_item) -> str:
    """The pack/unit noun shown after the quantity (best-effort display only)."""
    key = (canonical_item or "").strip().lower()
    if key in _DISPLAY:
        return _DISPLAY[key][1]
    return "unit"


def cheaper_alternate(canonical_item, current_supplier) -> dict | None:
    """Return ``{'alternate', 'note'}`` when a cheaper supplier is known for this
    item AND the current supplier is the pricier one named in the spec. Returns
    ``None`` otherwise (no nagging when they already buy from the cheap source)."""
    key = (canonical_item or "").strip().lower()
    supplier = (current_supplier or "").strip().lower()
    if not key or not supplier:
        return None
    for rule in _ALTERNATES:
        if key not in rule["items"]:
            continue
        # Don't suggest the alternate they're already using.
        if rule["alternate"].lower() in supplier:
            continue
        if any(s in supplier for s in rule["from_suppliers"]):
            return {"alternate": rule["alternate"], "note": rule["note"]}
    return None
