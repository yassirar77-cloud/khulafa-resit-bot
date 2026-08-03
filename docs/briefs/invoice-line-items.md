# Invoice line items for wastage analysis

**Ships:** `receipt_items`, `uom_conversion`, a second OCR pass, and
`/wastage_purchases <outlet> <YYYY-MM>`.

## The problem

A supplier invoice used to be one `receipts` row (merchant, date, total) plus a
loose `items` jsonb blob. That answers "what did we spend" but not "how many kg
of ayam did Klang buy last month, and at what cost per kg" — the two numbers
the Wastage Report's Cost Calculator sheet is built on. The quantities were
trapped in free text, and every supplier prints its own unit: `KG`, `EKOR`,
`GUNI`, `PAPAN`, `CTN`, `BTL`, `PKT`.

## The pipeline

```
photo -> OCR pass 1 (bot.OCR_PROMPT)      -> receipts row
      -> OCR pass 2 (invoice_ocr)          -> {supplier, invoice_no, lines[], subtotal, total}
         -> receipt_validation.check_subtotal
         -> receipt_items.build_item_rows   -> canonicalise + convert
            -> item_canonicalization_v2     (34 categories / 205 variations)
            -> uom_conversion               (printed unit -> g / ml / pcs)
         -> receipt_items rows
```

Pass 2 is a separate vision call, not extra keys on the main prompt: the two
prompts want opposite things from the model (pass 1 summarises the receipt,
pass 2 must not summarise anything). It runs only for `SUPPLIER_PURCHASE`
receipts that clear the review floor, inside the OCR semaphore, reusing the
already-resized bytes. Set `INVOICE_LINES_ENABLED=false` to turn it off.

`OCR_MAX_CONCURRENCY` now defaults to **1**. The heavy region runs two vision
calls per receipt and production is a 512MB Render box.

## The one rule: never guess a factor

`uom_conversion` is the only place a printed unit becomes a base quantity.
There is no fallback heuristic anywhere in the code. Lookup order, most
specific first:

1. `supplier` + `canonical_item` + `unit_raw`
2. `canonical_item` + `unit_raw` (supplier `NULL`)
3. `unit_raw` (supplier and item `NULL`)
4. no match → `qty_base = NULL`, `needs_review = true`

Only definitional factors are seeded: the KG/G, L/ML families and the count
words (`PCS`, `PC`, `PIECE`, `UNIT`, `BIJI`, `EKOR`). **`GUNI`, `PAPAN`, `CTN`,
`BTL`, `PKT` and `TIN` are deliberately absent** — a sack of beras is 25kg at
one supplier and 10kg at another. Lines using them are stored with their money
and their printed unit intact, flagged for review, and the ops chat gets a
"unit tak dikenali" alert naming the units to add.

Adding a real factor is one insert:

```sql
INSERT INTO public.uom_conversion (supplier, canonical_item, unit_raw, factor, base_unit, note)
VALUES ('BESTARI FARM', 'telur', 'PAPAN', 30, 'pcs', 'counted 2026-08');
```

Re-send the photo afterwards and the upsert on `(receipt_id, line_no)` corrects
the existing rows rather than duplicating them.

## When the invoice doesn't add up

`abs(Σ line_total − subtotal) > RM0.02` means an OCR error somewhere, and which
row is unknowable from the numbers. So:

* every line is marked `needs_review`,
* **no** line gets a `qty_base` — including the lines that look fine,
* the receipt is posted back to the **outlet group that sent it** (the paper is
  in that room; the ops chat can't correct it).

A missing subtotal is *not* a failure — the check simply doesn't run, and the
quantities are written normally. Separately, a line whose `qty × unit_price`
disagrees with its own `line_total` flags itself without condemning the invoice.

## The monthly export

`/wastage_purchases klang 2026-08` (reviewers only) attaches
`Khulafa_KLANG_B_EMAS_AUGUST_2026_Cost_Calculator.csv`:

```
canonical_item,total_qty_base,base_unit,total_cost,avg_cost_per_kg
ayam,20000,g,196,9.8
ayam,6,pcs,132,
```

Grouped by `(canonical_item, base_unit)` — an item bought both by weight and by
the piece is two rows, not one nonsensical sum. `avg_cost_per_kg` is computed
only for gram-based items and left **blank** otherwise, so a spreadsheet AVERAGE
skips it instead of being dragged to zero.

Rows with `needs_review` or a NULL `qty_base` are excluded from the CSV and
reported in the caption ("N line(s) excluded · RM… not counted"). Their money is
real but their quantity is unknown, and counting the cost against an incomplete
quantity would inflate cost-per-kg by exactly the amount nobody would notice.

## Files

| File | Role |
| --- | --- |
| `migrations/0038_receipt_items_uom.sql` | Both tables, indexes, RLS, seed factors |
| `invoice_ocr.py` | Pass-2 prompt + response parsing |
| `uom_conversion.py` | Lookup order, unit folding, conversion |
| `receipt_items.py` | Row building, per-line confidence, upsert |
| `receipt_validation.py` | Subtotal / line arithmetic checks, flag message |
| `wastage_export.py` | Month parsing, aggregation, CSV, summary |
