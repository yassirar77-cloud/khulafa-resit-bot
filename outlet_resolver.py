"""Normalise a free-form ``receipts.outlet`` string to a canonical outlet name.

``receipts.outlet`` is derived from the Telegram chat title (bot.derive_outlet),
so it arrives in many shapes — "SEK 20", "Sek 6", "KHULAFA SEK-20", "Vista",
"Damansara", "D", "HJ SHARFUDDIN SEK 6", "Kl Sg Besi" — while sales rows key on
the canonical names the owners use ("SEK-20", "SEK-6", "Vista", "D.U", ...).
Without a single normaliser the digest splits one outlet's spend across several
raw strings and undercounts it (PR #37 bug 3: "Signature RM714 for the whole
week"), and — worse — reconciliation drops every unresolved receipt, so food
cost % comes out as 0 matches against the POS payouts (hotfix).

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
    "DAMANSARAUPTOWN": "D.U",
    "JAKEL": "Jakel",
    "KLANG": "Klang B.Emas",
    "KLANGBEMAS": "Klang B.Emas",
    "KLANGBAYUEMAS": "Klang B.Emas",
    "KLANGBAYUMAS": "Klang B.Emas",
    "BAYUEMAS": "Klang B.Emas",
    "BAYUMAS": "Klang B.Emas",
    "BEMAS": "Klang B.Emas",
    "SBESI": "SBESI",
    "KLSGBESI": "SBESI",
    "SGBESI": "SBESI",
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

# Leading brand/operator tokens that aren't part of the outlet's location:
# "RESTORAN", "NASI KANDAR", "KHULAFA", and the operator "HJ/HAJI SHARFUDDIN"
# (who runs both the SEK-6 and Klang outlets). The repeating group strips a run
# of them — "HJ SHARFUDDIN SEK 6" -> "SEK 6" — and the trailing separator class
# tolerates spaces, hyphens, dots and colons ("KHULAFA-SEK20" -> "SEK20").
_BRAND_PREFIX_RE = re.compile(
    r"^(?:(?:restoran|nasi\s+kandar|khulafa|hj|haji|sharfuddin)\b[\s\-_:.]*)+",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]", re.IGNORECASE)


def _compact(value: str) -> str:
    """Strip leading brand/operator prefixes, then reduce to letters+digits
    upper-cased so "SEK 20", "Sek-20", "KHULAFA SEK 20", "HJ SHARFUDDIN SEK 20"
    all collapse to the single key "SEK20"."""
    no_prefix = _BRAND_PREFIX_RE.sub("", value).strip()
    # If stripping the prefix emptied the string the prefix WAS the name (e.g. a
    # bare "KHULAFA" or "HJ SHARFUDDIN"); fall back to the original so it doesn't
    # vanish (it simply won't resolve to a canonical outlet).
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
