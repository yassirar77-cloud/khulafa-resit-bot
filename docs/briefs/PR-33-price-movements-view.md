# PR #33 — price_movements materialised view

**Status:** Implemented.
**Depends on:** PR #30 (merchant canonicals), PR #31 (receipts tagged), PR #32
(item canonicals), PR #32b (item_resolutions populated).
**Blocks:** PR #34 (digest price-trend/anomaly sections read this view).

> This brief was rewritten to match the implemented spec. The earlier draft
> described a different design — `price_movements` with 7/30-day rolling
> averages PLUS a second `supplier_arbitrage` view, both sourced from an
> `item_prices` table and refreshed on a nightly schedule. The implemented PR is
> a single denormalised view sourced from `receipts` + `item_resolutions`, with
> no rolling-average maths (that's PR #34) and manual refresh.

---

## Why

We now have canonical merchants on receipts and canonical items in
`item_resolutions`. This view denormalises them into one row per resolved item
line — merchant + item + qty/unit_price/line_total — so reporting can group by
canonical item/merchant without re-doing the joins each query.

## Scope (`migrations/0011_price_movements_view.sql`)

Materialised view `price_movements`, one row per (receipt, resolved item line),
joining `receipts` → `merchant_canonical` → `item_resolutions` → `item_canonical`
and dereferencing `receipts.items[item_resolutions.item_index]` for qty/price.

**Filters:** `merchant_canonical_id IS NOT NULL`, `confidence >= 60`,
`receipt_type IN (SUPPLIER_PURCHASE, UTILITY, RENT_LICENSE, INTERNAL_TRANSFER)`,
`item_resolutions.canonical_id IS NOT NULL`.

**Line maths (important):** in `receipts.items` jsonb the `price` field is the
**line total** (the bot derives unit_price as price/qty everywhere else). So the
view sets `line_total = items[idx].price` and `unit_price = line_total / qty`
(qty defaults to 1) — the invariant `line_total = qty * unit_price` holds. This
inverts the spec's "line_total = qty × unit_price (price as unit price)" wording,
which would have over-counted spend on any qty>1 line. Numeric casts are
regex-guarded so dirty jsonb (e.g. "1/2 kg") becomes NULL instead of aborting a
REFRESH.

**Indexes:** unique on `(receipt_id, item_index)` — NOT
`(receipt_id, item_canonical_id)` as the spec drafted, because two lines on one
receipt can resolve to the same canonical item and would collide, breaking
`REFRESH ... CONCURRENTLY` (which requires a unique index). Plus secondary
indexes on `(item_canonical_id, receipt_date DESC)`,
`(merchant_canonical_id, receipt_date DESC)`, `(receipt_date DESC)`,
`(merchant_category)`. (The view carries an extra `item_index` column to support
the unique index.)

**Refresh:** `refresh_price_movements()` runs `REFRESH MATERIALIZED VIEW
CONCURRENTLY price_movements`.

## `analytics.py` + commands

Pure aggregations over fetched view rows: `top_items`, `top_suppliers`,
`price_history`, `summarise_status`, plus `refresh(client)` and formatters.
Owner-only: `/refresh_analytics`, `/price_movements_status`, `/top_items <N>`,
`/top_suppliers <N>`, `/price_history <item_canonical_id>`.

`row_passes_filters` / `compute_line` are a Python reference of the view's WHERE
+ line maths; tests exercise them for the exclusion/computation cases and a
migration-content test pins the SQL to the same clauses.

## Out of scope

Dashboard UI; price trend/anomaly detection (PR #34); auto-refresh; the
`supplier_arbitrage` view from the old draft.

## Tests (`tests/test_price_movements.py`)

Exclusion filters (low confidence, non-reportable type, unresolved item, null
merchant), line_total = qty×unit_price, refresh callable (fake rpc), top_items /
top_suppliers ordering + aggregation, price_history filter+sort, status summary,
and migration content (view, joins, filters, unique grain index, refresh fn).

## Rollout

1. Apply migration 0011 (creates + populates the matview).
2. Created WITH DATA, so already populated; later use `/refresh_analytics`.
3. Sanity: `SELECT count(*) FROM price_movements;` (~1500–2000).
4. `/top_items 10`, `/top_suppliers 10` in Telegram.
