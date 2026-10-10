"""One normaliser for POS item names, shared by ingestion and the kitchen
comparison, so "Ikan  Goreng", " Ayam_goreng" and "Briyani Awam Bawang" are
matched as "ikan goreng", "ayam goreng" and "briyani ayam bawang"."""
from __future__ import annotations

import re

from config.kitchen_compare import POS_NAME_TYPOS

_SEPARATORS_RE = re.compile(r"[_+]")
_SPACES_RE = re.compile(r"\s+")
_TYPO_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, POS_NAME_TYPOS), key=len, reverse=True)) + r")\b"
) if POS_NAME_TYPOS else None


def normalize_pos_name(name) -> str:
    """Lower-case, "_"/"+" to spaces, whitespace collapsed and trimmed, known
    POS typos fixed (whole words only)."""
    n = _SEPARATORS_RE.sub(" ", str(name or "").lower())
    n = _SPACES_RE.sub(" ", n).strip()
    if _TYPO_RE is not None:
        n = _TYPO_RE.sub(lambda m: POS_NAME_TYPOS[m.group(1)], n)
    return n


def name_issues(raw) -> list[str]:
    """What normalisation had to fix in a raw POS name (for the audit)."""
    raw = str(raw or "")
    out = []
    if raw != raw.strip():
        out.append("leading/trailing space")
    if re.search(r"\s{2,}", raw.strip()):
        out.append("double space")
    if "_" in raw or "+" in raw:
        out.append("'_'/'+' separator")
    base = _SPACES_RE.sub(" ", _SEPARATORS_RE.sub(" ", raw.lower())).strip()
    for word in re.findall(r"[a-z0-9]+", base):
        if word in POS_NAME_TYPOS:
            out.append(f"typo '{word}'→'{POS_NAME_TYPOS[word]}'")
    return out
