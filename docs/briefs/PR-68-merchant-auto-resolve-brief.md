# PR #68 — Risk-weighted merchant auto-resolution

Clear the ~136-merchant backlog and the ~RM5,007 in unclassified receipts by
auto-resolving confident merchant matches, escalating risky ones to the owner,
and silently deferring the long tail — **without** silently mis-tagging a
high-value receipt.

Built **on top of** the existing layers, not replacing them:

- `merchant_canonical` / `merchant_alias` (PR #30) — canonical store + aliases.
- `receipt_classifier` (PR #31) — receipt typing.
- `reconciliation_service.run_reconciliation` (PR #37) — per-date food cost.

All new logic lives in **`merchant_auto_resolve.py`**; the bot wires three new
owner-only commands; one migration adds the audit table.

## Part 1 — `normalize_merchant_name()` (pure)

UPPERCASE → strip punctuation → collapse whitespace → strip SDN BHD variants,
**including the truncated `SDN. BH`** (OCR clips the final D), plus `BERHAD` and
`SENDIRIAN BERHAD`. Pure and idempotent (`f(f(x)) == f(x)`, input never
mutated).

## Part 2 — fuzzy match to canonicals

`match_confidence()` returns a confidence in **[0, 1]** from a token-set overlap
blended with a substring **anchor** (one normalised phrase contained in the
other on whole-word boundaries) and a `difflib` sequence ratio.

**Fuzzy backend: stdlib `difflib`, not `rapidfuzz`.** ~136 strings × ~56
canonicals once per pass is far below where rapidfuzz's speed matters, and
avoiding a C-extension keeps the Render deploy reproducible. The swap point is
documented in the module docstring.

Only an **exact normalised match** or a **clean anchored containment** reaches
the auto-resolve band (≥ 0.90); every other (typo, partial) match is capped at
`_FUZZY_CONFIDENCE_CEILING = 0.85` so it must clear the risk gate. Below
`MIN_CANDIDATE_CONFIDENCE = 0.35` no canonical is attached (an unknown vendor
reads as "no match", not a misleading guess).

## Part 3 — the CORE: risk-weighted decision

```
risk = (1 - confidence) × RM_at_stake

confidence ≥ AUTO_RESOLVE_CONF_CUTOFF        → auto-resolve (any RM)
confidence < cutoff and risk <  threshold    → defer   (silent long tail)
confidence < cutoff and risk ≥  threshold    → escalate to owner
```

Exposed constants (one place to tune):

| constant | value | meaning |
|---|---|---|
| `AUTO_RESOLVE_CONF_CUTOFF` | `0.90` | clean match auto-resolves at any RM |
| `ESCALATION_RISK_THRESHOLD` | `200.0` (RM) | tuned against production — RM50 escalated 107 merchants; RM200 keeps the 24 high-value ones (RM32,781) in the queue and lets 83 lower-risk ones auto-resolve/defer |

### Worked examples (must hold in tests)

| # | confidence | RM at stake | risk | decision |
|---|---|---|---|---|
| 1 | 0.96 | 1,240.50 | 49.62 | **auto-resolve** (conf ≥ cutoff, any RM) |
| 2 | 0.92 | 6.00 | 0.48 | **auto-resolve** (conf ≥ cutoff, tiny RM) |
| 3 | 0.70 | 18.00 | 5.40 | **defer** (risk < 200) |
| 4 | 0.65 | 700.00 | 245.00 | **escalate** (risk ≥ 200) |

## Part 4 — reversible & re-runnable

Auto-resolve writes a `merchant_alias` row (`created_via = 'auto_resolved'`),
tags the receipts, and writes a `merchant_resolution_log` row carrying the undo
metadata (`alias_id`, `receipt_ids`, `affected_dates`). **Every alias write
triggers a reconciliation re-run** for each affected `business_date` so food
cost updates across history.

- **Reversible** — `/merchant_undo <log_id>` (`undo_resolution`) untags the
  receipts, deletes the alias, marks the log `undone`, and re-reconciles.
- **Re-runnable** — only NULL-canonical receipts are candidates, so a second
  pass is a no-op.

## Part 5 — `/merchant_review`

Owner queue of active escalations ranked by **RM at stake, descending**. A
single digest line (`format_review_digest_line`) is appended to the nightly
digest when the queue is non-empty — once a day (the digest is once-nightly), no
spam.

## Part 6 — `/merchant_resolve_now`

One-pass backfill of the whole backlog. Reports resolved / escalated / deferred
counts and re-reconciles every affected date **once** at the end.

## Schema

`migrations/0025_merchant_resolution_log.sql` — new `merchant_resolution_log`
table, plus a widened `merchant_alias.created_via` CHECK to accept
`'auto_resolved'`.

## Out of scope (kept attributable)

Food cost calculation & digest timing (untouched), item-level
canonicalisation, manager delivery (stays `False`).

## Tests (`tests/test_merchant_auto_resolve.py`)

Normalisation (punct/ws, `SDN. BHD.`, truncated `SDN. BH`, BERHAD, purity);
scorer (exact, anchored-high, typo-below-cutoff, no-overlap floor,
`best_canonical`); the four worked examples + threshold boundary + constants
pin; review-queue ranking, queue format, digest-line gating; and the DB layer on
`FakeSupabase` with a recording reconcile stub: auto-resolve tags+alias+log
+reconcile, escalate-without-tagging, defer, re-runnable no-op, queue read-back,
undo reverses+re-reconciles, undo idempotent; plus migration & bot-wiring
source checks.
