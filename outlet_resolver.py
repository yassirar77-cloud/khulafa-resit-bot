"""Normalise a free-form ``receipts.outlet`` string to a canonical outlet name.

``receipts.outlet`` is derived from the Telegram chat title (bot.derive_outlet),
so it arrives in many shapes — "SEK 20", "Sek 6", "KHULAFA SEK-20", "Vista",
"Damansara", "D" — while sales rows key on the canonical names the owners use
("SEK-20", "SEK-6", "Vista", "D.U", ...). Without a single normaliser the digest
splits one outlet's spend across several raw strings and undercounts it (PR #37
bug 3: "Signature RM714 for the whole week"), and food cost % can't join
purchases to sales.

Pure + unit-testable: no I/O. The canonical names match
sales_parser.OUTLET_CANONICAL_BY_CODE so a join on the result is exact.
"""

from __future__ import annotations

import re

# Compact key (letters+digits only, upper-cased) -> canonical outlet name.
# Every realistic spelling collapses to one of these keys after _compact().
_CANONICAL_BY_KEY: dict[str, str] = {
    "BISTRO": "Bistro",
    "BISTRO7": "Bistro",
    "DU": "D.U",
    "D": "D.U",
    "DAMANSARA": "D.U",
    "JAKEL": "Jakel",
    "KLANG": "Klang B.Emas",
    "KLANGBEMAS": "Klang B.Emas",
    "KLANGBAYUEMAS": "Klang B.Emas",
    "SBESI": "SBESI",
    "SIGNATURE": "Signature",
    "SEK14": "Signature",
    "ONEBISTRO": "One Bistro",
    "SEK15": "One Bistro",
    "SEK20": "SEK-20",
    "SEK6": "SEK-6",
    "VISTA": "Vista",
    "VISTAALAM": "Vista",
    "STKHULAFA": "ST Khulafa",
    "STKHU": "ST Khulafa",
    "MB": "MB",
    "KLRAZAK": "K.L Razak",
    "RAZAK": "K.L Razak",
}

# The set of canonical names, so a value already in canonical form short-circuits.
CANONICAL_NAMES = frozenset(_CANONICAL_BY_KEY.values())

_KHULAFA_PREFIX_RE = re.compile(r"^(restoran\s+)?(nasi\s+kandar\s+)?khulafa\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]", re.IGNORECASE)


def _compact(value: str) -> str:
    """Strip a leading KHULAFA/RESTORAN/NASI KANDAR prefix, then reduce to
    letters+digits upper-cased so "SEK 20", "Sek-20", "KHULAFA SEK 20" all map
    to the single key "SEK20"."""
    no_prefix = _KHULAFA_PREFIX_RE.sub("", value).strip()
    # If stripping the prefix emptied the string the prefix WAS the name
    # (e.g. a bare "KHULAFA"); fall back to the original so it doesn't vanish.
    base = no_prefix or value
    return _NON_ALNUM_RE.sub("", base).upper()


def canonical_outlet(value) -> str | None:
    """Resolve a raw outlet string to a canonical outlet name, or ``None`` when
    it is empty / a placeholder ("UNKNOWN") / unrecognised."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or raw.upper() == "UNKNOWN":
        return None
    if raw in CANONICAL_NAMES:
        return raw
    return _CANONICAL_BY_KEY.get(_compact(raw))
