"""Issues staff report in their replies — flagged, routed, tracked.

The reply reader (staff_live.parse_reply) now also returns::

    "issue": {"type": "equipment|staff|supplier|customer|cash|other|none",
              "summary_en": "...", "urgent": true|false}

Every issue is saved to ``staff_issues`` (migrations/0051). An urgent one
(fire, gas leak, no electricity, robbery, injury ...) is forwarded to the
director chat at once; the rest wait for the 23:30 digest. ``/issues``
lists what is still open, ``/resolve <id>`` closes one.

The AI's reading comes first. When it gives no issue (or is down) a small
keyword rule (``classify``) still catches the plain cases — "gas habis",
"cashier tak datang", "supplier lambat" — in the languages staff write.
A reply that says everything is fine is never an issue.

Pure: no Telegram, no database. bot.py saves and sends.
"""
from __future__ import annotations

import re
from datetime import datetime

import cashier_names

TABLE = "staff_issues"
TYPES = ("equipment", "staff", "supplier", "customer", "cash", "other")
NONE = "none"

# Letters of the scripts staff write in, so a keyword never matches inside a
# longer word ("api" in "apis", "mc" in "mcd").
_L = r"A-Za-z஀-௿ঀ-৿"


def _rx(words) -> re.Pattern:
    return re.compile(rf"(?<![{_L}])(?:{'|'.join(words)})(?![{_L}])", re.IGNORECASE)


_URGENT = _rx([
    r"api", r"fire", r"kebakaran", r"terbakar", r"banjir", r"flood", r"kecemasan",
    r"emergency", r"accident", r"kemalangan", r"cedera", r"injured", r"luka", r"rompak",
    r"robbery", r"robbed", r"curi", r"theft", r"stolen", r"polis", r"police", r"hospital",
    r"pengsan", r"faint(?:ed)?", r"bocor gas", r"gas bocor", r"gas leak", r"leaking gas",
    r"blackout", r"tiada elektrik", r"takde elektrik", r"tak ada elektrik", r"no electricity",
    r"no power", r"letrik putus", r"elektrik putus", r"தீ", r"விபத்து", r"போலீஸ்", r"agun",
    r"dakat", r"chor", r"bidyut nai", r"current nai", r"கரண்ட் இல்ல",
])

_RULES = (
    # order matters: a supplier problem mentions goods, a staff problem people
    ("supplier", _rx([
        r"supplier", r"pembekal", r"delivery", r"deliver", r"hantar", r"penghantaran",
        r"tak sampai", r"belum sampai", r"x sampai", r"barang (?:tak|belum|x) (?:sampai|datang)",
        r"lori", r"lorry", r"maal ashe nai", r"maal ase nai", r"சாமான் வரல", r"supplier வரல",
        r"stok (?:tak|belum) sampai", r"barang lambat",
    ])),
    ("staff", _rx([
        r"cashier", r"kasir", r"staff", r"pekerja", r"worker", r"chef", r"tukang masak",
        r"tak datang", r"tidak datang", r"x datang", r"takde orang", r"tak ada orang",
        r"kurang orang", r"mc", r"sakit", r"sick", r"cuti", r"absent", r"resign", r"berhenti",
        r"lambat masuk", r"ashe nai", r"oshustho", r"ஆள் இல்ல", r"ஆள் வரல", r"cashier வரல",
        r"staff இல்ல",
    ])),
    ("equipment", _rx([
        r"gas", r"aircon(?:d)?", r"air-cond", r"penghawa dingin", r"peti(?: ais| sejuk)?",
        r"fridge", r"chiller", r"freezer", r"rosak", r"broken", r"spoil(?:t|ed)?", r"dapur",
        r"stove", r"elektrik", r"letrik", r"electric", r"lampu", r"paip", r"pipe", r"bocor",
        r"leak(?:ing)?", r"mesin", r"machine", r"printer", r"wifi", r"pos", r"kipas", r"fan",
        r"sinki", r"sink", r"tandas", r"toilet", r"tersumbat", r"blocked", r"generator",
        r"bhenge", r"nosto", r"ரோசக்", r"உடைஞ்சு", r"ஓடல", r"வேலை செய்யல",
    ])),
    ("customer", _rx([
        r"customer", r"pelanggan", r"complain(?:t|ed)?", r"komplen", r"komplain", r"aduan",
        r"refund", r"marah", r"angry", r"food poisoning", r"sakit perut", r"rambut dalam",
        r"வாடிக்கையாளர்", r"customer மாறி", r"grahok", r"complain korche",
    ])),
    ("cash", _rx([
        r"cash", r"tunai", r"duit", r"wang", r"kurang rm", r"short rm", r"drawer",
        r"laci", r"tak cukup duit", r"duit tak cukup", r"taka kom", r"taka", r"பணம்", r"காசு",
        r"salah kira", r"lebih rm", r"cash kurang", r"cash short",
    ])),
)

_FINE = re.compile(
    r"^\W*(?:ok(?:ay|e)?|semua ok|all ok|all good|fine|baik|tak ?ada|takde|tiada|no(?:ne)?"
    r"|nothing|thik ache|shob thik|எல்லாம் சரி|சரி|ஓகே|aman|beres|done|dah|siap"
    r"|noted|ya|yes|boleh)\W*(?:boss?|bos|tuan|sir)?\W*$",
    re.IGNORECASE,
)


def classify(text) -> dict | None:
    """Keyword reading of a reply: ``{type, urgent}`` or None when nothing in
    it sounds like a problem. Only the plain cases; the AI does the rest."""
    raw = str(text or "").strip()
    if not raw or _FINE.match(raw):
        return None
    urgent = bool(_URGENT.search(raw))
    for kind, rx in _RULES:
        if rx.search(raw):
            return {"type": kind, "urgent": urgent}
    if urgent:
        return {"type": "other", "urgent": True}
    return None


def normalize(raw) -> dict | None:
    """The AI's ``issue`` object, cleaned: None unless it names a real type."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or NONE).strip().lower()
    if kind not in TYPES:
        return None
    return {"type": kind, "summary_en": str(raw.get("summary_en") or "").strip()[:300],
            "urgent": raw.get("urgent") is True}


_PROBLEM_STATUSES = ("problem", "short", "finished", "other")


def from_reply(parsed: dict | None, text, *, force: bool = False) -> dict | None:
    """The issue in one reply, if any. The AI's reading wins; when it gave
    none the keyword rule is tried for replies that were not a plain OK or
    an order (``force`` tries it regardless — typed details after a
    "Problem" tap)."""
    ai = normalize((parsed or {}).get("issue")) if parsed else None
    if ai:
        kw = classify(text)
        if kw and kw["urgent"]:
            ai["urgent"] = True
        return ai
    status = (parsed or {}).get("status")
    if parsed is not None and not force and status not in _PROBLEM_STATUSES:
        return None
    kw = classify(text)
    if not kw:
        return None
    summary = (parsed or {}).get("summary_en") or str(text or "").strip()[:200]
    return {"type": kw["type"], "summary_en": summary, "urgent": kw["urgent"]}


def row(thread: dict, issue: dict, text, now: datetime | None = None) -> dict:
    """``staff_issues`` row for one flagged reply."""
    return {
        "outlet_code": thread.get("outlet_code"),
        "chat_id": thread.get("chat_id"),
        "thread_id": thread.get("id"),
        "slot": thread.get("slot"),
        "cashier": thread.get("cashier"),
        "type": issue["type"],
        "summary_en": issue.get("summary_en") or "",
        "urgent": bool(issue.get("urgent")),
        "raw_reply": str(text or "")[:1000],
        "ts": (now or datetime.now(cashier_names.MALAYSIA_TZ)).isoformat(),
    }


_ICON = {"equipment": "🔧", "staff": "👤", "supplier": "🚚", "customer": "🙋", "cash": "💵",
         "other": "❔"}


def urgent_text(issue_row: dict, label=str) -> str:
    """The immediate forward to the director chat."""
    return (f"🚨 URGENT — {label(issue_row.get('outlet_code'))} "
            f"({issue_row.get('type')}): {issue_row.get('summary_en') or issue_row.get('raw_reply')}\n"
            f"— {issue_row.get('cashier') or 'cashier'} wrote: {issue_row.get('raw_reply')}\n"
            f"Issue #{issue_row.get('id', '?')} · /resolve {issue_row.get('id', '<id>')} when handled")


def _hhmm(value) -> str:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "?"
    return ts.astimezone(cashier_names.MALAYSIA_TZ).strftime("%d/%m %H:%M")


def format_open(rows: list[dict], label=str) -> str:
    """``/issues``: every open issue, urgent first, newest first."""
    if not rows:
        return "✅ No open staff issues."
    rows = sorted(rows, key=lambda r: (not r.get("urgent"), -int(r.get("id") or 0)))
    lines = [f"🛠️ Open staff issues ({len(rows)}) — /resolve <id> to close one", ""]
    for r in rows:
        mark = "🚨" if r.get("urgent") else _ICON.get(r.get("type"), "❔")
        lines.append(f"{mark} #{r.get('id')} {label(r.get('outlet_code'))} · {r.get('type')} · "
                     f"{_hhmm(r.get('ts') or r.get('created_at'))}\n"
                     f"   {r.get('summary_en') or r.get('raw_reply')}")
    return "\n".join(lines)
