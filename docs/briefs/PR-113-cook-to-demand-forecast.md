# PR #113 — Cook-to-demand forecast

**Status:** shipped
**Module:** `demand_forecast.py` · **Migration:** `0041_kitchen_demand_forecast.sql`

## Why

Every wastage alert the bot sends ends the same way — *"sales பாத்து அளவா
masak பண்ணுங்க"*, cook to the sales. The bot has never told anyone what that
number is. The kitchen learns it was wrong at 02:00, the morning after, when
the leftovers are already leftovers.

The data to answer it forward has been sitting in `kitchen_daily_usage` since
PR #32-era kitchen logging: per outlet, per day, per item — Cooked, Left,
Used, and (once the 24h POS day reconciles) the dishes actually sold. This PR
turns that history into a number the chef gets in the morning, before anything
goes in the pot.

## What ships

**Daily cook plan**, posted 11:00 MY (after the 10:45 slow-item watch, hours
before the 18:00 COOKED form). Per outlet, per tracked item:

- the quantity to cook today,
- one line of reasoning — over-cooking and how much is wasted daily, or
  selling out and how often,
- Malay header + trade words, Tamil reasoning, same register as the kitchen
  form and the wastage follow-up.

Routed through `wmr.route_message`, so until `MANAGER_DELIVERY_ENABLED` is
flipped every plan goes to the owner with the `[TEST]` prefix. The owner
always gets the English roll-up in the alert group.

**Commands:** `/cook_plan_now` (build + post on demand),
`/forecast_accuracy [days]` (how close it has been).

**Offline validation:** `scripts/backtest_demand_forecast.py` — walk-forward
replay over history (read-only), reporting both forecast error and what the
recommendation *would have changed* against what was actually cooked.

## The model

```
forecast  = level × dow_factor × trend
recommend = round(forecast × (1 + safety))
```

| Part | Choice | Why |
|---|---|---|
| level | median of trailing 28 demand days | one 3000-pc key-in typo must not move tomorrow's plan |
| dow_factor | that weekday's median ÷ level, shrunk `n/(n+2)`, clamped 0.70–1.40 | Friday genuinely runs above Tuesday; two observed Fridays are not a pattern |
| trend | recent 14-day median ÷ prior 14, clamped ±15 % | follows a shop that is really growing; won't chase a quiet week into under-cooking |
| safety | base 5 % + 0.5 × relative MAD + 3 %/recent sell-out, capped 25 % | a buffer sized by volatility, not a fudge factor |
| rounding | pcs whole (nearest 5 above 50), kg nearest 0.5 | a number the bench can actually act on |

**Demand truth, in order:** POS dishes sold (what customers bought) → Used =
Cooked − Left. Telur Ikan never uses the POS path: its `pos_qty` column holds
kg *purchased*, so reading it as demand would forecast the supplier's delivery
rhythm instead of the shop's.

**Censoring is the part that matters.** A day ending with `left_qty = 0` is a
sell-out: demand was *at least* what was cooked, and every later customer was
turned away. Counting that as ordinary demand teaches the model to keep
under-cooking exactly the dishes that sell best. Sell-out days are flagged,
lifted 10 % before entering the level, and add safety buffer.

**Actions** — CUT (over-cooking past BOTH a % and an absolute gate, with the
daily surplus named), RAISE (sold out ≥2 times in 14 days and the plan is above
the usual cook — lost sales cost more than leftovers, and a kitchen told to cut
on a sell-out day stops reading the message), HOLD (already right; saying so is
a result). An item needs 10 data days before it gets a number at all; thin
outlets are counted and skipped, never guessed at.

## Measuring itself

Every forecast is written to `kitchen_demand_forecast` at plan time and scored
the next morning against what the day actually demanded — absolute and
percentage error, plus whether the shop sold out (on those days the recorded
demand is censored, so the error shown is a floor, not a fact).
`/forecast_accuracy` reports the **median** error, the within-10 %/20 % rates,
and a per-outlet breakdown. Median, not mean: one item that sold 2 instead of
20 would otherwise drag a whole month's score.

## Safety properties

- Nothing here raises: every entry point swallows and returns a safe default,
  so a bad forecast can never block the kitchen forms, the comparison or the
  digest.
- The history read pages (`db_pagination.fetch_all_pages`) — ten outlets ×
  eleven items × 56 days is well past the PostgREST 1000-row cap, and a
  truncated read would silently forecast off a slice.
- Migration 0041 not applied yet? The plan is still built and sent; only the
  log and the accuracy scoring are skipped, with a warning naming the
  migration.
- A forecast never sees its own day: `forecast_item` uses strictly earlier
  days only, which is also what makes the backtest an honest walk-forward.
- Writes nothing into `kitchen_daily_usage`, the POS tables or any money
  figure — the feature is advisory plus self-measurement.

## Tests

`tests/test_demand_forecast.py` (52 cases): demand extraction and the Telur
Ikan trap, sell-out censoring, weekday shrinkage, trend clamps, safety sizing,
the CUT/RAISE dual gates, Bistro-only item scoping, message rendering,
plan→score round trip on the fake client, idempotent re-runs, and the
missing-table degradation path.

## Follow-ups (not in this PR)

- Post the plan into the kitchen group chat as well as to the manager
  (`config/kitchen_groups.resolve_groups` already has the chat ids).
- Feed `recommend_qty` into `order_generator` so purchase drafts are driven by
  forecast demand rather than past buying rhythm.
- Festival / school-holiday calendar as an explicit factor — currently those
  days just widen the safety buffer through volatility.
