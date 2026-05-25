"""Merchant name normalisation (PR #30).

Maps a raw OCR merchant string to a canonical merchant id via a tiered match
(exact -> case-insensitive -> punctuation-normalised -> fuzzy). The matching
core (``match_merchant``) is pure and takes in-memory snapshots of the alias /
canonical tables, so it is unit-testable without Supabase. ``resolve_merchant``
is the thin DB wrapper that loads the snapshot and records auto-aliases.

Scope (PR #30): this resolves names and manages the alias tables. Wiring it
into the live upload path and backfilling historical receipts is PR #31.
"""

import logging
import re

logger = logging.getLogger(__name__)

CANONICAL_TABLE = "merchant_canonical"
ALIAS_TABLE = "merchant_alias"

# Confidence per match tier (0-100).
CONF_EXACT = 100
CONF_CASE_INSENSITIVE = 95
CONF_NORMALISED = 90
CONF_FUZZY_ALIAS = 80
CONF_FUZZY_CANONICAL = 60

# Levenshtein thresholds. Aliases are matched tightly (short merchant names
# like "REZA" produce false positives above 2); canonical display names get a
# looser budget since they're the last resort before giving up.
LEV_ALIAS_MAX = 2
LEV_CANONICAL_MAX = 5

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalise_text(value: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Used for the
    punctuation-insensitive tier and as the basis for fuzzy comparison."""
    if not value:
        return ""
    lowered = value.lower()
    stripped = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", stripped).strip()


def levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings (pure DP, no external dependency)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost,  # substitution
            ))
        previous = current
    return previous[-1]


def match_merchant(raw_text, aliases, canonicals):
    """Resolve ``raw_text`` against in-memory snapshots.

    ``aliases``: iterable of dicts with ``alias_text`` and ``canonical_id``.
    ``canonicals``: iterable of dicts with ``id`` and ``display_name``.

    Returns ``(canonical_id | None, confidence)``.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None, 0
    raw = raw_text.strip()

    # 1. Exact.
    for a in aliases:
        if a.get("alias_text") == raw:
            return a["canonical_id"], CONF_EXACT

    # 2. Case-insensitive.
    raw_lower = raw.lower()
    for a in aliases:
        if (a.get("alias_text") or "").lower() == raw_lower:
            return a["canonical_id"], CONF_CASE_INSENSITIVE

    # 3. Punctuation / whitespace normalised.
    raw_norm = normalise_text(raw)
    for a in aliases:
        if normalise_text(a.get("alias_text") or "") == raw_norm:
            return a["canonical_id"], CONF_NORMALISED

    # 4. Fuzzy vs aliases (Levenshtein <= 2 on normalised text).
    best = None
    for a in aliases:
        d = levenshtein(raw_norm, normalise_text(a.get("alias_text") or ""))
        if d <= LEV_ALIAS_MAX and (best is None or d < best[1]):
            best = (a["canonical_id"], d)
    if best is not None:
        return best[0], CONF_FUZZY_ALIAS

    # 5. Fuzzy vs canonical display names (Levenshtein <= 5).
    best = None
    for c in canonicals:
        d = levenshtein(raw_norm, normalise_text(c.get("display_name") or ""))
        if d <= LEV_CANONICAL_MAX and (best is None or d < best[1]):
            best = (c["id"], d)
    if best is not None:
        return best[0], CONF_FUZZY_CANONICAL

    # 6. No match.
    return None, 0


def load_snapshot(client):
    aliases = (
        client.table(ALIAS_TABLE).select("id, alias_text, canonical_id").execute().data
        or []
    )
    canonicals = (
        client.table(CANONICAL_TABLE).select("id, display_name").execute().data or []
    )
    return aliases, canonicals


def record_fuzzy_alias(client, raw_text, canonical_id, confidence) -> None:
    """Persist a fuzzy match as a 'fuzzy_auto' alias so the same OCR variant
    resolves exactly (and faster) next time, and surfaces for owner review."""
    try:
        client.table(ALIAS_TABLE).insert({
            "alias_text": raw_text,
            "canonical_id": canonical_id,
            "match_confidence": confidence,
            "created_via": "fuzzy_auto",
        }).execute()
    except Exception:
        logger.warning("merchant_resolver: could not record fuzzy alias %r", raw_text, exc_info=True)


def resolve_merchant(raw_text, client):
    """Resolve a merchant against the live tables. On a fuzzy (sub-100) match,
    records the raw string as a fuzzy_auto alias. Returns
    ``(canonical_id | None, confidence)``."""
    aliases, canonicals = load_snapshot(client)
    canonical_id, confidence = match_merchant(raw_text, aliases, canonicals)
    if canonical_id is not None and confidence < CONF_EXACT:
        record_fuzzy_alias(client, raw_text.strip(), canonical_id, confidence)
    return canonical_id, confidence


# --- Pure formatters (consumed by the bot's /merchant_* commands) -----------

def format_merchant_list(canonicals, alias_counts) -> str:
    """``canonicals``: rows with id/display_name/category.
    ``alias_counts``: {canonical_id: count}."""
    if not canonicals:
        return "No canonical merchants seeded."
    by_category: dict[str, list] = {}
    for c in canonicals:
        by_category.setdefault(c.get("category") or "unknown", []).append(c)
    lines = ["Canonical merchants:"]
    for category in sorted(by_category):
        rows = sorted(by_category[category], key=lambda r: r.get("display_name") or "")
        lines.append(f"\n[{category}] ({len(rows)})")
        for c in rows:
            n = alias_counts.get(c.get("id"), 0)
            lines.append(f"  #{c.get('id')} {c.get('display_name')} — {n} alias(es)")
    return "\n".join(lines)


def format_merchant_show(canonical, aliases) -> str:
    if not canonical:
        return "Canonical merchant not found."
    lines = [
        f"#{canonical.get('id')} {canonical.get('display_name')}",
        f"  legal: {canonical.get('legal_name')}",
        f"  category: {canonical.get('category')}",
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


def compute_coverage(merchant_counts, aliases, canonicals) -> dict:
    """Read-only coverage over distinct receipt merchants.

    ``merchant_counts``: iterable of ``(merchant_text, occurrence_count)``.
    Uses ``match_merchant`` (pure, no DB writes — does NOT record fuzzy
    aliases). Returns totals plus the 20 most frequent unresolved merchants.
    """
    resolved = 0
    unresolved = []
    total = 0
    for text, count in merchant_counts:
        total += 1
        cid, conf = match_merchant(text, aliases, canonicals)
        if cid is not None and conf > 0:
            resolved += 1
        else:
            unresolved.append((text, count))
    unresolved.sort(key=lambda x: (-x[1], x[0]))
    return {
        "total_unique": total,
        "resolved": resolved,
        "unresolved": len(unresolved),
        "top_unresolved": unresolved[:20],
    }


def format_coverage_report(summary) -> str:
    lines = [
        "Merchant coverage:",
        f"  unique merchants: {summary['total_unique']}",
        f"  resolved (any confidence): {summary['resolved']}",
        f"  unresolved (confidence 0): {summary['unresolved']}",
    ]
    top = summary["top_unresolved"]
    if top:
        lines.append(f"\nTop {len(top)} unresolved (by occurrences):")
        for text, count in top:
            lines.append(f"  {count:>4}x  {text}")
    else:
        lines.append("\nAll merchants resolved.")
    return "\n".join(lines)


def format_pending_aliases(aliases) -> str:
    if not aliases:
        return "No fuzzy_auto aliases pending review."
    lines = ["Fuzzy aliases pending review (confirm/reject):"]
    for a in aliases:
        lines.append(
            f"  [{a.get('id')}] {a.get('alias_text')} -> canonical #{a.get('canonical_id')} "
            f"(conf {a.get('match_confidence')})"
        )
    return "\n".join(lines)
