# PR #29 — OCR data quality fixes

**Status:** In progress (branch `claude/focused-franklin-XoTkQ`)
**Depends on:** PR #28 (merged 2026-05-23)
**Blocks:** PR #30 (merchant normalisation), PR #33 (price_movements view)

---

## Why

The price intelligence layer (PR #33 onwards) is only as good as the
totals and dates we feed into it. Today the OCR pipeline produces two
classes of corrupting errors that we already know about:

1. **PVS SANTAN bug** — a single receipt landed at RM18,000 because
   the OCR split a `RM | Sen` column header into a 5-digit
   concatenation.
2. **NASI LEMAK bug** — a RM82.50 receipt got logged as RM8,250
   because the decimal point dropped out of the OCR markdown.

A third issue, the existing FIXME at `ocr_glm.py:153-161`, is that
when glm-ocr re-renders a date it sometimes transposes day/month
(observed: `10/05/2026` -> `2026-10-05`). The current `_find_date`
picks the first candidate it sees with no plausibility check, so a
date 5 months in the future sails through.

If we ship the daily digest (PR #34) on top of corrupted totals and
dates, owners will see "RM18,000 increase on jintan" alerts and lose
trust in the system within the first week.

## Scope

Four fixes, all in the GLM markdown parser plus a thin shared
quality module:

### 1. Total sanity (RM/Sen split + decimal-loss)

Add a quality pass that runs after `_find_total` returns its initial
candidate.

- **Detect split column:** if `raw_text` matches the pattern
  `(?i)\b(RM)\b.*?\b(Sen)\b` on a header row (table or otherwise),
  treat numeric data rows as `<ringgit> <cents>` pairs.
- **Cross-validate against line items:** compute
  `sum_items = sum(item.price for item in items if item.price)`.
  If `sum_items > 0` and `abs(total - sum_items) / sum_items > 0.5`
  but `abs(total/100 - sum_items) / sum_items < 0.1`, the total is
  100x off and we correct to `total / 100`.
- Same check for the opposite direction (`total * 100`) — covers
  the "RM 24 100" case where the cents column has 3 digits.
- Log every correction at WARNING with the original, corrected,
  and sum_items values so we can audit in production.

### 2. Date sanity

Wrap `_find_date` so the candidate has to land in a plausible window
before we accept it.

- Plausible window: `today - 365 days <= date <= today + 7 days`.
- If multiple `\b(\d{4})[/-.](\d{1,2})[/-.](\d{1,2})\b` and
  `\b(\d{1,2})[/-.](\d{1,2})[/-.](\d{2,4})\b` candidates exist,
  prefer (a) one anchored on a `Date:` / `Tarikh:` / `Dated:`
  label, (b) one inside the plausible window, (c) the first one.
- Drop the existing FIXME behaviour where YMD wins even when the
  underlying receipt was DD/MM/YY.
- Today is injected as a parameter (default `datetime.today()`) so
  tests can pin it.

### 3. Currency separator normalisation

`_normalize_amount` currently strips commas and spaces unconditionally.
This silently mangles European-style `1.234,56` (= 1234.56 EUR) when
it appears on imported supplier docs.

- If the string has both `.` and `,` and the rightmost separator is
  `,` with exactly 2 digits after, treat as European: drop `.`,
  swap `,` for `.`.
- Otherwise treat as Malaysian/US: drop `,` and spaces, keep `.`.
- Reject anything with more than one `.` after normalisation
  (catches `13.00.50` -> None).

### 4. Confidence adjustment

When a quality heuristic fires, deduct confidence:
- Decimal correction applied: -20.
- Date out of window (kept but flagged): -15.
- Split column detected and corrected: -10.

This lets PR #34's digest filter (`confidence >= 80`) hide suspect
data without us having to plumb a separate `flags` column.

## Files

| File | Change |
|---|---|
| `ocr_quality.py` | NEW. Pure functions: `correct_total_with_items`, `validate_date`, `normalize_amount_locale_aware`. No I/O, no telegram/supabase deps. |
| `ocr_glm.py` | Wire the three helpers into `parse_markdown_receipt`. Subtract from `_heuristic_confidence` when quality fires. |
| `tests/test_ocr_quality.py` | NEW. Cover PVS SANTAN, NASI LEMAK, date-transpose, European separators, multi-date receipts. |

## Tests

Concrete cases to lock in:

- `correct_total_with_items(18000.0, items=[{"price": 180.0}])` -> `180.0` (decimal corrected, 100x off).
- `correct_total_with_items(8250.0, items=[{"price": 82.50}])` -> `82.50`.
- `correct_total_with_items(180.0, items=[{"price": 180.0}])` -> `180.0` (no-op).
- `correct_total_with_items(100.0, items=[])` -> `100.0` (no items, no correction).
- `validate_date("2026-10-05", today=date(2026, 5, 23))` -> rejected (5 months future), look for alternative.
- `validate_date("10/05/2026", today=date(2026, 5, 23))` -> `2026-05-10`.
- `normalize_amount_locale_aware("1.234,56")` -> `1234.56`.
- `normalize_amount_locale_aware("1,234.56")` -> `1234.56`.
- `normalize_amount_locale_aware("1 234.56")` -> `1234.56`.

## Out of scope

- Re-OCR of historical receipts. PR #29 only fixes the parser; the
  18-month re-parse is a separate batch job that the owner runs on
  demand (per roadmap risks section).
- Changing the GLM OCR prompt itself.
- Merchant normalisation (PR #30).
- Item canonicalisation (PR #32).

## Acceptance

- All existing tests pass.
- New `tests/test_ocr_quality.py` covers the cases above.
- A real PVS SANTAN markdown sample (anonymised in fixture) parses
  to the correct total.
- `parse_markdown_receipt` still returns the canonical schema
  unchanged; only field values may differ when a heuristic fires.
- Confidence drops below 80 when any heuristic fires, so the
  digest filter (planned in PR #34) will hide these.
