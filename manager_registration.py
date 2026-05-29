"""Outlet-manager registration (PR #67, Phase 1 — Option A one-time codes).

Two tables back this (see migrations/0024_outlet_manager_reports.sql):

  outlet_registration_codes  one-time codes the owner generates and hands out
  outlet_managers            the resolved outlet -> manager_name -> chat_id map

Flow:
  1. Owner runs /gen_codes -> one fresh code per outlet, e.g. ``SEK20-7K2A``.
     Generating new codes invalidates any prior *unused* code for that outlet
     so only the latest is live (a hand-out sheet can't be replayed forever).
  2. A manager DMs the bot ``/register SEK20-7K2A``. We validate the code,
     map outlet -> their name + chat_id, mark the code used, and REPLACE any
     existing manager for that outlet (staff turnover is the norm, not the
     exception, in a mamak chain).
  3. A bad / unknown / used code returns a clean, generic error that NEVER
     leaks the outlet list or which codes exist.

I/O convention matches reconciliation_service: every DB function takes the
``supabase`` client as its first argument, so this module stays import-safe
(no client construction at import time) and trivially testable with a fake.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

CODES_TABLE = "outlet_registration_codes"
MANAGERS_TABLE = "outlet_managers"

# Unambiguous code alphabet: no 0/O, 1/I/L — a manager typing a code off a
# printed slip shouldn't have to guess. 4 random chars after the outlet prefix.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_SUFFIX_LEN = 4

# Generic, leak-free errors. They must never name an outlet or reveal whether a
# given code exists — a stranger guessing codes learns nothing.
INVALID_CODE_MESSAGE = (
    "That registration code isn't valid. Please double-check it with the "
    "office and try again."
)
USED_CODE_MESSAGE = (
    "That registration code has already been used. Please ask the office for "
    "a new one."
)


@dataclass(frozen=True)
class Outlet:
    code: str        # short prefix used in registration codes, e.g. "SEK20"
    display: str     # human label, e.g. "SEK 20"
    canonical: str   # matches purchase_reconciliation.outlet_canonical


# The nine outlets that have a March-2026 food-cost baseline (data/outlet_
# benchmarks.json). ``canonical`` ties each to the name reconciliation rows use,
# so a manager's weekly food cost can be looked up. SEK 15 / ST Khulafa / MB /
# Razak are intentionally absent (no baseline -> no benchmark message).
OUTLETS: tuple[Outlet, ...] = (
    Outlet("SEK6", "SEK 6", "SEK-6"),
    Outlet("SEK14", "SEK 14", "Signature"),
    Outlet("SEK20", "SEK 20", "SEK-20"),
    Outlet("KLANG", "Klang", "Klang B.Emas"),
    Outlet("VISTA", "Vista", "Vista"),
    Outlet("JAKEL", "Jakel", "Jakel"),
    Outlet("BISTRO7", "Bistro", "Bistro"),
    Outlet("SBESI", "S.Besi", "SBESI"),
    Outlet("DU", "Damansara", "D.U"),
)

_BY_CODE = {o.code: o for o in OUTLETS}
_BY_CANONICAL = {o.canonical: o for o in OUTLETS}


def outlet_by_code(code) -> Outlet | None:
    return _BY_CODE.get((code or "").strip().upper())


def outlet_by_canonical(canonical) -> Outlet | None:
    return _BY_CANONICAL.get(canonical)


def display_name(outlet_code) -> str:
    o = outlet_by_code(outlet_code)
    return o.display if o else (outlet_code or "?")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- code generation (pure) --------------------------------------------------

def _random_suffix(rng=None) -> str:
    pick = (rng.choice if rng is not None else secrets.choice)
    return "".join(pick(_CODE_ALPHABET) for _ in range(_CODE_SUFFIX_LEN))


def generate_registration_code(outlet_code, *, rng=None) -> str:
    """One code in the ``SEK20-7K2A`` shape: outlet prefix + dash + 4 chars."""
    return f"{(outlet_code or '').strip().upper()}-{_random_suffix(rng)}"


def normalize_code(code) -> str:
    """Canonical form for storage / comparison: trimmed + upper-cased."""
    return (code or "").strip().upper()


# --- persistence (take the supabase client) ----------------------------------

def create_registration_codes(supabase, *, rng=None) -> list[dict]:
    """Generate and persist one fresh code per outlet, invalidating any prior
    *unused* code for that outlet first. Returns
    ``[{outlet_code, display, code}]`` in OUTLETS order for the owner to hand
    out."""
    out: list[dict] = []
    for o in OUTLETS:
        # Drop superseded, still-unused codes so only the newest is live.
        supabase.table(CODES_TABLE).delete().eq("outlet_code", o.code).eq(
            "used", False
        ).execute()
        code = generate_registration_code(o.code, rng=rng)
        supabase.table(CODES_TABLE).insert(
            {
                "outlet_code": o.code,
                "code": code,
                "used": False,
                "created_at": _now_iso(),
            }
        ).execute()
        out.append({"outlet_code": o.code, "display": o.display, "code": code})
    return out


def register_manager(supabase, code, manager_name, chat_id) -> dict:
    """Validate a one-time code and (re)map its outlet to a manager.

    Returns ``{"ok": True, "outlet_code", "outlet_display"}`` on success, or
    ``{"ok": False, "error": <generic message>}`` otherwise. The error text is
    deliberately generic — it never reveals the outlet behind a code or whether
    a code exists."""
    norm = normalize_code(code)
    if not norm:
        return {"ok": False, "error": INVALID_CODE_MESSAGE}

    resp = (
        supabase.table(CODES_TABLE)
        .select("*")
        .eq("code", norm)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return {"ok": False, "error": INVALID_CODE_MESSAGE}

    row = rows[0]
    if row.get("used"):
        return {"ok": False, "error": USED_CODE_MESSAGE}

    outlet_code = row.get("outlet_code")
    display = display_name(outlet_code)

    # Staff turnover: a fresh registration REPLACES the prior manager for the
    # outlet rather than stacking, so an outlet always has exactly one manager.
    supabase.table(MANAGERS_TABLE).delete().eq("outlet_code", outlet_code).execute()
    supabase.table(MANAGERS_TABLE).insert(
        {
            "outlet_code": outlet_code,
            "manager_name": manager_name,
            "chat_id": chat_id,
            "registered_at": _now_iso(),
        }
    ).execute()

    # Burn the code so it can't be reused.
    supabase.table(CODES_TABLE).update(
        {"used": True, "used_by_chat_id": chat_id, "used_at": _now_iso()}
    ).eq("code", norm).execute()

    return {"ok": True, "outlet_code": outlet_code, "outlet_display": display}


def get_manager(supabase, outlet_code) -> dict | None:
    resp = (
        supabase.table(MANAGERS_TABLE)
        .select("*")
        .eq("outlet_code", outlet_code)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def get_all_managers(supabase) -> dict:
    """``{outlet_code: manager_row}`` for every registered outlet."""
    resp = supabase.table(MANAGERS_TABLE).select("*").execute()
    return {m.get("outlet_code"): m for m in (resp.data or [])}
