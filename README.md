# Khulafa Resit Bot

Telegram bot for the Khulafa restaurant group: reads supplier bills sent to
the outlet groups (GLM OCR), keeps the buying history, ingests POS sales
emails, and talks to each outlet's cashier in their own language through the
staff chat. Runs as one process on Render (`bot.py`: Telegram polling,
APScheduler jobs, a Flask health server). Data lives in Supabase.

* Run: `python bot.py` with the variables in `.env.example`.
* Tests: `python -m unittest discover -s tests -p "test_*.py"` (no network).
* Migrations: `migrations/00NN_*.sql`, applied once in the Supabase SQL editor.

## DeepSeek in this bot

DeepSeek is the language model behind the **staff chat**. It never decides
what to say: the code picks the question and the facts from the database,
DeepSeek only phrases it in the cashier's language, and the fact check
throws the wording away (and sends the plain template) if it carries a
number, item, supplier, money figure or name that is not in the facts.
Every message is logged to `staff_chat_log` with its facts, the AI text, the
template, which one went, why, and the provider / model / tokens.

All provider-specific code is in `staff_ai.py` (`STAFF_CHAT_AI` picks the
provider; `complete_json` is the one call every feature uses). Reasoning is
off and the output is forced to JSON. `/health` shows when DeepSeek last
answered and today's token spend.

| Feature | Module | What DeepSeek does | Writes to |
| --- | --- | --- | --- |
| Check-ins (08:00 open, 10:35 stock, 11:05 cook, 15:00 lunch, 20:05 order, 21:05 bills, 23:00 night) | `staff_chat.py`, `staff_live.py` | Words the question in the cashier's language; back-translates and judges Tamil | `staff_chat_log`, `staff_chat_thread` |
| Reply reading | `staff_live.py` | Reads the cashier's reply: answer or chatter, status, English summary, order items, "are you a bot?", issue, explanation | `staff_chat_thread`, `staff_order_items` |
| Acknowledgement | `staff_ack.py` | None (plain template, fact-checked): one line back saying what was understood ("Noted: 12 ayam, 5 kg bawang"), the transcript for a voice note, or "which question is that for?" when the reply matched none | — |
| Follow-up nudges | `staff_nudge.py` | Rephrases the two reminders (outlet, check-in, minutes elapsed are the only facts); `NUDGE_AFTER_MIN`, 07:00–23:30, none for `/closed` outlets | `staff_chat_log` (`kind='nudge'`, `nudge_no`), `outlet_closed_days` |
| Nightly director digest (23:30) | `staff_digest.py` | Writes the day's replies and non-replies in plain English, max 12 lines, ordered by concern; each line fact-checked, plain list as fallback | `staff_chat_log` (`kind='digest'`) |
| Issue flagging | `staff_issues.py` | Classifies a problem in a reply (equipment / staff / supplier / customer / cash), urgent ones forwarded to the director; keyword fallback; `/issues`, `/resolve <id>` | `staff_issues` |
| Tomorrow's order (20:05) | `order_proposal.py` | Phrases the proposed order (median of the last 4 same-weekday buys; thin lines marked "confirm qty"); "ok" or corrections saved; `/order <outlet>` | `staff_order_items` (`source`) |
| Anomaly questions | `staff_anomaly.py` | Phrases a targeted question when sales, wastage or an order quantity is outside `ANOMALY_PCT` of the 4-week same-weekday average | `staff_chat_log` (`kind='anomaly'`, `deviation_pct`) |
| Receipt-vs-order mismatch | `po_mismatch.py` | Reads the cashier's explanation (`mismatch_explained`) for a bill that differs from the order; the question itself is fixed wording | `receipts.po_mismatch / po_explanation`, `staff_chat_thread` |
| Voice replies | `staff_voice.py` | Voice notes are transcribed by Groq-hosted Whisper (`GROQ_API_KEY`, `GROQ_STT_MODEL`), always in the cashier's `/lang` language, then read like a typed reply; below `VOICE_MIN_CONFIDENCE` or on any error the cashier is asked to type | `staff_chat_log` (`kind='voice'`, `voice_file_id`, `transcript`, language and duration in `facts`) |
| Director Q&A | `director_sql.py` | Turns a question in the director chat into one SELECT (guarded in code, run read-only with a 5 s timeout, `LIMIT 200`) and writes the one-line answer from the rows; `DIRECTOR_QA=on` | `director_sql_log` |
| Learning loop (Mon 08:00) | `staff_learning.py` | Gets the three fastest-answered wordings per language and check-in as few-shot examples, capped at +30% prompt tokens | `phrasing_examples` |

Safety rails that apply everywhere: `STAFF_CHAT_STYLE=preview` sends every
check-in to the director chat only; the fact check runs on every AI wording;
any provider failure falls back to the plain template silently; every call
is logged with provider, model and tokens.

### Speech-to-text (voice replies)

Groq-hosted Whisper large v3 turbo (`whisper-large-v3-turbo`, about US$0.04
per hour of audio) through the OpenAI-compatible audio endpoint. The
cashier's `/lang` setting is sent as the language on every call (ta, ms,
bn, en, id; the Malay+Tamil mix as ms) — never auto-detect. Confidence is
derived from Whisper's per-segment log-probabilities; below
`VOICE_MIN_CONFIDENCE` (0.6) or on any API error the bot asks the cashier
to type instead. Notes longer than two minutes are not transcribed. Every
bounce is logged with its reason — `api_error` (with the HTTP status and
message, at WARNING), `empty_text`, `low_confidence` (with the score),
`no_key`, `too_long`, `download_error` — and kept on the log row, so
`/voice_stats` can show transcribed vs bounced per outlet with the reasons.

## Migrations added with the DeepSeek roadmap

| File | Adds |
| --- | --- |
| `0050_staff_nudges.sql` | `staff_chat_thread.nudge_count`, `staff_chat_log.kind / nudge_no`, `outlet_closed_days` |
| `0051_staff_issues.sql` | `staff_issues` |
| `0052_order_proposal.sql` | `staff_order_items.source` |
| `0053_po_mismatch.sql` | `receipts.po_mismatch / po_explanation / po_explained_at` |
| `0054_staff_voice.sql` | `staff_chat_log.voice_file_id / transcript`, `staff_chat_thread.reply_clear` |
| `0055_director_sql.sql` | `director_readonly` role, `director_sql(q)` function, `director_sql_log` |
| `0056_phrasing_examples.sql` | `phrasing_examples` |
| `0057_nudge_off.sql` | `outlet_nudge_off` |
| `0058_outside_purchases.sql` | `approved_suppliers`, `allowed_outside_items`, `cashier_roster`, `outside_purchases`, view `cashier_strikes` (RLS on) |
| `0059_pinpoint_v2.sql` | `outside_purchases.mode / notified_at / source`, `outlet_known_merchants` (seeded), `overbuy_flags`, `holiday_calendar` (seeded) — RLS on |

## Outside purchases + cashier strikes ("Pinpoint Target")

`outside_purchase.py`, `migrations/0058_outside_purchases.sql`. When a
cashier uploads a bill from a shop that is **not** an approved supplier
(Lotus, 99 Speedmart, pasar, kedai runcit …) the bot works purely from what
OCR already extracted (no new vision call) and pinpoints:

* **who** — the cashier on shift (`cashier_roster`, outlet + morning/night,
  Asia/Kuala_Lumpur, night shift runs past midnight); a cashier who linked
  their account with `/daftar_cashier` is matched directly by uploader id;
* **when** — the receipt date + a time read from the OCR text, else the
  upload time; **where** — the outlet; **what** — the items (qty, price);
* **how much extra** — against the latest approved-supplier unit price in
  `item_prices` (skipped when there is no approved price).

Rules: merchants match `approved_suppliers` on exact name / alias /
word-bounded phrase / clear OCR drift — never a bare `%bestari%`
(BESTARI MINIMART is outside, BESTARI FARM (M) SDN BHD is ours). Items in
`allowed_outside_items` (ais, emergency gas) never count; a bill with only
those gets no strike. **False-positive guard:** a fuzzy grey-zone merchant,
an unreadable merchant or a receipt the verifier scored below
`OUTSIDE_MIN_CONFIDENCE` is held as `pending_review` and the director chat
gets **[Beli Luar ✅] [Supplier Kita ❌]** — no strike until a human confirms.

Strikes are counted per cashier and outlet over `STRIKE_WINDOW_DAYS` (30),
`status = 'counted'` only (view `cashier_strikes`). The reply under the
receipt is BM + Tamil (the Tamil lines need a native-speaker review before
go-live, see the module docstring):

| Strike | Reply in the group |
| --- | --- |
| 1 | info: this bill is from an outside shop, please order from the official supplier |
| 2–3 | reminder with the count and the extra cost vs the approved supplier |
| 4 | final warning: the next one is reported to management |
| 5+ (`SCOLD_THRESHOLD`) | firm warning listing every purchase in the window with totals and extra cost, "management has been informed"; the full report also goes to `ALERT_CHAT_ID` |

The warnings criticise the action, never the person: no insults, nothing
about race, religion or nationality. `SCOLD_CHANNEL=dm` sends the warning to
the cashier's DM instead (only works once they ran `/daftar_cashier` and
started the bot; otherwise it falls back to the group reply).

Commands (admin = director chat or a reviewer): `/beli_luar [outlet] [days]`
(per cashier: count, RM, extra cost, top items), `/beli_luar_cashier <name>`
(full history), `/izin <id> <reason>` (approved emergency — strike removed),
`/bukan_beli_luar <id>` (false positive), `/tambah_supplier <name> [= <supplier>]`
(approve a supplier or add an alias). Cashiers run `/daftar_cashier` in their
outlet group and pick shift + name with buttons. The monthly close
(`/monthly_kg`, 1st of the month) ends with a per-outlet "Beli Luar" section.

### v2: shadow mode, known merchants, overbuy (`migrations/0059_pinpoint_v2.sql`)

* **Shadow mode.** `OUTSIDE_PURCHASE_MODE=shadow` (the default) detects,
  attributes, records, counts strikes and sends the review buttons and
  reports to the director chat, but sends **nothing** to cashiers.
  `/pinpoint_shadow` (and a 21:45 job while in shadow) lists what WOULD have
  gone out. Every row carries the mode it was recorded in; after the switch
  to `live` the cashier's strike number counts live rows only, so old shadow
  strikes never fire retroactively (management reports keep the full count).
* **One bill = one message.** When the pinpoint reply or the overbuy question
  goes out, the mini-market "why?" and the invoice question stay quiet.
* **Known merchants per outlet** (`known_merchants.py`,
  `outlet_known_merchants`). Only a merchant that is neither an approved
  supplier nor known for THAT outlet is pinned. The baseline is the last 90
  days (>= 3 bills at the outlet); approved suppliers are known everywhere.
  The nightly 03:30 refresh streams receipts page by page and only recounts —
  a shop never becomes known by being used; `/tambah_supplier` or a manual
  row does that. `/merchant_known <outlet>` (`all` for the full report),
  `/buang_merchant <outlet> <name>`.
* **Overbuy** (`overbuy_check.py`, `overbuy_flags`, `holiday_calendar`). A
  known-supplier bill with an item at >= `OVERBUY_PCT` (40) above the
  outlet's cadence-adjusted usual (median qty per day of cover over the last
  8 purchases at that supplier) while yesterday's POS sales were not above
  the 14-day average asks the cashier why — BM + Tamil, reply to the bill,
  buttons [Stok habis] [Ada tempahan/katering] [Supplier hantar lebih]
  [Lain-lain] (typed reason). Skipped, never flagged: POS not in yet, bad
  qty or a receipt whose lines don't add up, < 4 past purchases, standing
  orders (roti / capati / gas), Jakel, public holidays. No answer in 12h →
  `no_reply`. The director gets the numbers (sales RM, 14-day average, %
  drop) with [Terima] [Tolak]; only `no_reply` and `rejected` count toward
  the separate overbuy strike counter (same tiers, rolling 30 days).
  `/lebih_beli [outlet] [days]`; the monthly close gains an "Overbuy" block.
* **Cashiers never see sales figures.** No sales RM, average or % in any
  cashier-facing text; their own purchase quantities and bill totals may
  appear. Management texts keep the numbers. The weekly food-cost % is
  management-only (`group_reports.MANAGEMENT_ONLY`, never a group or a
  manager DM), and the kitchen Used-vs-POS recap in the group shows only the
  gap per item ("Ayam: guna lebih 4 pcs dari jangkaan" / "Ayam: OK") while
  the full Used / POS numbers go to the director chat.
* **Staff payments are not purchases.** Leave pay (and its OCR spellings),
  gaji / salary / wages, advances and loans, overtime pay, allowances,
  bonuses, EPF / SOCSO, ustad / surau / khairat payments
  (`outside_purchase.is_staff_payment`) are outside the whole Pinpoint flow:
  no outside-purchase check, no overbuy check, never a strike, never seeded
  as a known merchant. "ADVANCE" alone is a shop's brand word: it only counts
  with payroll context (SALARY / GAJI / STAF / voucher or form words) or a
  staff name, never for ADVANCE ENTERPRISE / ADVANCES ACCESSORIES SHOP
  (`receipt_classifier.advance_is_staff`).
* **Classifier knows the Pinpoint lists (shadow-week fix).** `classify_receipt`
  takes `suppliers=` (approved suppliers + the outlet's active known
  merchants, from `outside_purchase.cached_config`, two-minute cache). A bill
  from one of them is SUPPLIER_PURCHASE, so price history, Pinpoint and the
  overbuy check all run on it. Deactivated known merchants do not count.
  Fuel-only bills (E5 / B10 / B20 / RON95 / RON97 / Diesel / Primax /
  V-Power / FuelSave / FS Diesel, `receipt_classifier.is_fuel_line`) stay
  PETTY_CASH whatever the total, as do LALAMOVE and Touch 'n Go reloads;
  LPG cylinders are gas, not fuel. A petty-cash keyword on a bill with stock
  lines (drinks, fruit, meat, dairy, bottles) is a purchase. Short keywords
  (TOL, TNB, LHDN, KWSP...) match whole words only, and the e-invoice footer
  "LHDN VALIDATED LINK" is stripped before matching.
* `item_price_quarantine` (`migrations/0042_item_price_quarantine.sql`) must
  exist for the price sanity gate; it was applied to production on
  2026-10-02 together with a backfill of UNKNOWN bills from approved / known
  merchants into `item_prices`.
* **Per-kg lines carry the weight (approvals round).** Before pricing,
  `items_utils.resolve_weighed_lines` maps alternative OCR keys
  (quantity / unit_price / description / total) and, when the bill total is
  known, replaces order counts (30 birds, 40 legs) with the printed weight:
  from the line total ÷ price, a weight in the name, or the raw text in either
  layout ("count name weight price total" delivery orders, "qty price weight
  total" invoices). A correction is only kept when it brings the lines to the
  bill total. The OCR prompt now asks for the weight as qty on per-kg lines.
* `fixed_costs_archive` (0060) and `petty_cash_archive` (0061) hold side-table
  rows moved out after a director-approved retype, with a reason; nothing is
  deleted outright.
* Kitchen form edits (`kitchen_usage._edit_form_message`) log a failed
  `edit_message_text` with its traceback instead of suppressing it;
  "message is not modified" is logged at debug.
* **Chicken cuts are their own items (2026-10-02).** `ayam_leg` (WHOLE LEG,
  W.LEG / WING / DRUMSTICK / THIGH, paha / peha), `ayam_wing`, `ayam_isi`
  (ISI AYAM, ISI / MINCED / CHOP / FILLET / B.LEG) and `ayam_breast` are
  priced separately, but `item_canonicalization_v2.item_family` maps them to
  `ayam`, so monthly kg, key-stock, overbuy-watch and the overbuy POS check
  still count them as chicken. Director price questions resolve a cut to the
  `ayam` family and narrow by name, as before. WHOLE LEG lines now count as kg
  (the "whole" per-piece marker no longer swallows "whole leg").
* **Classifier (approvals round 2).** A supplier's "License Number : ..." /
  "Lesen No." header line is a company ID, not a licence payment. PINJAM on a
  bill with SILINDER / TONG / GAS / BOTOL / DEPOSIT is a cylinder loan, not a
  staff advance.
* Migrations 0062–0065: 0062 locks the anon key out of the 11 RLS-off tables
  and `receipts`; 0063 gives `director_readonly` read policies on every RLS
  table (except `outlet_registration_codes`), limits `director_sql()` to
  service_role, and revokes anon/authenticated default privileges; 0064
  applies 0035 (+ outlet backfill), 0036 (text sentinel) and the missing
  indexes; 0065 adds `receipts.receipt_date_original` and
  `staff_advances_archive`.
* **Undated bills (close-out).** `save_item_prices` dates the price rows of a
  bill with no receipt date by its Malaysia upload day, so reports that
  filter `item_prices.receipt_date` no longer drop it; the receipt keeps
  `receipt_date` NULL. The digest's weekly outlet spend counts undated
  supplier bills on their upload day. Rule-based date fixes keep the OCR'd
  date in `receipts.receipt_date_original`. The same upload-day fallback
  (`date_utils.receipt_day` / `upload_window`) covers `/summary`, missing-bill
  alerts and the known-merchant baseline; migration 0067 rebuilds the
  `price_movements` view on `COALESCE(receipt_date, upload day)`.
* Migration 0066 drops `audit_responses`' `anon_read` policy (the last table
  the anon key could still read after 0062).

Apply the migrations yourself (not done by the bot):

```
psql "$SUPABASE_DB_URL" -f migrations/0058_outside_purchases.sql
psql "$SUPABASE_DB_URL" -f migrations/0059_pinpoint_v2.sql
```

## Director commands added

`/closed <OUTLET> [YYYY-MM-DD] [reason]`, `/nudge_off <OUTLET> today` (no nudges for the rest of the day, outlet not closed), `/voice_stats` (voice notes this week per outlet: transcribed vs bounced, with reasons), `/staff_digest_now`, `/issues`,
`/resolve <id>`, `/order <OUTLET>`, `/phrasing_now`. Existing: `/lang`,
`/draft`, `/staff_preview`, `/staff_samples`.
