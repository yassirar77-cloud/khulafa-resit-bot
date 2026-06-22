"""Telegram group chat_id -> outlet_code map for the Daily Kitchen Usage Log.

The kitchen-usage schedulers (post COOKED at 18:00, post LEFT at 02:00) need to
know which chat to post each outlet's form to. The resit pipeline never keeps a
static chat_id->outlet table (``bot.GROUP_OUTLET_MAP`` is empty); it derives the
outlet from each incoming message's chat TITLE and stores ``chat_id`` + the
resolved ``outlet`` string on every ``receipts`` row. So the SAME chat IDs the
bot already receives receipts from are sitting in the receipts table.

Rather than make the owner paste 10 IDs, ``resolve_groups`` reads them straight
from there: it groups receipts by ``chat_id``, resolves each chat's stored
outlet string to a kitchen ``outlet_code`` (``outlet_code_from_text``), keeps
only group chats (negative chat_id), and — when one outlet has more than one
chat — picks the busiest (the real group, not a test/forward). The result is
cached for the process.

A manual ``KITCHEN_GROUPS`` override still wins if it is ever populated, so a
chat that can't be auto-resolved can be pinned by hand.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Optional manual override: chat_id (int) -> outlet_code (str). Leave empty to
# auto-resolve from the receipts table. Anything here WINS over auto-resolution.
KITCHEN_GROUPS: dict[int, str] = {}

# Ordered (substring, outlet_code) rules matched case-insensitively against a
# chat title / stored receipts.outlet string. ORDER MATTERS — more specific
# patterns come first so they win:
#   * "sharfuddin" before "klang": the SEK-6 group is "Hj Sharfuddin Klang Bayu
#     Emas" and would otherwise be mis-tagged KLANG.
#   * "sek 15" before "bistro": SEK-15 is "One Bistro" but its group is titled
#     "...sek 15...", while BISTRO7's group is "...bistro...".
# Codes match item_prices.outlet_code / outlet_mapping (KLRAZAK resolves to the
# "K.L Razak" canonical via outlet_resolver, matching sales' S-RAZAK).
_CODE_RULES: list[tuple[str, str]] = [
    ("sharfuddin", "SEK6"),
    ("sek 15", "SEK15"),
    ("sek15", "SEK15"),
    ("sek 20", "SEK20"),
    ("sek20", "SEK20"),
    ("sek 14", "SEK14"),
    ("sek14", "SEK14"),
    ("sek 6", "SEK6"),
    ("sek6", "SEK6"),
    ("signature", "SEK14"),
    ("one bistro", "SEK15"),
    ("bistro", "BISTRO7"),
    ("razak", "KLRAZAK"),
    ("vista", "VISTA"),
    ("jakel", "JAKEL"),
    ("damansara", "D"),
    ("sungai besi", "SBESI"),
    ("sg besi", "SBESI"),
    ("sbesi", "SBESI"),
    ("klang", "KLANG"),
]

_RECEIPTS_TABLE = "receipts"

# Process-level cache of the resolved {chat_id: outlet_code} map.
_resolved_cache: dict[int, str] | None = None


def outlet_code_from_text(text) -> str | None:
    """Resolve a chat title / stored outlet string to a kitchen outlet_code.

    Returns ``None`` for empty / placeholder ("UNKNOWN") / unrecognised values."""
    if not isinstance(text, str):
        return None
    haystack = text.strip().lower()
    if not haystack or haystack == "unknown":
        return None
    for needle, code in _CODE_RULES:
        if needle in haystack:
            return code
    return None


def resolve_groups(client, *, refresh: bool = False) -> dict[int, str]:
    """Build (and cache) the chat_id -> outlet_code map from the receipts table.

    Reuses the chat IDs the resit pipeline already receives. Only group chats
    (negative chat_id) that resolve to a known outlet_code are kept; when an
    outlet has several chats the busiest one wins. The manual ``KITCHEN_GROUPS``
    override is layered on top. Cached after the first successful build."""
    global _resolved_cache
    if _resolved_cache is not None and not refresh:
        return _resolved_cache
    if client is None:
        return dict(KITCHEN_GROUPS)

    # chat_id -> {code -> receipt_count}, so we can pick the busiest chat per code.
    counts: dict[int, dict[str, int]] = {}
    try:
        rows = (
            client.table(_RECEIPTS_TABLE)
            .select("chat_id, outlet")
            .execute()
        )
        data = getattr(rows, "data", None) or []
    except Exception:
        logger.warning("kitchen_groups: receipts lookup failed", exc_info=True)
        data = []

    for r in data:
        chat_id = r.get("chat_id")
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            continue
        if chat_id >= 0:
            # Group/supergroup chats are negative; skip private DMs.
            continue
        code = outlet_code_from_text(r.get("outlet"))
        if code is None:
            continue
        counts.setdefault(chat_id, {})
        counts[chat_id][code] = counts[chat_id].get(code, 0) + 1

    # Each chat -> its most-seen code; then each code -> its busiest chat.
    best_chat_for_code: dict[str, tuple[int, int]] = {}
    resolved: dict[int, str] = {}
    for chat_id, code_counts in counts.items():
        code = max(code_counts.items(), key=lambda kv: kv[1])[0]
        total = sum(code_counts.values())
        prev = best_chat_for_code.get(code)
        if prev is None or total > prev[1]:
            if prev is not None:
                resolved.pop(prev[0], None)
            best_chat_for_code[code] = (chat_id, total)
            resolved[chat_id] = code

    # Manual override wins.
    resolved.update(KITCHEN_GROUPS)
    _resolved_cache = resolved
    if resolved:
        logger.info("kitchen_groups: resolved %d group(s) from receipts", len(resolved))
    else:
        logger.info("kitchen_groups: no kitchen groups resolved yet")
    return resolved


def configured_groups(client=None) -> list[tuple[int, str]]:
    """(chat_id, outlet_code) pairs for every kitchen group. Resolves from the
    receipts table when a client is given, else the manual override only."""
    mapping = resolve_groups(client) if client is not None else dict(KITCHEN_GROUPS)
    return list(mapping.items())


def outlet_for_chat(chat_id, client=None) -> str | None:
    """Resolve a Telegram group chat_id to its outlet_code, or ``None`` when the
    chat is not a kitchen group."""
    try:
        key = int(chat_id)
    except (TypeError, ValueError):
        return None
    if key in KITCHEN_GROUPS:
        return KITCHEN_GROUPS[key]
    if client is not None:
        return resolve_groups(client).get(key)
    return (_resolved_cache or {}).get(key)
