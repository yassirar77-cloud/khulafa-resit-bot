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
