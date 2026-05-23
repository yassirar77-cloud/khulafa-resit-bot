# PR #34 — Daily digest report (11pm Telegram delivery)

**Status:** Brief drafted, not implemented
**Depends on:** PR #33 (price_movements and supplier_arbitrage
matviews must exist and refresh). Must land after.
**Blocks:** Nothing in this queue — final PR of the chain.

---

## Why

This is the entire reason the previous seven PRs exist. The daily
digest is the owner-visible product: a Telegram message at 23:00
Asia/Kuala_Lumpur that tells Yassir, Ariffin, and Datuk Wahith
what happened today across all 10 outlets and what to act on
before opening tomorrow.

Success looks like: owners check the digest before sleeping and
take at least one specific action per week based on its content
(switch supplier, chase a staff loan, fix an outlet, follow up on
a flagged receipt). If the digest is noisy, slow, or wrong, owners
will start ignoring it within a week and the project fails.

## Scope

### 1. New module `daily_digest.py`

Composed of ten section functions, each returning a Telegram-
formatted MarkdownV2 string or an empty string when the section
has nothing to report. The composer assembles non-empty sections
in a fixed order, capping total length at 4,096 characters
(Telegram's hard limit) and splitting into a continuation message
if needed.

Sections:

* `header(date, freshness)` — date, day name, "Data freshness:
  YYYY-MM-DD HH:MM" from PR #33's `view_refresh_log`. Always
  rendered.
* `receipts_summary(date)` — count by `receipt_type`, outlets
  active vs silent. Always rendered.
* `price_increases(date)` — top 5 from `price_movements` where
  `pct_change_7d > 10` for items with `sample_count >= 3` in the
  last 7 days. Render only when non-empty.
* `price_drops(date)` — same logic, `< -10`. Non-empty only.
* `arbitrage_opportunities()` — top 3 from `supplier_arbitrage`
  by `savings_pct * total_qty_30d` (i.e. weighted by usage).
  Non-empty only, and only when the implied saving exceeds
  RM20/month.
* `cross_outlet_anomalies(date)` — same item / same week /
  same merchant / different outlets, price gap > 15%. Non-empty
  only.
* `staff_advances(date)` — outstanding total, new today, aged
  30+ days, repaid today. Always rendered when there's any
  outstanding balance.
* `supplier_activity(date)` — active today, missing (no receipt
  in last 3 days where there normally would be one), new faces
  (canonical merchant with first appearance today). Always
  rendered.
* `data_quality_alerts(date)` — pending_review count, OCR-suspect
  receipts (confidence < 80 today), future-dated receipts.
  Always rendered (even if zero — owners need to know they're not
  hiding silently).
* `tomorrows_checks(date)` — auto-generated heuristic list:
  outlets with low receipt count today, suppliers due for delivery,
  flagged items needing manual review. Render only when non-empty.

### 2. New tables

```sql
CREATE TABLE digest_subscribers (
  chat_id BIGINT PRIMARY KEY,
  owner_name TEXT NOT NULL,
  active BOOLEAN DEFAULT TRUE,
  notification_level TEXT DEFAULT 'all'
    CHECK (notification_level IN ('all', 'alerts_only', 'silent')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE digest_log (
  id BIGSERIAL PRIMARY KEY,
  digest_date DATE NOT NULL,
  chat_id BIGINT NOT NULL,
  sent_at TIMESTAMPTZ DEFAULT NOW(),
  message_id BIGINT,
  status TEXT NOT NULL DEFAULT 'sent'
    CHECK (status IN ('sent', 'failed', 'skipped')),
  error TEXT,
  message_length INTEGER,
  UNIQUE (digest_date, chat_id)
);

CREATE INDEX idx_digest_log_date ON digest_log(digest_date DESC);
```

Migration filename: `migrations/0011_daily_digest.sql`.

### 3. Scheduler

```python
scheduler.add_job(
    send_daily_digest,
    'cron', hour=23, minute=0,
    timezone='Asia/Kuala_Lumpur',
    id='daily_digest',
    misfire_grace_time=7200,   # allow up to 2h late if bot was down
    coalesce=True,
)
```

`send_daily_digest`:

1. Read latest `view_refresh_log` to confirm data freshness.
2. Compose the digest once.
3. For each active subscriber (filtered by `notification_level`):
   * `notification_level = 'silent'`: skip entirely.
   * `notification_level = 'alerts_only'`: render only the
     `price_increases`, `arbitrage_opportunities`, and
     `data_quality_alerts` sections.
   * `notification_level = 'all'`: full digest.
4. Send via Telegram, log the outcome to `digest_log`.

If the subscriber already has a `digest_log` row for the same
`digest_date`, skip (the `UNIQUE` constraint backstops accidental
double-sends).

### 4. Smart filters

* Receipts with `confidence < 80` are excluded from
  `price_increases` / `price_drops` / `cross_outlet_anomalies` so
  one bad OCR row doesn't headline the digest.
* `arbitrage_opportunities` requires both `savings_pct > 10` AND
  `savings_pct * total_qty_30d > 20` (RM20/month minimum effective
  saving).
* Each section caps its own row count (5 / 5 / 3 / 5 / 5 / 5 / 5).
* Empty sections are omitted from `all` and `alerts_only` modes
  EXCEPT `data_quality_alerts` which always renders.

### 5. Telegram formatting

* Use Telegram's MarkdownV2 with the `parse_mode='MarkdownV2'`
  argument. Escape all owner-supplied content (merchant names,
  staff names) through a single `escape_md(s)` helper.
* Emoji headers per section so owners can scan vertically.
* Bold the headline numbers.
* End each digest with a one-line nudge:
  `/digest yesterday | /digest weekly | /digest <outlet>`.

### 6. New commands

* `/digest` — re-send today's digest on demand. Reads the latest
  composed digest if same-day already exists; otherwise composes
  fresh.
* `/digest yesterday` — re-send yesterday's. Compose from data
  scoped to that date.
* `/digest weekly` — 7-day rollup variant of the daily digest
  (sum of receipts processed, top changes by absolute value,
  outlets-silent-all-week list).
* `/digest <outlet>` — outlet-specific view (e.g.
  `/digest Bistro`). Filters every section by outlet name match.
* `/digest off` / `/digest on` — toggle the calling chat's
  `digest_subscribers.active` flag.
* `/subscribers list` — owner-only. Shows active subscribers and
  notification levels.
* `/subscribers add <chat_id> <name> [level]` — owner-only. Inserts
  a new subscriber.
* `/subscribers remove <chat_id>` — owner-only. Deactivates.

`/digest` and its variants are owner-only — non-subscribers get
no response. Allowed-chat list reuses `REVIEWER_CHAT_IDS` from
PR #29b plus a digest-only `DIGEST_SUBSCRIBER_CHAT_IDS` superset.

### 7. Initial subscribers

Insert via migration seed (env-driven, not hardcoded):

```sql
INSERT INTO digest_subscribers (chat_id, owner_name, notification_level)
VALUES
  (:yassir_chat_id,        'Yassir',        'all'),
  (:ariffin_chat_id,       'Ariffin',       'all'),
  (:datuk_wahith_chat_id,  'Datuk Wahith',  'alerts_only');
```

Datuk Wahith starts on `alerts_only` for the first week per the
roadmap's risk mitigation. Adjusted manually after feedback.

## Files

| File | Change |
|---|---|
| `migrations/0011_daily_digest.sql` | NEW. Two tables + indexes + seed inserts (parameterised). |
| `daily_digest.py` | NEW. Ten section functions + composer. Pure (no Telegram I/O) — composer accepts a Supabase client and returns the formatted string. |
| `digest_sender.py` | NEW. Wraps composer with Telegram I/O and subscriber iteration. Separates pure composition from delivery for testability. |
| `bot.py` | Wire up `/digest*` and `/subscribers*` commands; schedule `send_daily_digest`. |
| `tests/test_daily_digest.py` | NEW. Unit tests on section functions with mocked DB fixtures. |
| `tests/test_digest_sender.py` | NEW. Source-level + mocked-Telegram tests on subscriber filtering. |

## Tests

* Each section function returns an empty string when given a fixture
  with no qualifying data (no crash, no `None`).
* `header` always renders date and freshness even when no data.
* `price_increases` returns top-5 ordered by descending
  `pct_change_7d` from a fixture with 12 candidate rows.
* `arbitrage_opportunities` filters out items with implied saving
  < RM20/month.
* `compose_digest('all')` for a subscriber returns full content;
  `compose_digest('alerts_only')` returns only the three alert
  sections.
* `send_daily_digest` for a subscriber with `active=False` does
  nothing and writes no `digest_log` row.
* `send_daily_digest` running twice for the same date is
  idempotent (the `UNIQUE (digest_date, chat_id)` constraint).
* MarkdownV2 escaping: a merchant name containing literal `*` or
  `_` characters does not break parse_mode.
* `/digest off` toggles `digest_subscribers.active = FALSE` for
  the calling chat.
* `/digest` from an unsubscribed chat ID returns no response and
  writes no log row.

## Out of scope

* WhatsApp delivery. Telegram only for v1.
* Web dashboard. Telegram only.
* Predictive analytics ("we expect price to rise next week").
  Backward-looking only.
* Multi-language. Manglish + English mixed; Tamil / Mandarin / Jawi
  later if owners request.
* Email digests.
* Per-section opt-in (subscribers can't say "I only want the
  arbitrage section but not advances"). Levels are coarse: `all` /
  `alerts_only` / `silent`.
* AI-generated narrative ("Hey Yassir, jintan went up again so
  maybe…"). Structured data only.

## Acceptance

* Migration runs; three initial subscribers seeded.
* Scheduled job runs at 23:00 for 7 consecutive days. Each day
  produces three `digest_log` rows with `status='sent'`.
* False alarm rate < 10% on a manual sample of 20 surfaced price
  alerts (i.e. fewer than 2 of the 20 should be OCR noise).
* Owner-initiated action count >= 3 in the first week, tracked by
  manual journal (owner writes down what they did after reading
  the digest).
* At least one supplier-switch decision in the first 30 days
  traceable back to an `arbitrage_opportunities` entry.
* `/digest yesterday` works on day 2 and re-renders the previous
  day's composition.
* No crash when a section's underlying matview is empty.

## Followup tickets (out of this PR, but worth noting)

* PR #34b: web dashboard mirroring the digest for desk
  review.
* PR #34c: weekly executive summary (Sunday 22:00) — coarser cut
  for Datuk Wahith.
* PR #34d: digest A/B mode where Yassir gets two variants for
  one week and picks the format he wants permanently.
