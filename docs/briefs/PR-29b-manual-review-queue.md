# PR #29b — Low-confidence receipt manual-review queue

**Status:** Brief drafted, not implemented
**Depends on:** PR #29 — must land after. PR #29 is the module that
sets the confidence values this PR routes on.
**Blocks:** PR #29c (historical re-parse uses the same
`pending_review` table for its manual approval flow).

---

## Why

PR #29 introduces three OCR sanity heuristics that adjust receipt
confidence downward when a correction is applied (decimal-fix −20,
date out of window −15, split column −10). The intent is for the
upcoming daily digest (PR #34) to hide receipts with `confidence < 80`
from owner-visible reports.

That filter is the right move for **reporting** but it's the wrong
move for **storage**. Today a receipt with confidence 50 still
writes to `receipts` and `item_prices`, polluting the price
intelligence layer regardless of whether the digest displays it.
PR #33's `price_movements` materialised view computes rolling
averages off `item_prices` — a single bad PVS SANTAN row that
slipped through the OCR heuristics (because its line items were
empty, so cross-validation couldn't fire) would still poison the
7-day average for santan prices.

The fix is to interpose a manual-review checkpoint. Receipts whose
confidence falls below a configurable threshold do NOT auto-save.
They land in a `pending_review` table, the bot DMs an authorised
reviewer with the photo and parsed data, and the receipt only
reaches `receipts` when the reviewer explicitly approves or edits.

This is also the foundation for PR #29c — the historical re-parse
flow writes its proposed corrections through the same review queue
rather than overwriting live rows.

## Scope

### 1. New `pending_review` table

```sql
CREATE TABLE pending_review (
  id BIGSERIAL PRIMARY KEY,
  telegram_message_id BIGINT,
  chat_id BIGINT,
  photo_file_id TEXT,
  parsed_merchant TEXT,
  parsed_total NUMERIC,
  parsed_date DATE,
  parsed_items JSONB,
  confidence INTEGER,
  reason TEXT,
  status TEXT DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'edited', 'rejected')),
  reviewer_chat_id BIGINT,
  reviewed_at TIMESTAMPTZ,
  edited_data JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_pending_review_status ON pending_review(status, created_at);
```

Migration filename: `migrations/0005_pending_review.sql`.

### 2. Routing decision in `bot.py handle_photo`

After OCR + classification, check `confidence < CONFIDENCE_FLOOR`
(default `60`, exposed as `os.environ["REVIEW_CONFIDENCE_FLOOR"]`).
If under the floor:

* Write to `pending_review` with `reason` populated from the
  quality flags (`"decimal_corrected"`, `"date_out_of_window"`,
  `"split_column"`, `"low_base_confidence"`).
* DM each authorised reviewer with the photo + parsed snapshot +
  three inline buttons.
* Do NOT write to `receipts` or `item_prices`.

If at-or-above the floor: existing path, no behaviour change.

### 3. Inline button callbacks

* **✅ Save as-is** — copy `pending_review` row into `receipts`
  with the original parsed values; mark status `approved` with
  `reviewer_chat_id` and `reviewed_at`.
* **✏️ Edit** — start a Telegram conversation flow (use
  `ConversationHandler`) that prompts in order for: corrected
  total, corrected merchant, corrected date. Each prompt accepts
  `skip` to keep the parsed value. Save resulting overrides to
  `edited_data` JSONB, then copy to `receipts` with the edits
  applied; mark status `edited`.
* **❌ Discard** — mark status `rejected`. Nothing saved to
  `receipts`. Photo remains on Telegram for forensic purposes.

Callback data format: `review:<id>:<action>`. Reject callbacks
where `reviewer_chat_id` is not in the authorised list (see
section 5).

### 4. Daily digest integration

PR #34 will read pending counts from this table for the data
quality alerts section. No code change needed in this PR for the
digest itself — the table just needs to exist with the right
shape. Document the expected query:

```sql
SELECT COUNT(*) FROM pending_review WHERE status = 'pending';
```

### 5. Authorised reviewer list

New file `config/reviewers.py`:

```python
REVIEWER_CHAT_IDS = frozenset([
    YASSIR_CHAT_ID,   # set via env
    ARIFFIN_CHAT_ID,
])
```

Chat IDs are pulled from environment variables, not hardcoded.
Document the env var names in the PR description.

Datuk Wahith does NOT get review buttons in v1 — he gets the
nightly digest only. Adjust if owner asks.

## Files

| File | Change |
|---|---|
| `migrations/0005_pending_review.sql` | NEW. Table + index above. |
| `config/reviewers.py` | NEW. Authorised reviewer chat IDs from env. |
| `bot.py` | Branch in `handle_photo` to route low-confidence receipts; new callback handlers for the three buttons; conversation flow for edits. |
| `pending_review.py` | NEW. Pure helpers: `should_queue(confidence, flags)`, `serialize_parsed_for_review(parsed)`, `apply_edits_to_parsed(parsed, edits)`. Keeps bot.py thin and testable. |
| `tests/test_pending_review.py` | NEW. Unit tests for the helpers. |
| `tests/test_bot_review_flow.py` | NEW. Source-level checks that bot.py routes < floor to review and gates inline callbacks on reviewer auth. Same pattern as the existing `BotGatingTests` for PR #28. |

## Tests

* `should_queue(confidence=50, flags=[])` -> `True`.
* `should_queue(confidence=80, flags=[])` -> `False`.
* `should_queue(confidence=85, flags=["decimal_corrected"])` ->
  `False` (already adjusted; threshold check is on the final value).
* A receipt with confidence 50 written via the routing path lands
  in `pending_review`, NOT in `receipts` (source-level test that
  bot.py contains the early return).
* Reviewer click ✅ → mocked Supabase insert into `receipts`, row
  removed (or status flipped) in `pending_review`.
* Reviewer click ✏️ → bot enters edit conversation, accepts
  three field corrections, writes `edited_data`.
* Non-reviewer chat_id triggers callback → bot returns without
  touching the row.

## Out of scope

* A web dashboard for review. Telegram inline buttons only for v1.
* Auto-approval of repeat low-confidence receipts from the same
  merchant. Always human-in-the-loop for v1.
* Bulk-approve / bulk-reject. Future iteration once the queue
  proves itself.
* Notifications to reviewers when the queue exceeds N items.

## Acceptance

* All existing tests pass.
* New `pending_review` table exists in production.
* A receipt with synthetic confidence 50 (e.g. an OCR test image
  containing a future-dated header) routes to the queue and does
  NOT appear in `receipts`.
* Reviewer can ✅ / ✏️ / ❌ end-to-end via Telegram.
* Non-reviewer attempting to click a button receives no
  visible response and no DB write occurs.
