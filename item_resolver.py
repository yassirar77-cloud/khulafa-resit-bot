"""Item name normalisation (PR #32).

Maps a raw OCR item name to a canonical item id via the same tiered match the
merchant resolver uses (exact -> case-insensitive -> punctuation-normalised ->
substring containment -> fuzzy). The matching algorithm is shared with
``merchant_resolver`` (imported, not re-implemented) — only the table names and
DB wrappers differ. Substring containment matters a lot here: items arrive as
"AYAM BERSIH 30KG", "JINTAN PUTIH 1KG", etc., which contain the canonical name.

Scope (PR #32): resolves names and manages the alias tables. Populating
``item_resolutions`` (the backfill) is PR #32b.
"""

import logging

from merchant_resolver import (
    CONF_EXACT,
    compute_coverage,
    match_merchant,
    tier_for_confidence,
)

logger = logging.getLogger(__name__)

CANONICAL_TABLE = "item_canonical"
ALIAS_TABLE = "item_alias"

# Re-export so callers (and the backfill) read item confidence semantics from
# here rather than reaching into merchant_resolver.
tier_for_confidence = tier_for_confidence
compute_coverage = compute_coverage


def match_item(raw_text, aliases, canonicals):
    """Pure resolver. ``aliases``: dicts with ``alias_text``/``canonical_id``;
    ``canonicals``: dicts with ``id``/``display_name``. Returns
    ``(canonical_id | None, confidence)``. Identical tiering to merchants."""
    return match_merchant(raw_text, aliases, canonicals)


def load_snapshot(client):
    aliases = (
        client.table(ALIAS_TABLE).select("id, alias_text, canonical_id").execute().data or []
    )
    canonicals = (
        client.table(CANONICAL_TABLE).select("id, display_name").execute().data or []
    )
    return aliases, canonicals


def record_fuzzy_alias(client, raw_text, canonical_id, confidence) -> None:
    """Persist a fuzzy item match as a 'fuzzy_auto' alias so the same OCR
    variant resolves exactly next time and surfaces for owner review."""
    try:
        client.table(ALIAS_TABLE).insert({
            "alias_text": raw_text,
            "canonical_id": canonical_id,
            "match_confidence": confidence,
            "created_via": "fuzzy_auto",
        }).execute()
    except Exception:
        logger.warning("item_resolver: could not record fuzzy alias %r", raw_text, exc_info=True)


def resolve_item(raw_text, client):
    """Resolve an item name against the live tables. On a sub-100 match records
    the raw string as a fuzzy_auto alias. Returns ``(canonical_id | None,
    confidence)``."""
    aliases, canonicals = load_snapshot(client)
    canonical_id, confidence = match_item(raw_text, aliases, canonicals)
    if canonical_id is not None and confidence < CONF_EXACT:
        record_fuzzy_alias(client, raw_text.strip(), canonical_id, confidence)
    return canonical_id, confidence


# --- pure formatters (consumed by the /item_* commands) ---------------------

def format_item_list(canonicals, alias_counts) -> str:
    if not canonicals:
        return "No canonical items seeded."
    by_category: dict[str, list] = {}
    for c in canonicals:
        by_category.setdefault(c.get("category") or "other", []).append(c)
    lines = ["Canonical items:"]
    for category in sorted(by_category):
        rows = sorted(by_category[category], key=lambda r: r.get("display_name") or "")
        lines.append(f"\n[{category}] ({len(rows)})")
        for c in rows:
            n = alias_counts.get(c.get("id"), 0)
            unit = c.get("unit")
            unit_str = f" /{unit}" if unit else ""
            lines.append(f"  #{c.get('id')} {c.get('display_name')}{unit_str} — {n} alias(es)")
    return "\n".join(lines)


def format_item_show(canonical, aliases) -> str:
    if not canonical:
        return "Canonical item not found."
    lines = [
        f"#{canonical.get('id')} {canonical.get('display_name')}",
        f"  category: {canonical.get('category')}",
        f"  unit: {canonical.get('unit')}",
    ]
    if canonical.get("notes"):
        lines.append(f"  notes: {canonical.get('notes')}")
    lines.append(f"  aliases ({len(aliases)}):")
    for a in aliases:
        lines.append(
            f"    [{a.get('id')}] {a.get('alias_text')} "
            f"({a.get('created_via')}, conf {a.get('match_confidence')})"
        )
    return "\n".join(lines)


def format_pending_aliases(aliases) -> str:
    if not aliases:
        return "No fuzzy_auto item aliases pending review."
    lines = ["Fuzzy item aliases pending review (confirm/reject):"]
    for a in aliases:
        lines.append(
            f"  [{a.get('id')}] {a.get('alias_text')} -> canonical #{a.get('canonical_id')} "
            f"(conf {a.get('match_confidence')})"
        )
    return "\n".join(lines)


def format_coverage_report(summary) -> str:
    lines = [
        "Item coverage:",
        f"  unique item names: {summary['total_unique']}",
        f"  resolved (any confidence): {summary['resolved']}",
        f"  unresolved (confidence 0): {summary['unresolved']}",
    ]
    top = summary["top_unresolved"]
    if top:
        lines.append(f"\nTop {len(top)} unresolved (by occurrences):")
        for text, count in top:
            lines.append(f"  {count:>4}x  {text}")
    else:
        lines.append("\nAll item names resolved.")
    return "\n".join(lines)
