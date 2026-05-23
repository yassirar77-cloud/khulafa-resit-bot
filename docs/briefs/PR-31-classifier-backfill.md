# PR #31 — Classifier backfill on historical receipts

**Status:** Brief drafted, not implemented
**Depends on:** PR #30 (merchant_canonical_id available makes the
classifier more accurate). Must land after.
**Blocks:** PR #34 (digest's staff advances / supplier activity
sections rely on populated side tables).

---

## Why

Production has ~1,500 receipts. The majority were uploaded before
PR #28 / PR #28b shipped, which means:

* Most are classified as `UNKNOWN` because the classifier never
  saw the merchant header.
* The `staff_advances`, `fixed_costs`, and `petty_cash` side
  tables are nearly empty — historical PAYOUT / PINJAM / TNB /
  KWSP / SHELL receipts never reached their respective ledgers.
* Reports like `/advances all_time` return effectively nothing.

The classifier now does the right thing on new uploads. We need
to re-run it across the historical set so the side tables fill
out and the digest has real data to summarise.

This is a one-time batch job, not an ongoing pipeline. It is
designed to be safe to re-run.

## Scope

### 1. One-time script `scripts/backfill_classifier.py`

Algorithm:

```
For each receipt where receipt_type = 'UNKNOWN':
  result = classify_receipt(
    ocr_text=receipt.raw_text,
    parsed_items=receipt.items,
    total=receipt.total,
    merchant=receipt.merchant,
  )
  UPDATE receipts SET receipt_type = result.receipt_type WHERE id = ...
  
  if result.receipt_type == STAFF_ADVANCE and no staff_advances row exists:
    INSERT staff_advances (receipt_id, staff_name, amount, issued_by, ...)
  elif result.receipt_type in (UTILITY, RENT_LICENSE) and no fixed_costs row:
    INSERT fixed_costs (receipt_id, vendor, amount, ...)
  elif result.receipt_type == PETTY_CASH and no petty_cash row:
    INSERT petty_cash (receipt_id, category, amount, ...)
```

Three guarantees:

* **Idempotent.** The script checks for an existing side-table row
  keyed by `receipt_id` before inserting. Re-running is a no-op
  on already-processed receipts.
* **Non-destructive on disagreement.** If a receipt's
  `receipt_type` is already set to something specific (i.e. NOT
  `UNKNOWN`) — even if the classifier now disagrees — the script
  does NOT overwrite. It logs the disagreement to a
  `backfill_disagreements` table so the owner can review and
  decide.
* **Manual review queue for missing data.** When the classifier
  returns `STAFF_ADVANCE` but `extract_staff_name` returns `None`,
  insert a row into the `pending_review` table from PR #29b with
  `reason = 'staff_name_missing'`. The owner edits the name and
  approves; the side-table row is created on approval.

### 2. `backfill_disagreements` table

```sql
CREATE TABLE backfill_disagreements (
  id BIGSERIAL PRIMARY KEY,
  receipt_id BIGINT REFERENCES receipts(id) ON DELETE CASCADE,
  existing_type TEXT NOT NULL,
  proposed_type TEXT NOT NULL,
  reason TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (receipt_id)
);
```

Migration filename: `migrations/0008_backfill_disagreements.sql`.

### 3. Final report

```
Backfill complete (idempotent):
  Receipts queried (UNKNOWN):     1,287
  Re-classified:                    662
    SUPPLIER_PURCHASE:              412
    STAFF_ADVANCE:                   87  (12 missing staff_name -> review queue)
    UTILITY:                         45
    RENT_LICENSE:                    23
    PETTY_CASH:                      95
  Still UNKNOWN:                    625
  Disagreements logged:               4
  
  Side-table inserts:
    staff_advances:    75  (87 STAFF_ADVANCE - 12 queued)
    fixed_costs:       68  (UTILITY + RENT_LICENSE)
    petty_cash:        95
  
  Next:
    /backfill_disagreements_review
    /pending_review   (for queued staff_advance names)
```

### 4. Two new owner-only Telegram commands

* `/backfill_disagreements_review` — paginated listing of every
  row in `backfill_disagreements`. Each entry: receipt_id, current
  type, proposed type, snippet of raw_text. Inline buttons to
  accept the proposed type, keep the current type, or open the
  receipt in Telegram.
* `/backfill_stats` — last backfill run report (re-rendered from
  the audit table — see section 5).

### 5. Backfill audit log

Persist the final report into a `backfill_runs` table so the
owner can audit when each run happened and the counts:

```sql
CREATE TABLE backfill_runs (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  triggered_by_chat_id BIGINT,
  receipts_queried INTEGER,
  reclassified_count INTEGER,
  side_table_inserts JSONB,
  disagreements INTEGER,
  pending_review INTEGER,
  notes TEXT
);
```

## Files

| File | Change |
|---|---|
| `migrations/0008_backfill_disagreements.sql` | NEW. Two new tables. |
| `scripts/backfill_classifier.py` | NEW. The batch processor. |
| `bot.py` | Two new owner-only command handlers. |
| `backfill.py` | NEW. Pure helpers: `propose_classification(receipt_row)`, `should_insert_side_row(conn, receipt_id, receipt_type)`. |
| `tests/test_backfill.py` | NEW. Unit tests. |

## Tests

* Backfilling the Dina RM500 PAYOUT receipt produces
  `receipt_type = STAFF_ADVANCE` AND a row in `staff_advances`
  with `staff_name = 'Dina'`.
* Backfilling an EVEREST receipt produces `SUPPLIER_PURCHASE`.
* Running the script twice in a row produces zero additional
  side-table rows on the second run.
* A receipt whose `receipt_type` is already `SUPPLIER_PURCHASE`
  but the new classifier returns `UNKNOWN` is left untouched and
  a row appears in `backfill_disagreements`.
* A `STAFF_ADVANCE` classification with `staff_name = None` lands
  in `pending_review`, NOT in `staff_advances`.

## Out of scope

* Auto-rerunning the backfill on a schedule. Manual via owner
  command only.
* Backfilling receipts that already have a non-UNKNOWN type
  (overwriting is too risky without per-row owner approval).
* Side-table schema changes. Re-use whatever the current
  `staff_advances` / `fixed_costs` / `petty_cash` tables look
  like.

## Acceptance

* Migration runs cleanly.
* Script runs end-to-end on production data.
* < 15% of receipts remain `UNKNOWN` after the run.
* Owner can `/advances all_time` and see historical loans for
  Dina, Siti, Kumar, etc.
* Re-running the script the same day produces zero additional
  inserts.
