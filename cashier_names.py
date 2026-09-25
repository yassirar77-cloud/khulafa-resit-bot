"""Cashier on shift — who a message to an outlet group is addressed to.

At Khulafa the cashier IS the outlet manager: they upload the bills in the
outlet's Telegram group, so the group's chat id is what ``outlet_managers``
delivers to. Every bot message to one of those groups opens with the name
of the cashier on shift at that moment ("Rahim," / "Mahadir / Pandi,"),
BM and Tamil alike.

Shifts are Malaysia time: morning 07:00-18:59, night 19:00-06:59. The night
shift runs past midnight, so 01:00 belongs to the night shift that started
at 19:00 the day before (``shift_at`` returns that start date).

Names live in the ``cashier_names`` table (outlet_code, shift, name) so the
director can change them from Telegram (``/cashier SEK20 night Ismath``)
without a code change. No name for that outlet/shift -> "Cashier,".

Lookups are served from an in-memory cache refreshed every few minutes, so
the send path adds no database round trip per message. Nothing here raises:
a failed refresh keeps the last good cache.
"""
from __future__ import annotations

import html
import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MALAYSIA_TZ = ZoneInfo("Asia/Kuala_Lumpur")

TABLE = "cashier_names"
MANAGERS_TABLE = "outlet_managers"

MORNING = "morning"
NIGHT = "night"
SHIFTS = (MORNING, NIGHT)
MORNING_START_HOUR = 7
NIGHT_START_HOUR = 19

DEFAULT_NAME = "Cashier"
CACHE_TTL_SECONDS = 300

_SHIFT_WORDS = {
    "morning": MORNING, "pagi": MORNING, "day": MORNING, "siang": MORNING,
    "night": NIGHT, "malam": NIGHT,
}

_lock = threading.Lock()
_client = None
_groups: dict[int, str] = {}           # group chat_id -> outlet_code
_names: dict[tuple[str, str], str] = {}  # (outlet_code, shift) -> name
_langs: dict[tuple[str, str], str] = {}  # (outlet_code, shift) -> language
_loaded_at: float | None = None


# --- shift logic -------------------------------------------------------------

def shift_at(now: datetime | None = None) -> tuple[str, date]:
    """``(shift, shift_start_date)`` for a moment, in Malaysia time.

    Naive datetimes are taken as Malaysia time already."""
    now = now or datetime.now(MALAYSIA_TZ)
    if now.tzinfo is not None:
        now = now.astimezone(MALAYSIA_TZ)
    if now.hour >= NIGHT_START_HOUR:
        return NIGHT, now.date()
    if now.hour < MORNING_START_HOUR:
        return NIGHT, now.date() - timedelta(days=1)
    return MORNING, now.date()


def normalize_shift(word) -> str | None:
    """"morning"/"pagi" -> morning, "night"/"malam" -> night, else None."""
    return _SHIFT_WORDS.get(str(word or "").strip().lower())


# --- cache -------------------------------------------------------------------

def configure(supabase_client) -> None:
    """Hand the module the client it refreshes from, and load once now."""
    global _client
    _client = supabase_client
    refresh()


def refresh(supabase_client=None) -> bool:
    """Reload groups and names. Keeps the old cache on failure."""
    global _groups, _names, _langs, _loaded_at
    client = supabase_client or _client
    if client is None:
        return False
    try:
        mgr_rows = client.table(MANAGERS_TABLE).select("*").execute().data or []
        groups: dict[int, str] = {}
        for row in mgr_rows:
            try:
                chat_id = int(row.get("chat_id"))
            except (TypeError, ValueError):
                continue
            code = str(row.get("outlet_code") or "").strip().upper()
            if chat_id < 0 and code:
                groups[chat_id] = code
        name_rows = client.table(TABLE).select("*").execute().data or []
        names: dict[tuple[str, str], str] = {}
        langs: dict[tuple[str, str], str] = {}
        for row in name_rows:
            code = str(row.get("outlet_code") or "").strip().upper()
            shift = normalize_shift(row.get("shift"))
            name = str(row.get("name") or "").strip()
            if code and shift and name:
                names[(code, shift)] = name
            lang = str(row.get("language") or "").strip().lower()
            if code and shift and lang:
                langs[(code, shift)] = lang
    except Exception:
        logger.exception("cashier names: refresh failed (keeping last cache)")
        with _lock:
            # Back off a full TTL instead of retrying on every message.
            _loaded_at = time.monotonic()
        return False
    with _lock:
        _groups, _names, _langs, _loaded_at = groups, names, langs, time.monotonic()
    return True


def reset_cache() -> None:
    """Forget every group and name (tests; never needed in production)."""
    global _groups, _names, _langs, _loaded_at
    with _lock:
        _groups, _names, _langs, _loaded_at = {}, {}, {}, None


def is_stale() -> bool:
    return _loaded_at is None or time.monotonic() - _loaded_at > CACHE_TTL_SECONDS


def refresh_if_stale() -> None:
    if is_stale():
        refresh()


def outlet_for_chat(chat_id) -> str | None:
    """Outlet code of a registered outlet GROUP, from the cache (no I/O)."""
    try:
        return _groups.get(int(chat_id))
    except (TypeError, ValueError):
        return None


def is_outlet_group(chat_id) -> bool:
    return outlet_for_chat(chat_id) is not None


def group_chats() -> dict[int, str]:
    """Snapshot of ``{chat_id: outlet_code}`` for every registered group."""
    return dict(_groups)


def name_for(outlet_code, shift) -> str:
    """Cashier name for an outlet and shift; "Cashier" when unknown."""
    code = str(outlet_code or "").strip().upper()
    return _names.get((code, normalize_shift(shift) or ""), DEFAULT_NAME)


def language_for(outlet_code, shift, default="bm_tamil") -> str:
    """Language the cashier on this outlet/shift reads (``/lang``)."""
    code = str(outlet_code or "").strip().upper()
    return _langs.get((code, normalize_shift(shift) or ""), default)


def language_for_chat(chat_id, now: datetime | None = None, *, shift=None,
                      default="bm_tamil") -> str:
    """Language of the cashier on shift in this group (or on ``shift``);
    ``default`` for a chat that isn't an outlet group."""
    code = outlet_for_chat(chat_id)
    if code is None:
        return default
    return language_for(code, shift or shift_at(now)[0], default)


def pick(table: dict, language: str) -> str:
    """One text from ``{language: text}`` in the cashier's language. BM+Tamil
    readers get both lines; an unknown language falls back to BM."""
    if language == "bm_tamil":
        return f"{table['bm']}\n{table['tamil']}"
    return table.get(language) or table["bm"]


def all_names() -> set[str]:
    """Every cashier name, split on "/" ("Mahadir / Pandi" -> both)."""
    out = set()
    for name in _names.values():
        out.update(p.strip() for p in name.split("/") if p.strip())
    return out


def name_on_shift(chat_id, now: datetime | None = None) -> str | None:
    """Name of the cashier on shift in this group now; None if not a group."""
    code = outlet_for_chat(chat_id)
    if code is None:
        return None
    shift, _ = shift_at(now)
    return name_for(code, shift)


# --- message prefix ----------------------------------------------------------

def address_line(name: str) -> str:
    return f"{name},"


def with_address(chat_id, text, *, parse_mode=None, now: datetime | None = None):
    """Prefix ``text`` with "<name>," when ``chat_id`` is an outlet group.

    Anything else — a DM, the director chat, empty text — is returned
    unchanged. Never raises."""
    try:
        if not isinstance(text, str) or not text.strip():
            return text
        name = name_on_shift(chat_id, now)
        if name is None:
            return text
        line = address_line(name)
        if text.startswith(line):
            return text
        if str(parse_mode or "").upper() == "HTML":
            line = html.escape(line, quote=False)
        return f"{line}\n{text}"
    except Exception:
        logger.exception("cashier names: prefix failed")
        return text


# --- edits -------------------------------------------------------------------

def set_name(supabase_client, outlet_code, shift, name, updated_by=None) -> dict:
    """Save a cashier name. Returns ``{"ok": bool, ...}``; never raises."""
    code = str(outlet_code or "").strip().upper()
    shift_norm = normalize_shift(shift)
    clean = " ".join(str(name or "").split())
    if not code:
        return {"ok": False, "error": "Outlet code missing."}
    if shift_norm is None:
        return {"ok": False, "error": "Shift must be morning or night."}
    if not clean:
        return {"ok": False, "error": "Name missing."}
    try:
        supabase_client.table(TABLE).upsert(
            {
                "outlet_code": code,
                "shift": shift_norm,
                "name": clean,
                "updated_at": datetime.now(MALAYSIA_TZ).isoformat(),
                "updated_by": updated_by,
            },
            on_conflict="outlet_code,shift",
        ).execute()
    except Exception:
        logger.exception("cashier names: save failed")
        return {"ok": False, "error": "Could not save — see logs."}
    with _lock:
        _names[(code, shift_norm)] = clean
    return {"ok": True, "outlet_code": code, "shift": shift_norm, "name": clean}


def set_language(supabase_client, outlet_code, shift, language, updated_by=None) -> dict:
    """Save the language for an outlet/shift. ``language`` must already be
    normalised by the caller (staff_chat.normalize_language). Never raises."""
    code = str(outlet_code or "").strip().upper()
    shift_norm = normalize_shift(shift)
    lang = str(language or "").strip().lower()
    if not code or shift_norm is None or not lang:
        return {"ok": False, "error": "Usage: /lang <CODE> <morning|night> <language>"}
    try:
        table = supabase_client.table(TABLE)
        rows = (
            table.select("*").eq("outlet_code", code).eq("shift", shift_norm)
            .execute().data or []
        )
        stamp = datetime.now(MALAYSIA_TZ).isoformat()
        if rows:
            (
                supabase_client.table(TABLE)
                .update({"language": lang, "updated_at": stamp, "updated_by": updated_by})
                .eq("outlet_code", code).eq("shift", shift_norm).execute()
            )
        else:
            supabase_client.table(TABLE).insert({
                "outlet_code": code, "shift": shift_norm, "name": DEFAULT_NAME,
                "language": lang, "updated_at": stamp, "updated_by": updated_by,
            }).execute()
    except Exception:
        logger.exception("cashier names: language save failed")
        return {"ok": False, "error": "Could not save — see logs."}
    with _lock:
        _langs[(code, shift_norm)] = lang
    return {"ok": True, "outlet_code": code, "shift": shift_norm, "language": lang}


# --- director-facing text ----------------------------------------------------

def _shift_label(shift: str, start: date) -> str:
    if shift == MORNING:
        return f"pagi / morning — started {start:%d %b} 07:00"
    return f"malam / night — started {start:%d %b} 19:00"


def ping_text(now: datetime | None = None) -> str:
    """The /ping_managers test message. The bot client adds the cashier's
    name on top; the shift line lets the director check the shift logic."""
    shift, start = shift_at(now)
    return (
        "🔔 Test from Khulafa bot, please ignore.\n"
        "Ujian daripada bot Khulafa — sila abaikan.\n"
        f"Shift: {_shift_label(shift, start)}"
    )


def format_roster(now: datetime | None = None) -> str:
    """Every registered group with its morning and night cashier, and who
    is on shift right now (from the cache)."""
    shift, start = shift_at(now)
    codes = sorted(set(_groups.values()))
    lines = [f"👥 Cashiers — now on shift: {_shift_label(shift, start)}", ""]
    if not codes:
        lines.append("No outlet groups registered in outlet_managers.")
    for code in codes:
        morning = _names.get((code, MORNING), f"({DEFAULT_NAME})")
        night = _names.get((code, NIGHT), f"({DEFAULT_NAME})")
        morning += f" [{language_for(code, MORNING)}]"
        night += f" [{language_for(code, NIGHT)}]"
        lines.append(f"{code}: morning {morning} · night {night}")
    lines += [
        "",
        "Change: /cashier <CODE> <morning|night> <name>",
        "e.g. /cashier SEK20 night Ismath",
        "Language: /lang <CODE> <morning|night> "
        "<tamil|bm|bengali|english|indonesian|bm_tamil>",
    ]
    return "\n".join(lines)
