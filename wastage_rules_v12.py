"""The v12 wastage rule set: POS dish name -> theoretical ingredient usage.

Until now only KAMBING (180 g) and DAGING (60 g) had locked portions, so every
other ingredient — including ayam, roughly a third of food cost — came out of the
monthly report as NOT MODELLED. This module encodes the rest.

The rules are owner-locked, not derived. Where a portion was not given, none is
invented: udang, sotong and hati ayam are counted as DISHES with an explicit
"no portion rule" marker rather than converted to grams on a guess. That is the
same discipline `uom_conversion` applies to printed units, for the same reason —
a wrong portion silently corrupts every variance computed from it, while a
missing one is visible and fixable.

Two structural facts drive the design:

* **A dish can consume several ingredients.** "Nasi Goreng Pattaya" is a Thai
  dish (isi ayam), fried (oil), rice-based (beras) and a goreng (one egg). So a
  dish maps to a *set* of ingredient amounts, not to one.
* **The Thai/Mamak split is a sourcing distinction, not a portion one.** Daging
  is 60 g either way; which stock it comes out of (MYSOOR fresh vs MD HANI
  frozen) changes only which purchase line it should be compared against.

Pure functions, no I/O.
"""
from __future__ import annotations

import re
from typing import Any

# --- Thai keywords -----------------------------------------------------------
#: A dish whose name contains any of these is cooked on the Thai line. That
#: drives two things: chicken comes from ISI AYAM fillet rather than whole cuts,
#: and beef comes from the frozen MD HANI stock rather than fresh MYSOOR.
#: Multi-word entries are matched as phrases; the list is owner-locked.
THAI_KEYWORDS: tuple[str, ...] = (
    "basa", "singapore", "seafood", "tomyam", "tom yam", "ladna", "sup",
    "kungfu", "kung fu", "pattaya", "hailam", "bandung", "cina", "kunyit",
    "merah", "paprik", "belacan", "cendawan", "cilipadi", "cili padi",
    "ikan masin", "kampung", "thai", "usa", "udang", "sotong",
)

# --- portions ----------------------------------------------------------------
#: ingredient key -> (portion size, unit). Every value here was given by the
#: owner. An ingredient absent from this map has NO portion rule and is only
#: ever counted in dishes — see UNPORTIONED.
PORTIONS: dict[str, tuple[float, str]] = {
    "isi_ayam": (50.0, "g"),            # Thai-line chicken fillet
    "ayam_whole": (1.0, "pcs"),         # Mamak whole-cut, counted in pieces
    "kambing": (180.0, "g"),            # bone-in
    "daging_mysoor": (60.0, "g"),       # Mamak, fresh
    "daging_md_hani": (60.0, "g"),      # Thai line, frozen
    "tulang": (50.0, "g"),              # soup bones
    "ikan_tenggiri": (180.0, "g"),
    "beras_biasa": (120.0, "g"),        # raw weight
    "beras_basmati": (150.0, "g"),      # raw weight, briyani
    "serbuk_teh": (5.0, "g"),           # per cup
    "serbuk_kopi": (5.0, "g"),          # per cup
    "minyak_masak": (80.0, "ml"),       # per fried dish
    # 1 tin serves 9 drinks (the owner's 8-10 band, midpoint locked), so one
    # drink consumes 1/9 of a tin. 1 carton = 24 tins is a purchase-side
    # conversion and belongs in uom_conversion, not here.
    "susu_pekat": (1.0 / 9.0, "tin"),
    "susu_cair": (1.0 / 9.0, "tin"),
    "telur": (1.0, "pcs"),
}

#: Ingredients the rules identify but give NO portion for. They are reported as
#: dish counts so the demand is visible, and never converted to a quantity.
UNPORTIONED: frozenset[str] = frozenset({"udang", "sotong", "hati_ayam"})

#: ingredient key -> the ``receipt_items.canonical_item`` its purchases land
#: under. ``None`` means canonicalization v2 has no category for it, so there is
#: nothing on the purchase side to compare against — reported explicitly rather
#: than shown as a zero.
CANONICAL_BY_INGREDIENT: dict[str, str | None] = {
    "isi_ayam": "ayam",
    "ayam_whole": "ayam",
    "hati_ayam": "ayam",
    "kambing": "kambing",
    "daging_mysoor": "daging",
    "daging_md_hani": "daging",
    "ikan_tenggiri": "ikan",
    "udang": "udang",
    "sotong": "sotong",
    "serbuk_teh": "tea_masala",
    "serbuk_kopi": "kopi",
    "tulang": None,
    "beras_biasa": None,
    "beras_basmati": None,
    "minyak_masak": None,
    "susu_pekat": None,
    "susu_cair": None,
    "telur": None,
}

# --- chicken -----------------------------------------------------------------
#: Whole-cut chicken types. BAWANG and REMPAH are the SAME cut and are
#: interchangeable on the line, so they share one bucket — counting them
#: separately would imply a distinction the kitchen does not make.
CHICKEN_TYPE_BAWANG_REMPAH = "bawang_rempah"
CHICKEN_TYPE_TANDOORI = "tandoori"
CHICKEN_TYPE_HATI = "hati"
CHICKEN_TYPE_DEFAULT = "default"

#: One physical piece is cut into three servings for these styles.
_THIRD_PIECE_STYLES = ("rendang", "kurma", "kari", "curry")
_DOUBLE_RE = re.compile(r"\b(double|dbl)\b", re.IGNORECASE)

#: A Thai dish is NOT isi ayam when it is meat-free by name, or when another
#: protein is clearly the main one.
_ISI_AYAM_EXCLUDE_WORDS = ("sayur", "kosong")

# --- eggs --------------------------------------------------------------------
#: Dishes that name an egg preparation. Each contributes one egg (two if the
#: dish is doubled). The owner confirmed there is no generic "Nasi Goreng
#: Telur" SKU, so a bare "telur" in a name is NOT treated as a named egg dish.
NAMED_EGG_DISHES: tuple[str, ...] = (
    "telur mata", "telur dadar", "telur bistik", "roti telur", "tosai telur",
    "thosai telur",
)

#: Deep-fried dishes use no egg despite carrying "goreng".
DEEP_FRY_DISHES: tuple[str, ...] = ("ayam goreng", "ikan goreng", "kailan goreng")

# --- oil ---------------------------------------------------------------------
_OIL_TRIGGERS = ("goreng", "fried", "tandoori", "tandori", "roti")
#: Breads cooked on a dry griddle, and toast. No cooking oil.
_OIL_EXCLUSIONS = ("capati", "chapati", "chapatti", "naan", "roti bakar")

# --- rice --------------------------------------------------------------------
_BASMATI_TRIGGERS = ("briyani", "beriani", "biryani")
_RICE_TRIGGERS = ("nasi",)

# --- drinks ------------------------------------------------------------------
_TEH_TRIGGERS = ("teh", "tea")
_KOPI_TRIGGERS = ("kopi", "coffee", "nescafe")
#: "Teh O" / "Kopi O" — no milk and no sugar.
_O_VARIANT_RE = re.compile(r"\bo\b", re.IGNORECASE)
#: "Teh C" / "Kopi C" — evaporated (cair) milk instead of the default condensed.
_C_VARIANT_RE = re.compile(r"\bc\b", re.IGNORECASE)

# --- other proteins ----------------------------------------------------------
_KAMBING_WORDS = ("kambing", "mutton")
_DAGING_WORDS = ("daging", "beef")
_IKAN_WORDS = ("ikan", "fish", "tenggiri", "tengirri")
#: "ikan masin" (salted fish) and "ikan bilis" (anchovies) are seasonings, not
#: the main protein — without stripping them, every Thai "ikan masin" dish would
#: be read as a fish dish and wrongly excluded from isi ayam.
_IKAN_NOT_MAIN = ("ikan masin", "ikan bilis")
_SOUP_WORDS = ("sup", "soup", "tulang")
_SEAFOOD_WORDS = ("seafood",)
_UDANG_WORDS = ("udang", "prawn")
_SOTONG_WORDS = ("sotong", "squid")
_STAFF_WORDS = ("staff",)


def _normalise(name: Any) -> str:
    """Lower-case with runs of whitespace collapsed, for keyword matching."""
    if not isinstance(name, str):
        return ""
    return re.sub(r"\s+", " ", name.strip().lower())


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def is_thai_dish(name: Any) -> bool:
    """True when the dish name carries any owner-locked Thai keyword."""
    return _has(_normalise(name), THAI_KEYWORDS)


def is_staff_meal(name: Any) -> bool:
    """Staff meals never consume counted stock (v12 keeps them out entirely)."""
    return _has(_normalise(name), _STAFF_WORDS)


def main_protein(name: Any) -> str | None:
    """The dish's main protein: 'kambing' / 'daging' / 'ikan' / 'ayam' / None.

    Checked in that order because a dish naming two proteins is named for its
    main one first in practice ("Nasi Kambing Daging" is a kambing dish).
    """
    text = _normalise(name)
    if _has(text, _KAMBING_WORDS):
        return "kambing"
    if _has(text, _DAGING_WORDS):
        return "daging"
    # Strip the seasoning phrases before asking whether this is a fish dish.
    fish_text = text
    for phrase in _IKAN_NOT_MAIN:
        fish_text = fish_text.replace(phrase, " ")
    if _has(fish_text, _IKAN_WORDS):
        return "ikan"
    if "ayam" in text:
        return "ayam"
    return None


def chicken_type(name: Any) -> str:
    """Which whole-cut chicken a Mamak dish draws on."""
    text = _normalise(name)
    if "hati" in text:
        return CHICKEN_TYPE_HATI
    if "tandoori" in text or "tandori" in text:
        return CHICKEN_TYPE_TANDOORI
    if "bawang" in text or "rempah" in text:
        return CHICKEN_TYPE_BAWANG_REMPAH
    return CHICKEN_TYPE_DEFAULT


def chicken_pieces(name: Any) -> float:
    """Pieces of whole-cut chicken one serving of this dish consumes.

    1 by default; 2 when the dish is doubled; 1/3 for rendang / kurma / kari,
    where one physical piece is cut into three servings. The two compose — a
    doubled rendang is 2/3 of a piece.
    """
    text = _normalise(name)
    pieces = 1.0
    if any(style in text for style in _THIRD_PIECE_STYLES):
        pieces /= 3.0
    if _DOUBLE_RE.search(text):
        pieces *= 2.0
    return pieces


def uses_isi_ayam(name: Any) -> bool:
    """True when a dish draws on the Thai line's ISI AYAM fillet.

    Every Thai-keyword dish does — including the ones with no protein in the
    name at all (Maggi Sup, Nasi Goreng Pattaya, Kueyteow Kungfu, Maggi Goreng
    Basa), which is the point of the rule: the Thai line puts fillet in them.

    Excluded: dishes named 'sayur' or 'kosong' (deliberately meat-free), and
    dishes whose main protein is daging, kambing or ikan.
    """
    text = _normalise(name)
    if not is_thai_dish(text):
        return False
    if _has(text, _ISI_AYAM_EXCLUDE_WORDS):
        return False
    if main_protein(text) in ("daging", "kambing", "ikan"):
        return False
    return True


def egg_count(name: Any) -> float:
    """Eggs one serving consumes.

    A goreng contributes one egg; a named egg dish contributes one more (two if
    doubled). Deep-fried dishes carry "goreng" but use no egg, so they cancel
    the baseline.
    """
    text = _normalise(name)
    if _has(text, DEEP_FRY_DISHES):
        baseline = 0.0
    else:
        baseline = 1.0 if "goreng" in text else 0.0
    named = 0.0
    if _has(text, NAMED_EGG_DISHES):
        named = 2.0 if _DOUBLE_RE.search(text) else 1.0
    return baseline + named


def uses_cooking_oil(name: Any) -> bool:
    """True for goreng / fried / tandoori / roti dishes, minus the dry breads."""
    text = _normalise(name)
    if _has(text, _OIL_EXCLUSIONS):
        return False
    return _has(text, _OIL_TRIGGERS)


def drink_components(name: Any) -> dict[str, float]:
    """Powder and milk a drink consumes, in servings (portions applied later).

    Condensed milk (susu pekat) is the default in every standard tarik drink;
    a 'C' variant swaps it for evaporated (susu cair); an 'O' variant has
    neither milk nor sugar.
    """
    text = _normalise(name)
    is_teh = _has(text, _TEH_TRIGGERS)
    is_kopi = _has(text, _KOPI_TRIGGERS)
    if not (is_teh or is_kopi):
        return {}

    out: dict[str, float] = {}
    # "teh masala"/"tea masala" is the powder itself, and kopi wins when a name
    # somehow carries both (e.g. "Kopi Teh Campur") — the cup is one drink.
    out["serbuk_kopi" if is_kopi else "serbuk_teh"] = 1.0

    if _O_VARIANT_RE.search(text):
        return out                      # O: no milk, no sugar
    if _C_VARIANT_RE.search(text):
        out["susu_cair"] = 1.0
    else:
        out["susu_pekat"] = 1.0
    return out


def rice_component(name: Any) -> str | None:
    """Which rice a dish draws on: basmati for briyani, else plain."""
    text = _normalise(name)
    if _has(text, _BASMATI_TRIGGERS):
        return "beras_basmati"
    if _has(text, _RICE_TRIGGERS):
        return "beras_biasa"
    return None


def seafood_components(name: Any) -> list[str]:
    """Udang / sotong a dish calls for. No portion exists for either.

    A 'seafood'-named dish takes both; a dish naming udang or sotong directly
    takes that one.
    """
    text = _normalise(name)
    out: list[str] = []
    if _has(text, _SEAFOOD_WORDS):
        return ["udang", "sotong"]
    if _has(text, _UDANG_WORDS):
        out.append("udang")
    if _has(text, _SOTONG_WORDS):
        out.append("sotong")
    return out


def servings_for_dish(name: Any) -> dict[str, float]:
    """SERVINGS of each ingredient one unit of this dish consumes.

    Servings, not quantities: multiply by :data:`PORTIONS` to get grams/ml/tins.
    Ingredients in :data:`UNPORTIONED` stay as serving counts because no portion
    rule exists for them.

    Returns ``{}`` for a staff meal — those are excluded from the comparison
    entirely, exactly as in the daily v12 rules.
    """
    text = _normalise(name)
    if not text or is_staff_meal(text):
        return {}

    out: dict[str, float] = {}

    def add(key: str, amount: float) -> None:
        if amount:
            out[key] = out.get(key, 0.0) + amount

    protein = main_protein(text)
    thai = is_thai_dish(text)

    # --- proteins ---
    if uses_isi_ayam(text):
        add("isi_ayam", 1.0)
    elif protein == "ayam":
        cut = chicken_type(text)
        if cut == CHICKEN_TYPE_HATI:
            add("hati_ayam", 1.0)       # liver: no portion rule
        else:
            add("ayam_whole", chicken_pieces(text))

    if protein == "daging":
        add("daging_md_hani" if thai else "daging_mysoor", 1.0)
    elif protein == "kambing":
        add("kambing", 1.0)
    elif protein == "ikan":
        add("ikan_tenggiri", 1.0)

    if _has(text, _SOUP_WORDS):
        add("tulang", 1.0)

    for component in seafood_components(text):
        add(component, 1.0)

    # --- staples ---
    rice = rice_component(text)
    if rice:
        add(rice, 1.0)
    if uses_cooking_oil(text):
        add("minyak_masak", 1.0)
    add("telur", egg_count(text))

    # --- drinks ---
    for key, amount in drink_components(text).items():
        add(key, amount)

    return out


def usage_for_dish(name: Any, qty: float = 1.0) -> dict[str, dict]:
    """Ingredient usage for ``qty`` servings of a dish.

    Returns ``{ingredient: {"amount", "unit", "servings", "portioned"}}``.
    ``portioned`` is False for udang / sotong / hati ayam, whose ``amount`` is a
    serving count and whose ``unit`` is ``"dishes"`` — never grams.
    """
    try:
        servings = float(qty)
    except (TypeError, ValueError):
        return {}
    if servings <= 0:
        return {}

    out: dict[str, dict] = {}
    for key, per_serving in servings_for_dish(name).items():
        total_servings = per_serving * servings
        portion = PORTIONS.get(key)
        if portion is None:
            out[key] = {"amount": total_servings, "unit": "dishes",
                        "servings": total_servings, "portioned": False}
        else:
            size, unit = portion
            out[key] = {"amount": total_servings * size, "unit": unit,
                        "servings": total_servings, "portioned": True}
    return out


def explain_dish(name: Any) -> dict:
    """Every rule verdict for one dish, for auditing a surprising number."""
    text = _normalise(name)
    return {
        "name": name,
        "normalised": text,
        "staff_meal": is_staff_meal(text),
        "thai": is_thai_dish(text),
        "main_protein": main_protein(text),
        "isi_ayam": uses_isi_ayam(text),
        "chicken_type": chicken_type(text) if main_protein(text) == "ayam" else None,
        "chicken_pieces": chicken_pieces(text) if main_protein(text) == "ayam" else None,
        "eggs": egg_count(text),
        "oil": uses_cooking_oil(text),
        "rice": rice_component(text),
        "seafood": seafood_components(text),
        "drink": drink_components(text),
        "servings": servings_for_dish(text),
    }
