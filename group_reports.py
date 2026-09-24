"""Which reports may reach an outlet's staff group.

An outlet's Telegram group is the whole shop floor, not one manager. It gets
TASKS — stock checks, slow items, cook plan, kitchen forms, order draft,
missing bills, the price-spike question. The money reports stay with the
director, who already gets each of them in full:

  ``food_cost``      weekly food-cost %          (HQ weekly summary)
  ``overbuy``        weekly overbuying question  (overbuy owner summary)
  ``praise``         weekly praise with counts   (response scoreboard)
  ``bill_analysis``  nightly bill note           (owner bill analysis)

``GROUP_MONEY_REPORTS`` (Render env) lists the ones allowed into groups,
comma-separated, e.g. ``food_cost,praise``; ``all`` allows every one. Unset
or empty (the default) keeps them all out. A manager registered by DM (a
positive chat id) is a person, not a group, and still gets everything.

The env is read on every call so a change on Render needs no code change.
"""
from __future__ import annotations

import os

FOOD_COST = "food_cost"
OVERBUY = "overbuy"
PRAISE = "praise"
BILL_ANALYSIS = "bill_analysis"
MONEY_REPORTS = (FOOD_COST, OVERBUY, PRAISE, BILL_ANALYSIS)


def allowed_in_groups() -> set[str]:
    raw = os.environ.get("GROUP_MONEY_REPORTS") or ""
    keys = {k.strip().lower() for k in raw.split(",") if k.strip()}
    if "all" in keys:
        return set(MONEY_REPORTS)
    return keys & set(MONEY_REPORTS)


def is_group_chat(chat_id) -> bool:
    """Telegram group and supergroup ids are negative; users are positive."""
    try:
        return int(chat_id) < 0
    except (TypeError, ValueError):
        return False


def blocked(report, chat_id, owner_chat_id=None) -> bool:
    """True when ``report`` must not be sent to ``chat_id``.

    The director's own chat is never blocked — the owner fallback
    ("[NO MANAGER REGISTERED]", "[TEST …]") keeps working as before."""
    if owner_chat_id is not None and _same(chat_id, owner_chat_id):
        return False
    return is_group_chat(chat_id) and report not in allowed_in_groups()


def _same(a, b) -> bool:
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False
