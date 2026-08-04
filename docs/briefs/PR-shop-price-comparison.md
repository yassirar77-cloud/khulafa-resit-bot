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

## The four ways the raw table lies

A naive "group `item_prices` by merchant" produces garbage. The first cut of this
feature did exactly that, and the director got nine "shops" for ayam spanning
RM1.70 to RM54.00. Every entry was wrong for a different reason:

1. **Internal transfers.** RESTORAN KHULAFA / KHULAPA SIGNATURE / MYMOON'S
   KITCHEN are Khulafa's own outlets moving stock, not shops selling to us.
2. **OCR merchant variants.** MYMOON'S / MYMOOK'S / MIMOON'S KITCHEN counted as
   three shops; so did "BESTARI FARM" and "BESTARI FARM (M) SDN BHD".
3. **Mixed cuts and pack sizes.** Whole birds, leg quarters and a 30kg carton all
   canonicalise to `ayam`, so their unit prices are not comparable.
4. **Corrupt dates.** Receipts stamped months into the future ("last 26 Dec" in
   August) sat inside a 90-day window.

`load_price_rows` is the filter that fixes all four, and everything else — the
alert block, the report, and the spike baseline itself — is built on it.

## What changed

### `shop_price_comparison.py` (new)

- `load_price_rows(client, canonical_item, lookback_days=90, exclude_receipt_id,
  today)` — the cleaning pass. Keeps only `SUPPLIER_PURCHASE` receipts (joined
  from `receipts` in `.in_()` batches) whose merchant's canonical category is a
  real supplier; drops own-outlet names (`khulafa`/`khulapa`, belt and braces for
  rows never canonicalised); drops rows dated after today; collapses merchants
  onto `receipts.merchant_canonical_id`, falling back to `merchant_resolver`'s
  tiered matcher **read-only** (a price lookup must not write fuzzy aliases) and
  then to a suffix-stripped key; tags every row with its item variant. If the
  `receipts` lookup fails the rows are kept with name-based filtering only — a
  degraded comparison beats an empty one.
- `item_variant(raw_line, canonical)` — the cut, with pack size stripped:
  `AYAM BERSIH 30KG` and `Ayam Bersih 1 kg` are both `AYAM BERSIH`; `PAHA AYAM`
  stays separate.
- `shop_key` / `shop_display` — grouping is aggressive (`BALAJI ENTERPRISE SDN
  BHD` -> `BALAJI`), the label the director reads is not (`BALAJI ENTERPRISE`).
- `get_shop_prices(..., variant=None)` — one summary per shop (`latest_price`,
  `latest_date`, `avg_price`, `min_price`, `max_price`, `sample_count`), cheapest
  latest price first. "Latest" is the newest `receipt_date`, `receipt_id`
  breaking same-day ties — not the lowest price ever seen. Paginated
  (`db_pagination.fetch_all_pages`) so a busy item isn't truncated by the
  PostgREST 1000-row cap.
- `get_variant_prices(...)` — the per-cut breakdown the report prints.
- `format_shop_comparison(...)` — the alert block. Cheapest 🥇, the receipt's own
  shop 👉 (always shown, even past `max_shops`), tail summarised, closing with
  the cheapest-vs-current gap in RM and %.
- `resolve_item_query(text)` / `pick_variant(text, variants)` — free text to an
  item, then to one cut: exact key, spaces/hyphens for underscores (`ais batu`),
  English synonyms (`chicken` -> `ayam`), the raw receipt-line canonicaliser,
  then prefix matching with "did you mean" suggestions. `/shop_prices paha ayam`
  shows leg quarters only.
- `build_shop_price_report(...)` — the full `/shop_prices` reply, one short block
  per cut.

### `price_spike_detection.py`

The baseline had the same disease: `get_historical_average` averaged every row
that canonicalised to the item, so a leg-quarter price was judged against
internal transfers and whole birds. `detect_spikes` now builds **both** the
baseline and the comparison block from `load_price_rows`, scoped to the receipt
line's own cut — same shop first (>=5 samples), then all shops.
`get_historical_average` stays as the fallback for when the cleaned rows can't be
loaded at all, so a DB hiccup degrades the detector to its old behaviour instead
of silencing it.

`format_spike_message` titles the alert with the cut (`Paha Ayam — BESTARI FARM`)
and appends the block between the "Today:" line and "Did you ask supplier?". The
current receipt is excluded from the baseline but **kept** in the block — the
point is to show today's price beside everyone else's. A spike with no
`shop_prices`, or only one shop, renders exactly the message it did before.

### `bot.py`

`/shop_prices <item>` (aliases `/all_prices`, `/harga`) — the same comparison on
demand for any item. Answers in the alert group and to reviewers anywhere;
supplier pricing is not dumped into arbitrary outlet groups.

## Failure behaviour

Every entry point swallows exceptions and returns `[]` / `""` / a plain error
string. A comparison failure degrades the alert to its old form; it never blocks
a price alert or crashes the receipt pipeline.

## Example
## Example

Alert:

```
⚠️ Price increase detected

Paha Ayam — BESTARI FARM
Previous average: RM7.83 (from 5 receipts, merchant scope)
Range: RM7.70 - RM7.90
Today: RM9.60 (+22.6%)

🏪 Paha Ayam — price at all shops (last 90 days):
🥇 AYAM BERLIAN — RM8.20 · 01 Aug
• BALAJI ENTERPRISE — RM8.60 · 29 Jul
👉 BESTARI FARM — RM9.60 · 04 Aug (6x)  ← this receipt
💡 Cheapest: AYAM BERLIAN at RM8.20 — RM1.40 (15%) below BESTARI FARM.

Did you ask supplier?
```

`/shop_prices ayam`:

```
🏪 Ayam — supplier prices (last 90 days)

Paha Ayam
🥇 BESTARI FARM — RM7.80 · 02 Aug (5x)
• AYAM BERLIAN — RM8.20 · 01 Aug
• BALAJI ENTERPRISE — RM8.60 · 29 Jul

Ayam Bersih
🥇 BESTARI FARM — RM15.20 · 31 Jul
• AYAM BERLIAN — RM15.90 · 02 Aug
```

## Tests

`tests/test_shop_price_comparison.py` — the four cleaning filters (internal
transfers by name / by receipt type / by merchant category, OCR variant
collapsing, future dates, receipt-lookup failure), variant and shop-key
derivation, aggregation (grouping, latest-vs-cheapest, same-day tiebreak,
lookback window, price hygiene, item isolation, receipt exclusion), the alert
block, query and cut resolution, the `/shop_prices` report, and the never-raises
contract. `tests/test_price_spike_detection.py` gains a `SpikeShopComparison`
class covering the cut-scoped baseline, internal transfers not moving it, the
alert wiring for an arbitrary item, and the opt-out.
