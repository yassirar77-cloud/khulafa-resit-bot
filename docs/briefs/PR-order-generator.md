# Auto Order-List Generator — Phase 1

Drafts a next-purchase order list per outlet from purchase history + cadence,
sends it to each outlet manager's Telegram for review/edit. The bot drafts; the
manager confirms; the office boy forwards to the supplier. **Never auto-sends.**

## What it does

Each evening (default 20:00 MY, ahead of the 23:00 digest) the bot:

1. Reads the passive `item_prices` corpus (one row per receipt line: outlet,
   canonical item, qty, unit_price, merchant, receipt_date) over the last
   `CADENCE_LOOKBACK_DAYS` (default 90).
2. Per (outlet × item) learns the **buying rhythm** — median gap between buys →
   DAILY / TWICE_WEEKLY / WEEKLY / MONTHLY, with a confidence from sample count
   and gap variance. Erratic/low-confidence items are flagged `NEEDS_REVIEW`,
   never force-classified or dropped.
3. Decides what's **due tomorrow** (daily always; others when last + median gap
   lands on tomorrow ± tolerance, or overdue, or matches a day-of-week pattern).
4. **Forecasts quantity**, forked by cadence: daily = trailing average daily buy,
   weekend-adjusted by a multiplier *derived from the outlet's own history*;
   weekly/monthly = the per-cycle buy (one buy already covers the cycle). Rounds
   up; flags unknown pack sizes for the manager.
5. Builds one Telegram draft per outlet, grouped by supplier, with the reasoning
   on every line and the flags: 💰 cheaper alternate (Shree Map Jaya for
   Saida/Balaji spices, Quiwave Oceanic for Fook Leong udang/sotong), ⚠️ price
   spike, ❓ NEEDS_REVIEW / unknown pack.
6. Persists drafts to `order_drafts` (status `draft`) and a learned-rhythm
   snapshot to `item_cadence`.

## Modules

- `order_items.py` — perishable/dry/exclude classification, display labels,
  cheaper-alternate rules (pure).
- `order_cadence.py` — median-gap cadence detection + due logic (pure).
- `order_draft.py` — quantity fork + weekend multiplier + draft formatting (pure).
- `order_generator.py` — DB glue: fetch `item_prices`, build per-outlet drafts,
  persist.
- `bot.py` — `post_order_drafts` evening job + `/order_drafts_now` owner command,
  routed through the **same** `MANAGER_DELIVERY_ENABLED` gate as the weekly
  report (`weekly_manager_reports.route_message`).

## Safety / human-in-the-loop

- Delivery is gated by `MANAGER_DELIVERY_ENABLED` (default **False**). While off,
  every draft routes to the owner with a `[TEST]` prefix; the owner always gets
  the HQ summary. The flag is flipped by the owner via env var when ready — it is
  **not** set True in code.
- Nothing is ever sent to a supplier automatically. `order_drafts.status` only
  reaches `sent` by a human action.

## Config (env, all optional)

| var | default | meaning |
|-----|---------|---------|
| `ORDER_DRAFT_SEND_HOUR` | `20` | evening send hour, MY time |
| `CADENCE_LOOKBACK_DAYS` | `90` | purchase-history window |
| `MANAGER_DELIVERY_ENABLED` | `False` | flip True to deliver to real managers |

## Deviations from the spec (codebase reality)

- **No "v12 wastage engine" exists** (the spec's dish→ingredient-grams forecast).
  The honest Phase-1 signal is purchase history itself — for no-stockpile
  perishables, what they buy *is* what they use. The quantity layer is isolated
  so a real sales forecast can be dropped in later without touching cadence or
  formatting.
- **No pack-size data** on receipts, so quantities round to whole units and the
  draft tags each line for the manager to confirm the real pack.
- **`outlet_manager_telegram`** (spec §7) is *not* added — the existing
  `outlet_managers` table already maps outlet → chat_id; the generator reuses it.

## Qwen OCR shadow — field-by-field scoring (spec §5)

The shadow infra already existed (measurement-only, `QWEN_SHADOW_ENABLED`-gated,
never in the live path). The missing piece was scoring the four fields that drive
ordering: **item / qty / unit / price**. Added:

- `ocr_shadow_fields.py` (pure) — aligns GLM and Qwen item lines by canonical
  item and emits per-field match rows + a win/tie/lose verdict.
- migration `ocr_shadow_log` table.
- `scripts/qwen_shadow_backfill.py` now also writes the per-field log.
- `scripts/qwen_shadow_summary.py --fields` prints per-field accuracy + the
  switch decision.

GLM stays primary; the live receipt path is untouched and the safety-rail test
(production never imports the shadow modules) still holds. To actually run the
shadow before the 2026-07-02 quota expiry:

```
QWEN_SHADOW_ENABLED=1 QWEN_API_KEY=... TELEGRAM_BOT_TOKEN=... \
SUPABASE_URL=... SUPABASE_KEY=... python scripts/qwen_shadow_backfill.py --limit 50
python scripts/qwen_shadow_summary.py --fields
```

## Deploy

1. Apply `migrations/0028_order_generator.sql` in Supabase.
2. Deploy (Render auto-deploy is manual — deploy after review).
3. Watch the nightly `[TEST]` drafts land in the owner chat; once the drafts read
   right, register managers (`/gen_codes` → `/register`) and set
   `MANAGER_DELIVERY_ENABLED=True`.
