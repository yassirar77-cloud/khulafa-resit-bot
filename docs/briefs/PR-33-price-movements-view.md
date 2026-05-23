# PR #33 — Price movements materialised view

**Status:** Brief drafted, not implemented
**Depends on:** PR #32 (item_canonical_id and merchant_canonical_id
must exist on `item_prices`). Must land after.
**Blocks:** PR #34 (the daily digest reads from these two
materialised views).

---

## Why

The daily digest needs to answer questions like "what changed in
price today vs the 7-day average?" against potentially millions of
`item_prices` rows. Running that aggregation at 23:00 every night
against the live OLTP table is fragile (lock contention with
incoming receipts, latency spikes, depends on which indexes happen
to be hot).

Two materialised views, refreshed once nightly at 22:50, do all
the heavy lifting:

* `price_movements` — per-(item, merchant, outlet, date) average
  price, plus 7-day and 30-day rolling averages with percent
  change.
* `supplier_arbitrage` — per-item min/max price across active
  suppliers over the last 30 days, plus the implied saving if the
  outlet switched to the cheapest.

The digest at 23:00 then runs single-row lookups against these
views — sub-second, no aggregation at read time.

## Scope

### 1. `price_movements` materialised view

```sql
CREATE MATERIALIZED VIEW price_movements AS
WITH daily_prices AS (
  SELECT
    item_canonical_id,
    merchant_canonical_id,
    outlet,
    receipt_date,
    AVG(unit_price_per_base_unit) AS avg_price,
    COUNT(*)                       AS sample_count,
    SUM(qty_in_base_unit)          AS total_qty
  FROM item_prices
  WHERE item_canonical_id     IS NOT NULL
    AND merchant_canonical_id IS NOT NULL
    AND unit_price_per_base_unit IS NOT NULL
    AND unit_price_per_base_unit > 0
  GROUP BY 1, 2, 3, 4
),
rolling AS (
  SELECT
    *,
    AVG(avg_price) OVER (
      PARTITION BY item_canonical_id, merchant_canonical_id
      ORDER BY receipt_date
      RANGE BETWEEN INTERVAL '7 days'  PRECEDING AND INTERVAL '1 day' PRECEDING
    ) AS avg_7d,
    AVG(avg_price) OVER (
      PARTITION BY item_canonical_id, merchant_canonical_id
      ORDER BY receipt_date
      RANGE BETWEEN INTERVAL '30 days' PRECEDING AND INTERVAL '1 day' PRECEDING
    ) AS avg_30d
  FROM daily_prices
)
SELECT
  *,
  ((avg_price - avg_7d)  / NULLIF(avg_7d,  0)) * 100 AS pct_change_7d,
  ((avg_price - avg_30d) / NULLIF(avg_30d, 0)) * 100 AS pct_change_30d
FROM rolling;

CREATE UNIQUE INDEX idx_pm_unique
  ON price_movements(item_canonical_id, merchant_canonical_id, outlet, receipt_date);
CREATE INDEX idx_pm_date       ON price_movements(receipt_date DESC);
CREATE INDEX idx_pm_pct_change ON price_movements(pct_change_7d DESC);
```

Notes:

* The `WHERE unit_price_per_base_unit > 0` guard skips rows that
  PR #32's normaliser couldn't parse (NULL or zero).
* `RANGE BETWEEN INTERVAL '7 days' PRECEDING ... '1 day' PRECEDING`
  excludes the current day from the rolling average — so today's
  price spike doesn't average into its own baseline.
* The unique index lets us refresh `CONCURRENTLY` (PostgreSQL
  requires a unique index on a materialised view for concurrent
  refresh).

### 2. `supplier_arbitrage` materialised view

```sql
CREATE MATERIALIZED VIEW supplier_arbitrage AS
SELECT
  item_canonical_id,
  MIN(avg_price)                                      AS min_price,
  MAX(avg_price)                                      AS max_price,
  ARRAY_AGG(DISTINCT merchant_canonical_id
            ORDER BY avg_price ASC)                   AS merchants_by_price,
  ((MAX(avg_price) - MIN(avg_price))
    / NULLIF(MIN(avg_price), 0)) * 100                AS savings_pct,
  SUM(sample_count)                                    AS total_purchases_30d,
  SUM(total_qty)                                       AS total_qty_30d
FROM price_movements
WHERE receipt_date >= NOW() - INTERVAL '30 days'
GROUP BY item_canonical_id
HAVING ((MAX(avg_price) - MIN(avg_price))
        / NULLIF(MIN(avg_price), 0)) * 100 > 10
   AND COUNT(DISTINCT merchant_canonical_id) >= 2;

CREATE UNIQUE INDEX idx_sa_item ON supplier_arbitrage(item_canonical_id);
```

Two filters:

* `savings_pct > 10` — only show items where switching supplier
  saves at least 10%. Tighter thresholds (e.g. 20%) tuned by PR #34
  based on the digest "noise level".
* `COUNT(DISTINCT merchant) >= 2` — arbitrage needs at least two
  suppliers to compare.

PR #34 layers on a per-row dollar-saving filter (RM20/month
minimum) when surfacing in the digest.

Migration filename: `migrations/0010_price_movements_views.sql`.

### 3. Refresh schedule

In `bot.py` (or wherever the existing APScheduler instance lives):

```python
scheduler.add_job(
    refresh_price_movements,
    'cron', hour=22, minute=50,
    timezone='Asia/Kuala_Lumpur',
    id='refresh_price_movements',
    misfire_grace_time=600,   # allow up to 10 min late
    coalesce=True,
)
```

`refresh_price_movements` runs:

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY price_movements;
REFRESH MATERIALIZED VIEW CONCURRENTLY supplier_arbitrage;
```

and writes a row to a `view_refresh_log` table (see section 4).

If the refresh fails (e.g. unique index conflict, dead lock):

* Log the exception to Render.
* Insert a row with `status = 'failed'` and the exception text.
* Do NOT retry automatically in the same window — PR #34's
  digest reads the latest successful refresh and warns owners if
  data is stale.

### 4. `view_refresh_log` health table

```sql
CREATE TABLE view_refresh_log (
  id BIGSERIAL PRIMARY KEY,
  view_name TEXT NOT NULL,
  started_at TIMESTAMPTZ DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  duration_ms INTEGER,
  row_count BIGINT,
  status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
  error TEXT
);

CREATE INDEX idx_view_refresh_log_view_date
  ON view_refresh_log(view_name, started_at DESC);
```

PR #34's digest header includes
`Data freshness: <last successful refresh timestamp>` read from
this table.

### 5. Manual refresh command

`/refresh_views` (owner-only) — kicks off both refreshes
on-demand. Used during testing and when the digest is needed
mid-day. Same code path as the scheduled job.

## Files

| File | Change |
|---|---|
| `migrations/0010_price_movements_views.sql` | NEW. Two matviews + indexes + refresh log table. |
| `bot.py` | Schedule the nightly refresh; add `/refresh_views` command. |
| `views_refresh.py` | NEW. Refresh helper + status query (`last_refresh_at(view_name) -> datetime`). |
| `tests/test_views_refresh.py` | NEW. Unit tests on the helpers (mocking the Supabase client). |

## Tests

* After PR #32 backfill, query `price_movements` for
  `(JINTAN PUTIH, SAIDA)`: returns one row per receipt_date with
  populated `avg_7d`, `avg_30d`, and percent-change fields.
* Query `supplier_arbitrage` for last 30 days: returns items with
  `>10% spread` AND at least 2 distinct suppliers; e.g. JINTAN
  PUTIH should appear with SAIDA's price as `max_price` and
  SHREE MAP as `min_price`.
* Refreshing the view twice in a row succeeds (CONCURRENTLY
  doesn't error on the second invocation).
* `last_refresh_at('price_movements')` returns the most recent
  successful run timestamp.
* If `item_prices` has zero matching rows (fresh schema), both
  matviews are empty but their refreshes succeed.

## Out of scope

* Real-time view (no event-driven refresh). Daily refresh only.
* Forecasting future prices. We summarise what happened, not
  what will happen.
* Outlet-level arbitrage. Arbitrage is computed across all
  outlets; outlet-specific cuts of the same data can come later
  if owners ask.
* Refresh-on-receipt-upload. Too noisy and too slow.

## Acceptance

* Migration applies cleanly.
* Scheduled job runs at 22:50 in production for one week and
  writes a `view_refresh_log` row each night.
* `price_movements` row count matches expectations (rough sanity
  check: at least one row per active `(item, merchant)` pair per
  day with sufficient samples).
* `supplier_arbitrage` returns at least 5 candidate items in
  production (we already know jintan, udang, daun kari are
  candidates).
* PR #34's digest section that consumes these views renders in
  < 1 second.
