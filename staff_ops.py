"""Staff questions v2: cost, wastage and food quality in the outlet groups.

What goes to each live outlet group (approved by the director, see
docs/staff_questions_v2_review.md):

  03:00  leftover       night cashier: what's left, kept, thrown + a
                        rotating food-safety line — only groups that
                        didn't fill in the 02:00 kitchen form     [buttons]
  09:00  sales          morning cashier: yesterday busier/quieter than a
                        usual same weekday -> cook/order less/more (info,
                        items sold only, never RM)
  10:30  wastage        morning cashier: anything thrown yesterday [buttons]
  16:00  afternoon      one menu item selling clearly less -> "taste it and
                        tell me" [buttons]; otherwise the daily tip (info)
  21:05  bills          unchanged (staff_live / staff_chat)
  on upload  invoice    a supplier invoice far above that outlet's usual,
                        or an item it rarely buys             [buttons]
  on upload  minimarket a mini market / kedai runcit receipt   [buttons]
  Mon 11:05  praise     weekly praise in every group (info)

Max 5 staff messages per group per day (reminders and reactions on bills don't
count): the tip and the sales note are skipped first, then extra
invoice/mini-market questions (those still reach the director's summary).

Everything here is pure: detection rules, texts in the cashier's language,
button sets. Telegram and database glue live in bot.py. The texts are the
approved fixed wordings (no AI), so they carry only the numbers and names
from the data.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta

import staff_chat

# --- settings ------------------------------------------------------------------

OPS_SLOTS = ("leftover", "sales", "wastage", "afternoon", "invoice", "minimarket",
             "praise")
INFO_SLOTS = ("sales", "tip", "praise")          # no reply expected
EVENT_SLOTS = ("invoice", "minimarket")

DAILY_CAP = 5
EVENTS_PER_DAY = 2
SALES_CAP_BEFORE = 4       # the 09:00 note only if fewer than 4 already today
TIP_CAP_BEFORE = 4         # the tip only if fewer than 4 (leaves room for bills)

INVOICE_HIGH_RATIO = 1.3
INVOICE_MIN_HISTORY = 3    # purchases of that item from that supplier in 28 days
RARE_MAX_DAYS = 1          # bought on at most this many days in 8 weeks = rare
RARE_MIN_OUTLET_DAYS = 20  # outlets with thin history never get "rare" questions

SALES_THRESHOLD = 0.15
SALES_MIN_WEEKS = 3

DROP_MIN = 0.30            # item down 30%+ ...
DROP_GAP = 0.20            # ... and 20 points worse than the whole shop
DROP_MIN_BASE = 15         # usual plates a day
DROP_RECENT_DAYS = 3
DROP_MIN_BASE_DAYS = 15
DROP_REPEAT_DAYS = 7       # don't ask about the same item again within a week


# Send time shown in the director's "by check-in time" lines.
SLOT_TIMES = {"leftover": "03:00", "sales": "09:00", "wastage": "10:30",
              "afternoon": "16:00", "invoice": "upload", "minimarket": "upload",
              "praise": "Mon 11:05"}

# Statuses that mean a message really reached the group.
SENT_STATUSES = ("open", "reminded", "answered", "no_reply", "info")


def may_send(slot: str, sent_today: int, events_today: int = 0) -> bool:
    """The per-group daily limit. ``sent_today``: staff messages already in
    the group today (bills included); ``events_today``: invoice / mini market
    questions among them. The 21:05 bills question always goes; everything
    else needs room for it (fewer than 4 so far), and at most 2 upload
    questions a day. Worst case is 5 a day."""
    if slot == "bills":
        return True
    if sent_today >= DAILY_CAP - 1:
        return False
    if slot in EVENT_SLOTS and events_today >= EVENTS_PER_DAY:
        return False
    return True


def count_today(threads, outlet_code, day: date, tz) -> tuple[int, int]:
    """``(sent, events)`` for one outlet on local ``day`` from thread rows."""
    sent = events = 0
    for t in threads or []:
        if t.get("outlet_code") != outlet_code or t.get("status") not in SENT_STATUSES:
            continue
        asked = t.get("asked_at")
        try:
            ts = asked if isinstance(asked, datetime) else datetime.fromisoformat(
                str(asked).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.astimezone(tz).date() != day:
            continue
        sent += 1
        events += t.get("slot") in EVENT_SLOTS
    return sent, events


# --- mini market -----------------------------------------------------------------

_MINIMARKET = re.compile(
    r"(9{2,3}\s*-?\s*speed\s*mart|speedmart|\bkk\s*(super|mart)|7\s*-?\s*eleven|"
    r"family\s*mart|\bmynews\b|\bmydin\b|econsave|\blotus|\btesco\b|\baeon\b|"
    r"\bgiant\b|hero\s*market|jaya\s*grocer|cold\s*storage|\bpasaraya\b|"
    r"mini\s*-?\s*mar(ket|t)|kedai\s*runcit|\brunci?t\b|convenience)",
    re.IGNORECASE,
)
# Wholesalers are regular suppliers, never "mini market" (Pasaraya Borong
# SNS Ali is Sek 20's wholesaler).
_WHOLESALE = re.compile(r"(borong|wholesale|\bb\s*/\s*s\b)", re.IGNORECASE)


def is_minimarket(merchant) -> bool:
    name = str(merchant or "")
    return bool(_MINIMARKET.search(name)) and not _WHOLESALE.search(name)


def shop_name(merchant) -> str:
    """'99 SPEED MART SDN. BHD.' -> '99 Speed Mart'."""
    return staff_chat._short_supplier(merchant) or str(merchant or "").title()


# --- invoice check ---------------------------------------------------------------

def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def invoice_flag(lines, history, *, receipt_date, merchant, outlet_days: int) -> dict | None:
    """The one question worth asking about an invoice, or None.

    ``lines``: this invoice, ``[{canonical_item, qty}]`` (summed per item).
    ``history``: this outlet's item_prices rows of the last 8 weeks, other
    receipts only: ``[{merchant, canonical_item, qty, receipt_date, receipt_id}]``.
    Returns ``{"kind": "high", item, qty, usual, ratio}`` for the biggest jump
    (30%+ over the median of the same supplier+item in 28 days, same weekday
    when there are 3+ of those), else ``{"kind": "rare", item, qty}`` for an
    item this outlet bought on at most one day in 8 weeks.
    """
    rdate = _day(receipt_date) or date.today()
    merchant_u = str(merchant or "").upper()
    per_receipt: dict[tuple, dict] = {}
    days_by_item: dict[str, set] = {}
    for h in history or []:
        d = _day(h.get("receipt_date"))
        item = str(h.get("canonical_item") or "").lower()
        if not d or not item or d > rdate:
            continue
        days_by_item.setdefault(item, set()).add(d)
        if str(h.get("merchant") or "").upper() != merchant_u or d < rdate - timedelta(days=28):
            continue
        key = (item, h.get("receipt_id") or d)
        slot = per_receipt.setdefault(key, {"qty": 0.0, "date": d})
        slot["qty"] += float(h.get("qty") or 0)

    best = None
    for line in lines or []:
        item = str(line.get("canonical_item") or "").lower()
        qty = float(line.get("qty") or 0)
        if not item or qty <= 0:
            continue
        past = [v for (i, _r), v in per_receipt.items() if i == item and v["qty"] > 0]
        if len(past) >= INVOICE_MIN_HISTORY:
            same_wd = [v["qty"] for v in past if v["date"].weekday() == rdate.weekday()]
            pool = same_wd if len(same_wd) >= 3 else [v["qty"] for v in past]
            usual = statistics.median(pool)
            if usual > 0 and qty >= INVOICE_HIGH_RATIO * usual and qty - usual >= 1:
                ratio = qty / usual
                if best is None or best["kind"] != "high" or ratio > best["ratio"]:
                    best = {"kind": "high", "item": item, "qty": qty,
                            "usual": usual, "ratio": ratio}
            continue
        if (best is None and outlet_days >= RARE_MIN_OUTLET_DAYS
                and len(days_by_item.get(item, set()) - {rdate}) <= RARE_MAX_DAYS):
            best = {"kind": "rare", "item": item, "qty": qty}
    return best


# --- morning sales note ------------------------------------------------------------

def sales_signal(yesterday_count, usual_counts) -> dict | None:
    """``{"direction": "low"|"high", "count", "usual"}`` when yesterday is
    15%+ off the median of the same weekday (3+ weeks needed), else None."""
    usual = [float(u) for u in usual_counts or [] if u]
    if yesterday_count is None or len(usual) < SALES_MIN_WEEKS:
        return None
    med = statistics.median(usual)
    if med <= 0:
        return None
    change = float(yesterday_count) / med - 1
    if abs(change) < SALES_THRESHOLD:
        return None
    return {"direction": "low" if change < 0 else "high",
            "count": int(round(float(yesterday_count))), "usual": int(round(med / 10.0) * 10)}


# --- item sales drop ---------------------------------------------------------------

def pos_item_label(name) -> str:
    """'MILO AIS ( T )' -> 'Milo Ais'; 'ROTI TELUR' -> 'Roti Telur';
    'SOTONG  RM' -> 'Sotong' (never a price or 'RM' in a staff message)."""
    s = re.sub(r"\(.*?\)", "", str(name or ""))
    s = re.sub(r"\bRM\b.*$|\b\d+\.\d{2}\b", "", s, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", s).title()


# Taste checks are for cooked dishes and hand-made hot drinks only. The POS
# categories are unreliable (vadai sits under SOFT DRINKS, a half-boiled egg
# under MINUMAN), so this goes by the item name.
_NOT_TASTEABLE = re.compile(
    r"("
    # cold drinks and juices
    r"\bAIS\b|AIS\s*\(|AIS$|\bICE\b|SEJUK|JUICE|\bJUS\b|\bSIRAP\b|"
    # bottled / canned / packet / plain water
    r"MINERAL|\bTIN\b|\bCAN\b|BOTOL|100\s*PLUS|\bCOKE\b|COCA|PEPSI|SPRITE|F\s*&\s*N|"
    r"EXTRA\s*JOSS|RED\s*BULL|\bSOYA\b|BOBO|\bYEO|DUTCH|\bAIR\s+(SUAM|PANAS|KOSONG)\b|"
    # plain sides and add-ons
    r"PAPADOM|PAPPADOM|NASI\s+PUTIH|NASI\s+KOSONG|^TAMBAH|^EXTRA|^EX\b|TAMPA|"
    r"^TELUR\s*(REBUS|MASIN|MATA|DADAR|GORENG|1/2|SEPARUH)|CHILLI\s+HIJAU|^KUAH|^SAMBAL\b|"
    r"^ACAR|KEROPOK|KERUPUK|"
    # bought-in snacks, fruit, sweets, fees
    r"KACANG|\bGULA\b|\bASAM\b|\bBUAH\b|^PISANG$|SAMOSA|KARIPAP|KARI\s*POP|DELIVERY"
    r")",
    re.IGNORECASE,
)


def tasteable(name) -> bool:
    """True for a cooked dish or a hand-made hot drink (teh tarik, kopi,
    milo panas); False for cold drinks, bottled/canned items, papadom,
    plain rice, eggs and other plain sides."""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    return bool(s) and not _NOT_TASTEABLE.search(s)


def item_drop(daily: dict, *, asked_recently=()) -> dict | None:
    """The one item whose plates dropped most, relative to the shop. Only
    cooked dishes and hand-made hot drinks count (``tasteable``).

    ``daily``: ``{business_date: {ITEM: qty}}`` for the last ~5 weeks of full
    days. Recent = the last 3 days, base = the days before. Rule: item down
    30%+ and 20 points worse than the whole shop, usual 15+ a day.
    """
    days = sorted(daily)
    if len(days) < DROP_RECENT_DAYS + DROP_MIN_BASE_DAYS:
        return None
    recent, base = days[-DROP_RECENT_DAYS:], days[:-DROP_RECENT_DAYS]

    def total(ds):
        return sum(sum(daily[d].values()) for d in ds) / len(ds)

    shop_change = total(recent) / total(base) - 1 if total(base) else 0.0
    asked = {str(a).upper() for a in asked_recently}
    items = {i for d in days for i in daily[d]}
    best = None
    for item in items:
        if item.upper() in asked or not tasteable(item):
            continue
        base_vals = [daily[d].get(item, 0) for d in base]
        present = [v for v in base_vals if v]
        if len(present) < DROP_MIN_BASE_DAYS:
            continue
        base_avg = sum(base_vals) / len(base)
        if base_avg < DROP_MIN_BASE:
            continue
        recent_avg = sum(daily[d].get(item, 0) for d in recent) / len(recent)
        change = recent_avg / base_avg - 1
        if change > -DROP_MIN or (change - shop_change) > -DROP_GAP:
            continue
        gap = change - shop_change
        if best is None or gap < best["gap"]:
            best = {"item": item, "label": pos_item_label(item),
                    "drop_pct": round(-change * 100), "shop_pct": round(shop_change * 100),
                    "gap": gap}
    return best


# --- rotation -----------------------------------------------------------------------

def safety_index(day: date) -> int:
    return day.toordinal() % len(SAFETY["bm"])


def tip_index(day: date) -> int:
    return day.toordinal() % len(TIPS)


# --- buttons (registered into staff_live) -------------------------------------------

# code -> (reply_status, English summary, asks for typed details)
CHOICES = {
    "event": ("other", "Event / booking", False),
    "stockout": ("other", "Stock ran out", False),
    "supextra": ("other", "Supplier sent extra", False),
    "newmenu": ("other", "New / special menu", False),
    "wrongdel": ("problem", "Wrong delivery", False),
    "nodeliv": ("problem", "Supplier didn't deliver", False),
    "forgot": ("other", "Forgot to order", False),
    "suddenout": ("short", "Ran out suddenly", False),
    "other": ("other", "Other reason", True),
    "tasteok": ("ok", "Tastes OK", False),
    "tastebad": ("problem", "Taste not right", True),
    "nottried": ("other", "Didn't try yet", False),
    "allgone": ("ok", "All finished", False),
    "kept": ("other", "Some kept for tomorrow", True),
    "thrown": ("finished", "Some thrown away", True),
    "none": ("ok", "Nothing thrown", False),
    "threw": ("finished", "Food thrown yesterday", True),
}

BUTTON_SETS = {
    "invoice": ("event", "stockout", "supextra", "other"),
    "rare": ("event", "newmenu", "wrongdel", "other"),
    "minimarket": ("nodeliv", "forgot", "suddenout", "other"),
    "taste": ("tasteok", "tastebad", "nottried"),
    "leftover": ("allgone", "kept", "thrown"),
    "wastage": ("none", "threw"),
}

# Max 1-3 words per button; "type the reason" comes after the tap.
LABELS = {
    "bm": {"event": "Event/tempahan", "stockout": "Stok habis", "supextra": "Supplier lebih",
           "newmenu": "Menu baru", "wrongdel": "Salah hantar", "nodeliv": "Supplier tak hantar",
           "forgot": "Lupa order", "suddenout": "Habis tiba-tiba", "other": "Lain",
           "tasteok": "Rasa ok", "tastebad": "Rasa kurang", "nottried": "Tak sempat try",
           "allgone": "Semua habis", "kept": "Ada lebih", "thrown": "Ada buang",
           "none": "Tiada", "threw": "Ada buang"},
    "tamil": {"event": "Event/order", "stockout": "Stock தீர்ந்தது", "supextra": "Supplier அதிகம்",
              "newmenu": "புது menu", "wrongdel": "தவறா வந்தது", "nodeliv": "Supplier அனுப்பல",
              "forgot": "Order மறந்துட்டோம்", "suddenout": "திடீர்னு தீர்ந்தது", "other": "வேற",
              "tasteok": "Taste சரி", "tastebad": "Taste சரியில்ல", "nottried": "Try பண்ணல",
              "allgone": "எல்லாம் தீர்ந்தது", "kept": "மீதம் இருக்கு", "thrown": "கொட்டணும்",
              "none": "இல்ல", "threw": "கொட்டினோம்"},
    "bengali": {"event": "Event/order", "stockout": "Stock shesh", "supextra": "Supplier beshi",
                "newmenu": "Notun menu", "wrongdel": "Bhul delivery", "nodeliv": "Supplier dey nai",
                "forgot": "Order bhule gechi", "suddenout": "Hothat shesh", "other": "Onno",
                "tasteok": "Shaad thik", "tastebad": "Shaad kom", "nottried": "Try kori nai",
                "allgone": "Shob shesh", "kept": "Baki ache", "thrown": "Felte hobe",
                "none": "Na", "threw": "Fele diyechi"},
    "english": {"event": "Event/booking", "stockout": "Ran out", "supextra": "Supplier extra",
                "newmenu": "New menu", "wrongdel": "Wrong delivery", "nodeliv": "No delivery",
                "forgot": "Forgot order", "suddenout": "Ran out suddenly", "other": "Other",
                "tasteok": "Tastes OK", "tastebad": "Not right", "nottried": "Didn't try",
                "allgone": "All finished", "kept": "Some left", "thrown": "Throwing some",
                "none": "None", "threw": "Threw some"},
    "indonesian": {"event": "Event/pesanan", "stockout": "Stok habis", "supextra": "Supplier lebih",
                   "newmenu": "Menu baru", "wrongdel": "Salah kirim", "nodeliv": "Supplier tak kirim",
                   "forgot": "Lupa pesan", "suddenout": "Tiba-tiba habis", "other": "Lain",
                   "tasteok": "Rasa oke", "tastebad": "Rasa kurang", "nottried": "Belum coba",
                   "allgone": "Semua habis", "kept": "Ada sisa", "thrown": "Ada dibuang",
                   "none": "Tidak ada", "threw": "Ada dibuang"},
}

DETAIL_PROMPTS = {
    "other": {"bm": "Ok, taip sebab ringkas ya.", "tamil": "சரி, காரணத்தை சுருக்கமா type பண்ணுங்க.",
              "bengali": "Thik ache, karon-ta chhoto kore likhe din.",
              "english": "OK, please type the reason in a few words.",
              "indonesian": "Oke, tolong ketik alasannya singkat ya."},
    "tastebad": {"bm": "Apa yang kurang? Taip sikit ya.", "tamil": "என்ன சரியில்ல? கொஞ்சம் type பண்ணுங்க.",
                 "bengali": "Ki thik nei? Ektu likhe din.",
                 "english": "What's not right? Please type a few words.",
                 "indonesian": "Apa yang kurang? Tolong ketik sedikit ya."},
    "kept": {"bm": "Apa yang disimpan untuk esok? Taip ya.",
             "tamil": "நாளைக்கு என்ன வெச்சீங்க? Type பண்ணுங்க.",
             "bengali": "Kal-er jonno ki rakhlen? Likhe din.",
             "english": "What are you keeping for tomorrow? Please type it.",
             "indonesian": "Apa yang disimpan untuk besok? Tolong ketik ya."},
    "thrown": {"bm": "Apa yang dibuang? Taip ya.", "tamil": "என்ன கொட்டணும்? Type பண்ணுங்க.",
               "bengali": "Ki felte hobe? Likhe din.",
               "english": "What is being thrown away? Please type it.",
               "indonesian": "Apa yang dibuang? Tolong ketik ya."},
    "threw": {"bm": "Apa yang dibuang dan kenapa? Taip ya.",
              "tamil": "என்ன கொட்டினீங்க, ஏன்? Type பண்ணுங்க.",
              "bengali": "Ki fele diyechen ar keno? Likhe din.",
              "english": "What was thrown away, and why? Please type it.",
              "indonesian": "Apa yang dibuang dan kenapa? Tolong ketik ya."},
}


def button_set(slot: str, facts: dict | None) -> str | None:
    """Which buttons an ops question gets (None for info messages)."""
    facts = facts or {}
    if slot == "invoice":
        return "rare" if facts.get("kind") == "rare" else "invoice"
    if slot == "minimarket":
        return "minimarket"
    if slot == "afternoon":
        return "taste" if facts.get("item") else None
    if slot in ("leftover", "wastage"):
        return slot
    return None


# --- texts ----------------------------------------------------------------------------

WEEKDAYS = {
    "bm": ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"],
    "tamil": ["திங்கள்கிழமை", "செவ்வாய்க்கிழமை", "புதன்கிழமை", "வியாழக்கிழமை",
              "வெள்ளிக்கிழமை", "சனிக்கிழமை", "ஞாயிற்றுக்கிழமை"],
    "bengali": ["Shombar", "Mongolbar", "Budhbar", "Brihospotibar", "Shukrobar",
                "Shonibar", "Robibar"],
    "english": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "indonesian": ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"],
}

SAFETY = {
    "bm": ["Sejukkan dalam chiller dalam masa 2 jam.", "Tutup dan label tarikh.",
           "Panaskan semula sampai betul-betul panas.",
           "Jangan guna semula nasi, makanan laut atau kuah santan yang dibiar di suhu bilik."],
    "tamil": ["2 மணி நேரத்துக்குள்ள chiller-ல வைங்க.", "மூடி வெச்சு தேதி எழுதுங்க.",
              "திரும்ப சூடு பண்ணும்போது நல்லா கொதிக்க விடுங்க.",
              "அறை வெப்பநிலையில விட்ட சாதம், கடல் உணவு, santan குழம்பை திரும்ப பயன்படுத்தாதீங்க."],
    "bengali": ["2 ghontar moddhe chiller-e rakhun.", "Dheke tarikh likhe rakhun.",
                "Abar gorom korle khub gorom kore nin.",
                "Ghorer tapmatray rakha bhaat, samudrik khabar ba santan torkari abar byabohar korben na."],
    "english": ["Chill within 2 hours.", "Cover and label with the date.",
                "Reheat until very hot.",
                "Never reuse rice, seafood or santan curries left at room temperature."],
    "indonesian": ["Dinginkan di chiller dalam 2 jam.", "Tutup dan beri label tanggal.",
                   "Panaskan ulang sampai benar-benar panas.",
                   "Jangan pakai ulang nasi, seafood, atau kuah santan yang dibiarkan di suhu ruang."],
}

_TEXTS = {
    "invoice_high": {
        "bm": "Invois {supplier} hari ni: {item} {qty}, biasa {usual}. Kenapa lebih kali ni?",
        "tamil": "இன்னைக்கு {supplier} invoice-ல {item} {qty}, வழக்கமா {usual}. இந்த தடவை ஏன் அதிகம்னு சொல்லுங்க?",
        "bengali": "Aajker {supplier} invoice-e {item} {qty}, shadharon {usual}. Ebar beshi keno, bolben?",
        "english": "{supplier} invoice today: {item} {qty}, usually {usual}. Why more this time?",
        "indonesian": "Nota {supplier} hari ini: {item} {qty}, biasanya {usual}. Kenapa lebih kali ini?",
    },
    "invoice_rare": {
        "bm": "Invois {supplier} hari ni ada {item} {qty} — kedai ni jarang beli {item}. Untuk apa ya?",
        "tamil": "இன்னைக்கு {supplier} invoice-ல {item} {qty} இருக்கு — நம்ம கடையில {item} அதிகமா வாங்கறது இல்ல. எதுக்குன்னு சொல்லுங்க?",
        "bengali": "Aajker {supplier} invoice-e {item} {qty} ache — ei dokane {item} shadharon kena hoy na. Kisher jonno, bolben?",
        "english": "{supplier} invoice today has {item} {qty} — this outlet rarely buys {item}. What is it for?",
        "indonesian": "Nota {supplier} hari ini ada {item} {qty} — outlet ini jarang beli {item}. Untuk apa ya?",
    },
    "minimarket": {
        "bm": "Ada bil {shop} masuk: {items}. Kenapa kali ni beli kat kedai runcit?",
        "tamil": "{shop} bill வந்திருக்கு: {items}. இந்த தடவை ஏன் mini market-ல வாங்கினீங்க?",
        "bengali": "{shop}-r bill esheche: {items}. Ebar dokan theke kinlen keno, bolben?",
        "english": "A {shop} bill came in: {items}. Why buy from a mini market this time?",
        "indonesian": "Ada nota {shop} masuk: {items}. Kenapa kali ini beli di minimarket?",
    },
    "sales_low_day": {
        "bm": "Semalam shift siang {count} item terjual, biasa hari {weekday} sekitar {usual} — kurang dari biasa. Hari ni masak dan order kurang sikit ya.",
        "tamil": "நேத்து பகல் shift-ல {count} item வித்துச்சு, வழக்கமா {weekday} சுமார் {usual} — வழக்கத்தை விட குறைவு. இன்னைக்கு கொஞ்சம் குறைவா சமைச்சு, குறைவா order பண்ணுங்க.",
        "bengali": "Gotokal diner shift-e {count} item bikri hoyeche, shadharon {weekday} pray {usual} — shadharon-er cheye kom. Aaj ektu kom ranna korun, kom order din.",
        "english": "Yesterday's day shift sold {count} items; a usual {weekday} is about {usual} — lower than usual. Please cook and order a bit less today.",
        "indonesian": "Kemarin shift siang terjual {count} item, biasanya hari {weekday} sekitar {usual} — lebih sedikit dari biasa. Hari ini masak dan pesan sedikit lebih sedikit ya.",
    },
    "sales_high_day": {
        "bm": "Semalam shift siang {count} item terjual, biasa hari {weekday} sekitar {usual} — lebih dari biasa. Hari ni sedia lebih sikit ya.",
        "tamil": "நேத்து பகல் shift-ல {count} item வித்துச்சு, வழக்கமா {weekday} சுமார் {usual} — வழக்கத்தை விட அதிகம். இன்னைக்கு கொஞ்சம் அதிகமா தயார் பண்ணுங்க.",
        "bengali": "Gotokal diner shift-e {count} item bikri hoyeche, shadharon {weekday} pray {usual} — shadharon-er cheye beshi. Aaj ektu beshi toiri rakhun.",
        "english": "Yesterday's day shift sold {count} items; a usual {weekday} is about {usual} — higher than usual. Please prepare a bit more today.",
        "indonesian": "Kemarin shift siang terjual {count} item, biasanya hari {weekday} sekitar {usual} — lebih banyak dari biasa. Hari ini siapkan sedikit lebih banyak ya.",
    },
    "sales_low_full": {
        "bm": "Semalam {count} item terjual, biasa hari {weekday} sekitar {usual} — kurang dari biasa. Hari ni masak dan order kurang sikit ya.",
        "tamil": "நேத்து {count} item வித்துச்சு, வழக்கமா {weekday} சுமார் {usual} — வழக்கத்தை விட குறைவு. இன்னைக்கு கொஞ்சம் குறைவா சமைச்சு, குறைவா order பண்ணுங்க.",
        "bengali": "Gotokal {count} item bikri hoyeche, shadharon {weekday} pray {usual} — shadharon-er cheye kom. Aaj ektu kom ranna korun, kom order din.",
        "english": "Yesterday sold {count} items; a usual {weekday} is about {usual} — lower than usual. Please cook and order a bit less today.",
        "indonesian": "Kemarin terjual {count} item, biasanya hari {weekday} sekitar {usual} — lebih sedikit dari biasa. Hari ini masak dan pesan sedikit lebih sedikit ya.",
    },
    "sales_high_full": {
        "bm": "Semalam {count} item terjual, biasa hari {weekday} sekitar {usual} — lebih dari biasa. Hari ni sedia lebih sikit ya.",
        "tamil": "நேத்து {count} item வித்துச்சு, வழக்கமா {weekday} சுமார் {usual} — வழக்கத்தை விட அதிகம். இன்னைக்கு கொஞ்சம் அதிகமா தயார் பண்ணுங்க.",
        "bengali": "Gotokal {count} item bikri hoyeche, shadharon {weekday} pray {usual} — shadharon-er cheye beshi. Aaj ektu beshi toiri rakhun.",
        "english": "Yesterday sold {count} items; a usual {weekday} is about {usual} — higher than usual. Please prepare a bit more today.",
        "indonesian": "Kemarin terjual {count} item, biasanya hari {weekday} sekitar {usual} — lebih banyak dari biasa. Hari ini siapkan sedikit lebih banyak ya.",
    },
    "itemdrop": {
        "bm": "{item} minggu ni kurang laku dari biasa — mungkin tak ada apa-apa, cuma nak pastikan. Boleh try rasa hari ni dan bagitau, ok tak rasanya?",
        "tamil": "இந்த வாரம் {item} வழக்கத்தை விட கொஞ்சம் குறைவா விக்குது — ஒண்ணும் இருக்காது, சும்மா check பண்ணத்தான். இன்னைக்கு ஒரு {item} try பண்ணி பாருங்க, taste சரியா இருக்கான்னு சொல்லுங்க?",
        "bengali": "Ei shoptahe {item} shadharon-er cheye ektu kom bikri hocche — hoyto kichu na, shudhu nishchit hote chai. Aaj ekta kheye dekhun, shaad thik ache kina bolben?",
        "english": "{item} is selling a bit less than usual this week — maybe nothing, just making sure. Could you taste it today and say if it tastes right?",
        "indonesian": "{item} minggu ini kurang laku dari biasa — mungkin tidak apa-apa, cuma ingin memastikan. Bisa dicoba rasanya hari ini dan kabari, oke tidak rasanya?",
    },
    "leftover": {
        "bm": "Makanan masak yang tinggal malam ni — apa nak simpan untuk esok, apa nak buang? 🧊 {safety}",
        "tamil": "சமைச்ச சாப்பாடு ஏதாவது மீதம் இருக்கா? நாளைக்கு எது வெச்சுக்கணும், எது கொட்டணும்னு சொல்லுங்க. 🧊 {safety}",
        "bengali": "Ranna kora khabar ki kichu baki ache? Kal-er jonno ki rakhben, ki fele diben, bolben? 🧊 {safety}",
        "english": "Any cooked food left tonight? What will you keep for tomorrow, what will you throw away? 🧊 {safety}",
        "indonesian": "Ada makanan masak yang tersisa malam ini? Apa yang disimpan untuk besok, apa yang dibuang? 🧊 {safety}",
    },
    "wastage": {
        "bm": "Semalam ada makanan yang dibuang? Kalau ada, apa dan kenapa?",
        "tamil": "நேத்து ஏதாவது சாப்பாடு கொட்ட வேண்டி வந்துச்சா? இருந்தா, என்ன, ஏன்னு சொல்லுங்க?",
        "bengali": "Gotokal ki kono khabar fele dite hoyechilo? Hole, ki ar keno, bolben?",
        "english": "Was any food thrown away yesterday? If yes, what and why?",
        "indonesian": "Kemarin ada makanan yang dibuang? Kalau ada, apa dan kenapa?",
    },
    "praise": {
        "bm": "🏆 Minggu lepas: {lines}. Terima kasih semua! 🙏",
        "tamil": "🏆 போன வாரம்: {lines}. எல்லாருக்கும் நன்றி! 🙏",
        "bengali": "🏆 Gato shoptaho: {lines}. Shobaike dhonnobad! 🙏",
        "english": "🏆 Last week: {lines}. Thank you all! 🙏",
        "indonesian": "🏆 Minggu lalu: {lines}. Terima kasih semua! 🙏",
    },
}

PRAISE_PARTS = {
    "minimarket": {"bm": "paling kurang beli di mini market — {who}",
                   "tamil": "mini market-ல குறைவா வாங்கினது — {who}",
                   "bengali": "shobcheye kom mini market theke kinecche — {who}",
                   "english": "fewest mini market buys — {who}",
                   "indonesian": "paling sedikit belanja di minimarket — {who}"},
    "wastage": {"bm": "paling kurang buang makanan — {who}",
                "tamil": "குறைவா கொட்டினது — {who}",
                "bengali": "shobcheye kom khabar fele diyeche — {who}",
                "english": "least food thrown away — {who}",
                "indonesian": "paling sedikit membuang makanan — {who}"},
    "replies": {"bm": "paling rajin jawab — {who}",
                "tamil": "அதிகமா பதில் சொன்னது — {who}",
                "bengali": "shobcheye beshi uttor diyeche — {who}",
                "english": "best at replying — {who}",
                "indonesian": "paling rajin menjawab — {who}"},
}

# 30 daily tips (approved). English/Indonesian readers get the BM line.
TIPS = [
    {"bm": "Timbang dulu sebelum masak — ikut sukatan resipi, bukan agak-agak.",
     "tamil": "சமைக்கிறதுக்கு முன்னாடி எடை போட்டு பாருங்க — கண்ணளவு இல்ல, recipe அளவுப்படி.",
     "bengali": "Ranna-r age ojon kore nin — chokher andaje na, recipe-r map moto."},
    {"bm": "Order ikut keperluan 1–2 hari saja — stok lebih cepat rosak.",
     "tamil": "அடுத்த 1–2 நாளுக்கு தேவையானது மட்டும் order பண்ணுங்க — அதிக stock சீக்கிரம் கெட்டுப்போகும்.",
     "bengali": "Porer 1–2 diner dorkar moto shudhu order din — beshi stock taratari nosto hoy."},
    {"bm": "Semak barang sampai dengan invois: berat, bilangan, kualiti — sebelum lori pergi.",
     "tamil": "சாமான் வந்ததும் invoice-ஓட ஒப்பிட்டு பாருங்க: எடை, எண்ணிக்கை, தரம் — lorry போறதுக்கு முன்னாடியே.",
     "bengali": "Maal ashle invoice-er sathe milie nin: ojon, songkha, maan — lorry jawar agei."},
    {"bm": "Beli dari supplier biasa; kedai runcit lebih mahal.",
     "tamil": "வழக்கமான supplier-கிட்ட வாங்குங்க; mini market-ல விலை அதிகம்.",
     "bengali": "Niyomito supplier theke kinun; dokan theke kinle dam beshi."},
    {"bm": "Tutup gas dan fryer bila tak masak.",
     "tamil": "சமைக்காத நேரத்துல gas-ஐயும் fryer-ஐயும் off பண்ணுங்க.",
     "bengali": "Ranna na korle gas ar fryer bondho rakhun."},
    {"bm": "Tapis minyak fryer setiap malam; tukar bila dah gelap atau berbau.",
     "tamil": "Fryer எண்ணெயை தினமும் ராத்திரி வடிகட்டுங்க; கருப்பா அல்லது வாசனை வந்தா மாத்துங்க.",
     "bengali": "Protidin raate fryer-er tel chheke nin; kalo ba gondho hole bodlan."},
    {"bm": "Catat apa yang dibuang setiap malam — apa yang dikira, boleh dikurangkan.",
     "tamil": "தினமும் ராத்திரி என்ன கொட்டினோம்னு எழுதுங்க — அளந்தாதான் குறைக்க முடியும்.",
     "bengali": "Protidin raate ki fela holo likhe rakhun — mapa gele-i komano jay."},
    {"bm": "Masak ikut batch kecil; tambah bila dulang tinggal separuh.",
     "tamil": "சின்ன batch-ஆ சமைங்க; tray பாதி காலியானதும் சேர்த்துக்கோங்க.",
     "bengali": "Chhoto batch-e ranna korun; tray ordhek khali hole abar din."},
    {"bm": "Lepas 21:00, masak batch kecil saja — waktu ramai dah lepas.",
     "tamil": "21:00 மணிக்கு அப்புறம் சின்ன batch மட்டும் சமைங்க — கூட்டம் முடிஞ்சிடும்.",
     "bengali": "21:00-er pore shudhu chhoto batch ranna korun — bhir shesh hoye jay."},
    {"bm": "Sayur yang dah dipotong, guna pada hari yang sama.",
     "tamil": "வெட்டின காய்கறியை அன்னைக்கே பயன்படுத்துங்க.",
     "bengali": "Kata sobji shei dini byabohar korun."},
    {"bm": "Masak nasi ikut keperluan, periuk demi periuk.",
     "tamil": "சாதத்தை தேவைக்கு ஏத்த மாதிரி, ஒவ்வொரு பானையா வடிங்க.",
     "bengali": "Bhaat dorkar moto, ek ek hari kore ranna korun."},
    {"bm": "Masa tutup kedai, bagitau lauk apa yang tinggal — plan esok boleh ubah.",
     "tamil": "கடை மூடும்போது எந்த கறி மீதம்னு சொல்லுங்க — மறுநாள் plan மாத்தலாம்.",
     "bengali": "Dokan bondho-r shomoy kon torkari baki thake bolun — porer diner plan bodlano jabe."},
    {"bm": "Masuk dulu, keluar dulu: stok baru di belakang, stok lama di depan.",
     "tamil": "முதல்ல வந்தது முதல்ல போகணும்: புது stock பின்னாடி, பழசு முன்னாடி.",
     "bengali": "Age asha maal age byabohar: notun stock pichone, purono shamne."},
    {"bm": "Label setiap bekas dengan tarikh masak atau buka.",
     "tamil": "ஒவ்வொரு container-லயும் சமைச்ச / திறந்த தேதியை எழுதுங்க.",
     "bengali": "Protiti container-e ranna ba kholar tarikh likhun."},
    {"bm": "Setiap pagi semak tarikh; guna yang paling lama dulu.",
     "tamil": "காலையில தேதியை பாருங்க; பழையதை முதல்ல பயன்படுத்துங்க.",
     "bengali": "Protidin shokale tarikh dekhun; purono-ta age byabohar korun."},
    {"bm": "Jangan buka pek baru selagi pek lama belum habis.",
     "tamil": "பழைய pack முடியுற வரைக்கும் புதுசை திறக்காதீங்க.",
     "bengali": "Purono packet shesh na hole notun ta khulben na."},
    {"bm": "Chiller 0–5°C, freezer -18°C atau lebih sejuk — semak dua kali sehari.",
     "tamil": "Chiller 0–5°C, freezer -18°C அல்லது அதுக்கு கீழ — தினமும் ரெண்டு தடவை check பண்ணுங்க.",
     "bengali": "Chiller 0–5°C, freezer -18°C ba tar kom — dine duibar check korun."},
    {"bm": "Jangan sumbat chiller; udara sejuk kena mengalir.",
     "tamil": "Chiller-ஐ ரொம்ப அடைக்காதீங்க; குளிர் காத்து சுத்தணும்.",
     "bengali": "Chiller beshi bhorben na; thanda hawa chola dorkar."},
    {"bm": "Pintu chiller sentiasa tutup; buka sekejap saja.",
     "tamil": "Chiller கதவை மூடியே வைங்க; சீக்கிரம் திறந்து மூடுங்க.",
     "bengali": "Chiller-er dorja bondho rakhun; taratari khule bondho korun."},
    {"bm": "Cairkan daging dalam chiller semalaman, bukan atas kaunter.",
     "tamil": "இறைச்சியை ராத்திரி chiller-லயே thaw பண்ணுங்க, counter-ல வைக்காதீங்க.",
     "bengali": "Mangsho raate chiller-ei boroph gola korun, counter-e na."},
    {"bm": "Guna senduk yang sama untuk setiap pinggan — sukatan sama setiap kali.",
     "tamil": "ஒவ்வொரு plate-க்கும் ஒரே கரண்டி பயன்படுத்துங்க — எப்பவும் ஒரே அளவு.",
     "bengali": "Protiti plate-e ek-i chamoch byabohar korun — protibar ek-i map."},
    {"bm": "Ketulan lauk: ikut bilangan standard setiap pinggan.",
     "tamil": "இறைச்சி துண்டுகள்: ஒரு plate-க்கு standard எண்ணிக்கைப்படி போடுங்க.",
     "bengali": "Mangsher tukro: plate proti standard songkha moto din."},
    {"bm": "Ajar staf baru sukatan portion pada hari pertama.",
     "tamil": "புது staff-க்கு முதல் நாளே portion அளவை சொல்லிக்கொடுங்க.",
     "bengali": "Notun staff-ke prothom dinei portion-er map shikhan."},
    {"bm": "Kuah: senduk standard, jangan lebih.",
     "tamil": "குழம்பு: standard கரண்டி அளவு, அதிகமா வேண்டாம்.",
     "bengali": "Jhol: standard chamoch, beshi na."},
    {"bm": "Basuh tangan sebelum masak, lepas tandas, lepas pegang daging mentah.",
     "tamil": "சமைக்கிறதுக்கு முன்னாடி, toilet போனதுக்கு அப்புறம், பச்சை இறைச்சி தொட்டதுக்கு அப்புறம் கை கழுவுங்க.",
     "bengali": "Ranna-r age, toilet-er pore, kacha mangsho dhorar pore haat dhuye nin."},
    {"bm": "Asingkan papan dan pisau: daging mentah, makanan laut, sayur.",
     "tamil": "பச்சை இறைச்சி, கடல் உணவு, காய்கறிக்கு தனித்தனி board, கத்தி பயன்படுத்துங்க.",
     "bengali": "Kacha mangsho, samudrik khabar ar sobji-r jonno alada board o chhuri byabohar korun."},
    {"bm": "Dalam chiller, letak daging mentah di bawah makanan yang dah masak.",
     "tamil": "Chiller-ல பச்சை இறைச்சியை சமைச்ச சாப்பாட்டுக்கு கீழ வைங்க.",
     "bengali": "Chiller-e kacha mangsho ranna kora khabar-er niche rakhun."},
    {"bm": "Lap kaunter dan meja dengan kain bersih; tukar kain setiap hari.",
     "tamil": "Counter, table-ஐ சுத்தமான துணியால துடைங்க; துணியை தினமும் மாத்துங்க.",
     "bengali": "Counter ar table porishkar kapor diye muchhun; kapor protidin bodlan."},
    {"bm": "Tutup semua makanan di kaunter dari lalat dan habuk.",
     "tamil": "Counter-ல இருக்கிற எல்லா சாப்பாட்டையும் ஈ, தூசி படாம மூடி வைங்க.",
     "bengali": "Counter-er shob khabar machi ar dhulo theke dheke rakhun."},
    {"bm": "Tutup rambut, potong kuku; jangan masak bila sakit.",
     "tamil": "முடியை மூடுங்க, நகத்தை வெட்டுங்க; உடம்பு சரியில்லைன்னா சமைக்காதீங்க.",
     "bengali": "Chul dheke rakhun, nokh chhoto rakhun; osustho hole ranna korben na."},
]


def _lang(language: str) -> str:
    return language if language in WEEKDAYS else "bm"


def _in(language: str, render) -> str:
    """One text in the cashier's language; BM+Tamil cashiers get both lines."""
    if language == staff_chat.BM_TAMIL:
        return f"{render('bm')}\n{render('tamil')}"
    return render(_lang(language))


def _num(n) -> str:
    return f"{int(round(float(n))):,}"


def qty_text(qty, canonical) -> str:
    import order_items
    unit = order_items.unit_noun(canonical)
    q = staff_chat.fmt_qty(qty, unit if unit == "kg" else None) or _num(qty)
    return f"{q}{unit}" if unit == "kg" else f"{q} {unit}"


def invoice_text(flag: dict, supplier: str, language: str) -> str:
    import order_items
    item = order_items.display_name(flag["item"])
    key = "invoice_rare" if flag["kind"] == "rare" else "invoice_high"
    values = {"supplier": supplier, "item": item, "qty": qty_text(flag["qty"], flag["item"]),
              "usual": qty_text(flag.get("usual") or 0, flag["item"])}
    return _in(language, lambda l: _TEXTS[key][l].format(**values))


def minimarket_items(names, limit: int = 3) -> str:
    # "5594 SONGKHLA AIR LIMAU 1L" -> "Songkhla Air Limau 1L"
    items = [pos_item_label(re.sub(r"^\s*\d{3,}\s+", "", str(n))) for n in names
             if str(n or "").strip()]
    if not items:
        return "-"
    more = len(items) - limit
    shown = ", ".join(items[:limit])
    return f"{shown} (+{more})" if more > 0 else shown


def minimarket_text(shop: str, items: str, language: str) -> str:
    return _in(language, lambda l: _TEXTS["minimarket"][l].format(shop=shop, items=items))


def sales_text(signal: dict, weekday: int, language: str, *, full_day: bool) -> str:
    key = f"sales_{signal['direction']}_{'full' if full_day else 'day'}"
    return _in(language, lambda l: _TEXTS[key][l].format(
        count=_num(signal["count"]), usual=_num(signal["usual"]), weekday=WEEKDAYS[l][weekday]))


def itemdrop_text(label: str, language: str) -> str:
    return _in(language, lambda l: _TEXTS["itemdrop"][l].format(item=label))


def leftover_text(day: date, language: str) -> str:
    i = safety_index(day)
    return _in(language, lambda l: _TEXTS["leftover"][l].format(safety=SAFETY[l][i]))


def wastage_text(language: str) -> str:
    return _in(language, lambda l: _TEXTS["wastage"][l])


def tip_text(day: date, language: str) -> str:
    tip = TIPS[tip_index(day)]
    return _in(language, lambda l: "💡 " + tip.get(l, tip["bm"]))


def praise_text(winners: dict, language: str) -> str:
    """``winners``: ``{category: "Jakel, Klang"}`` for the categories with a
    winner. "" when nobody qualifies."""
    if not winners:
        return ""

    def render(l):
        parts = [PRAISE_PARTS[c][l].format(who=w) for c, w in winners.items()]
        return _TEXTS["praise"][l].format(lines="; ".join(parts))
    return _in(language, render)


# --- weekly praise / summary ------------------------------------------------------------

PRAISE_MAX_SHARED = 3      # a prize shared by more outlets than this is no prize
PRAISE_MIN_ANSWERS = 3     # leftover/wastage answers needed for the wastage prize


def weekly_winners(threads, outlets, label=str) -> dict:
    """Fewest mini market buys, fewest "thrown" answers, best reply rate over
    the given week's threads, among live ``outlets``. Ties share the prize;
    a prize more than 3 outlets share is dropped. The wastage prize needs
    3+ answered leftover/wastage questions (no answers is not "no waste")."""
    stats = {o: {"mm": 0, "thrown": 0, "waste_answers": 0, "asked": 0, "answered": 0}
             for o in outlets}
    for t in threads or []:
        s = stats.get(t.get("outlet_code"))
        if s is None:
            continue
        slot, status = t.get("slot"), t.get("status")
        if slot == "minimarket":
            s["mm"] += 1
        if slot in ("leftover", "wastage") and status == "answered":
            s["waste_answers"] += 1
            s["thrown"] += t.get("reply_status") == "finished"
        if status in ("answered", "no_reply", "open", "reminded"):
            s["asked"] += 1
            s["answered"] += status == "answered"
    if not stats:
        return {}

    def best(vals, reverse=False):
        if not vals:
            return None
        target = (max if reverse else min)(vals.values())
        who = [o for o in sorted(vals) if vals[o] == target]
        if len(who) > PRAISE_MAX_SHARED:
            return None
        return ", ".join(label(o) for o in who)

    out = {
        "minimarket": best({o: s["mm"] for o, s in stats.items()}),
        "wastage": best({o: s["thrown"] for o, s in stats.items()
                         if s["waste_answers"] >= PRAISE_MIN_ANSWERS}),
    }
    rates = {o: s["answered"] / s["asked"] for o, s in stats.items() if s["asked"]}
    if rates and max(rates.values()) > 0:
        out["replies"] = best(rates, reverse=True)
    return {k: v for k, v in out.items() if v}


def summary_sections(threads, label=str) -> list[str]:
    """Director morning summary additions: unusual invoices, mini market buys,
    item sales drops + taste feedback, leftovers and wastage reported."""
    def reason(t):
        if t.get("status") == "answered":
            return t.get("reply_en") or t.get("reply_text") or "answered"
        if t.get("status") == "dropped" or (t.get("facts") or {}).get("not_asked"):
            return "not asked (daily limit)"
        return "no answer yet" if t.get("status") in ("open", "reminded") else "no reply"

    lines: list[str] = []
    inv = [t for t in threads if t.get("slot") == "invoice"]
    if inv:
        lines += ["", "🧾 Unusual invoices:"]
        for t in inv:
            f = t.get("facts") or {}
            what = (f"{f.get('item_label')} {f.get('qty_text')} (usual {f.get('usual_text')})"
                    if f.get("kind") == "high" else f"{f.get('item_label')} {f.get('qty_text')} (rarely bought)")
            lines.append(f"• {label(t.get('outlet_code'))}: {f.get('supplier')} — {what} — {reason(t)}")
    mm = [t for t in threads if t.get("slot") == "minimarket"]
    if mm:
        lines += ["", "🏪 Mini market buys:"]
        for t in mm:
            f = t.get("facts") or {}
            lines.append(f"• {label(t.get('outlet_code'))}: {f.get('shop')} — {f.get('items')} — {reason(t)}")
    drops = [t for t in threads if t.get("slot") == "afternoon" and (t.get("facts") or {}).get("item")]
    if drops:
        lines += ["", "🍛 Item sales drops + taste check:"]
        for t in drops:
            f = t.get("facts") or {}
            lines.append(f"• {label(t.get('outlet_code'))}: {f.get('label')} −{f.get('drop_pct')}% "
                         f"(shop {f.get('shop_pct'):+d}%) — {reason(t)}")
    waste = [t for t in threads if t.get("slot") in ("leftover", "wastage")
             and t.get("status") == "answered" and t.get("reply_status") != "ok"]
    if waste:
        lines += ["", "🗑️ Leftovers / wastage reported:"]
        for t in waste:
            what = "03:00 leftover" if t.get("slot") == "leftover" else "wastage"
            lines.append(f"• {label(t.get('outlet_code'))} ({what}): {reason(t)}")
    return lines


def weekly_minimarket(threads, label=str) -> list[str]:
    """Monday: mini market buys per outlet and the top reasons, last 7 days."""
    mm = [t for t in threads if t.get("slot") == "minimarket"]
    if not mm:
        return []
    per: dict = {}
    reasons: dict = {}
    for t in mm:
        per[t.get("outlet_code")] = per.get(t.get("outlet_code"), 0) + 1
        if t.get("status") == "answered":
            r = (t.get("reply_en") or "other").split(":")[0]
            reasons[r] = reasons.get(r, 0) + 1
    lines = ["", "🏪 Mini market buys — last 7 days:"]
    lines += [f"• {label(o)}: {n}" for o, n in sorted(per.items(), key=lambda kv: -kv[1])]
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        lines.append("Top reasons: " + ", ".join(f"{r} ({n})" for r, n in top))
    return lines
