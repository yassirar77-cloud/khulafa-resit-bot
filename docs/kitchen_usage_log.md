# Daily Kitchen Usage Log

Track daily protein usage per outlet and reconcile it against POS sales, all by
tapping (no word-typing). Three entries per business day:

- **18:00** — COOKED (evening batch).
- **00:00** — COOKED night-cook (**optional, additive**): the chef keys only the
  EXTRA amount cooked at night, which is ADDED to the 18:00 cooked_qty (50 then
  +20 → 70). Skip the form entirely if there's no night cook.
- **02:00** — LEFT (balance).

`Total Cooked = evening + night`, and `Used = (evening + night) − Left`. The bot
compares Used with POS dishes sold and flags mismatches.

## Business day

The business day is the **18:00 date**. A COOKED entry at 22 Jun 18:00 and a
LEFT entry at 23 Jun 02:00 both belong to `business_date = 2026-06-22`. Any
local time before noon folds back to the previous calendar day
(`kitchen_usage.business_date_for`, cutoff `BUSINESS_DAY_CUTOFF_HOUR = 12`).

## Items tracked

| code | label | unit |
|------|-------|------|
| `ayam_goreng` | Ayam Goreng | pcs |
| `ayam_bawang` | Ayam Bawang | pcs |
| `ayam_rempah` | Ayam Rempah | pcs *(BISTRO7 only)* |
| `ayam_kicap` | Ayam Kicap | pcs |
| `ayam_madu` | Ayam Madu | pcs |
| `ayam_tandoori` | Ayam Tandoori | pcs |
| `ikan_goreng` | Ikan Goreng | pcs |
| `ikan_kari` | Ikan Kari | pcs |
| `telur_ikan` | Telur Ikan | kg |
| `kambing` | Kambing | kg |
| `daging` | Daging | kg |

`pcs` items are whole numbers; `kg` items allow one decimal. **Ayam Rempah only
appears on the BISTRO7 form** (`items_for_outlet`).

## Key-in UX (tap buttons + numpad)

The bot posts **one** message with **one inline button per item**, each showing
its value — `✓ Ayam Goreng: 50` when filled, `Ayam Goreng: —` when empty — plus
a **📤 Hantar** button at the bottom. The header reads "Tap untuk key-in" (+ a
Tamil line). Staff:

1. **Tap an item button** → an inline **numpad** appears (`1 2 3 / 4 5 6 /
   7 8 9 / ⌫ 0 ✓`, plus `.` for kg items).
2. Tap digits, then **✓** → the value is saved and the button updates to
   `✓ [item]: [value]`.
3. **📤 Hantar** when done. **Untouched items save as 0** (COOKED/LEFT); ≥1 item
   is required to submit. The night form is additive and only writes keyed items.

**Fixing mistakes before Hantar** (no post-submit editing): re-tap any item to
change it — the numpad opens with an **empty buffer** and shows the saved value as
a reference (`Sekarang: 50`), so typing a new number **replaces** it. ⌫ edits
digits (safe on an empty buffer), and **🗑 Kosongkan** resets that item back to
`—` if the wrong item was tapped. Pressing ✓ with nothing typed keeps the existing
value (no accidental clear). Anyone in the group can make these edits. The form
header notes "Tekan balik barang untuk betulkan sebelum Hantar."

**Numpad is instant** (the lag fix). Two things made per-digit taps feel slow and
both are addressed:

- **No message edit per digit.** A `editMessageText` is a 300-800ms Telegram
  round-trip; doing it on every digit was the visible lag. Instead the running
  value is shown in the **callback answer toast** (`query.answer(text="Ayam
  Goreng: 20")`) — one lightweight `answerCallbackQuery` that both displays the
  value AND clears the spinner. The message (and keyboard) are edited **only on
  ✓ commit** (back to the item list). The buffer lives in an **in-memory**
  `_numpad_state` (keyed chat+user+session+item) — no DB per keystroke; the value
  is written to `kitchen_log_session` only on ✓ (entries also persist on Hantar).
  On the common path `answer()` is the first await, so the spinner clears
  instantly. A memory miss (restart mid-entry) recovers the buffer from the DB
  once.
- **Concurrent update processing.** The bot runs with
  `Application.concurrent_updates(True)`, so a slow handler (a multi-second OCR
  on an uploaded receipt) no longer blocks the event loop's update queue — numpad
  taps are processed immediately instead of waiting behind an in-flight OCR.

Per-tap timing is logged (`kitchen numpad d5 -> '50': recv->answer_start Xms,
answer Yms (toast, no edit)` and `... ✓ commit ...: edit Zms`) so the latency
source is visible in the logs.

`callback_data` is namespaced `kdu:{session_id}:{item_code}:{action}` so it never
collides with the existing `review:` / `reparse:` / `backfill:` handlers. Entry
is **tap-only** — the earlier bulk-message and ForceReply typing experiments were
removed (the pure `parse_bulk_entry` helper is retained but unused).

## Enabling the scheduled forms (`KITCHEN_LOG_ENABLED`)

The 18:00 COOKED / 02:00 LEFT posters are **OFF by default** and only run when
the env var `KITCHEN_LOG_ENABLED` is truthy (`true`/`1`/`yes`/`on`). This lets
the feature ship — and `/kitchen_groups_debug` verify the chat→outlet mapping —
without blasting a possibly-mis-mapped form to 10 groups. Flip it to `true` on
Render once the mapping is confirmed. `/kitchen_groups_debug` (DM only,
read-only) shows the current flag state in its reply.

## Schedule

Both jobs run on the same in-process APScheduler as the 23:00 digest
(Asia/Kuala_Lumpur):

- **18:00** — post the COOKED form (`post_cooked_forms`).
- **00:00** — post the optional night-cook (additive) form (`post_night_forms`).
- **02:00** — post the LEFT form (`post_left_forms`).

00:00 and 02:00 are both before the noon cutoff, so they fold back onto the
prior 18:00 business day. The night form (`phase='cooked_night'`) needs only one
item filled (it's additive and optional); 18:00/02:00 still require all items.

They iterate the kitchen groups from `config/kitchen_groups.py`, which
**auto-resolves the chat IDs from the `receipts` table** — the same chats the
resit pipeline already receives receipts from (nobody pastes IDs). It groups
receipts by `chat_id`, resolves each chat's stored `outlet` string to a kitchen
`outlet_code` via `outlet_code_from_text`, keeps only group chats (negative
`chat_id`), and when one outlet has multiple chats the busiest wins. A manual
`KITCHEN_GROUPS` override still takes precedence if ever populated. The jobs
no-op cleanly until at least one group resolves.

Group title → outlet_code:

| group | code |
|-------|------|
| Khulafa Bistro | `BISTRO7` |
| Khulafa Sek 20 | `SEK20` |
| Khulafa Signature | `SEK14` |
| Khulafa Sek 15 (One Bistro) | `SEK15` |
| Hj Sharfuddin Klang Bayu Emas | `KLANG` (this *is* the Klang B.Emas outlet) |
| Khulafa Vista | `VISTA` |
| Khulafa Jakel | `JAKEL` |
| Khulafa Damansara | `D` |
| Khulafa KL Razak / "Kl Sg Besi" | `KLRAZAK` (one outlet, two names; → "K.L Razak", matches sales' `S-RAZAK`) |
| Khulafa Sek 6 / Jalan Murai | `SEK6` (only a genuine Sek 6 group) |

`SEK6` only resolves from a real "Sek 6"/"Jalan Murai" titled group — the
Sharfuddin/Klang Bayu Emas group is KLANG, not SEK6.

### Startup visibility & live debug

At startup the bot logs one line, e.g.
`Kitchen groups resolved: 9/10 — missing: SEK6` (WARNING when any expected
outlet is missing — usually a group with no recent receipts — INFO when all 10
resolve). Admins can run **`/kitchen_groups_debug`** on the live bot to dump
every group chat the bot has seen with its `chat_id`, stored outlet text, and
resolved `outlet_code`, plus which expected outlets are still missing.

## Calculation & mismatch flag

`Used = Cooked − Left` per item. Most items are compared against **POS sold**
(`sales_daily_itemwise` for the same `business_date`, matched in
`ITEM_POS_KEYWORDS`). **Each whole-leg item stays on its OWN comparison line**
(ayam_bawang / ayam_kicap / ayam_rempah / ayam_madu are *not* combined) so a
mismatch is visible by name. **Kambing / Daging** convert POS portions to kg
using locked portion sizes (`KG_PORTION_GRAMS`): **Kambing 180 g, Daging 60 g**.

### Outlet-code join (why POS used to read 0)

The POS daily-summary keys every outlet with a one-letter shift/day prefix
(`S-KLANG` per shift, `D-KLANG` per day) while the kitchen keys the same outlet
bare (`KLANG`). A raw compare never matched, so POS came out 0.
`normalize_outlet_code` strips the `S-`/`D-` prefix and resolves the remainder
to its canonical outlet name, landing both sides on one key — for all 10
outlets, including the two whose names differ (kitchen `D` ↔ POS `D-DAMANSARA`
both → `D.U`; kitchen `KLRAZAK` ↔ POS `D-RAZAK` both → `K.L Razak`).

### Per-item POS matching

Each tracked item matches POS dishes by a precise rule, and **excludes**
Thai-chef *isi ayam* and staff meals so their sales never manufacture a false
mismatch (those proteins aren't in the kitchen log):

| item | counts | notes |
|------|--------|-------|
| `ayam_goreng` | `Ayam Goreng`, `Ayam Goreng Besar`, `Nasi Ayam Goreng Besar (Sayur)` | whole-cut only — the words *ayam goreng* must be **adjacent**, so `Nasi Goreng Ayam` / `Maggi Goreng Ayam` / `Mee Goreng Ayam` do NOT count |
| `ayam_bawang` | `Ayam Bawang`, `Nasi Ayam Bawang (Sayur)`, `Nasi Separuh Ayam Bawang`, `Briyani Ayam Bawang Set/Telur/Sayur` | any *bawang* dish |
| `ayam_kicap` | `Ayam Masak Kicap` (any `…kicap`) | |
| `ayam_rempah` | *rempah* dishes | **BISTRO7 only**; a fried `…berempah` dish is a goreng dish, not rempah |
| `ayam_madu` | `Ayam Madu` dishes | |
| `ayam_tandoori` | `Ayam Tandoori` / `Ayam Tandori` | excludes `…Staff` |
| `ikan_goreng` | `Ikan Goreng` dishes | |
| `ikan_kari` | ikan *kari* / *curry* dishes | |
| `kambing` | **all** kambing dishes × 180 g ÷ 1000 → kg | |
| `daging` | **all** daging dishes × 60 g ÷ 1000 → kg | |

**Excluded entirely** (no count, no flag): plain `Nasi Ayam` / `Nasi Separuh
Ayam` / `Isi Ayam` (no style → match nothing), any **THAI FOOD** category, staff
meals, and `Paprik / Tomyam / Maggi / Indomee / Kuey Teow / Mee Goreng` ayam +
`Ayam Rendang / Kurma / Kari`. Items with no kitchen entry and no POS match show
0 quietly.

**Telur Ikan is the exception** — it is not a POS dish, it is bought by weight.
So it is compared against **kg PURCHASED** (pulled from `receipts`, matching the
raw "telur ikan" / fish-roe line — the resit pipeline only canonicalizes it to
the coarse `ikan`, so the raw name is matched directly), NOT against POS, and
there is no portion-size guess. Approach **(b)** for v1: show both numbers and
only flag when a **same-day** purchase exists — purchases aren't daily, so a day
with no buy shows **"tiada rekod beli"** (➖) and is never flagged.

Dual-gate flag (both gates must trip):

| unit | % gate | abs gate |
|------|--------|----------|
| pcs | `> 8%` | `> 5 pcs` |
| kg | `> 10%` | `> 1.5 kg` |

- **Used > comparison → 🔴 LEAK** — possible leakage / unrecorded sale / over-portion.
- **Used < comparison → ⚠️ DATA** — likely a key-in error or carryover.
- The comparison is POS sold for every item except **Telur Ikan**, which is kg
  purchased ("X kg guna vs Y kg beli").

### Two-stage timing (POS isn't ingested at 02:00)

Same-day POS sales arrive via the **~7AM** sales email, so at 02:00 (when LEFT is
keyed) POS is always 0 — comparing then produced false 🔴 leak flags. The digest
is therefore split into two stages:

- **STAGE 1 — 02:00, right after LEFT is submitted** (`render_save_confirmation`):
  a **save confirmation + usage only**, header *"✅ Rekod siap — Guna [outlet]
  [business_date]"*, one line per item showing **masak / baki / guna**
  (cooked / left / used). **No POS comparison, no flags.** `finalize_submission`
  no longer writes `pos_qty` / `mismatch_flag` — they stay NULL until Stage 2.

- **STAGE 2 — 09:00, gated on POS COMPLETENESS** (`post_comparison_digests`,
  scheduled in `bot.py`; retries at 11:00 and 14:00 via
  `post_comparison_digests_retry`): the real **Used-vs-POS comparison**
  (`render_mini_summary`, v12-aware classification + dual-gate flags) for the
  business day that just closed at 02:00. `evaluate_outlet_day` persists `pos_qty`
  + `mismatch_flag` on the `kitchen_daily_usage` rows.

  **Why completeness matters (the two-shift POS day).** A 24h outlet reports its
  POS in **two shift-close emails** that BOTH fold to the same `business_date`:
  the **~7PM day shift** (closes the same evening) and the **~7AM-next-day
  overnight shift** (started the prior evening, closes after midnight). So a
  business day's true 24h POS total = both shifts combined, and the second email
  doesn't arrive until ~7AM the next day. Comparing before both are in would be
  against a **half-day** of sales. STAGE 2 therefore checks completeness via
  `pos_shift_coverage` / `pos_complete_for_outlet` and only compares when:
  - the **D-file daily summary** (`sales_daily_summary`, which carries the
    itemwise quantities used for the comparison) exists, **and**
  - the **overnight shift** is present in `sales_daily` (`shift_type='overnight'`
    for that outlet+`business_date`) — proof the post-midnight portion closed and
    was reported.

  **Fold is correct (verified).** `sales_parser.determine_shift_type_and_business_date`
  dates the day shift (closes 17–22h) to the close date and the overnight shift
  (closes 5–10h) to the close date **− 1 day**, so both shifts of business day D
  map to `business_date = D` — the SAME day the kitchen folds 18:00 D / 00:00 D+1
  / 02:00 D+1 into. The D-file (`sales_daily_parser.business_date_for_printed`)
  folds the same way. No fold mismatch; `scripts/report_shift_coverage.py` prints
  the live shift rows per `business_date` to confirm on real data.

  **Business date:** before noon (09:00, 11:00) the just-closed day is
  `business_date_for(now)` (noon-fold); the **14:00** retry is past noon, so it is
  pinned to `now − 1 day` to keep targeting the SAME closed day.

  **Safety / idempotency:** until POS is complete, the outlet shows
  **"⏳ POS belum lengkap"** (detail naming what's missing — nothing yet vs the
  overnight shift) and is **never flagged** (deferred — the 09:00 run notifies
  once; the 11:00/14:00 retries stay silent, then post once POS completes). A day
  already reconciled (`pos_qty` set) or with an incomplete COOKED/LEFT record is
  skipped.

## Digest

The 23:00 **Daily Intelligence digest** appends a **🍳 KITCHEN USAGE vs POS**
section per outlet for the just-completed business day (yesterday at digest
time). It reads the `pos_qty` / `mismatch_flag` written by the 09:00 Stage 2 job
(which ran earlier the same day), so by 23:00 the values are present. If COOKED or
LEFT is missing for an outlet, the row reads **"Rekod tak lengkap"** instead of a
false mismatch.

## Files

- `migrations/0032_kitchen_daily_usage.sql` — `kitchen_daily_usage` (generated
  `used_qty`) + `kitchen_log_session` (numpad state).
- `kitchen_usage.py` — pure logic (numpad state machine, Used arithmetic,
  POS matching, dual-gate flag) + Telegram handlers + schedulers.
- `config/kitchen_groups.py` — chat_id → outlet_code stub (paste the 10 IDs).
- `digest.py` / `digest_data.py` — the digest section + its data gatherer.
- `scripts/report_shift_coverage.py` — READ-ONLY: per-`business_date` POS shift
  rows + completeness for an outlet (run on Render to verify the two-shift fold
  and arrival times on live data).
- `tests/test_kitchen_usage.py` — numpad, Used, dual-gate, Bistro-only, span,
  two-stage timing + POS shift-completeness gate.
