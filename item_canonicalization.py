"""Supplier / item name canonicalization.

Loads the canonical items mapping once at import time and exposes lookup
helpers used by the receipt pipeline to normalize raw OCR'd supplier names
into stable category keys.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).resolve().parent / "data" / "canonical_items.json"


def _load() -> tuple[dict[str, list[str]], dict[str, str], list[tuple[str, str]]]:
    with _DATA_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    categories: dict[str, list[str]] = raw["categories"]
    reverse: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for canonical, variations in categories.items():
        for variation in variations:
            up = variation.strip().upper()
            if not up:
                continue
            reverse.setdefault(up, canonical)
            pairs.append((up, canonical))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return categories, reverse, pairs


_CATEGORIES, _REVERSE, _PAIRS_LONGEST_FIRST = _load()


def _word_match(needle: str, haystack: str) -> bool:
    """True iff needle appears in haystack on word boundaries."""
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack) is not None


def canonicalize_supplier(raw_name: Any) -> dict:
    """Map a raw supplier name to a canonical category.

    Returns a dict with keys:
      - canonical: str | None
      - raw: original input
      - matched: bool
    """
    result = {"canonical": None, "raw": raw_name, "matched": False}

    if raw_name is None or not isinstance(raw_name, str):
        return result

    cleaned = raw_name.strip().upper()
    if not cleaned:
        return result

    if cleaned in _REVERSE:
        return {"canonical": _REVERSE[cleaned], "raw": raw_name, "matched": True}

    for variation, canonical in _PAIRS_LONGEST_FIRST:
        if _word_match(variation, cleaned):
            return {"canonical": canonical, "raw": raw_name, "matched": True}

    for variation, canonical in reversed(_PAIRS_LONGEST_FIRST):
        if _word_match(cleaned, variation):
            return {"canonical": canonical, "raw": raw_name, "matched": True}

    return result


def list_canonical_categories() -> list[str]:
    """Return all canonical category keys, sorted alphabetically."""
    return sorted(_CATEGORIES.keys())


def get_variations(canonical: str) -> list[str]:
    """Return the list of known raw-name variations for a canonical category.

    Returns an empty list if the canonical key is unknown.
    """
    return list(_CATEGORIES.get(canonical, []))
