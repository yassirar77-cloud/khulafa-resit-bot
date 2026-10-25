# Classification Gap — roti/capati/gas missing from orders since ~2026-05-23

## Symptom
The auto order generator stopped listing Diamond Ball (roti/capati) and gas
items from ~22–23 May 2026. The user suspected ingestion had stopped.

## What the data actually showed (diagnostic SQL, run 2026-06-17)
- Receipts **are still arriving** for these merchants through June (Diamond Ball
  → 2026-06-15, "Inbois" → 2026-06-18, Petronas → 2026-06-15). Source is fine.
- But after ~05-24 their `item_prices` line-row count is **0**, and the receipt
  header flips to `receipt_type = UNKNOWN` (was `SUPPLIER_PURCHASE`).
- `pending_review` is nearly empty for them — they are auto-classified and
  dropped, not held for review.

So this is **not** a missing source and **not** "no purchases". It is a
**classification gap**: the receipts classify as `UNKNOWN`, and `item_prices` is
only ever populated for `SUPPLIER_PURCHASE` receipts.

## Root cause (in code)
1. At **live ingestion**, `receipt_type` comes *only* from the keyword
   classifier: `bot.py:1450` → `classify_receipt(...)` → stored at `bot.py:1483`.
   There is **no** merchant-canonical override on the live path.
2. `classify_receipt` assigns `SUPPLIER_PURCHASE` **only** when a token from the
   hardcoded `SUPPLIER_WHITELIST` matches (`receipt_classifier.py:139`,
   priorities 2 and 8). **`DIAMOND` and the gas suppliers were not on it.**
3. `item_prices` is written only on the `SUPPLIER_PURCHASE` fall-through
   (`bot.py:1598`); `UNKNOWN` returns early at `bot.py:1583`. So an `UNKNOWN`
   receipt contributes **zero** rows → the order generator sees no recent buys
   → stale cadence / item dropped.
4. The merchant-canonical category → `receipt_type` upgrade
   (`supplier` → `SUPPLIER_PURCHASE`) exists **only** in the offline
   `backfill_canonical.py --reclassify` script (`backfill_canonical.py:48-53`).

**Why the cliff at ~23 May:** Diamond Ball/gas receipts have *always* classified
`UNKNOWN` live (not whitelisted). They only ever appeared as `SUPPLIER_PURCHASE`
because `backfill_canonical --reclassify` had been run over older receipts. That
backfill simply hasn't been re-run for receipts after ~23 May, exposing the
underlying whitelist gap.

## Merchant category notes (don't "fix" these — they're correct)
- **Petronas** → `UTILITY` / `PETTY_CASH`: it's the **petrol station**, not LPG
  cooking-gas supply. Correctly excluded from supplier orders.
- **Victory** → `STAFF_ADVANCE`: staff advances at Damansara, **not** a gas
  supplier.
- **"Inbois"** → **CONFIRMED a real LPG gas supplier** (recurring ~2-day cycle
  across Vista/Jakel/Signature/SEK-20, months of history) — now whitelisted.
  Note the spelling: token `INBOIS` (with a **B**) is distinct from the Malay
  invoice word `INVOIS` (with a **V**) that prints on bill bodies, so it does not
  misclassify invoices (test `test_invois_word_alone_stays_unknown`).

## Open: extraction quality varies by receipt format / outlet (not just date)
`lines 0` is **not** a clean 23–24 May regression. Diamond Ball shows `lines 0`
consistently for **Kl Sg Besi** and **HJ SHARFUDDIN SEK 6** going back to early
May, while **Bistro / SEK-20** get `lines 2` — so extraction quality tracks the
**receipt format/outlet**, not only the date. Whether the fix is the item_prices
backfill (#85) or a **re-extraction/re-OCR** depends on case A vs B:
- **Case A** — `receipts.items` *has* line rows but they don't reach
  `item_prices` (canonical null, or header gated UNKNOWN): backfill/reclassify
  recovers them.
- **Case B** — the item block is **absent from the OCR/parse** for that format:
  backfill cannot help; those receipts need re-OCR/re-parse.

Raw-text samples to decide this are pending (one `lines 2` + two `lines 0`).

## Fix shipped in this PR (durable, live-path)
Added to `SUPPLIER_WHITELIST` so **new** receipts classify correctly at
ingestion, independent of the offline backfill:
- `DIAMOND` — Diamond Ball, roti/capati (all outlets)
- `RANAU PETROGAS` — LPG cooking gas (specific token; does not collide with the
  `PETROL`/`PETRONAS` petty-cash keywords)

Covered by `tests/test_receipt_classifier.py`
(`test_diamond_ball_now_whitelisted_supplier`,
`test_ranau_petrogas_supplier_not_petty_cash`,
`test_invois_word_not_misread_as_supplier`).

## Still to confirm before whitelisting more — diagnostic SQL
Pull the raw OCR text + resolved canonical for the failing receipts to (a) see
what "Inbois" really is and (b) find any other real gas/roti supplier hiding in
`UNKNOWN`:

```sql
SELECT id, receipt_date, outlet, merchant, merchant_canonical_id,
       receipt_type, confidence, left(raw_text, 400) AS raw_excerpt
FROM receipts
WHERE (merchant ILIKE '%inbois%' OR merchant ILIKE '%invois%'
       OR merchant ILIKE '%gas%' OR merchant ILIKE '%petrogas%')
  AND receipt_date >= DATE '2026-05-20'
ORDER BY receipt_date DESC
LIMIT 50;
```

## Historical recovery (the 23 May → now gap)
The whitelist fix is forward-looking. Existing receipts already stored as
`UNKNOWN`, and their missing `item_prices` rows, need a backfill:

1. **Reclassify** existing receipt headers from the now-correct rules / canonical
   categories:
   `python backfill_canonical.py --reclassify` (dry-run first).
2. **Repopulate `item_prices`** for receipts now `SUPPLIER_PURCHASE` —
   `price_aggregation` runs only at ingestion, so a dedicated backfill (extract
   `classify_and_extract_items` over stored `receipts.items` → `save_item_prices`)
   is required. *This script does not exist yet* — recommended as the immediate
   follow-up so the order generator regains roti/capati/gas history without
   waiting ~90 days for the window to refill from new receipts.

Scope of the gap to recover:
```sql
SELECT merchant, count(*) AS unknown_receipts,
       min(receipt_date) AS first, max(receipt_date) AS last
FROM receipts
WHERE receipt_type = 'UNKNOWN'
  AND receipt_date >= DATE '2026-05-20'
  AND (merchant ILIKE '%diamond%' OR merchant ILIKE '%petrogas%'
       OR merchant ILIKE '%inbois%')
GROUP BY merchant ORDER BY unknown_receipts DESC;
```
