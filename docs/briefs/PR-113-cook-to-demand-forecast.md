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
- one line of reasoning that matches the number — over-cooking and how much is
  wasted daily, selling out and how often, or a weekday that simply runs busier,
- Malay header + trade words, Tamil reasoning, same register as the kitchen
  form and the wastage follow-up.

Routed through `wmr.route_message` behind two gates (see below), so until both
are flipped every plan goes to the owner with the `[TEST]` prefix. The owner
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
| level | median of trailing 28 demand days, outliers removed | one 2000-pc key-in typo must not move tomorrow's plan |
| dow_factor | that weekday's median ÷ level, shrunk `n/(n+2)`, clamped 0.70–1.40 | Friday genuinely runs above Tuesday; two observed Fridays are not a pattern |
| trend | recent 14-day median ÷ prior 14, clamped ±15 % | follows a shop that is really growing; won't chase a quiet week into under-cooking |
| safety | base 5 % + 0.5 × relative MAD + 3 %/recent sell-out, capped 25 % | a buffer sized by volatility, not a fudge factor |
| rounding | pcs whole (nearest 5 above 50), kg nearest 0.5 | a number the bench can actually act on |

**Demand truth: always Used (Cooked − Left).** The first cut preferred
`pos_qty` — customers bought it, so surely that is demand. The production
backtest (120 days, ~1250 item-days) killed that:

| Problem | Measured |
|---|---|
| POS coverage | present on **46 %** of rows — a "POS when available, else Used" series alternates between two signals and its median measures neither |
| Scale gap (median Used ÷ median POS) | ayam_goreng **3.6x**, ayam_kicap **5.0x**, ikan_goreng **5.0x**, daging **5.6x**, telur_ikan **13.4x** |

The gap is by design: `kitchen_usage` counts only dishes mapping cleanly to a
whole cut (`AYAM_EXCLUDE_SUBSTRINGS` drops rendang/kurma/isi-ayam noodle and
rice dishes; Thai and staff meals excluded outright) so the Guna-vs-POS flag
compares like with like. A conservative subset is the right input for a
mismatch gate and the wrong one for an absolute level — forecasting on it told
shops to cook a third of what they need. `pos_qty` is still carried per point
for diagnostics; nothing reads it.

**Two guards run before the model.**

*Outlier rejection* (`reject_outliers`) — the kitchen log contains cooked
entries of 2000 pcs, 3500 kg and **10,900 kg of kambing**. A point is dropped
above `max(6 x median, median + 20)`: the double condition kills fat-fingers
while keeping a genuine heavy day on a small item.

*Volume floor* — most tracked items move 3-6 a day, where a single portion
swings the percentage error by a third. Under 10 pcs / 3 kg no plan is issued
at all, so in practice a shop gets numbers for the items that actually matter
(ayam goreng, ayam bawang) rather than eleven noisy ones. Same reasoning as
`item_sales_watch._MIN_MEDIAN_QTY`.

**Sell-outs are censored demand.** A day ending `left_qty = 0` means demand was
*at least* what was cooked, and every later customer was turned away. Counting
that as ordinary demand teaches the model to keep under-cooking exactly the
dishes that sell best — so those days are flagged, lifted 10 % before entering
the level, and add safety buffer.

**Actions are symmetric around what the shop already cooks** — RAISE ("cook
more than usual"), CUT ("cook less"), HOLD ("the usual is about right"), both
directions past BOTH a % and an absolute gate. Two rules earned by simulation:

- Sell-out evidence raises on its own, and **vetoes a cut** — telling a kitchen
  to cook less in a week it kept running out is the fastest way to make it stop
  reading.
- A raise needs its *reason* to match its number. An earlier cut returned HOLD
  for a Friday uplift and printed "your current amount is right" above a number
  30 pcs bigger. Each raise now carries `sellout` / `busy_day` / `trend` and
  renders the matching explanation. Only `sellout` raises and cuts reach the
  owner summary — a routine Friday uplift is the plan working, not news.

## Measuring itself

Every forecast is written to `kitchen_demand_forecast` at plan time and scored
the next morning against what the day actually demanded — absolute and
percentage error, plus whether the shop sold out (on those days the recorded
demand is censored, so the error shown is a floor, not a fact).
`/forecast_accuracy` reports the **median** error, the within-10 %/20 % rates,
and a per-outlet breakdown. Median, not mean: one item that sold 2 instead of
20 would otherwise drag a whole month's score.

## Delivery is gated OFF by default

`COOK_PLAN_ENABLED` (default **off**, same shape as
`kitchen_usage.kitchen_log_enabled`) stops the scheduled 11:00 job dead, and
forces `/cook_plan_now` to route every plan to the owner with the `[TEST]`
prefix regardless of `MANAGER_DELIVERY_ENABLED`. An unproven forecast must
never reach a kitchen on a timer — the first production backtest of this module
measured a 23-53 % median error per outlet, and that was with the POS mixture,
the fat-fingers and the sub-floor items all still in.

Flip it on only after a backtest run shows numbers worth acting on.

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

`tests/test_demand_forecast.py` (75 cases): the Used-only demand signal and the
POS/Telur-Ikan traps, outlier rejection against the real 10,900 kg entry, the
volume floor, the kill-switch, sell-out censoring, weekday shrinkage, trend
clamps, safety sizing, the symmetric CUT/RAISE gates and the sell-out cut-veto,
reason→copy agreement, owner-summary filtering, Bistro-only item scoping,
plan→score round trip on the fake client, outlet-code bridging, idempotent
re-runs, and the missing-table degradation path.

## Follow-ups (not in this PR)

- Post the plan into the kitchen group chat as well as to the manager
  (`config/kitchen_groups.resolve_groups` already has the chat ids).
- Feed `recommend_qty` into `order_generator` so purchase drafts are driven by
  forecast demand rather than past buying rhythm.
- Festival / school-holiday calendar as an explicit factor — currently those
  days just widen the safety buffer through volatility.
