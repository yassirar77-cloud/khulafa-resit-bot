# Bill analysis — every bill, every item, every shop, nightly

**Status:** Implemented.
**Depends on:** `item_prices` (PR #23b), the cleaning pass in
`shop_price_comparison` (PR-shop-price-comparison), manager registration
(PR #67) and the `MANAGER_DELIVERY_ENABLED` gate, the question ledger
(`supervisor`).

---

## Why

The bot already shouts when ONE receipt lands with a price more than 10% above
a trailing average (and only once five samples exist). That is a smoke alarm,
not a bill review. The owners' two standing questions are different:

1. **"Which items went up on today's bills?"** — a bill reader compares this
   bill with the LAST bill from the same shop, not with a 90-day average. A
   6% creep at the same supplier is a real cost and never trips the spike
   detector.
2. **"Why does Vista pay more for telur than Bistro?"** — Khulafa's own
   outlets buy the same items from different suppliers at different prices.
   Nobody inside one shop can see that. The corpus can.

Both answers must reach the people who can act: the owners (all of it, in
English, in the alert group) and each outlet's manager (their own bills, in
Tamil, with the supplier to ask and the branch that pays less).

## What runs

`bill_analysis.py`, nightly at **21:30 MY** (`bot.post_bill_analysis`), after
the 21:00 missing-bill check and ahead of the 23:00 digest. On demand:
`/bill_analysis_now` (owner). Everything rides on one bulk read of the cleaned
corpus (`shop_price_comparison.load_all_price_rows`, 90 days): no internal
transfers, no non-supplier receipts, no future dates, OCR merchant variants
collapsed, every line tagged with its cut (`item_variant`) and its outlet.

### Pass 1 — price changes on today's bills

* "Today's bills" = rows whose `item_prices.created_at` is within the last
  24h (upload time, so a late-uploaded bill is still analysed the night it
  arrives). Rows without a usable `created_at` fall back to
  `receipt_date` ∈ {yesterday, today}.
* Each new line is compared with the **same shop's previous price for the
  same cut** (`(canonical_item, variant, shop_key)`), the newest baseline row
  dated before it. One entry per (item, cut, shop, **outlet**) so a manager
  hears about their own bill; a shop delivering twice in a day is judged on
  the later bill.
* Thresholds: `|Δ| ≥ 5%` **and** `|Δ| ≥ RM0.02` (rounded to the sen first —
  `0.50 − 0.45` is `0.0499…` in binary). Above `200%` is treated as an OCR
  misread (counted as "implausible", never alerted). Decreases are kept
  separately and listed under PRICE DROPS.
* Each increase carries `cheaper_shops` (other suppliers below the new price,
  cheapest first, never the same shop under a legal-suffix variant) and
  `cheaper_outlets` (branches paying less, with their supplier).

### Pass 2 — every item across outlets

* Window: last 30 days. For each (item, cut) bought by **two or more
  outlets**: each outlet's latest price (newest `receipt_date`, `receipt_id`
  breaking ties), its supplier on that bill, average and sample count.
  Cheapest first; every other outlet gets `gap_rm` / `gap_pct` and
  `pays_more` (≥ 5% and ≥ RM0.02 above the cheapest).
* **Unit mismatch guard.** Dearest ÷ cheapest > 3× is two units (tray vs
  egg, kg vs pack), not two prices. The owner sees it as "⚠️ check unit";
  it never reaches a manager and is never an "alternative" on an increase.
* Sorted: real gaps first (widest spread), then the no-gap items (still
  listed — the owner asked for every item), unit mismatches last.

## What gets sent

**Owners (alert group, always, chunked to Telegram's limit):**

```
📈 Bill analysis — price changes (bills uploaded in the last 24h)

PRICE INCREASES (vs the same shop's previous price):
• Telur Gred A — SAIDA [Vista]
   RM0.41 (02 Sep) → RM0.46 (05 Sep)  +RM0.05 (+12.2%) · avg RM0.41 (1x)
   💡 cheaper at HANEE RM0.38 · Bistro pays RM0.38 (HANEE)

PRICE DROPS:
• Gula Pasir — SAIDA: RM3.00 → RM2.50 (-16.7%)

Bills analysed: 3 · line items: 3 · compared with history: 3 · no history yet: 0
```

```
🏪 Outlet price comparison — every item (last 30 days)

Telur Gred A:
   🥇 Bistro RM0.38 (HANEE) · SEK-20 RM0.42 (SAIDA) +11% · Vista RM0.46 (SAIDA) +21%

Bawang Besar:
   🥇 Vista RM3.00 (SAIDA) · Bistro RM3.60 (PASAR) +20%

Items compared: 4 · a branch pays ≥5% more: 3 · ⚠️ unit mismatch: 1
```

plus a delivery summary (who got a note, LIVE / TEST MODE banner), the same
shape as the weekly HQ summary.

**Each outlet manager (Tamil, one note per outlet, only when there is
something to say):** the items on THEIR bills that went up (ask the supplier
why; who is cheaper; which branch pays less), the items another branch buys
cheaper (ask your supplier for that rate, or try that branch's supplier), and
the items where they are the cheapest branch — praise, so the note is never
only a complaint. Routed through `weekly_manager_reports.route_message`: with
`MANAGER_DELIVERY_ENABLED` off every note goes to the owner with a `[TEST]`
prefix; a registered manager's note is logged in the question ledger as
`bill_analysis` so the reply-capture and reminder loop see it. The tone guard
(`contains_accusatory`) is asserted in the tests.

Outlet codes in `item_prices` are the receipt-side codes (`D`, `BISTRO7`);
`bot._registry_code_for_outlet` bridges them to the `outlet_canonical`
registration codes (`DAMANSARA`, …) via `outlet_resolver.canonical_outlet`, so
the right manager is found.

**On demand:** `/outlet_prices [item]` (alias `/branch_prices`) — the cross-
outlet table for one item (free text, same resolver as `/shop_prices`) or for
every item. Alert group, or reviewers anywhere.

## Vocabulary: the staples were invisible

`item_canonicalization_v2` had 34 categories and **no `telur`, `gula`,
`minyak`, `beras`, `bawang`, `cili`, `garam`, `susu`, `teh`, `sayur`…** Lines
that don't canonicalise are dropped before `item_prices`, so the owner's own
example (telur) could never have been compared. `data/canonical_items_v2.json`
gains 20 staple categories (`telur`, `gula`, `minyak_masak`, `fuel`, `beras`,
`bawang`, `cili`, `garam`, `susu`, `teh`, `sayur`, `limau`, `mee`, `rempah`,
`mentega`, `ghee`, `dhal`, `tauhu`, `packaging`, plus drink powders/syrups
under `drinks`). Longest variation wins, so the existing categories keep
their lines: `TELUR IKAN` stays `ikan`, `SOS CILI` stays `sos_cili`, `BAWANG
GORENG` stays `bawang_goreng`, `MINYAK DIESEL` is `fuel`, `TEA MASALA` stays
`tea_masala`. `order_items` classifies the new keys (eggs/veg/tofu/milk/lime
perishable, the rest dry, fuel excluded) and `/shop_prices` gains the English
synonyms (`eggs`, `sugar`, `oil`, `rice`, `onion`, …).

This only affects receipts ingested from now on; historical bills that
carried these items were skipped at ingestion and are not backfilled here.

## Failure behaviour

Same contract as the rest of the reporting layer: every entry point swallows
exceptions and returns `[]` / `""` / an empty bundle. A gather crash tells
the owner ("⚠️ Bill analysis failed to run"); a single bad bill or a manager
lookup failure never stops the other messages.

## Tests

`tests/test_bill_analysis.py` — new-bill detection (created_at, `Z`/naive
timestamps, receipt_date fallback), same-shop comparison (threshold, sen
floor, implausible, decreases, other shop / other cut are not baselines,
per-outlet entries, later bill wins), cross-outlet comparison (cheapest
first, gaps, unit mismatch, no-outlet rows, ordering), alternatives (own shop
excluded across legal suffixes, mismatches never alternatives), the
per-manager slice, every formatter incl. the Tamil note and the tone guard,
the on-demand report, the end-to-end gather through the cleaning pass, and
never-raises. `tests/test_bill_analysis_wiring.py` pins the bot wiring
(schedule, commands, gate, chunking, ledger, failure alert, code bridging).
`tests/test_item_canonicalization_v2.py` pins the vocabulary boundaries.
