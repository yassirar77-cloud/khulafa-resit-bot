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
to type instead. Notes longer than two minutes are not transcribed.

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

## Director commands added

`/closed <OUTLET> [YYYY-MM-DD] [reason]`, `/staff_digest_now`, `/issues`,
`/resolve <id>`, `/order <OUTLET>`, `/phrasing_now`. Existing: `/lang`,
`/draft`, `/staff_preview`, `/staff_samples`.
