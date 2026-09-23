# Ask the bot: plain-language item search for the director

**Status:** Implemented.
**Depends on:** PR #23b (`item_prices` populated by `price_aggregation`),
item canonicalisation v2, cross-shop price comparison, bill analysis
(`build_outlet_price_report`).

---

## Why

Every number the director wants about an item is already in the database.
Reaching it meant remembering the right slash command and its exact spelling —
`/shop_prices`, `/outlet_prices`, `/monthly_kg` — and the argument order that
goes with it. On a phone, mid-conversation, that is a wall, and the commands
went unused.

The question that actually gets typed is:

> beras buy from where

So answer that. `/ask` takes the question in plain words — English, Malay, or
the mix everybody actually types — and in the alert group and the owner's DM you
can drop the `/ask` and just type it.

## What it answers

| Ask                                        | Answer                                     |
| ------------------------------------------ | ------------------------------------------ |
| "beras beli kat mana", "harga ayam"        | every shop's latest price, per cut, cheapest first |
| "bila last beli telur"                     | the last purchases — date, shop, price, qty, outlet |
| "berapa belanja minyak bulan ni"           | spend, quantity and average paid, split by shop and outlet |
| "which branch pays most for gula"          | the branch-by-branch comparison            |
| "what items can I ask"                     | the full item vocabulary                   |

A bare item name ("beras") is read as *where do we buy this, and for how much* —
the question asked most.

Periods understood: `hari ni`, `semalam`, `minggu ni`, `bulan ni`, `bulan lepas`,
`30 hari` / `last 30 days`, `tahun ni`.

## How it works

`director_ask` does two things and nothing else.

**1. Understand the question** (`parse_question`) — pure, no database. The intent
comes from the wording, the time window comes out of it, the question is stripped
away, and what is left goes through the same resolver `/shop_prices` uses.

Intent order is load-bearing, first hit wins:

`items` → `branch` → `spend` → `last` → `shop` (also the fallback)

`branch` beats the price words so *"which branch pays most for beras"* is a branch
comparison, and `spend` beats `last` so *"how much did we spend on beras last
month"* is money, not a purchase list.

**2. Answer it** (`answer_question`) — route to the report that already exists
(`shop_price_comparison` for "which shop", `bill_analysis` for "which branch"),
and build the two that did not: `build_last_purchases` and `build_spend_summary`.

## The fragmentation bug this had to fix first

The first live test of `/shop_prices beras` came back with three blocks — "Beras
Rebus Saga", "Basmati King Jasmine", "Beras Idly" — all from one wholesaler, and
the verdict *"Only one supplier has priced this item."* The director buys beras
from several shops, mini markets included. `/shop_prices ayam` was worse: four
blocks and `… +38 more type(s): I Ayam, Knorr Stock Ayam, 1St Ayam, Mr Cm Ayam,
Lai Ayam`.

`item_variant` is the raw receipt line with the pack size stripped:

```
BERAS REBUS SAGA 10KG     -> 'BERAS REBUS SAGA'
BASMATI KING JASMINE 5KG  -> 'BASMATI KING JASMINE'
I AYAM                    -> 'I AYAM'
MR CM AYAM                -> 'MR CM AYAM'
```

So every OCR spelling and brand prefix becomes its own "cut". Grouping the
answer by that splits one item into dozens of blocks that each hold one shop,
the block cap then hides most of them, and the one-shop check fires on a
fragment rather than on the item. The suppliers were never missing from the
database — they were behind "+38 more type(s)".

`build_item_report` answers at ITEM level instead: every shop that sold it, in
one list, ordered by most recent, with the price range where a shop sells
several pack sizes. Underneath, the recent purchases carry date, shop, line,
price, quantity and the outlet that bought it, then which outlets buy it at all.

The per-cut comparison is kept, because it is the honest half of the old report:
a RM110 sack of beras idly is not a quote for the same thing as a RM33.90 bag of
basmati. "Cheapest" is claimed **only inside one variant, and only when two or
more shops sold it**. When no cut clears that bar the report says so plainly and
points at the shop list, instead of "only one supplier has priced this item".

Rows the pipeline drops (own-outlet transfers, non-supplier receipts, bad dates
and prices) are now counted in a `⚠️ Left out:` line, so a supplier that really
is missing is visible rather than silent.

A count alone still cannot say *which* supplier vanished, so `debug` adds the
names. `load_price_rows_with_stats` records the shop behind every dropped row
(capped at eight distinct names per reason), and the report prints them:

```
⚠️ Left out: 1 internal transfer / own outlet, 1 merchant not categorised as a
shop, 1 receipt not a supplier purchase, 1 bad price (zero or negative).

🔍 read 6 row(s) from item_prices, kept 2

🔍 Which shop each dropped row belonged to
• internal transfer / own outlet: RESTORAN KHULAFA
• merchant not categorised as a shop: KEDAI RUNCIT AHMAD
• receipt not a supplier purchase: TNB
• bad price (zero or negative): SOME SHOP
```

KEDAI RUNCIT AHMAD is a real mini market filed under `internal_transfer` by
mistake. Seeing the name turns "the data is wrong somewhere" into a one-line
fix with `/merchant_show`. The names appear only under `debug`.

`/shop_prices <item>` serves this view by default; `/shop_prices <item> cuts`
still gives the per-cut comparison, and `debug` still breaks down the filtering.

## Honouring the whole question, not just the item

`/shop_prices` answered *"Khulafa **bistro** ayam **whole leg** price"* with every
cut at every outlet: 188 purchases, four shops, three screens. The outlet and the
cut reached the report and were thrown away.

The report now reads all three, each through the code that already owns that
matching:

| Part | Source | Example |
| --- | --- | --- |
| Item | `shop_price_comparison.resolve_item_query` | "ayam" |
| Outlet | `outlet_mapping.outlet_match` | "bistro" → `BISTRO7` |
| Cut | `shop_price_comparison.pick_variant` | "whole leg" → `AYAM WHOLE LEG` |

`outlet_match` is new but not new logic: it is `outlet_from_chat_title`'s ordered
rules, returning *which* needle matched as well as the code, because the outlet
words have to be removed before what is left can be read as a cut.
`outlet_from_chat_title` now delegates to it.

Two things had to be got right for the filters to reach the rows rather than
merely parse:

**The item name must leave the cut phrase.** Left in, "ayam" matches the cut
literally called `AYAM` as well as `AYAM WHOLE LEG`; `pick_variant` sees two
candidates, returns `None`, and naming a cut does nothing. `cut_phrase` strips
the item's own tokens and any synonym that resolves to the same item, so
"chicken whole leg" also lands on `whole leg`.

**The outlet must be read from the original text.** `extract_item_text` drops
bare numbers, so "sek 6" arrived as "sek" and the rules — which match the literal
`"sek 6"` — never fired. `"Khulafa sek 6 ayam whole leg price"` parsed an outlet
and then answered about all four. The item is still resolved from the cleaned
text; only the outlet reads the original.

Filtering also changes what the rest of the report should say. The year-widening
is skipped when a filter is on — at one outlet, for one cut, a single supplier is
the normal answer, not a shortfall. The outlet roll-up and the per-row outlet tag
disappear when the question named one outlet, and the cut disappears from every
line when the question named one cut, because the heading already says both.

```
🔎 Ayam · Bistro · Whole Leg — last 90 days
4 purchases · 1 shop

🏪 Shop
• BESTARI FARM — 23 Sep · RM15.00 · 4x

🧾 Recent purchases
• 23 Sep · BESTARI FARM — RM15.00 × 80
• 18 Sep · BESTARI FARM — RM15.00 × 60
• 16 Sep · BESTARI FARM — RM609.00 ⚠️ × 1
… +1 more purchase(s)

⚠️ 1 row(s) may be OCR errors (RM609.00) — /shop_prices ayam debug
```

## A price range that describes the item, not its worst two rows

`range RM1.40–RM609.00` was a true min–max and a useless one. Across 160 rows it
is *guaranteed* to quote an OCR column merge at one end and a fragment at the
other, and it was printed as though both were prices.

`_price_band` takes the median (via `price_sanity.median_stats`, which already
ignores values past the absolute ceilings) and keeps the prices within
`OUTLIER_FACTOR` — 5×, the same multiple the order generator uses to reject qty
outliers.

A suspect row is then **excluded, not annotated**. An OCR column merge is not a
purchase at RM609; it is a bad read of one, and listing it among the real lines
makes it read as a price however it is marked. It comes out of the purchase log,
out of the shop summaries, out of the counts and out of the band — the figures
and the list are computed from the same clean rows — and is accounted for on its
own line:

```
🧾 Recent purchases
• 23 Sep · BESTARI FARM — RM15.00 × 80
• 18 Sep · BESTARI FARM — RM15.00 × 60
• 15 Sep · BESTARI FARM — RM15.00 × 60

⚠️ 1 purchase excluded as a possible OCR error — RM609.00
→ /shop_prices ayam debug
```

Nothing is hidden from accounting, it is moved: `debug` prints every excluded row
in full — date, shop, price, quantity, outlet, cut — and says what it was judged
against.

```
🧾 1 row excluded from the figures above as a possible OCR error
   (more than 5× or under 1/5 of the median RM15.00)
• 16 Sep · BESTARI FARM — RM609.00 × 1 · BISTRO7
   Ayam Whole Leg
```

If the filter would empty the report, the rows are kept instead — a flagged
answer beats no answer.

A per-shop band is judged against the **item's** median, so a shop whose every
row is garbage cannot make that garbage its own normal.

The band is not trigger-happy: RM110 for a sack of beras idly sits inside 5× of
its item's median and is kept as a real price.

## The blind spot behind "Left out"

`bot.py` writes `item_prices` only for a receipt classified `SUPPLIER_PURCHASE`;
an `UNKNOWN` one returns early. Those purchases have **no rows at all**, so no
drop counter can mention them — the shop is simply absent, and the `⚠️ Left out:`
line cannot see it.

`debug` now counts the receipts in the window that were never classified and
names their merchants, which is the only way "why is my mini market missing" has
a complete answer.

### What this says about PRs #82 and #83

Both were investigated against this report:

- **#82 (bad dates)** — already handled here. The report drops missing and
  future-dated rows, declares the count (`8 bad date`), and `debug` names the
  shops behind them. Dates too old are excluded by the window, not silently. No
  change needed for this report.
- **#83 (INBOIS)** — real and directly related, but its proposed fix is wrong for
  this case. `classify_receipt("INBOIS", …)` returns `UNKNOWN`, so that RM108
  Vista receipt never reached `item_prices`. Whitelisting `INBOIS` would tag
  every mis-OCR'd receipt as a supplier purchase and put a shop called "INBOIS"
  in the list — INBOIS is OCR of *INVOIS*, the Malay word for invoice, read off
  the bill header instead of the shop name. The fix belongs in merchant
  extraction, which is out of scope here. What is in scope is no longer hiding
  it: the `debug` count above names it.

## The three rules it is built on

**Never guess an item.** A confident answer about the wrong item is worse than no
answer. Text that does not resolve gets the near names instead — including for a
straight typo (`ayamm` → "Did you mean: Ayam?"), which the containment resolver
cannot see because a misspelling shares no substring with the canonical key. Close
matches are *offered*, never auto-selected.

**Only spend what the receipt shows.** `item_prices` carries `unit_price` and
`qty` but no line total, so money is `unit_price × qty`. A line with no quantity
is counted in the purchase count and left out of the money, and the report says
how many lines that was — rather than quietly under-reporting or inventing a
total.

**Say nothing rather than the wrong thing.** The whole risk of a bare text handler
is answering what nobody asked. In the alert group the text has to BOTH resolve to
a real item AND carry a question word or a question mark, so "ok" and "sudah
hantar" are never answered. In the owner's DM every message is addressed to the
bot, so a bare item name is enough. Anywhere else the free-text path stays quiet:
`/shop_prices` answers a reviewer in any chat, but that is a typed command —
un-prompted supplier prices landing in an outlet group because the owner happened
to mention ayam is not the same thing. Those chats still get an answer, through
`/ask`. Anything below the bar is left alone silently; a bot that replies "I don't
understand" to every group message is worse than one that says nothing.

## What changed

- **`director_ask.py`** (new) — question parsing, the window parser, the two new
  reports, the item index, and the routing entry point. Never raises; every entry
  point returns a safe default.
- **`bot.py`** — `/ask` (aliases `/tanya`, `/cari`, `/search`), the free-text
  handler, `HELP_TEXT` and the command menu. The text handler is registered LAST
  in the handler group so the review-edit conversation and the audit-reply handler
  always get first refusal, and it excludes replies entirely so an audit reply can
  never be mistaken for a question.
- **`tests/test_director_ask.py`** — the questions the director actually types, in
  both languages; intent precedence; window parsing; the two new reports; and the
  "never raises, never guesses" guarantees.
- **`tests/test_director_ask_wiring.py`** — the handler registration order and the
  access gates, which can't be unit-tested without Telegram.

No migration, no schema change, no new table: it reads what the receipt pipeline
already writes.
