# Cross-shop price comparison in the director alert

**Status:** Implemented.
**Depends on:** PR #23b (`item_prices` populated by `price_aggregation`), PR #25
(price-spike alerts), item canonicalisation v2.

---

## Why

The director alert said only *"this item got more expensive at this shop"*. The
next question is always *"who else sells it, and for how much?"* — and answering
it meant leaving Telegram. The alert now carries that answer, and the same
comparison is available on demand for **any** item, not only the ones anyone
happens to be watching closely.

## What changed

### `shop_price_comparison.py` (new)

- `get_shop_prices(client, canonical_item, lookback_days=90, exclude_receipt_id,
  today)` — reads `item_prices` for one canonical item inside the lookback
  window and returns one summary per shop (`merchant`): `latest_price`,
  `latest_date`, `avg_price`, `min_price`, `max_price`, `sample_count`, sorted
  cheapest-latest-price first (ties by shop name).
  - "Latest" is the newest `receipt_date`, with `receipt_id` as the tiebreaker
    for two receipts on the same day — not the lowest price seen.
  - Paginated read (`db_pagination.fetch_all_pages`) so a busy item isn't
    truncated by the PostgREST 1000-row cap; falls back to a single query for
    clients without `.order`/`.range`.
  - Non-positive prices are dropped; blank merchants become `Unknown shop`.
- `format_shop_comparison(...)` — the alert block. Marks the cheapest shop 🥇 and
  the receipt's own shop 👉, always shows the current shop even if it falls past
  `max_shops` (default 6, tail summarised as "+N more shops"), and closes with
  the cheapest-vs-current gap in RM and %.
- `resolve_item_query(text)` — free text → canonical key: exact key, key with
  spaces/hyphens for underscores (`ais batu` → `ais_batu`), English synonyms
  (`chicken` → `ayam`), the raw receipt-line canonicaliser
  (`AYAM BERSIH 30KG` → `ayam`), then prefix/token matching which returns
  "did you mean" suggestions for ambiguous input (`sos`).
- `build_shop_price_report(...)` — the full `/shop_prices` reply.

### `price_spike_detection.py`

`detect_spikes` attaches `shop_prices` to every spike (opt-out via
`include_shop_prices=False`), and `format_spike_message` appends the comparison
block between the "Today:" line and "Did you ask supplier?". The current receipt
is deliberately **not** excluded — its row is already in `item_prices`, so the
alerting shop shows today's price next to everyone else's.

A spike with no `shop_prices`, or only one shop with history, renders exactly the
message it did before — there is nothing to compare.

### `bot.py`

`/shop_prices <item>` (aliases `/all_prices`, `/harga`) — the same comparison on
demand for any item. Answers in the alert group and to reviewers anywhere;
supplier pricing is not dumped into arbitrary outlet groups.

## Failure behaviour

Every entry point swallows exceptions and returns `[]` / `""` / a plain error
string. A comparison failure degrades the alert to its old form; it never blocks
a price alert or crashes the receipt pipeline.

## Example

```
⚠️ Price increase detected

Ayam — BESTARI FARM
Previous average: RM10.12 (from 5 receipts, merchant scope)
Range: RM9.80 - RM10.50
Today: RM12.50 (+23.5%)

🏪 Price at all shops — Ayam (last 90 days):
🥇 SEGAR MART — RM8.50 (avg RM8.75, 2 receipts, last 02 Aug)
• PASAR BORONG SS15 — RM9.20 (avg RM9.20, 1 receipt, last 03 Aug)
👉 BESTARI FARM — RM12.50 (avg RM10.52, 6 receipts, last 04 Aug)  ← this receipt
💡 Cheapest: SEGAR MART at RM8.50 — RM4.00 (32%) below BESTARI FARM.

Did you ask supplier?
```

## Tests

`tests/test_shop_price_comparison.py` — aggregation (grouping, latest-vs-cheapest,
same-day tiebreak, lookback window, price/merchant hygiene, item isolation,
receipt exclusion), the alert block (markers, tail summary, current-shop-is-
cheapest, single shop), query resolution, the `/shop_prices` report, and the
never-raises contract. `tests/test_price_spike_detection.py` gains a
`SpikeShopComparison` class covering the wiring for an arbitrary item.
