"""Map Telegram chat titles to POS outlet codes.

Used by the receipt pipeline to resolve which outlet a chat belongs to so the
anomaly-detection layer can look up the correct March 2026 baseline.

The mapping is intentionally narrow: only chats that correspond to outlets in
``data/outlet_benchmarks.json`` get a code. Chats without a baseline (e.g.
"sek 15") return ``None`` so the intelligence layer skips them gracefully.
"""
from __future__ import annotations

from typing import Any

# Ordered list of (substring, outlet_code). Matching is case-insensitive
# substring against the chat title. Order matters: more specific patterns
# come first so they win over shorter/looser ones (e.g. "sharfuddin" is
# checked before any sek-number pattern, "sbesi" before "s besi").
_RULES: list[tuple[str, str]] = [
    ("sharfuddin", "SEK6"),
    ("bistro", "BISTRO7"),
    ("sek 14", "SEK14"),
    ("sek 20", "SEK20"),
    ("sek 6", "SEK6"),
    ("klang", "KLANG"),
    ("vista", "VISTA"),
    ("jakel", "JAKEL"),
    ("damansara", "D"),
    ("sbesi", "SBESI"),
    ("s besi", "SBESI"),
]


# Human labels for the internal outlet codes above. The order-draft header shows
# these so a manager never sees a bare code like "D" (Damansara / D.U) when the
# code doesn't line up with the outlet_canonical registry.
_DISPLAY_NAMES: dict[str, str] = {
    "SEK6": "SEK-6",
    "BISTRO7": "Bistro",
    "SEK14": "SEK-14",
    "SEK20": "SEK-20",
    "KLANG": "Klang",
    "VISTA": "Vista",
    "JAKEL": "Jakel",
    "D": "D.U",
    "SBESI": "Sungai Besi",
}


def outlet_display_name(code: Any) -> str:
    """Human label for an internal outlet code (used for the draft header).

    Falls back to the code itself for anything unmapped — never blank, never a
    bare single letter where a known name exists."""
    if not isinstance(code, str):
        return str(code) if code is not None else "?"
    key = code.strip().upper()
    return _DISPLAY_NAMES.get(key, code.strip() or "?")


def outlet_from_chat_title(chat_title: Any) -> str | None:
    """Resolve a Telegram chat title to a POS outlet code.

    Returns ``None`` for unmapped titles (including "sek 15", which is
    intentionally absent from the March 2026 benchmarks), empty strings,
    or non-string inputs.
    """
    if not isinstance(chat_title, str):
        return None
    haystack = chat_title.lower()
    if not haystack:
        return None
    for needle, code in _RULES:
        if needle in haystack:
            return code
    return None
