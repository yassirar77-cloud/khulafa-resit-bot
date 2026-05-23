# Khulafa Daily Intelligence System — Roadmap

**Owner:** Yassir
**Target completion:** ~3 weeks (PR #29 → PR #34)
**Last update:** 2026-05-23 (after PR #28 merge)

> Save-point for the goal doc agreed on 2026-05-23. Edit in place as
> scope shifts; commits are the audit trail.

---

## Vision

Transform `@khulafaresitbot` from a passive receipt logger into an
**active business intelligence system** that delivers a nightly digest
at 11pm to Khulafa owners (Yassir + Ariffin + Datuk Wahith)
summarising the day's financial signal across all 10 outlets, with
confirmed price movements, supplier opportunities, staff advance
status, and data quality alerts.

**Success criteria:** Owners check the 11pm digest before sleeping and
act on it before opening shops the next morning. Bot becomes part of
the daily management ritual, not just a logging tool.

---

## End state — the 11pm report

```
KHULAFA DAILY DIGEST — 23 Mei 2026 (Sabtu)

RECEIPTS PROCESSED TODAY
   Total: 47 (Supplier: 32 | Advance: 5 | Petty: 4 | Utility: 2 | Unknown: 4)
   Outlets active: 9/10 (Klang B.Emas — no receipts today)

PRICE INCREASES (confirmed, vs 7-day avg)
   - JINTAN PUTIH 1kg @ SAIDA: RM18 -> RM22 (+22%) [Bistro, SEK-20]
   - IKAN BILIS GRED A 1kg @ BALAJI: RM38 -> RM42 (+10%) [D.U]
   -> Action: Review SAIDA contract; consider SHREE MAP JAYA quote

PRICE DROPS
   - DAUN KARI 100g @ SAYUR: RM2.50 -> RM2.00 (-20%) [all outlets]

SUPPLIER SWITCHING OPPORTUNITIES
   - Jintan: SHREE MAP JAYA RM17/kg vs SAIDA RM22/kg
     -> 23% saving, 8kg/month avg -> est. RM40/month saved

CROSS-OUTLET ANOMALIES
   - Same item, same week, price gap >15%:
     - JINTAN @ SAIDA: Bistro RM22 vs SEK-20 RM18

STAFF ADVANCES
   Outstanding total: RM2,350 across 5 staff
   - New today: Dina (SEK-6) RM500
   - Aged 30+ days: Kumar (Bistro) RM800
   - Repaid today: Siti (D.U) RM150

SUPPLIER ACTIVITY
   Active today: BABAS, SAIDA, HANEE, EVEREST, BESTARI FARM
   No delivery: FOOK LEONG (last seen 3 days ago)
   New face: SHREE MAP JAYA delivered to SEK-6 first time

DATA QUALITY ALERTS
   - 4 receipts UNKNOWN — manual review needed
   - PVS SANTAN total looks off (RM18,000 single line) — likely OCR
   - 2 receipts have date >7 days mismatch with upload time

TOMORROW'S CHECKS
   - Compare BESTARI Farm chicken vs Bestari Wholesale this week
   - Verify Kl Sg Besi outlet tagging (not in 10-outlet roster)
   - Confirm Jakel RM181 Diamond Ball delivery (above avg)

/digest yesterday | /digest weekly | /digest <outlet>
```

---

## Dependency graph (must build in order)

```
PR #28 (classifier gating)              MERGED 2026-05-23
    v
PR #29 (OCR fixes: RM/Sen + decimal + date)
    v
PR #30 (merchant name normalisation)
    v
PR #31 (backfill classifier across historical)
    v
PR #32 (canonical item normalisation)
    v
PR #33 (price_movements materialised view)
    v
PR #34 (daily digest report)
```

Each PR unlocks the next. Skipping order = unreliable data feeds into
reports = owners lose trust = project dies.

---

## PR #29 — OCR data quality fixes

**Goal:** Stop OCR from producing wrong totals/dates that corrupt the
price intelligence layer.

**Problems to fix:**

1. **RM/Sen split column misread** (root cause of PVS SANTAN RM18,000 bug)
   - Detect: raw_text contains `RM` and `Sen` as column headers
     OR pattern `\d+\s+\d{2,3}` near `Total`.
   - Re-parse: split number on whitespace, treat right side as cents.

2. **Decimal point loss** (NASI LEMAK RM82.50 -> RM8,250 bug)
   - Heuristic: if extracted total >> sum(line_items) by ~100x,
     retry with decimal at position -2.
   - Cross-validate: total should equal sum(qty * unit_price).

3. **Date parsing reliability**
   - Reject dates >7 days in future or >365 days in past.
   - If raw_text has multiple date candidates, prefer one in
     valid range.
   - Flag suspect dates with confidence <80%, route to manual
     review.

4. **Currency separator confusion**
   - `1,234.56` vs `1.234,56` vs `1 234.56` — Malaysian receipts
     mix all three. Normalise on output to dot decimal, no
     thousand separator.

**Acceptance:**
- Re-parse top 50 highest-total historical receipts; manually
  verify >=45/50 now correct.
- New incoming receipts with split columns log correctly first
  time.

See `docs/briefs/PR-29-ocr-data-quality.md` for the implementation
brief.

---

## PR #30 — Merchant name normalisation

**Goal:** "MYMOON's KITCHEN" and "MYMOOH's KITCHEN" become the same
canonical entity in queries.

- New tables `merchant_canonical` + `merchant_alias`.
- Seed from 18 known suppliers + discovered ones (PVS SANTAN,
  DIAMOND BALL, EVEREST AISVARAM, MYMOON, BESTARI FARM/WHOLESALE).
- Fuzzy matcher: exact -> substring -> Levenshtein <=2 -> log to
  `unmapped_merchants`.
- Every new receipt: run normaliser before `classify_receipt`,
  store both `merchant` (OCR original) and `merchant_canonical_id`.

---

## PR #31 — Historical classifier backfill

**Goal:** Make 18 months of receipts queryable by `receipt_type`.

- Script `scripts/backfill_classifier.py`.
- Iterate receipts where `receipt_type = 'UNKNOWN'`, re-run
  classifier (now with merchant kwarg working post-#28), populate
  side tables (`staff_advances`, `fixed_costs`, `petty_cash`).
- Acceptance: <15% UNKNOWN; all historical PAYOUT/PINJAM in
  `staff_advances`.

---

## PR #32 — Canonical item normalisation

**Goal:** "JINTAN PUTIH 1KG" / "Jintan Putih" / "jintan 1kg" /
"white cumin 1kg" all map to one canonical item.

- New tables `item_canonical` + `item_alias`.
- Unit normalisation: store `unit_price_per_base_unit` (e.g.
  RM/kg) for comparison.
- Replace existing `canonical_item` field with FK to
  `item_canonical.id`.

---

## PR #33 — Price movements materialised view

**Goal:** Pre-compute price changes so daily report runs in <1s.

- Materialised view `price_movements` with 7d and 30d rolling
  averages.
- Refresh daily at 22:50 via APScheduler.
- Cross-merchant arbitrage view `supplier_arbitrage` filtered to
  savings >RM20/month.

---

## PR #34 — Daily digest report

**Goal:** Ship the 11pm summary to designated owner Telegram chats.

- New module `daily_digest.py`. Sections: receipts processed,
  price changes, switching opportunities, anomalies, advances,
  supplier activity, data quality, tomorrow's checks.
- APScheduler cron daily at 23:00 Asia/Kuala_Lumpur.
- Recipients table `digest_subscribers` with
  `notification_level` ('all' | 'alerts_only' | 'silent').
- Commands: `/digest`, `/digest yesterday`, `/digest weekly`,
  `/digest <outlet>`, `/digest off`, `/digest on`.
- Confidence threshold >=80% to surface in digest.
- Owner-only chat_ids hardcoded in config.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| OCR errors leak into reports -> owner trust collapses | PR #29 first. Confidence >=80% to surface in digest. Data quality alerts upfront. |
| Daily digest becomes noise | Top-N per section. Confirmed filter on all alerts. `/digest off` always available. |
| Materialised view stale if scheduler crashes | Health check pings refresh job. Header includes "Data freshness: <timestamp>". |
| OCR re-parse cost spike | PR #29 backfill is opt-in, in batches. |
| Owners overwhelmed | Start Datuk Wahith on `alerts_only`. Yassir + Ariffin full. Adjust. |
| Bot down at 11pm | APScheduler retry. Digest sent within 2h of scheduled time. |

---

## Timeline

| Week | PR | Status |
|---|---|---|
| Week 1 (now) | #28 (gating) | MERGED 2026-05-23 |
| Week 1 | #29 (OCR fixes) | In progress |
| Week 1-2 | #30 (merchant normalisation) | Pending #29 |
| Week 2 | #31 (backfill) | Pending #30 |
| Week 2 | #32 (item normalisation) | Parallel to #31 |
| Week 3 | #33 (price_movements view) | Pending #32 |
| Week 3 | #34 (daily digest) | Final ship |

---

## Non-goals (don't do these yet)

- BinaApp integration / dashboard UI
- WhatsApp delivery (Telegram only for v1)
- Mobile app
- Owner web dashboard (deferred to PR #40+)
- AI-generated narrative summaries
- Multi-language report (Manglish/English mixed; Tamil/Mandarin/Jawi later)
- Predictive forecasting
- Email digests

---

## Success metrics (track from PR #34 launch)

| Metric | Target after 30 days |
|---|---|
| Digest delivery uptime | 95%+ |
| False alarm rate (price alerts) | <10% |
| Owner-initiated actions per week from digest | >=3 |
| Suppliers switched based on arbitrage alerts | >=1 in first month |
| Staff advances repaid on time after age alerts | +20% vs baseline |
| Owner satisfaction (Yassir's gut check) | 8/10 |
