# Daily Kitchen Usage Log

Track daily protein usage per outlet and reconcile it against POS sales, all by
tapping (no word-typing). The assistant chef logs what was **cooked** at 18:00;
the cashier logs what is **left** at 02:00 the next morning. The bot computes
`Used = Cooked − Left`, compares it with POS dishes sold, and flags mismatches.

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

## Key-in UX (tap-only)

The bot posts **one** message with an inline keyboard — one button per item
showing its value (`—` empty, the number when filled, a `✓` prefix when done).
Tapping an item swaps the message into an inline **numpad**:

- **pcs** (3×4): `1 2 3 / 4 5 6 / 7 8 9 / ⌫ 0 ✓`
- **kg** (adds `.`): `1 2 3 / 4 5 6 / 7 8 9 / . 0 ⌫ / ✓ Simpan`

Each keypress edits the message to show the running value. `✓` writes the value
and returns to the item list. Tapping a filled item re-opens the numpad
pre-filled so it can be corrected. The final **📤 Hantar** button is gated until
every required item is filled.

`callback_data` is namespaced `kdu:{session_id}:{item_code}:{action}` so it never
collides with the existing `review:` / `reparse:` / `backfill:` handlers. The
in-progress form (committed values **and** the half-typed numpad buffer) lives in
`kitchen_log_session`, so a bot restart never loses a partially filled form.

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
- **02:00** — post the LEFT form (`post_left_forms`).

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
(`sales_daily_itemwise` for the same `business_date`, matched by base + style
keyword in `ITEM_POS_KEYWORDS`; the bot only canonicalizes to the bare "ayam",
so per-style splitting happens here). **Kambing / Daging** convert POS portions
to kg using locked portion sizes (`KG_PORTION_GRAMS`): **Kambing 180 g, Daging
60 g**.

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

Right after LEFT is submitted, a mini Used-vs-POS recap is posted to the group
(`render_mini_summary`). `pos_qty` and `mismatch_flag` are persisted on the
`kitchen_daily_usage` rows.

## Digest

The 23:00 **Daily Intelligence digest** appends a **🍳 KITCHEN USAGE vs POS**
section per outlet for the just-completed business day (yesterday at digest
time). If COOKED or LEFT is missing for an outlet, the row reads **"Rekod tak
lengkap"** instead of a false mismatch.

## Files

- `migrations/0032_kitchen_daily_usage.sql` — `kitchen_daily_usage` (generated
  `used_qty`) + `kitchen_log_session` (numpad state).
- `kitchen_usage.py` — pure logic (numpad state machine, Used arithmetic,
  POS matching, dual-gate flag) + Telegram handlers + schedulers.
- `config/kitchen_groups.py` — chat_id → outlet_code stub (paste the 10 IDs).
- `digest.py` / `digest_data.py` — the digest section + its data gatherer.
- `tests/test_kitchen_usage.py` — numpad, Used, dual-gate, Bistro-only, span.
