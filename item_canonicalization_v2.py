"""Item-name canonicalization (v2).

Distinct concern from ``item_canonicalization`` (PR #18), which maps SUPPLIER
names. This module maps raw OCR'd ITEM LINE names (e.g. "LI AGAM 4 KG") to
stable canonical category keys (e.g. "ayam"), and filters out noise lines
like open codes, deposits, and payroll entries.

Source data: data/canonical_items_v2.json, extracted from 374 production
Khulafa Supabase receipts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).resolve().parent / "data" / "canonical_items_v2.json"


def _load() -> tuple[dict[str, list[str]], dict[str, str], list[tuple[str, str]], list[str]]:
    with _DATA_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    categories: dict[str, list[str]] = raw["categories"]
    noise: list[str] = [p.strip().upper() for p in raw.get("noise_patterns", []) if p and p.strip()]
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
    return categories, reverse, pairs, noise


_CATEGORIES, _REVERSE, _PAIRS_LONGEST_FIRST, _NOISE_PATTERNS = _load()


def _word_match(needle: str, haystack: str) -> bool:
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack) is not None


def _is_noise(cleaned: str) -> bool:
    return any(pattern in cleaned for pattern in _NOISE_PATTERNS)


def canonicalize_item(raw_name: Any) -> dict:
    """Map a raw item line name to a canonical item category.

    Returns a dict:
      - canonical: str | None
      - raw: original input
      - matched: bool   (True iff a canonical was found)
      - is_noise: bool  (True iff the line matches a noise pattern)
    """
    result = {"canonical": None, "raw": raw_name, "matched": False, "is_noise": False}

    if raw_name is None or not isinstance(raw_name, str):
        return result

    cleaned = raw_name.strip().upper()
    if not cleaned:
        return result

    if _is_noise(cleaned):
        return {"canonical": None, "raw": raw_name, "matched": False, "is_noise": True}

    if cleaned in _REVERSE:
        return {"canonical": _REVERSE[cleaned], "raw": raw_name, "matched": True, "is_noise": False}

    for variation, canonical in _PAIRS_LONGEST_FIRST:
        if _word_match(variation, cleaned):
            return {"canonical": canonical, "raw": raw_name, "matched": True, "is_noise": False}

    # NOTE: v2 deliberately omits the reverse-substring step used by
    # item_canonicalization (PR #18). PR #18 needed it so bare "AYAM" would
    # match the multi-word "AYAM BESTARI" supplier. v2's variation list lists
    # bare item names directly (AYAM, KOPI, ...), so reverse-substring would
    # only produce false positives — e.g. matching bare "TELUR" against the
    # multi-word "TELUR IKAN" (fish roe).
    return result


def list_canonical_items() -> list[str]:
    """Return all canonical item keys, sorted alphabetically."""
    return sorted(_CATEGORIES.keys())


def get_item_variations(canonical: str) -> list[str]:
    """Return the known variations for a canonical key, or [] if unknown."""
    return list(_CATEGORIES.get(canonical, []))


def classify_items_in_receipt(items: Any) -> dict:
    """Classify a receipt's items list into canonical counts, noise, and unmatched.

    ``items`` is the array as parsed from a receipt: a list of dicts each with a
    ``name`` field and optionally a ``qty`` field (int or float). When ``qty``
    is missing or non-numeric, it is treated as 1.

    Returns:
      - canonical_counts: dict[str, int|float] keyed by canonical, summing qty
      - noise_count: total qty of lines matching a noise pattern
      - unmatched: list of raw names (cleaned) that had no canonical and were not noise
    """
    out = {"canonical_counts": {}, "noise_count": 0, "unmatched": []}
    if not items or not isinstance(items, list):
        return out

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        qty_raw = item.get("qty", 1)
        try:
            qty = float(qty_raw) if qty_raw is not None else 1
            if qty == int(qty):
                qty = int(qty)
        except (TypeError, ValueError):
            qty = 1

        res = canonicalize_item(name)
        if res["is_noise"]:
            out["noise_count"] += qty
        elif res["matched"]:
            key = res["canonical"]
            out["canonical_counts"][key] = out["canonical_counts"].get(key, 0) + qty
        else:
            if isinstance(name, str) and name.strip():
                out["unmatched"].append(name.strip().upper())

    return out
