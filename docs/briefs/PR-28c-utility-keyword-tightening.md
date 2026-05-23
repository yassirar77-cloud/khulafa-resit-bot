# PR #28c — UTILITY keyword tightening (word-boundary + brand names)

**Status:** Brief drafted, not implemented
**Depends on:** PR #28b (merged 2026-05-23). No further blocker.
**Blocks:** PR #29 — without this, PR #29's smoke tests may surface
new UTILITY false positives that look like OCR bugs.

---

## Why

The current `UTILITY_KEYWORDS` list relies on bare substring matching
against the upper-cased combined haystack. Two entries are
dangerously broad:

* `"TIME"` substring-matches the literal `Time: HH:MM` stamp printed
  on essentially every receipt by every POS / printer. This is the
  smoking gun behind the PR #28b incident — EVEREST receipt 1532
  classified as UTILITY because its body contained `Time: 09:59`.
* `"DIGI"` substring-matches `DIGITAL`, `DIGITS`, `DIGI-` prefixes,
  and Malaysian IC numbers that begin with the same letters. A
  receipt mentioning "DIGITAL RECEIPT" or "DIGITS PRINTED" would
  classify as UTILITY today.

PR #28b mitigated this for whitelisted suppliers by adding a
merchant-field priority-2 check, but the haystack fallback at
priority 5 still misfires on non-whitelisted merchants whose body
happens to contain `Time:` or `DIGI*`. With PR #29 about to land,
any new OCR-quality regression test that includes a timestamp would
trip the same wire — owners would think the OCR module broke when
the real culprit is the keyword list.

## Scope

Four narrow changes, all confined to `receipt_classifier.py` and its
test suite. No behaviour change for receipts whose merchant is on
the whitelist (PR #28b's tier-2 check already handles them).

### 1. Replace `"TIME"` with explicit brand strings

`UTILITY_KEYWORDS` entry `"TIME"` is replaced by:

* `"TIME DOTCOM"`
* `"TIME FIBRE"`
* `"TIME INTERNET"`

All three are full Time dotCom Berhad product names. The single
broad token is removed entirely.

### 2. Replace `"DIGI"` with explicit brand strings

`UTILITY_KEYWORDS` entry `"DIGI"` is replaced by:

* `"DIGI TELECOMMUNICATIONS"`
* `"DIGI POSTPAID"`
* `"DIGI PREPAID"`
* `"CELCOMDIGI"` (post-merger brand)

### 3. Audit remaining UTILITY and RENT_LICENSE keywords for
ambiguous tokens

Walk the existing lists and flag any 3-4 character substring that
could occur outside the intended utility/government context. Known
suspects to evaluate:

| Token | Risk | Recommendation |
|---|---|---|
| `IWK` | Low | Keep — Indah Water Konsortium is the only meaningful match |
| `MAXIS` | Low | Keep |
| `CELCOM` | Low | Keep |
| `UNIFI` | Low | Keep |
| `SEWA` | Medium | Could appear in addresses; keep but document |
| `LESEN` | Low | Keep |
| `LHDN` | Low | Keep |

Any token that ends up flagged here gets the same treatment as
`TIME` / `DIGI` — replace with full brand or rule string.

### 4. Add a debug log when UTILITY / RENT_LICENSE classification
fires

The existing `classify_receipt` log line already captures `vendor`
and `matched`, but for these two rules specifically we want a
diagnostic line that shows WHICH keyword fired AND whether it came
from the merchant-field tier (priority 3/4) or the haystack tier
(priority 5/6). Tag the log:

```
classify_receipt utility match: tier=merchant kw=TENAGA NASIONAL
classify_receipt utility match: tier=haystack kw=UNIFI
```

This makes post-deploy diagnosis trivial: if production starts
producing UTILITY classifications from the haystack tier on
receipts that should be SUPPLIER, the log line points straight at
the offending keyword.

## Files

| File | Change |
|---|---|
| `receipt_classifier.py` | Update `UTILITY_KEYWORDS` (replace `TIME` / `DIGI`). Add tier-tagged log inside the two UTILITY branches and the two RENT_LICENSE branches. |
| `tests/test_receipt_classifier.py` | New `UtilityKeywordTighteningTests` class covering the four bullet cases below plus a regression test for every removed/added token. |

## Tests

Concrete cases to lock in:

* `merchant=None`, body has `"Time: 14:35"` and `"BABAS JINTAN 22.00"`
  → `SUPPLIER_PURCHASE` (haystack fallback, not UTILITY).
* `merchant=None`, body is `"TIME DOTCOM BROADBAND BILL ... 150.00"`
  → `UTILITY`, vendor=`"TIME DOTCOM"`.
* `merchant=None`, body is `"DIGITAL OCEAN INVOICE ... 200.00"`
  → `UNKNOWN` (the bare `DIGI` substring no longer exists).
* `merchant=None`, body is `"DIGI TELECOMMUNICATIONS POSTPAID ... 89.00"`
  → `UTILITY`, vendor=`"DIGI TELECOMMUNICATIONS"`.
* Existing `test_tnb_utility` (and every other UTILITY test that
  currently passes) must continue to pass unchanged.

## Out of scope

* Adding new utility providers (e.g. ASTRO, YES4G). This PR only
  tightens existing entries.
* Switching `_find_keywords` to regex word-boundary matching as a
  general mechanism. Brand-name replacement is sufficient for the
  ambiguous cases and avoids a sweeping refactor.
* Changes to `SUPPLIER_WHITELIST`, `STAFF_ADVANCE_KEYWORDS`, or
  `PETTY_CASH_KEYWORDS`. Those lists are not implicated.

## Acceptance

* All existing classifier tests pass.
* Four new tests above pass.
* Re-classify 100 random historical receipts using the new
  keyword list. UTILITY count must not **increase** from the
  current production baseline (it may decrease as false positives
  drop out). Document the before/after counts in the PR description.
* Render redeployed; post-deploy log shows tier-tagged UTILITY
  match lines.
