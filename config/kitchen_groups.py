"""Telegram group chat_id -> outlet_code map for the Daily Kitchen Usage Log.

The kitchen-usage schedulers (post COOKED at 18:00, post LEFT at 02:00) iterate
this map and post each outlet's form to its own Telegram group. Unlike
``outlet_mapping.outlet_from_chat_title`` (which matches on the chat TITLE), this
is a hard chat_id -> code map so the right form always reaches the right group
regardless of how a group was renamed.

CONFIG STUB — paste the 10 real group chat IDs below.
====================================================
Until the IDs are filled in, ``KITCHEN_GROUPS`` stays empty and every scheduler
NO-OPS (it iterates nothing), so nothing is posted to the wrong place. Outlet
codes must match item_prices.outlet_code / outlet_mapping (SEK6, BISTRO7, SEK14,
SEK20, KLANG, VISTA, JAKEL, D, SBESI, and SEK15/One Bistro). Ayam Rempah only
appears for BISTRO7 (handled in kitchen_usage.items_for_outlet).

How to find a group's chat_id: add the bot to the group and read the
``chat.id`` of any message it receives there (it is a negative integer for
groups/supergroups, e.g. -1001234567890).

Example once known:
    KITCHEN_GROUPS = {
        -1001111111111: "SEK6",
        -1002222222222: "BISTRO7",
        ...
    }
"""
from __future__ import annotations

# chat_id (int) -> outlet_code (str). EMPTY until the real IDs are pasted.
KITCHEN_GROUPS: dict[int, str] = {
    # -100XXXXXXXXXX: "SEK6",
    # -100XXXXXXXXXX: "BISTRO7",
    # -100XXXXXXXXXX: "SEK14",
    # -100XXXXXXXXXX: "SEK20",
    # -100XXXXXXXXXX: "KLANG",
    # -100XXXXXXXXXX: "VISTA",
    # -100XXXXXXXXXX: "JAKEL",
    # -100XXXXXXXXXX: "D",
    # -100XXXXXXXXXX: "SBESI",
    # -100XXXXXXXXXX: "SEK15",
}


def outlet_for_chat(chat_id) -> str | None:
    """Resolve a Telegram group chat_id to its outlet_code, or ``None`` when the
    chat is not a configured kitchen group (schedulers/handlers no-op on None)."""
    try:
        return KITCHEN_GROUPS.get(int(chat_id))
    except (TypeError, ValueError):
        return None


def configured_groups() -> list[tuple[int, str]]:
    """(chat_id, outlet_code) pairs for every configured kitchen group. Empty
    list until the IDs are pasted, which makes the schedulers no-op cleanly."""
    return [(chat_id, code) for chat_id, code in KITCHEN_GROUPS.items()]
