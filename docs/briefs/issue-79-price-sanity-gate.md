# Issue #79 — price sanity gate + quarantine

**Problem.** The order-draft layer caps absurd forecast quantities
(PR #78), but that was a downstream band-aid: the garbage still lived in
`item_prices` and poisoned every consumer — forecasting, price-spike
detection, averages, shop comparison. Live findings: receipt 2254's OCR
column merge (`qty=40250`, `unit_price=100`, `line_total=RM4,025,000` —
it produced the "Ais — 5064 bag" draft), a row dated 10 days in the
future, and the RM5,000 receipt-total outlier filter not protecting
per-item rows at all.

**Fix — three layers.**

1. **Ingestion gate** (`price_sanity.py`, wired into
   `price_aggregation.save_item_prices`, the single write path into
   `item_prices`). Every row is checked before insert:
   - absolute ceilings: qty > 1,000, unit_price > RM5,000,
     line_total > RM10,000;
   - line_total > 2x the receipt's own total (only once the line also
     clears RM1,000, so a misread-low receipt total can't quarantine
     every normal line);
   - `receipt_date` after today (Malaysia time);
   - non-positive qty/price;
   - orders-of-magnitude vs the item's recent history: unit_price > 10x
     or qty > 20x the 180-day median, needing >= 3 samples, medians
     computed from in-ceiling rows only so old garbage can't stretch the
     bound.

   Rejects go to `item_price_quarantine` (migration 0042) with
   comma-separated reason codes and are warning-logged with raw values +
   receipt_id, mirroring the existing null-canonical skip logging. The
   gate never raises into the receipt pipeline, and a quarantine-table
   failure (migration not applied yet) still stores the clean rows.

2. **Visibility.** `/price_quarantine [n]` (reviewer-gated) lists the
   latest quarantined rows with reasons for threshold tuning; the nightly
   digest's DATA QUALITY section adds a line whenever rows were
   quarantined that day.

3. **Retro-clean** (`scripts/clean_item_prices.py`). Applies the same
   rules to the existing corpus (corpus-wide medians, no receipt-total
   join), dry-run by default; `--apply` upserts offenders into quarantine
   (original id kept in `item_price_id`, unique-indexed so re-runs are
   idempotent) and only then deletes them from `item_prices`.

**Deploy order.** Apply `migrations/0042_item_price_quarantine.sql` in
Supabase first, then deploy the bot, then run the retro-clean script
(dry-run, eyeball, `--apply`). The bot is safe to deploy before the
migration — rejects are then log-only until the table exists.
