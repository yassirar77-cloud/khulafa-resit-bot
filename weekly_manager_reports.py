"""Weekly manager food-cost reports (PR #67, Phase 1 — content + routing).

Phase 1 is the *mechanism* plus a silent self-test. The single most important
safety property lives here:

    MANAGER_DELIVERY_ENABLED is False by default.

While it is False, EVERY weekly message routes to the owner's chat, prefixed
``[TEST — would go to {outlet} manager]``. No real manager receives anything
until the owner deliberately flips the flag (constant or env var). The True
path is built and exercised by routing, but unreachable by default.

Tone is a benchmark, never an accusation. The weekly line is exactly:

    Your food cost: X% | Group avg: Y% | Last week: Z% | {note}

Notes are contextual and supportive — a spike reads as "likely bulk stocking",
a green week as "well done", a quiet week as "possible closure". We NEVER say
wasted / over-bought / failed: a manager who feels accused stops cooperating,
and the figure is a smoothed estimate, not a verdict.

Pure module: no DB, no Telegram. bot.py fetches reconciliation rows (reusing
PR #63's sales-weighted rolling food cost) and the manager map, then calls
these to build content and decide routing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

from food_cost_analytics import food_cost_status, status_emoji

logger = logging.getLogger(__name__)

# === SAFETY FLAG =============================================================
# Default False. Build the real-delivery path, keep it unreachable by default.
# The owner flips this manually — either by editing the constant or by setting
# the MANAGER_DELIVERY_ENABLED env var — when (and only when) real managers
# should start receiving messages. Until then everything goes to the owner.
MANAGER_DELIVERY_ENABLED = False

_TEST_PREFIX = "[TEST — would go to {outlet} manager]"
_NO_MANAGER_PREFIX = "[NO MANAGER REGISTERED — {outlet}]"

# A this-week-minus-last-week jump of at least this many percentage points
# reads as a bulk stocking week rather than a problem.
SPIKE_PP = 5.0

# --- contextual notes (benchmark tone, never accusatory) ---------------------
NOTE_INCOMPLETE = (
    "Some days looked unusually quiet — possible closure or a sales upload "
    "still to come. We'll read next week with that in mind."
)
NOTE_SPIKE = (
    "Up from last week — likely bulk stocking that averages back out over the "
    "month. Nothing to action."
)
NOTE_GREEN = "Well within the healthy range — nice work keeping it tight. 🟢"
NOTE_INLINE = "Right in line with the group this week — steady."
NOTE_NEUTRAL = "Thanks for keeping the receipts flowing in — it keeps this accurate."

# Words this feature must never put in front of a manager. Guard-railed by a
# test over every generated note + message so the tone can't regress.
BANNED_WORDS = (
    "wasted", "waste", "over-bought", "overbought", "over bought",
    "failed", "failure", "bleeding", "investigate", "careless", "lazy",
    "mismanaged", "theft", "stealing", "blame",
)


def delivery_enabled() -> bool:
    """Effective flag. Env var (if set) wins so the owner can flip delivery
    without a code change, but the default is firmly False either way."""
    env = os.environ.get("MANAGER_DELIVERY_ENABLED")
    if env is not None and env.strip() != "":
        return env.strip().lower() in ("1", "true", "yes", "on")
    return MANAGER_DELIVERY_ENABLED


def contains_accusatory(text) -> bool:
    """True if ``text`` contains any banned/accusatory word (case-insensitive)."""
    if not text:
        return False
    low = str(text).lower()
    return any(w in low for w in BANNED_WORDS)


# --- prior-week date math (pure) ---------------------------------------------

def prior_week_range(today: date) -> tuple[date, date]:
    """The Monday–Sunday of the week *before* the week containing ``today``.

    The Monday 09:00 job reports on the week that just closed: for any day in
    the current week it returns the previous full Mon–Sun (7 inclusive days)."""
    this_monday = today - timedelta(days=today.weekday())
    prior_monday = this_monday - timedelta(days=7)
    prior_sunday = this_monday - timedelta(days=1)
    return prior_monday, prior_sunday


def week_before_range(today: date) -> tuple[date, date]:
    """The Mon–Sun before ``prior_week_range`` — the "Last week: Z%" baseline."""
    pm, ps = prior_week_range(today)
    return pm - timedelta(days=7), ps - timedelta(days=7)


def dates_in_range(start: date, end: date) -> list[str]:
    """Inclusive ISO date strings from ``start`` to ``end``."""
    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]


# --- content -----------------------------------------------------------------

def _fmt_pct(pct) -> str:
    return f"{pct:.1f}%" if pct is not None else "—"


def contextual_note(this_pct, last_pct, group_pct, *, complete: bool) -> str:
    """Pick a supportive, benchmark-tone note for the week.

    Priority: incomplete data first (don't over-read a disrupted week), then a
    week-on-week spike (bulk stocking), then an outright-healthy week, then an
    in-line week, then a neutral thank-you."""
    if not complete or this_pct is None:
        return NOTE_INCOMPLETE
    if last_pct is not None and (this_pct - last_pct) >= SPIKE_PP:
        return NOTE_SPIKE
    if food_cost_status(this_pct) == "green":
        return NOTE_GREEN
    if group_pct is not None and this_pct <= group_pct:
        return NOTE_INLINE
    return NOTE_NEUTRAL


def safe_note(note) -> str:
    """Runtime tone safeguard. If a note trips the accusatory-word guard — a
    future regression, a hand-edited constant, anything — swap it for a neutral
    note and log loudly rather than send it. This is enforcement, not just a
    test: no banned word reaches a manager even if the note logic changes."""
    if contains_accusatory(note):
        logger.warning("Accusatory note blocked, replaced with neutral: %r", note)
        return NOTE_NEUTRAL
    return note


def format_manager_message(outlet_display, this_pct, group_pct, last_pct, note) -> str:
    """The weekly per-outlet message. Leads with a friendly header, then the
    exact benchmark line, then the contextual note.

    The note is run through ``safe_note`` so an accusatory word can never reach
    a manager, regardless of how the note was produced."""
    emoji = status_emoji(food_cost_status(this_pct))
    return (
        f"📊 {outlet_display} — last week's food cost {emoji}\n"
        "\n"
        f"Your food cost: {_fmt_pct(this_pct)} | Group avg: {_fmt_pct(group_pct)} "
        f"| Last week: {_fmt_pct(last_pct)}\n"
        "\n"
        f"{safe_note(note)}"
    )


# --- routing -----------------------------------------------------------------

@dataclass(frozen=True)
class RouteDecision:
    target_chat_id: int
    prefix: str       # prepended to the message body ("" when going to a manager)
    is_test: bool     # True whenever the message is diverted to the owner
    reason: str       # "delivery_disabled" | "no_manager" | "manager"


def route_message(delivery_enabled_flag, outlet_display, manager_chat_id, owner_chat_id) -> RouteDecision:
    """Decide where a per-outlet message goes.

    Flag OFF  -> owner, prefixed "[TEST — would go to {outlet} manager]".
    Flag ON, manager registered -> the manager, no prefix.
    Flag ON, no manager -> owner, prefixed "[NO MANAGER REGISTERED ...]" so it
    is never silently dropped."""
    if not delivery_enabled_flag:
        return RouteDecision(
            owner_chat_id,
            _TEST_PREFIX.format(outlet=outlet_display) + "\n\n",
            True,
            "delivery_disabled",
        )
    if manager_chat_id is None:
        return RouteDecision(
            owner_chat_id,
            _NO_MANAGER_PREFIX.format(outlet=outlet_display) + "\n\n",
            True,
            "no_manager",
        )
    return RouteDecision(int(manager_chat_id), "", False, "manager")


# --- consolidated HQ summary (owner ALWAYS gets this, regardless of flag) -----

def build_hq_summary(period_label, outlet_rows, group_pct, delivery_enabled_flag) -> str:
    """One consolidated digest for the owner: every outlet's X / Y / Z, who it
    routed to, and a banner making the delivery mode unmistakable.

    ``outlet_rows``: dicts with display, this_pct, last_pct, manager_name,
    route_reason — in the order they should appear."""
    mode = (
        "🟢 LIVE — messages delivered to registered managers"
        if delivery_enabled_flag
        else "🧪 TEST MODE — every message above was sent to you, NOT to managers"
    )
    lines = [
        f"🗂 HQ Weekly Food-Cost Summary — {period_label}",
        "",
        f"Group avg: {_fmt_pct(group_pct)}",
        "",
    ]
    for r in outlet_rows:
        emoji = status_emoji(food_cost_status(r.get("this_pct")))
        if r.get("route_reason") == "manager":
            who = f"→ {r.get('manager_name') or 'manager'}"
        elif r.get("route_reason") == "no_manager":
            who = "→ (no manager registered)"
        else:
            who = "→ you (test)"
        lines.append(
            f"{emoji} {r.get('display'):<10} "
            f"this {_fmt_pct(r.get('this_pct'))}  "
            f"last {_fmt_pct(r.get('last_pct'))}  {who}"
        )
    lines += ["", mode]
    return "\n".join(lines)
