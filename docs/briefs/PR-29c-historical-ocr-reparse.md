# PR #29c — Historical OCR re-parse (opt-in batch job)

**Status:** Brief drafted, not implemented
**Depends on:** PR #29 (OCR quality module exists) AND PR #29b
(pending_review table + edit flow exist). Must land after both.
**Blocks:** None. Improves data quality of PR #33's
`price_movements` view but does not gate any later PR.

---

## Why

PR #29 fixes the OCR quality issues going forward. But the
historical layer still carries the damage: somewhere on the order
of 100+ receipts have totals that are 100x wrong (PVS SANTAN
RM18,000 → RM180.00, NASI LEMAK RM8,250 → RM82.50, etc.), dates
in 2028, or merchants that didn't parse cleanly.

PR #33 will compute rolling price averages from `item_prices`. A
single uncorrected RM18,000 santan entry inflates the 30-day
average so badly that real price increases get drowned out and
real price drops look like noise. We need to clean the back
catalogue before turning on the digest.

Two non-negotiables for this work:

* **Nothing auto-applies.** The owner reviews and approves
  corrections in batches. The script never overwrites a live
  receipt row without explicit human approval.
* **Idempotent.** Running the script twice must not double-process
  receipts that have already been corrected. Use an audit table
  with an `applied` flag.

## Scope

### 1. One-time script `scripts/reparse_ocr_historical.py`

Query receipts likely to be wrong:

```sql
SELECT id, merchant, total, receipt_date, raw_text, confidence, items
FROM receipts
WHERE confidence < 80
   OR total > 5000
   OR receipt_date > NOW() + INTERVAL '7 days'
   OR receipt_date < DATE '2023-01-01'
ORDER BY total DESC, id ASC;
```

For each:

* Re-run `ocr_quality.correct_total_with_items(total, items)`.
* Re-run `ocr_quality.validate_date(raw_text)`.
* Re-run merchant normalisation (only if PR #30 has landed — until
  then leave merchant untouched).
* If any field changes, INSERT into `reparse_audit` (see below).
  Never touch the original `receipts` row.

The script must be safe to run repeatedly: skip any
`receipts.id` that already has an `applied = TRUE` row in
`reparse_audit`.

### 2. New `reparse_audit` table

```sql
CREATE TABLE reparse_audit (
  id BIGSERIAL PRIMARY KEY,
  receipt_id BIGINT REFERENCES receipts(id) ON DELETE CASCADE,
  old_total NUMERIC,
  new_total NUMERIC,
  old_date DATE,
  new_date DATE,
  old_merchant TEXT,
  new_merchant TEXT,
  confidence_old INTEGER,
  confidence_new INTEGER,
  applied BOOLEAN DEFAULT FALSE,
  applied_at TIMESTAMPTZ,
  applied_by_chat_id BIGINT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_reparse_audit_applied ON reparse_audit(applied, created_at);
CREATE UNIQUE INDEX idx_reparse_audit_unique_pending
  ON reparse_audit(receipt_id)
  WHERE applied = FALSE;
```

The partial unique index prevents the same `receipt_id` having
multiple pending audit rows — re-running the script just no-ops
on receipts that already have a pending entry.

Migration filename: `migrations/0006_reparse_audit.sql`.

### 3. Owner-only Telegram commands

* `/reparse_status` — counts: total audit rows, applied, pending,
  by-type breakdown (totals corrected vs dates corrected vs both).
* `/reparse_preview <n>` — show the next `n` pending changes as a
  formatted Telegram message, one per line: `#receipt_id:
  RM<old> → RM<new>, date <old> → <new>, conf <old> → <new>`.
  Default `n=10`. Max `n=50` (Telegram message length cap).
* `/reparse_apply <n>` — apply the next `n` pending audit rows in
  insertion order. Updates the original `receipts` row, sets
  `reparse_audit.applied=TRUE`, `applied_at=NOW()`,
  `applied_by_chat_id=<requestor>`. Default `n=10`.
* `/reparse_apply_all` — DANGER. Requires a Y/N confirmation
  step via Telegram inline button. Applies every pending row.

All four commands are restricted to the `REVIEWER_CHAT_IDS` set
from PR #29b. Non-reviewers get no response.

### 4. Conservative defaults

* If `correct_total_with_items` doesn't fire (no items, sum
  doesn't match), the audit row's `new_total` equals
  `old_total` — i.e. no proposed change. The script still records
  the row so the owner can see "this receipt was reviewed and
  nothing changed".
* If a receipt's `raw_text` is empty (legacy receipts), skip
  entirely. Log to a `reparse_skipped` count in the final report.
* No re-OCR of the original photo — we work from `raw_text` only.
  Re-running glm-ocr against 18 months of photos is a separate
  ticket (cost spike, rate limits).

### 5. Final report

After the script completes, send a summary DM to the owner who
launched it:

```
Reparse pass complete:
  Receipts evaluated:        237
  Audit rows created:        118
  Skipped (empty raw_text):  19
  Already applied (skipped): 22
  
  Proposed corrections:
    Total only:               74
    Date only:                17
    Total + date:             27
  
  Top 5 by total delta:
    #1532: RM18,000 -> RM180.00 (PVS SANTAN)
    #0911: RM8,250  -> RM82.50  (NASI LEMAK)
    ...
  
  Next: /reparse_preview 10
```

## Files

| File | Change |
|---|---|
| `migrations/0006_reparse_audit.sql` | NEW. Table + indexes above. |
| `scripts/reparse_ocr_historical.py` | NEW. The batch processor. Can be run locally against the production Supabase URL with env credentials. |
| `bot.py` | Four new command handlers. Re-use the auth check helper from PR #29b. |
| `reparse.py` | NEW. Pure helpers: `propose_corrections(receipt_row) -> dict`, `apply_audit_row(conn, audit_row)`. Keeps bot.py thin. |
| `tests/test_reparse.py` | NEW. Unit tests on the helpers using mock receipt rows. |

## Tests

* `propose_corrections({"total": 18000, "items": [{"price": 180}], "raw_text": "..."})`
  yields `{"new_total": 180.0, ...}`.
* `propose_corrections` on a receipt with no items and a clean
  total yields no change (returns `None` or an empty diff).
* `apply_audit_row` on an already-applied row is a no-op (returns
  `False` and does not re-update).
* Running the script twice on the same database produces zero
  net change on the second run (idempotency check).
* `/reparse_preview` from a non-reviewer chat returns no message
  and no DB writes occur.

## Out of scope

* Re-running glm-ocr against the original photos. Stick to
  parsing the already-stored `raw_text`.
* Auto-scheduled reparse jobs. Manual command only.
* Re-processing receipts where `reparse_audit.applied = TRUE`
  even if a later PR adds new heuristics. New corrections require
  new audit rows; this PR explicitly stops at the unique partial
  index.
* Bulk undo. If a reparse application is wrong, revert via direct
  SQL — not a UI feature in v1.

## Acceptance

* Migration runs cleanly.
* Script runs end-to-end on production data without errors.
* The PVS SANTAN receipt's audit row shows
  `old_total=18000, new_total=180`.
* `/reparse_apply 5` updates 5 receipts and flips their audit
  rows to `applied=TRUE`.
* Running the script twice in a row produces zero additional
  audit rows on the second invocation.
