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

## Schedule

Both jobs run on the same in-process APScheduler as the 23:00 digest
(Asia/Kuala_Lumpur):

- **18:00** — post the COOKED form (`post_cooked_forms`).
- **02:00** — post the LEFT form (`post_left_forms`).

They iterate `config/kitchen_groups.py`. **Until the 10 real group chat IDs are
pasted there the jobs no-op** — nothing is posted to the wrong place.

## Calculation & mismatch flag

`Used = Cooked − Left` per item. POS quantity comes from `sales_daily_itemwise`
for the same `business_date` (matched by base + style keyword in
`ITEM_POS_KEYWORDS`; the bot only canonicalizes to the bare "ayam", so per-style
splitting happens here). kg items convert POS portions to kg using locked
portion sizes (`KG_PORTION_GRAMS`): **Kambing 180 g, Daging 60 g, Telur Ikan
100 g** *(confirm the Telur Ikan portion — it lives in one place to adjust)*.

Dual-gate flag (both gates must trip):

| unit | % gate | abs gate |
|------|--------|----------|
| pcs | `> 8%` | `> 5 pcs` |
| kg | `> 10%` | `> 1.5 kg` |

- **Used > POS → 🔴 LEAK** — possible leakage / unrecorded sale / over-portion.
- **Used < POS → ⚠️ DATA** — likely a key-in error or carryover.

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
