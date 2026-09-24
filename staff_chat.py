"""Natural staff chat — the bot checking on each shop like an owner doing rounds.

The division of labour is strict:

* this code decides WHICH question to ask and WHICH facts go in, straight
  from the database (order drafts, cook-plan forecast, missing bills);
* the AI provider (``staff_ai``) only phrases it in the cashier's language;
* ``fact_check`` then rejects any wording that carries a number, item,
  supplier, money figure or name that isn't in the facts — and the plain
  template is sent instead. No invented numbers, ever.

``STAFF_CHAT_STYLE`` (Render env):
  classic  (default)  nothing changes; the check-in jobs do nothing
  preview             every check-in is written for every outlet and sent to
                      the DIRECTOR chat only, one digest per check-in, so the
                      wording can be read before any shop sees it

Every generated message is logged to ``staff_chat_log`` with the facts it
was given (see migrations/0044_staff_chat.sql).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime

import cashier_names
import staff_ai

logger = logging.getLogger(__name__)

LOG_TABLE = "staff_chat_log"

CLASSIC = "classic"
PREVIEW = "preview"
STYLES = (CLASSIC, PREVIEW)

# --- languages ---------------------------------------------------------------

BM_TAMIL = "bm_tamil"
LANGUAGES = ("tamil", "bm", "bengali", "english", "indonesian", BM_TAMIL)
DEFAULT_LANGUAGE = BM_TAMIL

_LANG_ALIASES = {
    "tamil": "tamil", "ta": "tamil",
    "bm": "bm", "malay": "bm", "melayu": "bm", "bahasa": "bm",
    "bengali": "bengali", "bangla": "bengali", "bn": "bengali",
    "english": "english", "en": "english", "eng": "english",
    "indonesian": "indonesian", "indo": "indonesian", "id": "indonesian",
    "bm_tamil": BM_TAMIL, "mix": BM_TAMIL, "bm+tamil": BM_TAMIL,
}

_KEEP_FACTS_LATIN = (
    " Keep every item name, supplier name, unit and number EXACTLY as given in "
    "the facts: English letters and 0-9 digits (e.g. 'Ayam 75kg', 'Bestari "
    "Farm') — never translate or transliterate them, so the cashier can match "
    "them to the bill."
)
_LANG_PROMPT = {
    "tamil": "simple spoken Malaysian Tamil in TAMIL SCRIPT, as a boss types "
             "to his cashier on WhatsApp. Always the RESPECTFUL form "
             "(சொல்லுங்க, பாருங்க, பாத்தீங்களா, போடுங்க) — never the informal "
             "one (சொல்லு, பாரு, பாத்தியா, நீ). Everyday English/Malay trade "
             "words (order, bill, draft, Lunch) are fine." + _KEEP_FACTS_LATIN,
    "bm": "simple spoken Malaysian Malay (Bahasa Malaysia), kedai register."
          + _KEEP_FACTS_LATIN,
    "bengali": "simple spoken Bengali written in ENGLISH LETTERS (Banglish), "
               "the way a Bangladeshi worker in Malaysia types it, e.g. 'Aaj "
               "order thik ache?'. Never use Bengali script." + _KEEP_FACTS_LATIN,
    "english": "simple, plain English for a non-native speaker." + _KEEP_FACTS_LATIN,
    "indonesian": "simple spoken Bahasa Indonesia." + _KEEP_FACTS_LATIN,
    BM_TAMIL: "one short line of simple Malay, then the same in simple spoken "
              "Tamil in TAMIL SCRIPT on the next line, always in the respectful "
              "form (சொல்லுங்க, பாருங்க)." + _KEEP_FACTS_LATIN,
}

# Scripts each language may be written in (beyond English letters).
_TAMIL_SCRIPT = re.compile(r"[\u0B80-\u0BFF]")
_BENGALI_SCRIPT = re.compile(r"[\u0980-\u09FF]")
_ALLOWED_SCRIPTS = {"tamil": {"tamil"}, BM_TAMIL: {"tamil"}}

# Informal Tamil (talking down to staff) — the check-ins always use the
# respectful form: சொல்லுங்க not சொல்லு, பாத்தீங்களா not பாத்தியா.
_TAMIL_CHARS = "\u0B80-\u0BFF"
_INFORMAL_TAMIL = (
    "சொல்லு", "பாரு", "போடு", "பண்ணு", "அனுப்பு", "எடு", "குடு", "கொடு",
    "செய்", "தட்டு", "வா", "போ", "நீ", "உன்", "உனக்கு", "உன்னோட",
    "பாத்தியா", "பார்த்தியா", "சொன்னியா", "போட்டியா", "பண்ணியா",
    "செஞ்சியா", "வந்தியா", "இருக்கியா", "அனுப்பியா", "எடுத்தியா",
)
_INFORMAL_RE = re.compile(
    "(?<![%s])(%s)(?![%s])" % (
        _TAMIL_CHARS, "|".join(map(re.escape, _INFORMAL_TAMIL)), _TAMIL_CHARS
    )
)


def informal_tamil(text) -> list[str]:
    """Informal (non-respectful) Tamil words in ``text``."""
    return sorted(set(_INFORMAL_RE.findall(str(text or ""))))


# No-data check-ins (open 08:00, lunch 15:00, night 23:00) must never say
# lunch or the shift is over: "Lunch முடிஞ்சுது" slipped through, and a
# morning opening once said the shift had finished. Rejected outright.
# Questions such as "முடிஞ்சுதா?" (did it run out?) are fine.
_OVER_RE = re.compile(
    "|".join([
        "(?:முடிஞ்சுது|முடிந்தது|முடிஞ்சாச்சு|முடிந்துவிட்டது|முடிஞ்சிடுச்சு)(?![%s])"
        % _TAMIL_CHARS,
        r"\b(?:lunch|makan tengah hari|shift|syif)\s+(?:dah|sudah|telah)\s+"
        r"(?:habis|tamat|siap|selesai)\b",
        r"\b(?:lepas|selepas|habis)\s+lunch\b",
        r"\b(?:lunch|shift)\s+(?:is\s+)?(?:over|finished|done|ended)\b",
        r"\bafter\s+lunch\b",
        r"\bend of (?:the )?shift\b",
    ]),
    re.IGNORECASE,
)
OVER_SLOTS = ("open", "lunch", "night")


def said_over(text) -> list[str]:
    """Phrases in ``text`` that say lunch / the shift is finished."""
    return sorted({m.group(0) for m in _OVER_RE.finditer(str(text or ""))})


# Fact values that must appear verbatim (so they stay in English letters and
# 0-9 digits, never translated): per slot, the keys that carry them.
_REQUIRED_FACTS = {
    "stock": ("item", "qty", "supplier"),
    "cook": ("item", "cook"),
    "bills": ("supplier", "days"),
}


def normalize_language(word) -> str | None:
    return _LANG_ALIASES.get(str(word or "").strip().lower())


def style() -> str:
    raw = (os.environ.get("STAFF_CHAT_STYLE") or "").strip().lower()
    return raw if raw in STYLES else CLASSIC


# --- check-in slots ----------------------------------------------------------

MORNING, NIGHT = cashier_names.MORNING, cashier_names.NIGHT

# slot -> (shift, scheduled time "HH:MM", what the question is for)
SLOTS: dict[str, tuple[str, str, str]] = {
    "open": (MORNING, "08:00", "start of the morning shift: is the shop "
             "ready, is anything short today"),
    "stock": (MORNING, "10:35", "stock check on ONE key item from today's "
              "order draft: will it last until tonight"),
    "cook": (MORNING, "11:05", "today's cook plan for ONE dish: how much to "
             "cook versus what they usually cook"),
    "lunch": (MORNING, "15:00", "ask exactly two questions and nothing "
              "else: how was the lunch crowd, and did any dish run out "
              "(sell out) early. Do not say or suggest that lunch or the "
              "shift is finished or over; do not add any other remark"),
    "order": (NIGHT, "20:05", "tomorrow's order draft: confirm or change "
              "(the main items are listed)"),
    "bills": (NIGHT, "21:05", "a regular supplier's bill hasn't been "
              "uploaded: is there a bill, please upload it"),
    "night": (NIGHT, "23:00", "late-night check: is everything OK, "
              "anything broken or finished"),
}
DATA_SLOTS = ("stock", "cook", "order", "bills")

# Key items asked about first, in order.
PRIORITY_ITEMS = ("ayam", "ikan", "kambing", "daging", "sotong", "udang", "telur")


# --- templates ---------------------------------------------------------------
# The plain fallback, and the reference the AI is asked to rephrase. Bodies
# only: in a group the bot client prepends "<cashier>," itself.

_T: dict[str, dict[str, str]] = {
    # Two wordings each for BM and Tamil, picked by day + outlet, so the
    # plain fallback doesn't read the same every day either. Item names,
    # suppliers, units and numbers stay in English letters and 0-9 digits in
    # every language — the cashier matches them to the bill, and the fact
    # check matches them to the data.
    "bm": {
        "open": ["Pagi 👋 Kedai dah siap? Ada barang kurang hari ni?",
                 "Kedai dah buka? 👋 Hari ni ada apa-apa yang kurang?"],
        "stock": ["{item} cukup sampai malam? Draft hari ni {qty} {pack} dari {supplier}.",
                  "Draft hari ni {item} {qty} {pack} dari {supplier}. Cukup sampai malam?"],
        "cook_cut": ["{item} hari ni masak {cook} {unit} cukup.{usual_bm} Ok?",
                     "{item} hari ni rasanya {cook} {unit} dah cukup.{usual_bm} Ok tak?"],
        "cook_raise": ["{item} hari ni masak {cook} {unit}, lebih sikit dari biasa.{usual_bm} Ok?",
                       "{item} hari ni masak lebih sikit, {cook} {unit}.{usual_bm} Ok tak?"],
        "lunch": ["Lunch tadi ramai? Ada lauk habis awal?",
                  "Ramai tak masa lunch tadi? Ada lauk yang habis awal?"],
        "order_ask": ["Esok nak order apa? Bagitau barang & berapa ya.",
                      "Untuk esok, nak order apa? Senaraikan barang & kuantiti ya."],
        "order": ["Order esok: {list}{more_bm}. Ok atau nak tukar?",
                  "Untuk esok: {list}{more_bm}. Ok ke, ada nak ubah?"],
        "bills": ["Bil {supplier} dah {days} hari tak masuk (last {last}). Ada bil? Tolong upload 🙏",
                  "{supplier} punya bil belum masuk, dah {days} hari (last {last}). Kalau ada, tolong upload 🙏"],
        "night": ["Malam ni ok? Ada barang rosak atau habis?",
                  "Malam ni semua ok? Ada yang rosak atau dah habis?"],
    },
    "tamil": {
        "open": ["காலை வணக்கம் 👋 கடை ரெடியா? இன்னைக்கு ஏதாவது சாமான் குறைவா?",
                 "கடை திறந்தாச்சா? 👋 இன்னைக்கு ஏதாவது குறைவா இருக்கா?"],
        "stock": ["{item} இரவு வரைக்கும் போதுமா? இன்னைக்கு draft-ல {supplier} {qty} {pack}.",
                  "இன்னைக்கு draft-ல {supplier} {item} {qty} {pack}. இரவு வரைக்கும் போதுமா?"],
        "cook_cut": ["இன்னைக்கு {item} {cook} {unit} சமைச்சா போதும்.{usual_ta} சரியா?",
                     "{item} இன்னைக்கு {cook} {unit} போதும்னு தோணுது.{usual_ta} ஓகேவா?"],
        "cook_raise": ["இன்னைக்கு {item} {cook} {unit} சமைங்க, வழக்கத்தை விட கொஞ்சம் அதிகம்.{usual_ta} சரியா?",
                       "{item} இன்னைக்கு கொஞ்சம் கூட, {cook} {unit} சமைங்க.{usual_ta} ஓகேவா?"],
        "lunch": ["மதியம் கூட்டம் எப்படி இருந்துச்சு? ஏதாவது கறி சீக்கிரமே தீர்ந்து போச்சா?",
                  "Lunch நேரத்துல கூட்டம் எப்படி? ஏதாவது dish சீக்கிரமே தீர்ந்துடுச்சா?"],
        "order_ask": ["நாளைக்கு என்ன order பண்ணணும்? சாமானும் அளவும் சொல்லுங்க.",
                      "நாளைக்கு order-க்கு என்ன வேணும்? சாமான், அளவு சொல்லுங்க."],
        "order": ["நாளைக்கு order: {list}{more_ta}. சரியா, மாத்தணுமா?",
                  "நாளைக்கான order: {list}{more_ta}. இது ஓகேவா, ஏதாவது மாத்தணுமா?"],
        "bills": ["{supplier} bill {days} நாளா வரல (கடைசி {last}). இருக்கா? Upload பண்ணுங்க 🙏",
                  "{supplier} bill இன்னும் வரல, {days} நாள் ஆச்சு (கடைசி {last}). இருந்தா photo போடுங்க 🙏"],
        "night": ["இன்னைக்கு ராத்திரி எல்லாம் சரியா? ஏதாவது உடைஞ்சதா, தீர்ந்ததா?",
                  "ராத்திரி எப்படி போகுது? ஏதாவது பிரச்சனை, தீர்ந்த சாமான் இருக்கா?"],
    },
    "english": {
        "open": "Morning 👋 Shop ready? Anything short today?",
        "stock": "{item} enough till tonight? Today's draft: {qty} {pack} from {supplier}.",
        "cook_cut": "Today {item}: cooking {cook} {unit} is enough.{usual_en} OK?",
        "cook_raise": "Today {item}: cook {cook} {unit}, a bit more than usual.{usual_en} OK?",
        "lunch": "How was the lunch crowd? Did any dish run out early?",
        "order_ask": "What do we need to order for tomorrow? Tell me the items and quantities.",
        "order": "Tomorrow's order: {list}{more_en}. OK or change?",
        "bills": "{supplier} bill not in for {days} days (last {last}). Got it? Please upload 🙏",
        "night": "All OK tonight? Anything broken or finished?",
    },
    "indonesian": {
        "open": "Pagi 👋 Toko sudah siap? Ada barang yang kurang hari ini?",
        "stock": "{item} cukup sampai malam? Draft hari ini {qty} {pack} dari {supplier}.",
        "cook_cut": "{item} hari ini masak {cook} {unit} cukup.{usual_id} Oke?",
        "cook_raise": "{item} hari ini masak {cook} {unit}, sedikit lebih dari biasa.{usual_id} Oke?",
        "lunch": "Makan siang tadi ramai? Ada lauk yang cepat habis?",
        "order_ask": "Besok mau order apa? Kasih tahu barang dan jumlahnya ya.",
        "order": "Order besok: {list}{more_id}. Oke atau mau ganti?",
        "bills": "Nota {supplier} sudah {days} hari belum masuk (terakhir {last}). Ada notanya? Tolong upload 🙏",
        "night": "Malam ini aman? Ada barang rusak atau habis?",
    },
    "bengali": {
        "open": "Suprobhat 👋 Dokan ready? Aaj kichu kom ache?",
        "stock": "{item} raat porjonto cholbe? Aajker draft: {supplier} theke {qty} {pack}.",
        "cook_cut": "Aaj {item} {cook} {unit} ranna korlei hobe.{usual_bn} Thik ache?",
        "cook_raise": "Aaj {item} {cook} {unit} ranna korun, shadharon er cheye ektu beshi.{usual_bn} Thik ache?",
        "lunch": "Dupure bhir kemon chilo? Kono torkari taratari shesh hoyeche?",
        "order_ask": "Kal ki order korte hobe? Jinish ar koto lagbe bolen.",
        "order": "Kalker order: {list}{more_bn}. Thik ache, na bodlaben?",
        "bills": "{supplier} er bill {days} din ashe nai (shesh {last}). Bill ache? Upload korun 🙏",
        "night": "Aaj raate shob thik? Kichu bhengeche ba shesh hoyeche?",
    },
}

_USUAL = {
    "usual_bm": " Biasa masak {usual}.",
    "usual_ta": " வழக்கமா {usual} சமைப்பீங்க.",
    "usual_en": " Usually you cook {usual}.",
    "usual_id": " Biasanya masak {usual}.",
    "usual_bn": " Shadharonto {usual} ranna hoy.",
}
_MORE = {
    "more_bm": " (+{n} lagi)", "more_ta": " (+{n})", "more_en": " (+{n} more)",
    "more_id": " (+{n} lagi)", "more_bn": " (+{n} aro)",
}


def _qty_pack(qty, pack) -> str:
    """'75kg' but '50 biji', '6 kotak'."""
    pack = str(pack or "")
    return f"{qty}{pack}" if pack.lower() in ("kg", "g", "l", "ml") else f"{qty} {pack}".strip()


def _template_key(slot: str, facts: dict) -> str:
    if slot == "cook":
        return "cook_raise" if facts.get("action") == "RAISE" else "cook_cut"
    if slot == "order" and facts.get("ask"):
        return "order_ask"
    return slot


def render_template(slot: str, language: str, facts: dict, variant: int = 0) -> str:
    """The plain message for a slot, in ``language``; ``variant`` picks one
    of the wordings where a language has several. ``""`` if facts are
    missing for a data slot. Never raises."""
    try:
        facts = facts or {}
        values = dict(facts)
        items = facts.get("items") or []
        values["list"] = ", ".join(
            f"{i['item']} {_qty_pack(i['qty'], i['pack'])}" for i in items
        )
        more = int(facts.get("more") or 0)
        for key, fmt in _MORE.items():
            values[key] = fmt.format(n=more) if more else ""
        usual = facts.get("usual")
        for key, fmt in _USUAL.items():
            values[key] = fmt.format(usual=usual) if usual not in (None, "") else ""
        key = _template_key(slot, facts)

        def one(lang):
            options = _T[lang][key]
            if isinstance(options, str):
                options = [options]
            return options[variant % len(options)].format(**values)

        if language == BM_TAMIL:
            return f"{one('bm')}\n{one('tamil')}"
        return one(language if language in _T else "bm")
    except (KeyError, ValueError, IndexError, TypeError):
        logger.exception("staff chat: template failed (slot=%s)", slot)
        return ""


# --- facts from the data -----------------------------------------------------

def fmt_qty(value, unit=None) -> str:
    """Numbers as the cashier should read them: whole pieces, kg to 0.5."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if unit in ("kg",):
        v = round(v * 2) / 2
    else:
        v = round(v)
    return f"{v:g}"


def item_label(canonical) -> str:
    code = str(canonical or "").strip()
    try:
        import kitchen_usage
        meta = kitchen_usage.ITEM_BY_CODE.get(code)
        if meta:
            return meta["label"]
    except Exception:
        pass
    try:
        import order_items
        name = order_items.display_name(code)
        if name:
            return name
    except Exception:
        pass
    return code.replace("_", " ").title()


def _priority(item: str) -> int:
    key = str(item or "").lower()
    for i, p in enumerate(PRIORITY_ITEMS):
        if key == p or key.startswith(p + "_"):
            return i
    return len(PRIORITY_ITEMS)


def stock_facts(draft_rows) -> dict | None:
    """ONE key item from today's order draft to ask about."""
    rows = [r for r in draft_rows or [] if r.get("item") and r.get("qty")]
    if not rows:
        return None
    rows.sort(key=lambda r: (_priority(r["item"]), -float(r["qty"] or 0)))
    r = rows[0]
    return {
        "item": item_label(r["item"]),
        "qty": fmt_qty(r["qty"], r.get("pack")),
        "pack": str(r.get("pack") or ""),
        "supplier": _short_supplier(r.get("supplier")),
    }


def order_facts(draft_rows, top: int = 3) -> dict | None:
    """Tomorrow's draft: the main items, and how many more."""
    rows = [r for r in draft_rows or [] if r.get("item") and r.get("qty")]
    if not rows:
        return None
    rows.sort(key=lambda r: (_priority(r["item"]), -float(r["qty"] or 0)))
    return {
        "items": [
            {"item": item_label(r["item"]), "qty": fmt_qty(r["qty"], r.get("pack")),
             "pack": str(r.get("pack") or "")}
            for r in rows[:top]
        ],
        "more": max(0, len(rows) - top),
    }


def cook_facts(forecast_rows) -> dict | None:
    """The one dish whose plan differs most from what they usually cook."""
    best, best_gap = None, 0.0
    for r in forecast_rows or []:
        if r.get("action") not in ("CUT", "RAISE"):
            continue
        try:
            rec = float(r["recommend_qty"])
            usual = float(r["usual_cooked"]) if r.get("usual_cooked") is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        gap = abs(rec - usual) / usual if usual else 0.0
        if best is None or gap > best_gap:
            best, best_gap = r, gap
    if best is None:
        return None
    unit = best.get("unit") or "pcs"
    cook = fmt_qty(best["recommend_qty"], unit)
    usual = fmt_qty(best.get("usual_cooked"), unit) if best.get("usual_cooked") is not None else None
    if usual == cook:
        return None
    return {
        "item": item_label(best.get("item_code")),
        "cook": cook,
        "unit": unit,
        "usual": usual,
        "action": best["action"],
    }


def bills_facts(entries) -> dict | None:
    """The most overdue regular supplier for one outlet chat."""
    rows = [e for e in entries or [] if e.get("supplier") and e.get("last_date")]
    if not rows:
        return None
    rows.sort(key=lambda e: -int(e.get("days_overdue") or 0))
    e = rows[0]
    last = e["last_date"]
    return {
        "supplier": _short_supplier(e["supplier"]),
        "days": str(int(e["days_missing"])),
        "last": f"{last.day:02d}/{last.month:02d}",
    }


_COMPANY_SUFFIX = re.compile(
    r"\s*(\(M\)|\bSDN\.?\s*BHD\b\.?|\bBHD\b\.?)", re.IGNORECASE
)


def _short_supplier(name) -> str:
    """'BESTARI FARM (M) SDN BHD' -> 'Bestari Farm' — how a cashier says it.
    Short acronyms stay upper case ('JY Resources')."""
    s = _COMPANY_SUFFIX.sub("", str(name or "")).strip(" .,-")
    if not s.isupper():
        return s
    return " ".join(w if len(w) <= 2 else w.capitalize() for w in s.split())


# --- fact check --------------------------------------------------------------

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_NON_ASCII_DIGITS = re.compile(r"[०-९০-৯௦-௯٠-٩]")
_MONEY = re.compile(r"\bRM\s?\d|\bRM\b|%|\$|ringgit|\bsen\b", re.IGNORECASE)
_LABELS = re.compile(r"ALERT|\[TEST|\[PREVIEW|WARNING", re.IGNORECASE)
_WORD = re.compile(r"[a-z]+")


def _norm_num(s: str) -> str:
    s = s.replace(",", ".")
    try:
        return f"{float(s):g}"
    except ValueError:
        return s


def _fact_strings(facts) -> list[str]:
    out = []
    if isinstance(facts, dict):
        for v in facts.values():
            out += _fact_strings(v)
    elif isinstance(facts, (list, tuple)):
        for v in facts:
            out += _fact_strings(v)
    elif facts is not None:
        out.append(str(facts))
    return out


def item_vocabulary() -> set[str]:
    """Every item name the bot knows, lowercased — used to spot an item the
    wording mentions that the facts don't."""
    vocab: set[str] = set(PRIORITY_ITEMS)
    try:
        import kitchen_usage
        for meta in kitchen_usage.ITEM_BY_CODE.values():
            vocab.add(meta["label"].lower())
    except Exception:
        pass
    try:
        import order_items
        for code, (label, _unit) in order_items._DISPLAY.items():
            vocab.add(label.lower())
            vocab.add(code.replace("_", " ").lower())
    except Exception:
        pass
    return {v for v in vocab if v}


def _missing_facts(text, facts, slot) -> list[str]:
    """Fact values the wording dropped or translated — each must appear
    as given (English letters, 0-9 digits)."""
    facts = facts or {}
    needed = [facts.get(k) for k in _REQUIRED_FACTS.get(slot, ())]
    if slot == "order":
        for item in facts.get("items") or []:
            needed += [item.get("item"), item.get("qty")]
    lower = text.lower()
    return [
        f"'{v}' not written as in the data"
        for v in needed
        if v not in (None, "") and str(v).lower() not in lower
    ]


def fact_check(text, facts, *, vocabulary=None, other_names=(), language=None,
               slot=None) -> list[str]:
    """Reasons ``text`` can't be sent; ``[]`` means it only says what the
    facts say."""
    problems: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return ["empty"]
    if len(text) > 400 or text.count("\n") > 3:
        problems.append("too long")
    if _LABELS.search(text):
        problems.append("system label")
    if _MONEY.search(text):
        problems.append("money figure")
    if _NON_ASCII_DIGITS.search(text):
        problems.append("non-0-9 digits")
    allowed = _ALLOWED_SCRIPTS.get(language, set())
    if _TAMIL_SCRIPT.search(text) and "tamil" not in allowed:
        problems.append("Tamil script for a non-Tamil cashier")
    if _BENGALI_SCRIPT.search(text) and "bengali" not in allowed:
        problems.append("Bengali script (Bengali goes in English letters)")
    problems += _missing_facts(text, facts, slot)
    rude = informal_tamil(text)
    if rude:
        problems.append("informal Tamil: " + ", ".join(rude))
    if slot in OVER_SLOTS:
        over = said_over(text)
        if over:
            problems.append("says lunch/shift is over: " + ", ".join(over))

    fact_text = " ".join(_fact_strings(facts))
    allowed_nums = {_norm_num(n) for n in _NUM.findall(fact_text)}
    for n in _NUM.findall(text):
        if _norm_num(n) not in allowed_nums:
            problems.append(f"number {n} not in data")

    lower = text.lower()
    fact_words = set(_WORD.findall(fact_text.lower()))
    for term in sorted(vocabulary if vocabulary is not None else item_vocabulary()):
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lower):
            if not set(_WORD.findall(term)) <= fact_words:
                problems.append(f"item '{term}' not in data")

    if re.search(r"sdn\.?\s*bhd", lower) and "sdn" not in fact_text.lower():
        problems.append("supplier not in data")
    for name in other_names:
        name = str(name or "").strip()
        if name and re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text):
            problems.append(f"name {name} not the cashier on shift")
    return problems


# --- wording -----------------------------------------------------------------

SYSTEM_PROMPT = (
    "You write short Telegram messages from Khulafa HQ, the office of a "
    "Malaysian restaurant group, to the cashier on shift at one outlet. The "
    "cashier also runs the shop. Sound like a boss who knows the shop "
    "messaging his staff on WhatsApp: friendly but firm, casual, human.\n"
    "Rules:\n"
    "- 1 to 3 short lines. No headers, no bullet points, no labels.\n"
    "- Do NOT start with or include the cashier's name; it is added before "
    "your text.\n"
    "- Never pretend to be a specific person and never sign the message.\n"
    "- Ask exactly ONE question.\n"
    "- Use ONLY the facts given. Every number, item, supplier and date you "
    "write must appear in the facts, written exactly as given with 0-9 "
    "digits. Never add prices, money, percentages, totals, other items, "
    "other suppliers or other people.\n"
    "- Say the same thing as the reference message, in your own words. Vary "
    "it: different opening, different phrasing from the recent messages "
    "listed (never repeat them), so it never feels automatic.\n"
    "- Write in the language asked for.\n"
    'Reply with JSON only: {"text": "<the message>", "english": "<the same '
    'message in plain English, for the director to read>"}'
)


TRANSLATE_PROMPT = (
    "Translate this Malaysian Tamil WhatsApp message into plain, literal "
    "English. Translate only what is written — do not guess context, do not "
    "fix or improve it. Keep names, items and numbers as they are.\n"
    'Reply with JSON only: {"english": "<literal translation>"}'
)

JUDGE_PROMPT = (
    "A restaurant office meant to send a cashier the INTENDED message. A "
    "literal translation of what was ACTUALLY written is given; the "
    "translation itself may be loose, so judge the meaning, not the words.\n"
    "Treat these as the SAME meaning: finished / ran out / sold out / "
    "finished early / finished quickly / habis / தீர்ந்து. Different "
    "polite phrasing, word order, greetings or small filler words are also "
    "the same. Do not reject for words the translation added on its own "
    "(e.g. 'afternoon', 'which dish').\n"
    "Only answer same_meaning=false when the written message:\n"
    "1. asks about a different topic than intended (e.g. start vs end of "
    "shift, today vs tomorrow, order vs stock, 'short/not enough' vs 'left "
    "over');\n"
    "2. adds or changes a fact, number or item that is not in the intended "
    "message;\n"
    "3. says lunch or the shift is over;\n"
    "4. is rude or informal (orders instead of asks).\n"
    'Reply with JSON only: {"same_meaning": true|false, "reason": "<short>"}'
)

MEANING_LANGUAGES = ("tamil", BM_TAMIL)
# Only check-ins that carry data get the back-translation judge. For the
# no-data ones (open, lunch, night) the hard rules in fact_check decide:
# no numbers/items outside the data, no "lunch/shift is over", no informal
# Tamil. The judge was rejecting correct Tamil there over loose
# back-translations ("finished quickly" vs "ran out").
MEANING_SLOTS = DATA_SLOTS


def _tamil_part(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if _TAMIL_SCRIPT.search(line)
    )


def meaning_check(text, intended_en, purpose, complete) -> dict:
    """Independent back-translation of the Tamil in ``text``, then a
    separate judgment of whether it means what was intended. Fails closed:
    no verdict = not OK. Returns ``{ok, back, reason}``."""
    tamil = _tamil_part(text)
    if not tamil:
        return {"ok": True, "back": "", "reason": "no Tamil"}
    try:
        tr = complete(TRANSLATE_PROMPT, tamil)
        back = str(((tr or {}).get("data") or {}).get("english") or "").strip()
        if not back:
            return {"ok": False, "back": "", "reason": "no back-translation"}
        verdict = complete(JUDGE_PROMPT, json.dumps(
            {"purpose": purpose, "intended": intended_en, "actually_written": back},
            ensure_ascii=False,
        ))
        data = (verdict or {}).get("data") or {}
        same = data.get("same_meaning")
        reason = str(data.get("reason") or "").strip()
        if same is True:
            return {"ok": True, "back": back, "reason": reason}
        return {"ok": False, "back": back,
                "reason": reason or "meaning check gave no verdict"}
    except Exception:
        logger.exception("staff chat: meaning check failed")
        return {"ok": False, "back": "", "reason": "meaning check failed"}


def _purpose(slot, facts) -> str:
    if slot == "order" and (facts or {}).get("ask"):
        return ("ask what they need to order for tomorrow (there is no draft "
                "to show; ask them to list items and quantities)")
    return SLOTS[slot][2]


def build_user_prompt(slot, language, facts, reference, seed, avoid=()) -> str:
    return json.dumps(
        {
            "purpose": _purpose(slot, facts),
            "language": _LANG_PROMPT.get(language, _LANG_PROMPT[DEFAULT_LANGUAGE]),
            "facts": facts or {},
            "reference_message": reference,
            "variation_seed": seed,
            # What this outlet got for this check-in on recent days: say it
            # differently so it never reads copy-pasted.
            "recent_messages_do_not_repeat": [a for a in avoid if a][:3],
        },
        ensure_ascii=False,
    )


def variant_for(seed: str) -> int:
    """Stable per seed (day + outlet + slot), different day to day."""
    return sum(seed.encode("utf-8")) if seed else 0


def build_message(slot, language, facts, *, seed="", complete=None,
                  vocabulary=None, other_names=(), avoid=()) -> dict:
    """Word one check-in. Returns ``{text, english, source, problems,
    template, ai_text, provider, model, tokens_in, tokens_out}`` where
    ``source`` is "ai" when the AI wording passed the fact check, otherwise
    "template". Never raises."""
    language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    template = render_template(slot, language, facts or {}, variant_for(seed))
    out = {
        "text": template, "english": "", "source": "template", "problems": [],
        "template": template, "ai_text": None, "provider": staff_ai.provider(),
        "model": staff_ai.model(), "tokens_in": None, "tokens_out": None,
        "back_translation": "", "meaning_ok": None,
    }
    if not template:
        out["problems"] = ["no template"]
        return out
    complete = complete or staff_ai.complete_json
    try:
        result = complete(
            SYSTEM_PROMPT,
            build_user_prompt(slot, language, facts, template, seed, avoid),
        )
    except Exception:
        logger.exception("staff chat: provider call failed")
        result = None
    if not result:
        out["problems"] = ["ai unavailable"]
        return out
    data = result.get("data") or {}
    ai_text = str(data.get("text") or "").strip()
    out.update(
        ai_text=ai_text,
        english=str(data.get("english") or "").strip(),
        provider=result.get("provider") or out["provider"],
        model=result.get("model") or out["model"],
        tokens_in=result.get("tokens_in"),
        tokens_out=result.get("tokens_out"),
    )
    problems = fact_check(
        ai_text, facts, vocabulary=vocabulary, other_names=other_names,
        language=language, slot=slot,
    )
    if problems:
        out["problems"] = problems
        out["english"] = ""
        return out
    if language in MEANING_LANGUAGES and slot in MEANING_SLOTS:
        check = meaning_check(
            ai_text,
            render_template(slot, "english", facts or {}),
            _purpose(slot, facts),
            complete,
        )
        out.update(back_translation=check["back"], meaning_ok=check["ok"])
        if not check["ok"]:
            out["problems"] = [f"meaning check: {check['reason']}"]
            out["english"] = ""
            return out
    out.update(text=ai_text, source="ai")
    return out


# --- preview digest ----------------------------------------------------------

def format_preview(slot: str, rows: list[dict], *, now: datetime | None = None) -> str:
    """One director message per check-in: every outlet's message as the
    cashier would read it. ``rows``: dicts with outlet_code, cashier,
    language and either ``result`` (from build_message) or ``skip``."""
    shift, time_, _purpose = SLOTS[slot]
    lines = [
        f"👀 Preview · {time_} {slot} check · {shift} shift",
        "Not sent to any group. ✏️ = AI wording failed the fact check, "
        "plain template shown.",
    ]
    for row in rows:
        head = f"{row['outlet_code']} · {row['cashier']} · {row['language']}"
        if row.get("skip"):
            lines += ["", f"{head} — {row['skip']}"]
            continue
        res = row["result"]
        if res["source"] != "ai":
            head += " ✏️ " + "; ".join(res["problems"][:3])
        lines += ["", head, f"{row['cashier']},", res["text"]]
        if res.get("english"):
            lines.append(f"↳ EN: {res['english']}")
        if res.get("back_translation"):
            lines.append(f"↳ back-translated: {res['back_translation']}")
    return "\n".join(lines)


def format_samples(rows: list[dict]) -> str:
    """Review samples: each with the check-in, the outlet, the language, the
    text, DeepSeek's own English gloss, the independent back-translation,
    and whether it passed (✅) or the template was used (✏️ + why). Ends
    with the pass rate per language."""
    langs = sorted({row.get("language", "tamil") for row in rows})
    label = " + ".join(_LANG_NAMES.get(lang, lang) for lang in langs) or "Tamil"
    lines = [
        f"🧪 {len(rows)} {label} samples — not sent to any group",
        "✅ = AI wording passed every check · ✏️ = template used (reason)",
    ]
    for i, row in enumerate(rows, 1):
        res = row["result"]
        lang = row.get("language", "tamil")
        mark = "✅" if res["source"] == "ai" else "✏️ " + "; ".join(res["problems"][:2])
        lines += ["", f"{i}. {row['slot']} · {row['outlet_code']} · {row['cashier']} · "
                      f"{_LANG_NAMES.get(lang, lang)} {mark}",
                  res["text"]]
        if res.get("english"):
            lines.append(f"↳ EN: {res['english']}")
        if res.get("back_translation"):
            lines.append(f"↳ back-translated: {res['back_translation']}")
    lines.append("")
    for lang in langs:
        mine = [r for r in rows if r.get("language", "tamil") == lang]
        passed = sum(1 for r in mine if r["result"]["source"] == "ai")
        lines.append(f"Pass rate {_LANG_NAMES.get(lang, lang)}: {passed}/{len(mine)}")
    return "\n".join(lines)


_LANG_NAMES = {"tamil": "Tamil", "bm": "BM", "bengali": "Bengali",
               "english": "English", "indonesian": "Indonesian",
               BM_TAMIL: "BM+Tamil"}


def log_row(slot, outlet_code, chat_id, cashier, language, facts, result, mode) -> dict:
    return {
        "slot": slot,
        "outlet_code": outlet_code,
        "chat_id": chat_id,
        "cashier": cashier,
        "language": language,
        "facts": facts or {},
        "template_text": result.get("template"),
        "ai_text": result.get("ai_text"),
        "final_text": result.get("text"),
        "source": result.get("source"),
        "problems": result.get("problems") or [],
        "provider": result.get("provider"),
        "model": result.get("model"),
        "tokens_in": result.get("tokens_in"),
        "tokens_out": result.get("tokens_out"),
        "back_translation": result.get("back_translation") or None,
        "meaning_ok": result.get("meaning_ok"),
        "mode": mode,
    }


def data_codes(registry_code: str) -> list[str]:
    """Codes the data tables use for an outlet registered under
    ``registry_code`` (order drafts say "D" for DAMANSARA, the kitchen log
    "KLRAZAK" for SBESI)."""
    import manager_registration
    code = str(registry_code or "").upper()
    return [code] + [a for a, t in manager_registration.CODE_ALIASES.items() if t == code]


def seed_for(slot: str, outlet_code: str, day: date) -> str:
    return f"{day.isoformat()}-{outlet_code}-{slot}"
