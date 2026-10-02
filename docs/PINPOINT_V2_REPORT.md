# Pinpoint Target v2 — go-live report (2026-10-02)

Branch `ccr-c6615023-gzuyht`. Mode on Render: **shadow** (`OUTSIDE_PURCHASE_MODE=shadow`).
Nothing reaches a cashier until you set it to `live` yourself.

## 1. Known merchants per outlet (history baseline, last 90 days, ≥ 3 bills)

Seeded by `migrations/0059_pinpoint_v2.sql` into `outlet_known_merchants`
(230 history rows + 180 approved-supplier rows across 10 outlets). A bill from
a merchant NOT on this list for its outlet is a pin target. ⚠️ = looks like a
mini market / grocer — remove with `/buang_merchant <outlet> <name>` so its
bills are pinned again. Bill counts are from `receipts` (purchase types only).

```
Bistro (25)
 81  EVEREST AISVARAM SDN. BHD.
 56  BIG BAZAR WHOLESALE AND RETAIL
 52  MYMOON'S KITCHEN
 44  CATERERS AT TANJUNG
 22  JUTA RIA SUCCESS ENTERPRISE
 19  DIAMOND BALL
 14  KWONG HIN HARDWARE TRADING
 13  BALAJI ENTERPRISE SDN BHD
 13  PETRONAS
 10  S. THAYANI ENTERPRISE
  9  BABA PRODUCTS (M) SDN BHD
  8  FOOK LEONG SEA PRODUCTS SDN BHD
  7  KM SETIA USAHA ENTERPRISE
  7  MD HANEE FROZEN AND SEAFOODS
  7  RK CAMELLIAA (M) SDN. BHD.
  6  REZA HUSSIEN SOLUTION
  6  SWEETTI FREEZEE ENTERPRISE
  5  SWEETTI FREEZE ENTERPRISE
  4  KWONG HIN HARDWARE TRADING SDN BHD
  3  ANTAN MAJU ENTERPRISE
  3  ICE CREAM MALAYSIA
  3  M/S GARDEN
  3  PVS SANTAN MAJU ENTERPRISE
  3  RT BISTO MILK
  3  TRADISI IMPIAN

D.U (2)
  4  PVS SANTAN MAJU ENTERPRISE
  3  JAYA GROCER  ⚠️

Jakel (28)
 80  EVEREST AISVARAM SDN. BHD.
 75  MYMOON'S KITCHEN
 39  PVS SANTAN MAJU ENTERPRISE
 25  INBOIS
 19  99 SPEED MART SDN. BHD.  ⚠️
 17  JUTA RIA SUCCESS ENTERPRISE
 13  HAMEED PLASTICS
 13  KM SETIA USAHA ENTERPRISE
 12  NOVI FROZEN FOODS
 11  FOOK LEONG SEA PRODUCTS SDN BHD
 11  S. THAYANI ENTERPRISE
  9  BABA PRODUCTS (M) SDN BHD
  9  BALAJI ENTERPRISE SDN BHD
  8  DIAMOND BALL
  8  RK CAMELLIAA (M) SDN. BHD.
  7  HARMONY D.I.Y. HARDWARE SDN BHD
  7  INBOIS / TUNAI
  6  ADFIRYN ENTERPRISE
  6  GCH RETAIL (MALAYSIA) SDN BHD        (Giant's company — likely a mini market)
  6  JFM JAYA TRADING
  5  SHREE MAP JAYA SDN. BHD.
  4  INBOIS/TUNAI
  3  AR RIYAADH MINI MARKET  ⚠️
  3  BUGEZZ PEST CONTROL
  3  FULLER TRADING SDN BHD
  3  JASMINE FOOD CORPORATION SDN. BHD.
  3  MEWAH DAIRIES SDN. BHD.
  3  UNICORN

Klang B.Emas (31)
 66  AYAM BERLIAN SDN BHD
 56  GT MART SDN BHD  ⚠️
 46  ALASKA ICE SDN. BHD.
 24  A & 1 FOOD INDUSTRIES SDN. BHD.
 17  DIAMOND BALL
 17  HAMEED PLASTICS
 16  JUTA RIA SUCCESS ENTERPRISE
 14  FOOK LEONG SEA PRODUCTS SDN BHD
 13  A & 1 FOOD INDUSTRIES SDN. BHD. (1454565-M)
 13  RK MUBARAKA SDN BHD
 12  BABA PRODUCTS (M) SDN BHD
 12  BALAJI ENTERPRISE SDN BHD
 12  MD HANEE FROZEN AND SEAFOODS
 12  WIN STAR BAKERY & ENTERPRISE
 11  KM SETIA USAHA ENTERPRISE
 11  NASI LEMAK CIK PYA
  8  UNICORN
  6  FRIZZ STATION SDN BHD
  6  RESTORAN NASI KANDAR HAJI SHARFUDDIN
  6  RK CAMELLIAA (M) SDN. BHD.
  5  GOLDCREST MARKETING SDN BHD
  5  HLS HARDWARE SDN BHD - BPL
  4  HLS HARDWARE SDN BHD
  4  MEWAH DAIRIES SDN. BHD.
  4  PARTHA MART  ⚠️
  3  AFIYA RESOURCES
  3  GEDA TRADING (SA0337500-P)
  3  JASMINE FOOD CORPORATION SDN. BHD.
  3  NASI LEMAK
  3  NASI LEMAK CIK PIA
  3  ONE ROOF GROCER  ⚠️

One Bistro (17)
 52  EVEREST AISVARAM SDN. BHD.
 35  PVS SANTAN MAJU ENTERPRISE
 21  7-ELEVEN MALAYSIA SDN. BHD.  ⚠️
 20  CHECKERS HYPERMARKET SDN BHD  ⚠️
 11  DIAMOND BALL
  8  FOOK LEONG SEA PRODUCTS SDN BHD
  7  RK MUBARAKA SDN BHD
  5  REZA HUSSIEN SOLUTION
  4  GEDA TRADING
  4  JUTA RIA SUCCESS ENTERPRISE
  4  KM SETIA USAHA ENTERPRISE
  4  LEAVE PAY                              (not a shop — remove)
  4  MARUTHU
  3  BALAJI ENTERPRISE SDN BHD
  3  LIAN HUAH COCONUT TRADING
  3  PASARAYA BORONG SNS ALI
  3  USTAD                                  (not a shop — remove)

SBESI (20)
 48  EVEREST AISVARAM SDN. BHD.
 34  DIAMOND BALL
 32  BUKITMAS ENTERPRISE
 20  99 SPEED MART SDN. BHD.  ⚠️
 14  GOLD EGGER ENTERPRISE
 12  FAMILYMART  ⚠️
 11  KM SETIA USAHA ENTERPRISE
 11  TUNAS MANJA SDN BHD
  9  RNS PASARAYA  ⚠️
  8  ABDUL WAHAB KM ENTERPRISE
  8  RK MUBARAKA SDN BHD
  8  S. THAYANI ENTERPRISE
  7  HAMEED PLASTICS
  7  PASARAYA EASA  ⚠️
  6  MR D.I.Y. SDN. BHD.
  5  ASOLUTIONS PLT
  5  BABA PRODUCTS (M) SDN BHD
  4  HK HARDWARE SHOP
  4  KK SUPERMART & SUPERSTORE SDN. BHD.  ⚠️
  3  EVERESTAISVARAM

SEK-20 (26)
 48  EVEREST AISVARAM SDN. BHD.
 35  SUN MAJU ENTERPRISE
 31  PASARAYA BORONG SNS ALI
 15  PASAR SIANG MALAM
 14  HAMEED PLASTICS
 14  SLH SOON LAM HING
 13  DIAMOND BALL
 11  JUTA RIA SUCCESS ENTERPRISE
 10  GARDENIA BAKERIES (KL) SDN BHD
  9  FOOK LEONG SEA PRODUCTS SDN BHD
  6  RK CAMELLIAA (M) SDN. BHD.
  5  BALAJI ENTERPRISE SDN BHD
  5  HARISSA'S CAKE
  5  KM SETIA USAHA ENTERPRISE
  5  MR. D.I.Y. (M) SDN. BHD.
  4  CANAI ENAKI
  4  CNH HARDWARE TRADING
  3  BABAS PRODUCTS (M) SDN BHD
  3  C N H HARDWARE TRADING
  3  DIYA AL DIN ENTERPRISE
  3  GEDA TRADING
  3  LIAN HUAH COCONUT TRADING
  3  MD HANEE FROZEN AND SEAFOODS
  3  S. THAYANI ENTERPRISE
  3  SHREE MAP JAYA SDN. BHD.
  3  TRIPLE A ENTERPRISE

SEK-6 (22)
103  PASAR MINI A M  ⚠️
 69  EVEREST AISVARAM SDN. BHD.
 55  AYAM BERLIAN SDN BHD
 53  MYMOON'S KITCHEN
 35  SYARIKAT SRI ALAM
 23  MD HANEE FROZEN AND SEAFOODS
 22  DIAMOND BALL
 14  NASI KANDAR HAJI SHARFUDDIN
 11  FOOK LEONG SEA PRODUCTS SDN BHD
 11  JUTA RIA SUCCESS ENTERPRISE
  9  BABA PRODUCTS (M) SDN BHD
  9  KM SETIA USAHA ENTERPRISE
  8  BALAJI ENTERPRISE SDN BHD
  7  PETRONAS
  7  S. THAYANI ENTERPRISE
  6  CHECKERS HYPERMARKET SDN BHD  ⚠️
  6  REZA HUSSIEN SOLUTION
  6  RK MUBARAKA SDN BHD
  4  MYMOON'S
  4  ST ROSYAM MART (SHAH ALAM) SDN BHD  ⚠️
  4  UNICORN
  3  KWONG HIN HARDWARE TRADING

Signature (28)
 51  NURUN HOLDINGS SDN BHD
 34  EVEREST AISVARAM SDN. BHD.
 24  MYMOON'S KITCHEN
 19  JUTA RIA SUCCESS ENTERPRISE
 17  CATERERS AT TANJUNG
 16  MD HANEE FROZEN AND SEAFOODS
 16  PVS SANTAN MAJU ENTERPRISE
 15  HAMEED PLASTICS
 11  BALAJI ENTERPRISE SDN BHD
 11  FOOK LEONG SEA PRODUCTS SDN BHD
 11  MELUR JAYA FROZEN FOODS (M) SDN BHD
  8  SWEETTI FREEZEE ENTERPRISE
  6  INBOIS
  5  FM TROPICAL LEGACY
  5  KM SETIA USAHA ENTERPRISE
  4  INBOIS TUNAI
  4  S. THAYANI ENTERPRISE
  4  SWEETI FREEZE & ENTERPRISE
  4  TONYAM
  3  CANAI ENAKI
  3  INBOIS / TUNAI
  3  INBOS / TUNAI
  3  MR D.I.Y. SDN. BHD.
  3  PAS SANTAN MAJU ENTERPRISE
  3  RK CAMELLIAA (M) SDN. BHD.
  3  SWEETTI FREEZE ENTERPRISE
  3  TOM YAM
  3  TONYAM GAJI                            (wages, not a shop — remove)

Vista (31)
 73  EVEREST AISVARAM SDN. BHD.
 50  CATERERS AT TANJUNG
 37  SWEETTI FREEZEE ENTERPRISE
 21  INBOIS
 17  99 SPEED MART SDN. BHD.  ⚠️
 13  MD HANEE FROZEN AND SEAFOODS
 11  JUTA RIA SUCCESS ENTERPRISE
 11  PVS SANTAN MAJU ENTERPRISE
 11  S. THAYANI ENTERPRISE
 10  CATERERS AT TANJUNG (PG-0233310-K)
 10  KM SETIA USAHA ENTERPRISE
 10  LIAN HUAH COCONUT TRADING
 10  RK MUBARAKA SDN BHD
  9  FOOK LEONG SEA PRODUCTS SDN BHD
  6  CHECKERS HYPERMARKET SDN BHD  ⚠️
  6  REZA HUSSIEN SOLUTION
  6  RK CAMELLIAA (M) SDN. BHD.
  5  CATERERS AT TANJUNG (PG-0233310-K) SEKSYEN 9 SHAH ALAM, SELANGOR
  5  DIAMOND BALL
  5  INBOIS / TUNAI
  4  BALAJI ENTERPRISE SDN BHD
  3  ADFIRYN ENTERPRISE
  3  AFIYA RESOURCES
  3  AYAM BERLIAN SDN. BHD.
  3  HARIS
  3  JASMINE FOOD CORPORATION SDN. BHD.
  3  KWONG HIN HARDWARE TRADING SDN BHD
  3  M.I.A.K TRADING SDN. BHD.
  3  REZA HUSSEIN SOLUTION
  3  TUNBOIS / TUNAI
  3  UNICOM
```

Suggested removals (my reading; the decision is yours):
`/buang_merchant SEK6 PASAR MINI A M`, `/buang_merchant JAKEL 99 SPEED MART SDN. BHD.`,
`/buang_merchant SBESI 99 SPEED MART SDN. BHD.`, `/buang_merchant VISTA 99 SPEED MART SDN. BHD.`,
`/buang_merchant SBESI FAMILYMART`, `/buang_merchant SBESI KK SUPERMART & SUPERSTORE SDN. BHD.`,
`/buang_merchant SBESI RNS PASARAYA`, `/buang_merchant SBESI PASARAYA EASA`,
`/buang_merchant SEK15 7-ELEVEN MALAYSIA SDN. BHD.`, `/buang_merchant SEK15 CHECKERS HYPERMARKET SDN BHD`,
`/buang_merchant SEK6 CHECKERS HYPERMARKET SDN BHD`, `/buang_merchant VISTA CHECKERS HYPERMARKET SDN BHD`,
`/buang_merchant KLANG GT MART SDN BHD`, `/buang_merchant KLANG PARTHA MART`, `/buang_merchant KLANG ONE ROOF GROCER`,
`/buang_merchant DAMANSARA JAYA GROCER`, `/buang_merchant JAKEL AR RIYAADH MINI MARKET`,
`/buang_merchant JAKEL GCH RETAIL (MALAYSIA) SDN BHD`, `/buang_merchant SEK6 ST ROSYAM MART (SHAH ALAM) SDN BHD`,
plus the non-shops `LEAVE PAY`, `USTAD` (One Bistro) and `TONYAM GAJI` (Signature).

## 2. Receipt-type coverage (report only, routing unchanged)

The classifier (`receipt_classifier.ReceiptType`) outputs exactly seven types.
Production counts (all time / last 90 days): UNKNOWN 6865 / 3190,
SUPPLIER_PURCHASE 3965 / 1815, STAFF_ADVANCE 1695 / 988, PETTY_CASH 192 / 112,
INTERNAL_TRANSFER 161 / 0, RENT_LICENSE 110 / 75, UTILITY 21 / 0.

| Type | Pinpoint hook | Could hold stock purchases? |
| --- | --- | --- |
| SUPPLIER_PURCHASE | yes | — |
| UNKNOWN | yes | — (most mini-market bills land here) |
| STAFF_ADVANCE | no | No. Keyword match on PAYOUT / PINJAM / ADVANCE / LOAN. |
| UTILITY | no | No (TNB, water, internet). |
| RENT_LICENSE | no | No. |
| INTERNAL_TRANSFER | no | Stock moving between our own outlets, not a purchase. Never emitted live (backfill only). |
| PETTY_CASH | **no — gap** | **Yes.** Keywords are RUNNER / TAMBANG / TOL / PARKING / PETROL / SHELL / **PETRONAS** / CALTEX under RM200, so every "Petronas 14kg – Filled" gas-cylinder bill (merchants PETRONAS, KHULATA, CK'S, blank) is filed as petty cash: 112 such receipts in 90 days, mostly fuel and gas. Gas is on `allowed_outside_items`, so it would never strike, but these bills also never reach the known-merchant or overbuy checks. |

There is no "daily pay", "expense" or "consumables" receipt type. "DAILY PAY" is a
merchant name on the old `KNOWN_SUPPLIERS` list in `bot.py`; its bills classify as
SUPPLIER_PURCHASE or UNKNOWN and are covered.

## 3. Cashier-facing sales figures removed

| Message | Before | Now |
| --- | --- | --- |
| Staff ops 11:00 sales note (`staff_ops._TEXTS sales_*`) | "613 item terjual, biasa sekitar 900" | "jualan kurang/lebih dari biasa untuk hari {weekday}" — no count, no average; the count/usual also dropped from the stored facts |
| Slow-items note (`item_sales_watch.format_manager_slow_items`) | "NAAN: 50 தான் போச்சு — வழக்கமா ~120 (-58%)" | item names only, "sold less than usual" |
| Weekly overbuy watch (`overbuy_watch.format_manager_overbuy`) | "sales RM10,200 (usual RM13,100 — 22% down)" | "sales lower than usual", purchase quantities kept |
| Anomaly check-in (`bot._anomaly_metrics`) | could ask "Sales today 480, usually 620" | sales metric removed; wastage and order metrics (the cashier's own numbers) stay |
| New pinpoint texts | — | the overbuy question says only "Jualan semalam lebih rendah dari biasa"; strike tiers carry purchase quantities only |

Kept, flagged for your decision: the kitchen Used-vs-POS recap shows POS
units sold per dish (a quantity, not RM/average/%); the weekly food-cost
message to DM-registered managers shows a food-cost % (a ratio, gated by
`MANAGER_DELIVERY_ENABLED` and `GROUP_MONEY_REPORTS`). Director-chat texts
keep every number.
