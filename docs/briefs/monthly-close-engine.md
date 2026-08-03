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

## Block boundaries need BOTH bounds

The report prints the column header `DESCRIPTION   AMOUNT` **twice** — over the
payouts block and over the supplier block — so a header alone cannot say *which*
block you are in. The closing total can, so it names the block.

But a closing total bounds only the **END**. With no start bound the payouts
block swallowed everything above it: the report's own summary lines
(`TOTAL SALES`, `NET AMOUNT`, `TOTAL QR PAY`) carry **no colon** and end in an
amount, so they read as payout rows. On the real July file that gave
**65 rows / RM524,145.60** in place of **56 rows / RM45,121.10**.

So both bounds are used:

```
START: ^\s*(CHEQUENO\s+)?DESCRIPTION\s+AMOUNT\s*$
END:   ^\s*TOTAL (PAYOUTS|CHEQUE OUT|PAY SALARY|STAFF MEALS)\s*:\s*([\d,]*\.\d\d)$
```

A block opens at its column header and is closed — and named — by its closing
total. **Rows outside an open block are ignored.** Inside one, a row must not
start with `TOTAL ` and must end in a 2-decimal amount (without that, the
report's own header line `MONTHLY REPORT-Damansara ON Jul 2026` parses as a
RM2,026.00 payout).

The colon-less summary lines still have to be *read* — they are where the report
states TOTAL SALES — so the header-totals parser picks up `LABEL   AMOUNT` rows
too, keyed on the known label map. Leaving the block and being read as a total
are two different things, and the fix has to do both.

`TOTAL STAFF MEALS` has no `DESCRIPTION` header above it, so that block never
opens. At `.00` that is correct; if it were ever non-zero the guard below turns
it into a rejection rather than a silent omission.

**Neither PAIKKASU nor PINJAM is a section.**

* Paikkasu is an inline tag on rows of both printed forms:
  `NON PAY TO CAMELLIA TEA _PAIKKASU_` and `PAY TO BABAS MASALA _PAIKKASU_`.
  Classification is on the tag, not the prefix, and the tag is stripped before
  supplier-master matching — left in place it becomes part of the payee and
  every paikkasu row falls out of COGS into the unmatched bucket.
  Paikkasu is **split out of** the till COGS rows, never added on top: there is
  no paikkasu total, so adding one would double every tagged row.
* Pinjam is `ADVANCE TO <name>` rows **inside** the payouts block. The parser
  separates them so nothing is counted in both.

## The silent-zero guard

The dangerous failure here is not a crash. If a block's rows go unrecognised its
sum is zero, and zero slots into the P&L without complaint — COGS simply comes
out light and every downstream number is quietly wrong. The parse is therefore
reconciled against the report's own printed totals and **raises**
`MonthlyReportIntegrityError` on:

1. a block with **zero rows but a non-zero closing total**;
2. supplier block sum ≠ `TOTAL SALARY (-)` in the summary;
3. Σ `ADVANCE TO *` ≠ `PAYOUT PINJAM`;
4. any block whose rows sum ≠ its own closing total — a half-parsed block is as
   silent as an unparsed one, and check 4 is what catches the 65-row bug.

`monthly_report_ingest` stores **nothing** for a failing month, and returns a
ready-to-post ops message naming the block and **both** numbers:

```
🛑 Monthly report REJECTED — it does not reconcile with its own totals

Outlet: D.U   Period: 2026-07

• salary_block [total_salary_control]
    parsed  RM0.00
    printed RM23,954.30
    delta   RM-23,954.30

NOTHING was stored for this month. …
```

`/monthly_ingest_now` posts it to the ops group. A control the report did not
print is reported by `missing_controls()` as *unverified* — an unrun check is
not a pass.

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
space. The parser is verified against the real shift file: its 143 itemwise rows
sum to the printed `TOTAL :3050.40`.

That file is a **shift** report, so it closes its money blocks with
`TOTAL PURCHASE` / `TOTAL PINJAM` — not the monthly anchors. The monthly parser
correctly claims nothing from it, and the money-block behaviour is exercised
against `tests/fixtures/monthly_synthetic/` instead (see that README: synthetic
structure, real layout, amounts that reconcile).

## P&L

```
Sales               = TOTAL SALES
COGS                = till purchases + paikkasu + supplier bank block
                      (paikkasu is split OUT of the till rows, not added on top)
Gross profit        = Sales − COGS
Till wages          = PAY [SALARY] + PAY [LP]
Other till expense  = gas, hardware, bills, pest, medical, lalamove,
                      shopee, donation, customer refunds
Outlet contribution = Gross profit − till wages − other till
```

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

Theoretical usage comes from `monthly_itemwise` through the owner-locked v12
rule set in `wastage_rules_v12.py`. Until this was encoded, only KAMBING and
DAGING had portions and everything else — including ayam, roughly a third of
food cost — came out NOT MODELLED.

**Portions** (owner-locked): isi ayam 50 g · kambing 180 g bone-in · daging 60 g
· tulang 50 g · ikan tenggirri 180 g · beras biasa 120 g raw · beras basmati
150 g raw · serbuk teh/kopi 5 g per cup · minyak masak 80 ml per fried dish ·
susu 1 tin per 9 drinks.

**Chicken.** Bawang and rempah are the same cut and share one bucket. Tandoori,
hati and default are separate. `DOUBLE`/`DBL` = 2 pcs; rendang, kurma and kari
are one piece cut into three, so ⅓ each — and the two compose (a doubled
rendang is ⅔).

**Isi ayam.** *Every* Thai-keyword dish takes fillet, including the ones with no
protein in the name (Maggi Sup, Nasi Goreng Pattaya, Kueyteow Kungfu, Maggi
Goreng Basa). Excluded: dishes named `sayur` or `kosong`, and dishes whose main
protein is daging, kambing, ikan, **sotong or udang** — a named seafood is a
main protein, so "Sotong Goreng" is a sotong dish, not a chicken one.

**Composites override that.** `seafood`, `campur` and `special` mark a dish built
from several proteins, so a named seafood does not displace the chicken:
"Tomyam Seafood" and "Mee Goreng Seafood" draw fillet **and** udang **and**
sotong, and "Nasi Goreng Sotong Campur" keeps its fillet. The markers are
stripped before the main-protein test — the same treatment `ikan masin` and
`ikan bilis` get, and for the same reason: they name a protein that is not
*this* dish's single main one. Without the ikan strip, every Thai *ikan masin*
dish reads as a fish dish and silently loses its fillet.

**Thai/Mamak split** is about sourcing, not size: daging is 60 g either way,
from MYSOOR (fresh, Mamak/unspecified) or MD HANI (frozen, Thai). Ayam is fillet
on the Thai line and whole-cut on the Mamak line.

**Eggs.** A goreng is one egg; a named egg dish (Telur Mata/Dadar/Bistik, Roti
Telur, Tosai Telur) adds one, so a goreng plus a named egg dish is two; `double`
on an egg dish makes it two. Deep-fried dishes (Ayam Goreng, Ikan Goreng, Kailan
Goreng) carry "goreng" but use none. There is no generic "Nasi Goreng Telur", so
a bare `telur` in a name adds nothing.

**Drinks.** Condensed milk (susu pekat) by default; `C` variants take evaporated
(susu cair); `O` variants take neither milk nor sugar. 1 tin = 9 drinks (the
owner's 8–10 band, midpoint locked); 1 carton = 24 tins is a purchase-side
conversion and lives in `uom_conversion`, not here.

**Oil** in every goreng / fried / tandoori / roti dish, minus capati, naan and
roti bakar.

```
variance % = (purchased − theoretical) / theoretical
> +15%   HIGH WASTAGE
−5%…+15% healthy
< −5%    OVER-USED
```

Rows are keyed `(canonical_item, unit)`, not by item alone — which is what makes
ayam comparable at all. Whole cuts are bought by the EKOR and consumed in pieces;
fillet is bought by the KG and consumed in grams; liver is neither. Collapsing
them would force a MIXED verdict on the largest ingredient in the report.
MYSOOR and MD HANI beef *do* share a bucket, because an invoice cannot tell them
apart — the split is kept as detail.

**Four distinct reasons for no percentage**, kept separate because each calls
for a different fix:

| Verdict | Meaning | Fix |
| --- | --- | --- |
| `NO PORTION RULE` | udang, sotong, hati ayam — demand shown in **dishes** | owner supplies a portion |
| `NO PURCHASE CATEGORY` | modelled, but canonicalization v2 has no category so purchases can never match | add the category to `data/canonical_items_v2.json` |
| `UNRELIABLE` | purchases exist but some are unconvertible, or in the wrong unit | fix `uom_conversion` / the flagged rows |
| `NOT MODELLED` | purchases exist for something no rule mentions | add a rule |

Nothing is estimated to close any of those gaps.

### Widening canonicalization v2

`telur`, `beras`, `minyak_masak`, `susu` and `tulang` had no category, so those
ingredients could never be compared however good the portion was. They are now
added — **additively**. `data/canonical_items_v2.json` is shared with
`price_aggregation` (which writes `item_prices.canonical_item`) and
`item_resolver`, so a category that quietly starts claiming strings another one
owned would re-label historical spend with no error anywhere.

`tests/test_canonical_v2_additive.py` is the gate. A 361-string baseline was
captured from the data as it stood *before* the addition — every declared
variation, every noise pattern, every item name in the real POS fixture, and
realistic invoice lines — and the hard assertion is that **none of the 205
pre-existing variations moves**. It doesn't: 34 categories → 39, zero drift.

Two collisions were live and are pinned by tests: `TELUR IKAN` is fish roe and
stays with `ikan` (the longer phrase beats bare `TELUR`), and
`MUTTON LEG BONE IN C` stays with `kambing` — which is why `tulang` deliberately
does *not* declare a bare `BONE`.

**`TEPUNG` was not added.** `tepung_roti` already declares `TEPUNG` and `FLOUR`;
a second `tepung` category would be a merge of an existing one, not an addition.

**One accepted change outside the gate**, recorded rather than buried: the POS
dish names `Roti Telur` and `Roti Telur Bawang` now resolve to `telur` instead
of `roti`, because longest-match prefers `TELUR` (5) over `ROTI` (4). Neither is
a declared variation and neither is a shape that appears on a supplier invoice —
canonicalization is applied to invoice lines, not menu names. If they should
stay `roti`, the fix is to declare them under `roti`, which means touching one
of the original 34.

Over the real 143-item Damansara itemwise section the rules now produce minyak
masak, beras, tea/kopi powder, ikan, ayam (both fillet **and** whole-cut),
kambing, daging, tulang, telur, susu and sotong — where previously only kambing
and daging resolved, and `NO PURCHASE CATEGORY` is now empty.

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
