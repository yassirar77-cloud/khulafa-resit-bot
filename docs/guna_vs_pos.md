# 📊 Ringkasan Guna vs POS — how it is computed

**Guna** = `cooked_qty − left_qty` from `kitchen_daily_usage`. `cooked_qty` is the
18:00 *Rekod Masak* form plus any 00:00 night top-up (added on); `left_qty` is the
02:00 *Rekod Baki* form. Both are typed on the inline numpad (`kitchen_usage.py`).

## Status (thresholds in `config/kitchen_compare.py`)
diff = guna − POS, pct = diff / POS
| Case | Icon |
|---|---|
| guna < 0 | ❌ Data salah – sila semak (never a status) |
| POS = 0 and guna > 0 | 🔴 |
| guna = 0 and POS > 0 | ⚠️ |
| pct > +15 % and diff ≥ floor | 🔴 |
| pct < −15 % and \|diff\| ≥ floor | ⚠️ |
| otherwise | ✅ |
Floor: pcs 2, kg 0.3.

## Key-in checks
* LEFT form: leftover above cooked + yesterday's leftover is refused (BM + Tamil), on ✓ and on Hantar.
* kg field ≥ 50: "Maksud 500 g = 0.5 kg?" with ✅ grams / keep kg / retype.

## Which POS window
The POS stamps morning closes (overnight S-file, D-file) with the **business date**,
not the calendar date; `pos_dates.py` uses the email arrival time to tell the two
apart. Kitchen business day D (cook D 18:00 → leftover D+1 ~02:00) is compared
with POS day D = day shift closing D 19:00 + overnight closing D+1 07:00. Every
summary ends with both windows, e.g.
`Dapur: 8/10 18:14 → 9/10 02:17 | POS: shift 2094+2095 (tutup 8/10 19:00, 9/10 07:00)`.

Itemwise source: the D-file(s) for that business day; only when no usable D-file
exists, the day + overnight S-files (which must be consecutive POS shifts). Never
both, never a D-file that lists another day's shifts.

## POS name → lauk button
Names are normalised first (`pos_names.py`: case, `_`/`+`, spaces, typos such as
"Awam"→"Ayam", "Sepuruh"→"Separuh"). Then (`ITEM_POS_KEYWORDS`):
* **Ayam Goreng** — "ayam goreng" + plain nasi-kandar plates (Nasi Ayam, Nasi Separuh Ayam, Briyani Ayam, Nasi Lemak Ayam…) with no other style word.
* **Ayam Bawang / Kicap / Madu / Rempah** — "ayam" + that style word.
* **Ayam Tandoori** — "ayam" + "tandoori"/"tandori" (staff meals excluded).
* **Ayam Kari/Rendang** — "ayam" + rendang / kari / curry / kurma / merah (THAI FOOD category excluded).
* **Ayam 65** — any name with "65" as a word: Ayam 65, Chicken 65, Nasiputih+65, Ayam Masala/65.
* **Ikan Goreng** — "ikan goreng". **Ikan Kari** — ikan + kari/curry, or plain Nasi Ikan / Nasi Putih Ikan / Nasi Separuh Ikan.
* **Kambing / Daging** — every dish with the word, POS portions × 180 g / 60 g. Not counted: Susu Kambing, Kambing Perut / Purut Kambing (tripe). Lamb Shank / Lamb Chop are separate (not counted).
* **Telur Ikan** — keyed in **pcs**; every "telur ikan" POS item counts as 1 pc whatever the size.
* Excluded everywhere: THAI FOOD category, staff meals, curry-gravy-only items (`GRAVY_ONLY_NAMES`: Kari Bungkus, Kua Kari, Nasi Putih (Kari), and SEK-15 "Kari Ayam/Kambing" — RM2.59, under the RM4 meat threshold); for ayam also isi-ayam noodle/rice dishes.
* A button the kitchen had no row for that day shows "➖ … tiada rekod dapur" with the POS count; Telur Ikan rows keyed before the switch (kg) show "rekod lama dalam kg".

Full 30-day audit: `docs/audits/pos_item_names_30d.md` (`scripts/audit_pos_item_names.py`).
