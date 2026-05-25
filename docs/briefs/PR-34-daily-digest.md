# PR #34 — Daily Telegram digest

**Status:** Implemented. Final PR of the analytics arc.
**Depends on:** PR #30–#33 (canonical merchants/items, price_movements view).

> Brief rewritten to match the implemented spec. The earlier draft assumed a
> `supplier_arbitrage` matview and the rolling-average `price_movements` from
> the original PR #33 draft; the shipped PR #33 is a denormalised view, and this
> digest computes its windows/alerts in Python over that view.

---

## Why

Yassir wants a once-a-night Manglish summary on his phone: what came in today,
where the money went this week, which prices moved, and what still needs a human
(review queue, unresolved merchants). It reads the clean `price_movements` view
for analytics, and queries `receipts` directly to count the rows the view
*excludes* (low confidence, OCR-outlier totals) — and says so openly.

## Files

- `digest.py` — pure builders: 8 section blocks, RM formatting, 25-char name
  truncation, Markdown escaping, price-alert/aggregation logic, and
  `pack_messages` (splits at section boundaries, ≤4096 chars).
- `digest_data.py` — `gather_digest_data(client, now_my)` (shared by cron +
  command) and `log_digest`.
- `scripts/send_daily_digest.py` — cron entry point: gather → build → DM each
  recipient → log. `run()` is injectable (client/send_fn/data) for tests.
- `bot.py` — owner-only `/test_digest` (sends to YASSIR_CHAT_ID immediately).
- `migrations/0013_digest_log.sql` — delivery log table.

## Sections (8)

TODAY'S RECEIPTS · TOP SUPPLIERS TODAY · TOP ITEMS THIS WEEK · PRICE ALERTS ·
OUTLET SPENDING THIS WEEK · DATA QUALITY ALERTS · OUTLIER FILTER NOTICE ·
NEW SUPPLIERS DISCOVERED. Always all 8, even with zero data.

## Decisions

- **Digest-level outlier cut:** top-items / top-suppliers drop any aggregate
  > RM5,000 (catches the residual curry-powder-fish phantom that survived the
  view filters), and the section labels say "filtered to exclude likely OCR
  outliers". The OUTLIER FILTER NOTICE section states the count excluded.
- **Honest numbers:** the notice explicitly says totals may look low because of
  filtering.
- **Markdown safety:** all dynamic text (merchant/item/outlet names) is escaped
  so a stray `_`/`*` can't produce an unbalanced entity (Telegram 400s on those).
- **Windows (Asia/Kuala_Lumpur):** today = D; this-week = D-6..D; price alert
  recent = D-7..D vs prior = D-14..D-8; alert only if |Δ| > 10% AND the
  (item, supplier) pair has ≥3 samples in *both* windows; max 5 alerts.
- **Recipients:** `DIGEST_RECIPIENTS` (comma-sep) else `YASSIR_CHAT_ID`.

## Render cron (no render.yaml in repo — configure in the Render dashboard)

> A `render.yaml` would take over the dashboard-managed deploy, so it's
> intentionally NOT added. Create a Cron Job service:
> - **Schedule:** `0 15 * * *` (15:00 UTC = 23:00 Asia/Kuala_Lumpur)
> - **Command:** `python scripts/send_daily_digest.py`
> - **Env:** `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`, and
>   `YASSIR_CHAT_ID` (or `DIGEST_RECIPIENTS`).

## Out of scope

Email delivery, per-user schedules, interactive buttons, historical replay.

## Tests (`tests/test_digest.py`)

Zero-receipt render, outlier filtering (>RM5,000), all-8-sections, name
truncation, empty price-alerts, price-alert threshold/count gating, message
splitting at section boundaries, `run()` logging to digest_log (success +
failed), migration content, `/test_digest` owner-gating.

## Rollout

1. Apply migration 0013 in Supabase.
2. Create the Render cron job (above) — or trigger once manually.
3. `/test_digest` in Telegram to preview.
4. Confirm it arrives at 23:00 MY time.
