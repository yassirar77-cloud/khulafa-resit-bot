# Standing orders — fixed daily staples that bypass OCR

`roti` / `capati` / `gas` arrive on handwritten-quantity receipts OCR can't read
(Case-B verdict). They're fixed daily staples, so we don't forecast them: the
order generator emits a configured `default_qty` straight from the
`standing_orders` table. No cadence, no `forecast_qty`, no `NEEDS_REVIEW` — just
`Roti — 6 pack`. The manager can still edit the qty before sending.

## What was built
- `migrations/0030_standing_orders.sql` — `standing_orders(outlet, supplier,
  item, default_qty, unit, cadence, active)`, unique on `(outlet, item)`.
- `standing_orders.py` — best-effort fetch + pure line builder.
- `order_generator.gather_order_drafts` — emits a clean standing line per active
  row, **excludes** those items from the OCR/forecast path (no double-emit, no
  review), and surfaces outlets that have *only* standing orders (no history).
- `order_draft` — clean render (full + compact), tagged `STANDING` in
  `order_drafts`.

## Step 1 — confirm the seed values from history (do this first)
Run this in Supabase to propose a `default_qty` per (outlet, item) from the
**clean pre-24-May history** (receipts whose item extraction worked, i.e. ≥2
item_prices lines). Median per-delivery qty is robust to the odd outlier:

```sql
WITH clean_receipts AS (
  SELECT receipt_id FROM item_prices GROUP BY receipt_id HAVING count(*) >= 2
),
deliveries AS (
  SELECT ip.outlet_code, ip.canonical_item, ip.receipt_id,
         sum(ip.qty) AS delivery_qty, max(ip.receipt_date) AS d
  FROM item_prices ip
  JOIN clean_receipts c ON c.receipt_id = ip.receipt_id
  WHERE ip.canonical_item IN ('roti','capati','gas')
    AND ip.receipt_date < DATE '2026-05-24'
    AND ip.qty > 0
  GROUP BY ip.outlet_code, ip.canonical_item, ip.receipt_id
)
SELECT outlet_code, canonical_item,
       count(*) AS deliveries,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY delivery_qty)::numeric, 0)
         AS proposed_default_qty,
       min(delivery_qty) AS lo, max(delivery_qty) AS hi,
       min(d) AS first_seen, max(d) AS last_seen
FROM deliveries
GROUP BY outlet_code, canonical_item
ORDER BY outlet_code, canonical_item;
```

Paste the result; correct any numbers (the median is a starting point, not
gospel — e.g. a 2-day gas cycle may want the per-day equivalent or stay as the
per-delivery qty with `cadence='EVERY_2_DAYS'` for documentation).

## Step 2 — seed (SHIPPED in migration 0030)
The confirmed baselines are seeded by `migrations/0030_standing_orders.sql`
itself (idempotent `ON CONFLICT`), so applying the migration creates AND seeds:

| outlet | supplier | item | default_qty | unit | note |
|---|---|---|---|---|---|
| BISTRO7 | DIAMOND BALL | roti | 6 | pack | clean history |
| BISTRO7 | DIAMOND BALL | capati | 1 | pack | clean history |
| JAKEL | DIAMOND BALL | roti | 6 | pack | clean history |
| JAKEL | DIAMOND BALL | capati | 1 | pack | clean history |
| D | DIAMOND BALL | roti | 6 | pack | median n=4 |
| D | DIAMOND BALL | capati | 1 | pack | clean history |
| VISTA | INBOIS | gas | 4 | tong | 4×4 consistent |
| JAKEL | INBOIS | gas | 4 | tong | corrected 5→4 (align Vista) |
| SEK20 | RANAU PETROGAS | gas | 5 | tong | corrected 27→5 (27 was n=3 outlier) |

`ON CONFLICT` makes re-seeding idempotent and lets you adjust a baseline later.
Set `active=false` to pause a standing order without deleting it.

## PENDING_MANAGER — gaps still missing real numbers
No clean history exists for these; the owner is collecting real baselines from
outlet managers. **Deliberately not seeded** (a guessed baseline is worse than
none). Add via the same `ON CONFLICT` upsert once confirmed:

- **roti + capati**: SEK6, SBESI (Kl Sg Besi), SEK15, SEK20, VISTA, SIGNATURE
- **gas**: every outlet other than VISTA / JAKEL / SEK20

Until added, these items fall through to the normal forecast path.

## Notes / scope
- Standing orders are emitted **every run** (these are daily staples); `cadence`
  is a label today. True non-daily scheduling (e.g. gas every 2 days) would need
  a last-emitted anchor — a deliberate follow-up, not in this PR.
- Phase-2 qty model / recipe engine untouched. Outlet codes are
  `item_prices.outlet_code` (SEK6, BISTRO7, VISTA, …); items are the v2 canonical
  keys.
