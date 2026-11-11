# Stok Dapur — proposed Phase 1 item list (for approval, NOT seeded yet)

Source: `receipts.items` for the last 3 months, all outlets (pulled 11 Nov 2026).
`[n/o]` = receipt lines / outlets that bought it. "Canonical" = the
`data/canonical_items_v2.json` key the line normalises to ("new" = no alias
yet; added as a match keyword on the stok item).

The unit is the unit the kitchen **counts in** at night. Meat follows the
supplier bill (kg) so the Terima pre-fill can be confirmed as-is. Packed goods
are counted by the pack the supplier sells (beg / karung / pek / tin).

## 1. Ayam — BESTARI FARM (M) SDN BHD (exact merchant match)

| # | Item (BM) | Unit | Receipt lines matched | [n/o] | Canonical |
|---|-----------|------|-----------------------|-------|-----------|
| 1 | Peha Ayam (Whole Leg) | kg | WHOLE LEG, WHITE LEG, 40 WHOLE LEG, WHOLE LEG (40 EKOR), WHOLE LEG 7 4 (BAG) | 150+/6 | ayam_leg |
| 2 | Ayam Super (Superch) | kg | SUPERCH, SUPER CH, SUPERCH 7 x (BAG) | 135+/5 | new |
| 3 | Ayam Potong 8 (P8) | kg | P8, SUPERCH P8 | 48/4 | new |
| 4 | Ayam Tandoori | kg | TANDOORI | 59/4 | new |
| 5 | Ayam 1/2 (4 kg) | kg | 1/2 AYAM 4 KG, AYAM 4 KG (+ OCR "LAI/BI/1I AYAM 4 KG") | 35/2 | ayam |
| 6 | Hati Ayam | kg | HATI 1 KG | 29/1 | new |

Only 6 cuts are actually bought from Bestari Farm. Isi ayam comes from AYAM
BERLIAN, which is out of scope.

## 2. Daging — fresh BESTARI FARM + frozen MD HANEE FROZEN AND SEAFOODS

| # | Item | Unit | Receipt lines | [n/o] | Canonical |
|---|------|------|---------------|-------|-----------|
| 1 | Daging Segar (Beef 3kg) | kg | BEEF 3 KG / 6 KG / 9 KG, BEEF | 190+/5 | daging |
| 2 | Daging Cincang | kg | BEEF CINCANG | 4/– | daging |
| 3 | Daging Beku Silverside | kg | INDIAN SILVERSIDE 44 T/C | 32/3 | new |
| 4 | Daging Beku Topside | kg | INDIAN TOPSIDE (NO.41) T/C | 14/3 | daging |
| 5 | Tulang Leher Lembu | kg | BEEF NECK BONE (TULANG LEHER/LEBAR) | 10/3 | daging |
| 6 | Ekor Lembu | kg | BEEF TAILS -EKOR | 6/2 | daging |

⚠️ "MYSOOR fresh" does not exist as a merchant. On the receipts, **MYSURE /
MYSORE** is a **mutton** line from Bestari Farm ("MUTTON MYSURE 5 KG"), so I've
put it under Kambing. Tell me if you meant a beef product.

## 3. Kambing — BESTARI FARM (fresh) + MD HANEE (frozen)

| # | Item | Unit | Receipt lines | [n/o] | Canonical |
|---|------|------|---------------|-------|-----------|
| 1 | Kambing Mysore (segar) | kg | MUTTON MYSURE/MYSORE/MASURE/MYURE 5 KG / 10 KG | 39/4 | kambing |
| 2 | Kambing Muscle (segar) | kg | MUTTON MUSCLE 5 KG | 13/4 | kambing |
| 3 | Kambing Kaki Bertulang (beku) | kg | MUTTON LEG BONE IN C | 29/4 | kambing |
| 4 | Perut Kambing | kg | MUTTON TRIPE (PERUT) | 14/5 | kambing |

Lamb shank and marrow bones (3 lines each) are left out.

## 4. Beras — JASMINE FOOD CORPORATION + BESTARI WHOLESALE

| # | Item | Unit | Receipt lines | [n/o] | Canonical |
|---|------|------|---------------|-------|-----------|
| 1 | Beras Rebus Saga 10kg | beg | BERAS REBUS SAGA 10KG | 21/8 | beras |
| 2 | Beras Jasmine Super Blue 10kg | beg | JASMINE SUPER BLUE (IMPORT) 10KG, BERAS IMPORT JASMINE 10KG | 15/5 | new |
| 3 | Beras Basmati King 5kg | beg | BASMATI KING JASMINE 5KG, BASMATHI KING (STEAMED) 5KG | 14/4 | beras |
| 4 | Beras Idly 25kg | beg | BERAS IDLY 25/26KG | 7/3 | beras |

❓ Bestari Wholesale also sells **Minyak Masak 4x5kg** (29), **Dhall
Australia** (23), **Ulunthu Dosai 15kg** (15) and **Urad Gota 10kg** (6). Put
these under Beras, under Barang Balaji, or leave them out?

## 5. Barang Balaji — BALAJI ENTERPRISE SDN BHD (dry goods only)

Left out: drinks (Coca-Cola, Sprite, 100Plus, lychee tin), teh/kopi/Milo/Nescafe,
sardin, cleaning (sponge, sabun).

| # | Item | Unit | Receipt line | [n/o] | Canonical |
|---|------|------|--------------|-------|-----------|
| 1 | Gula Pasir 50kg | karung | GULA PASIR 50KG | 16/8 | gula |
| 2 | Tepung Gandum 25kg | karung | TEPUNG GANDUM GUNI 25KG | 12/6 | tepung_roti |
| 3 | Tepung Jagung 25kg | karung | TEPUNG JAGUNG 25KG | 17/8 | tepung_roti |
| 4 | Tepung Atta 5kg | pek | PILLSBURY ATTA 5KG | 9/4 | new |
| 5 | Asam Jawa 6kg | pek | ASAM JAWA 6KG | 17/9 | asam_jawa |
| 6 | Garam Halus | pek | GARAM HALUS | 15/6 | garam |
| 7 | Ajinomoto 1kg | pek | AJI NO MOTO 1KG | 14/8 | new |
| 8 | Belacan 1kg | pek | BELACAN BLOCK (1KG) | 14/7 | new |
| 9 | Barli | pek | BARLI | 20/9 | new |
| 10 | Ghee Crispo 800g | tin | CRISPO GHEE 800GM | 14/7 | ghee |
| 11 | Pes Tomyam 3kg | tin | THAI TOMYAM 3KG TIN | 14/6 | new |
| 12 | Stok Ayam Knorr 1kg | pek | KNORR STOCK AYAM 1KG | 12/7 | (ayam — keyword clash, matched by full name) |
| 13 | Bihun Biho 3kg | pek | BIHUN CAP BIHO 3KG | 12/8 | mee |
| 14 | Puri Tomato 430g | tin | DUCHEF/DUCHES TOMATO PUREE (430GM) | 17/6 | sayur |
| 15 | Kicap Pekat 3kg | botol | KICAP PEKAT CAP GAJAH 3KG | 9/4 | kicap |

## 6. Masala — BABA(S) PRODUCTS (M) SDN BHD + RK MUBARAKA powders

| # | Item | Unit | Receipt lines | [n/o] | Canonical |
|---|------|------|---------------|-------|-----------|
| 1 | Serbuk Kari Ikan 1kg | pek | BABAS FISH CURRY POWDER 1KG; FISH CURRY POWDER | 56/8 | spices_babas |
| 2 | Serbuk Kunyit 1kg | pek | BABAS TURMERIC POWDER 1KG; TURMERIC POWDER | 63/8 | rempah |
| 3 | Serbuk Cili 1kg | pek | BABAS CHILLI POWDER 1KG/3KG; CHILLY POWDER | 61/8 | spices_babas |
| 4 | Kurma Mix 1kg | pek | BABAS KURMA MIX 1KG; KURMA POWDER | 43/5 | spices_babas |
| 5 | Serbuk Kari Daging 1kg | pek | BABAS MEAT CURRY POWDER 1KG/3KG | 31/6 | spices_babas |
| 6 | Serbuk Kari Ayam | pek | CHICKEN CURRY POWDER | 18/4 | rempah |
| 7 | Serbuk Ketumbar | pek | BABAS CORIANDER POWDER 1KG/250G; CORIANDER POWDER | 34/5 | rempah |
| 8 | Serbuk Jintan Putih 250g | pek | BABAS CUMIN POWDER 250G; CUMIN SEED POWDER | 44/5 | rempah |
| 9 | Serbuk Jintan Manis 250g | pek | BABAS FENNEL POWDER 250G; FENNEL SEED POWDER | 34/5 | new |
| 10 | Sambar Mix | pek | BABAS SAMBAR MIX 250G; SAMBAR POWDER | 28/5 | new |
| 11 | Garam Masala | pek | BABAS GARAM MASALA MIX 250G/70G | 13/3 | rempah |
| 12 | Serbuk Lada Putih | pek | BABAS WHITE PEPPER POWDER 250G; WHITE PEPPER POWDER | 18/4 | rempah |
| 13 | Serbuk Lada Hitam | pek | BLACK PEPPER POWDER | 10/4 | rempah |
| 14 | Serbuk Kas Kas | pek | BABAS KAS KAS MIX 1KG; POPPY SEED POWDER | 18/4 | new |
| 15 | Rava Thosai Mix 450g | pek | BABAS RAVA THOSAI MIX 450G | 35/8 | new |

No SAIDA lines turned up in the last 3 months (only 1 receipt).

## 7. Rempah (whole spices) — RK MUBARAKA SDN BHD (+ Balaji cili kering)

| # | Item | Unit | Receipt lines | [n/o] | Canonical |
|---|------|------|---------------|-------|-----------|
| 1 | Halba | kg | FENUGREEK / HALBA | 21/5 | rempah |
| 2 | Bunga Lawang | kg | STAR ANISE / BUNGA LAWANG | 17/4 | rempah |
| 3 | Biji Sawi | kg | MUSTARD SEED / BIJI SAWI | 22/5 | rempah |
| 4 | Cengkih | kg | CLOVES / CENGKEH | 19/4 | rempah |
| 5 | Buah Pelaga | kg | CARDAMOM / KAPULAGA | 21/4 | rempah |
| 6 | Kayu Manis | kg | CINNAMON / KAYU MANIS | 15/3 | rempah |
| 7 | Jintan Manis (biji) | kg | B.J. MANY / B.J.M (OCR of "biji jintan manis") | 13/3 | rempah |
| 8 | Jintan Putih (biji) | kg | B. J. PUNN / B. J. PUTIH | 5/2 | rempah |
| 9 | Lada Hitam (biji) | kg | B. L. HTRAM | 2/2 | rempah |
| 10 | Cili Kering | kg | CILI KERING INDIA 4.5KG (Balaji) | 4/2 | cili |

## 8. Sayur — ⚠️ no supplier on the OCR'd receipts

No merchant called SAYUR exists: 1 receipt in 3 months. Vegetables are not
being uploaded as bills (probably bought at the market for cash). So Sayur
gets **no Terima pre-fill**: staff key it by hand. Proposed common list (from
`canonical_items_v2` sayur/bawang/cili):

Bawang Besar, Bawang Merah, Bawang Putih, Halia, Tomato, Kentang, Kobis,
Timun, Lobak Merah, Cili Hijau, Cili Merah, Cili Boh 2.5kg (GEDA, 50+ lines),
Serai, Daun Ketumbar, Limau Nipis. All in **kg**, except Cili Boh in **pek**.

❓ Who supplies sayur? Give me the name and I'll match its receipts.

---
**Totals:** Ayam 6 · Daging 6 · Kambing 4 · Beras 4 · Balaji 15 · Masala 15
· Rempah 10 · Sayur 15 = **75 items**.

**Please confirm or correct:**
1. The list above: strike out or add items.
2. Where to put Minyak Masak / Dhall / Ulunthu / Urad.
3. MYSOOR → I put it under Kambing.
4. The Sayur supplier.
5. Units: meat in kg (matches the bill) vs pieces/bags. If you want bags,
   tell me kg per bag (e.g. Whole Leg 1 beg ≈ 40 kg?).
