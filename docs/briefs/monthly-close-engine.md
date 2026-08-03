# Monthly close engine

Ingests the POS monthly report, corrects two mislabelled blocks, and produces a
P&L, a cash reconciliation, a wastage variance and a menu-hygiene sheet per
outlet per month.

## The two corrections that make this necessary

The POS report is not a P&L, and two of its headings are wrong in ways that move
real money.

**(a) "TOTAL PAY SALARY" is not salary.** The block lists suppliers paid by bank
transfer — Balaji, Jasmine Rice, Juta Ria Telur, Mewah Dairies, Reza Plastic.
Booked as printed it understates COGS and overstates wages, flattering gross
margin and hiding food cost. Every row is matched against the supplier master
(`supplier_master.py`, now shared with the receipt classifier so the two cannot
disagree about what a supplier is). A supplier goes to COGS; a staff member from
`monthly_staff_advance` goes to wages; **anything matching neither is flagged and
its money goes into neither bucket.** The heading is known to lie, so an
unrecognised name under it is exactly where a guess is most likely to be wrong.

A short ambiguous token is not enough on its own. `99` is in the supplier master
(99 Speedmart) but also matches the staff nickname "MAT 99", so a bidirectional
substring hit alone flags rather than books.

**(b) "PAYOUT PINJAM" is not an expense.** `ADVANCE TO <staff>` is money the
business still owns. It is added back, and the detail rows are asserted against
the printed PINJAM total — a gap is reported with both numbers rather than
silently preferring one.

Neither correction is stored. The tables hold what the POS **printed**, verbatim;
`monthly_close.py` derives the categories at read time, so changing a rule
re-reads the source instead of needing a backfill.

## Parsing hazards, all real

The monthly report comes from the same generator as the S-/D- shift files, and
`tests/fixtures/sales/S-Damansara 25May2026.TXT` contains every hazard:

| Hazard | Real example |
| --- | --- |
| zeros print without a leading digit | `Air Panas    2    .60` |
| leading space in the item name | `" Nasi Goreng Daging"` |
| underscore instead of space | `" Tambah_nasi"` |
| double space inside the name | `"Barli  Panas"` |
| ragged description/amount columns | `14419  PAY [SALARY] TO KALEEL   120.00` |

Rows are therefore split from the **right** — trailing numeric columns are peeled
off and everything to their left is the name, with leading whitespace preserved.
`sales_parser._columns` is deliberately **not** reused: it splits on runs of 2+
spaces and would turn `"Barli  Panas"` into two columns, inventing an item.

Leading spaces are kept all the way into `monthly_itemwise.item_name` because the
menu-hygiene sheet exists to find the SKU that differs from its twin only by that
space. The parser is verified against the real file: its 143 itemwise rows sum to
the printed `TOTAL :3050.40`, and `TOTAL PURCHASE 974 + TOTAL PINJAM 50` equals
the printed `TOTAL PAYOUT 1024`.

## P&L

```
Sales               = TOTAL SALES
COGS                = till purchases + paikkasu + supplier bank block
Gross profit        = Sales − COGS
Till wages          = PAY [SALARY] + PAY [LP]
Other till expense  = gas, hardware, bills, pest, medical, lalamove,
                      shopee, donation, customer refunds
Outlet contribution = Gross profit − till wages − other till
```

Paikkasu already present in the till rows is not added twice.

Rent, TNB, Air Selangor, Unifi, central payroll and KWSP/PERKESO come from
`outlet_config` — they are paid from the bank and appear in no POS report. **When
that table has no row for the period, the bottom line is labelled
`CONTRIBUTION (pre-rent/utilities/central payroll)` and the words "net profit"
appear nowhere**, in the sheet or the digest. An estimated rent would make a
loss-making outlet look profitable, which is the one error that matters most.

## Cash reconciliation

```
cash_sales       = TOTAL SALES − TOTAL BANK SALES − TOTAL FP SALES
expected_bank_in = cash_sales − TOTAL PAYOUTS
```

`expected_bank_in` is the figure to match against the bank statement. Negative
values are flagged, as is a negative `cash_sales` (which means a channel total is
wrong). Channels the report did not print are treated as zero **and named** in
the flags, so a missing channel is never mistaken for a real zero.

## Wastage

Theoretical usage comes from `monthly_itemwise` through the locked v12 framework
in `kitchen_usage` — the same Thai/staff exclusions, ayam cut rules and portion
sizes the daily comparison uses, so the two can never disagree about the same
kitchen.

```
variance % = (purchased − theoretical) / theoretical
> +15%   HIGH WASTAGE
−5%…+15% healthy
< −5%    OVER-USED
```

**No percentage is printed** when it would be computed over bad data:

* any flagged or unconvertible purchase row → `UNRELIABLE`, with the flagged RM
  shown;
* purchases in a unit the theoretical figure isn't in, with no locked rule to
  convert → `UNRELIABLE`;
* an ingredient with no locked v12 portion rule → `NOT MODELLED`, purchases still
  reported.

v12 locks grams-per-portion for kambing (180 g) and daging (60 g) only. Ayam and
ikan are sold and logged as whole pieces. **Egg and drinks portion rules do not
exist in this repo**, so those ingredients report NOT MODELLED rather than being
converted on an invented factor — the same rule that governs `uom_conversion`.

## Menu hygiene

* **Duplicate SKUs** — names differing only by leading/trailing space, underscore
  or double space. Normalisation folds *only* those; no stemming or fuzzy match,
  because "Teh Ais" and "Teh Ais Special" are different products and a report
  that merged them would be telling the owner to delete a live SKU. Invisible
  differences are rendered `«like this»`.
* **Dead SKUs** — quantity ≤ 5 for the period, summed per name first.
* **Open-price / FOC / jumbo / discount** buttons, with txn count and RM. The one
  place a cashier chooses the number.

## Output

`/monthly_close <outlet> <YYYY-MM>` (reviewers only) posts the digest and attaches
`Khulafa_[OUTLET]_[MONTH]_[YEAR]_Wastage_Report.xlsx` with sheets **P&L**, **Cash
Reconciliation**, **Menu Hygiene**, **Wastage Variance**. An existing workbook is
extended in place — a hand-edited Cost Calculator sheet survives untouched — and
the four owned sheets are rewritten wholesale so a re-run never stacks two months
of rows.

`/monthly_ingest_now` polls the mailbox. The daily poller is reused; only the
subject classification (`'M'`) and the parser differ. Re-ingesting a month
replaces its detail rather than appending.

## Acceptance test

`tests/test_monthly_close_acceptance.py` runs the whole chain over the real
Jul 2026 Damansara file and checks the owner's known-good figures:

```
sales 148,586.80 | COGS 55,882.30 (37.6%) | contribution 84,889.40
cash_sales 69,638.50 | expected_bank_in 24,517.40
pinjam addback 5,378.00 | duplicate SKUs 7 | dead SKUs 131
```

**That file is production data and is not in the repo.** Until it is dropped at
`tests/fixtures/monthly/`, all 18 acceptance checks **skip** — they do not pass.
Each figure is asserted separately so a run names the one that disagrees.

## Files

| File | Role |
| --- | --- |
| `migrations/0039_monthly_close.sql` | 6 monthly tables + `outlet_config`, RLS |
| `monthly_report_parser.py` | Tolerant right-anchored fixed-width parser |
| `monthly_report_ingest.py` | Email → parse → store, replace-on-re-ingest |
| `monthly_close.py` | Reclassifications, P&L, cash reconciliation, digest |
| `monthly_wastage.py` | Theoretical vs actual variance via v12 |
| `menu_hygiene.py` | Duplicate / dead / leakage SKUs |
| `monthly_report_xlsx.py` | Writes the four sheets, preserves the rest |
| `supplier_master.py` | The shared supplier list (extracted from `bot.py`) |
