# POS re-dating — verification (11 Oct 2026)

## 1. Monthly POS reports vs stored D-files (Aug + Sep 2026)

Source: the POS "MONTHLY REPORT-<outlet> ON <Mon>-2026" emails (per-day SALE
column) vs `sales_daily_summary.day_sales` for the same outlet and business date.

| Outlet | Month | POS days | Match after fix | Match before fix | Days with no D-file stored |
|---|---|---:|---:|---:|---|
| BISTRO7 | Aug | 31 | 28 | 0 | 24, 29, 30 |
| DAMANSARA (D.U) | Aug | 31 | 29 | 0 | 3, 9 |
| JAKEL | Aug | 31 | 31 | 0 | — |
| KLANG | Aug | 31 | 29 | 0 | 1, 27 |
| SBESI | Aug | 31 | 31 | 0 | — |
| SEK14 | Aug | 31 | 24 | 0 | 9–15 |
| SEK15 | Aug | 31 | 31 | 0 | — |
| SEK20 | Aug | 31 | 31 | 0 | — |
| SEK6 | Aug | 31 | 31 | 0 | — |
| VISTA | Aug | 31 | 31 | 0 | — |
| BISTRO7 | Sep | 30 | 24 | 0 | 1, 3, 12, 13, 23, 30 |
| DAMANSARA (D.U) | Sep | 30 | 28 | 0 | 22, 30 |
| JAKEL | Sep | 29 | 27 | 0 | 20, 30 |
| KLANG | Sep | 30 | 30 | 0 | — |
| SBESI | Sep | 30 | 22 | 0 | 2–6, 28–30 |
| SEK15 | Sep | 30 | 30 | 0 | — |
| SEK20 | Sep | 30 | 23 | 0 | 14, 21, 24–28 |
| SEK6 | Sep | 30 | 30 | 0 | — |
| VISTA | Sep | 30 | 27 | 0 | 21, 24, 30 |

* **Every day that is stored matches the POS report to the sen (0 differences).**
  Under the old dates no day matched (each sat one day early).
* Where no day is missing, our month total now equals the POS report exactly
  (e.g. SEK15 Sep RM 115,540.80; before the fix our Sep total was RM 116,033.60).
* No SEK14 September monthly report was emailed.
* The POS's own "TOTAL SALES" line does not always equal the sum of its daily
  lines (BISTRO7 Aug: 396,369.71 vs 374,845.75); the check uses the daily lines.

## Reports affected by the re-dating

| Report | Reads | Effect |
|---|---|---|
| Daily sales / digest, item sales, key stock, overbuy | D/S-files by business date | Now correct; each day's sales moved +1 day. |
| Monthly close (`/monthly_kg`, sales from S-files) | `sales_daily.shift_business_date` | Aug RM 2,125,053.58 → 2,136,676.08; Sep 1,882,754.05 → 1,876,465.75 (only the month-edge days move). |
| Food cost % (`purchase_reconciliation`) | stored per outlet/day at reconcile time | **Stale**: Aug 296/308, Sep 246/298, Oct 29/110 rows hold another day's sales and payouts. Rebuild with `/reconcile_date 2026-05-25 2026-10-11`. |
| Wastage (10:30 staff question) | staff answers, dated by the question | Not affected. |
| Kitchen Guna vs POS | D-file by business date | Fixed; 8–10 Oct re-post pending approval. |

## 2. What the 09:00 run on 11 Oct posted (business day "9 Oct", old code)

The old code had re-dated data: the D-file it found for "9 Oct" was 10 Oct's
(shifts 2098+2099 SEK-14 / 2659+2660 D.U). It did not cover the "9 Oct" day
S-file, so the bot fell back to summing two S-files from **different days**:

* **SEK-14**: S-file shift **2096** (day, 9 Oct) + **2099** (night, 10 Oct).
  Tandoori 8 = 1 + 7; Bawang 63; Ayam Goreng 159.
* **D.U**: S-file shift **2657** (day, 9 Oct) + **2660** (night, 10 Oct).
  Ayam Goreng 96 = 68 + 28.

The correct 9 Oct POS is D-file 2096+2097 (SEK-14) and 2657+2658 (D.U).
