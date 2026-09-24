# Director feed — who sees what

The director chat (`ALERT_CHAT_ID`) was receiving 20+ kinds of message a day,
most of them per-receipt noise. This page is the map of every message the bot
sends, sorted by **who needs it** and **when**.

## The rule

> **Director = decisions and exceptions. Manager = actions in their own shop.**

The director should only see:
1. **Money that moved abnormally** (big price jump, big purchase, food cost % off).
2. **People not doing their job** (forms ignored, questions unanswered, bills missing).
3. **One daily scorecard and one weekly scorecard** comparing the shops.

Everything else goes to the shop manager, or is already in a scheduled report.

## Director — what stays

| When | Message | Why the director needs it |
|---|---|---|
| Live | Price jump ≥ 20% (`DIRECTOR_SPIKE_MIN_PCT`) | Real cost leak, act today |
| Live | Receipt could not be classified | Needs a human tag |
| Live | Manager's answer to an audit question | Closes the loop the director opened |
| 09:00 | Kitchen Used-vs-POS comparison (+14:00 missing-POS alert) | Wastage / theft signal |
| 17:00 | Who has gone quiet on questions | Manager accountability |
| 21:30 | Bill analysis — every increase + cheaper branch | All small increases live here |
| 23:00 | Nightly digest (sales, food cost %, top items, cash-no-receipt) | **The** daily scorecard |
| Mon 09:00 | HQ weekly food-cost summary | **The** weekly scorecard |
| Mon 11:00 | Response scoreboard | Who answers, who doesn't |
| 1st of month | kg-per-protein report | Monthly buying trend |

## Director — what was cut from the live feed (focus mode)

| Message | Where it still is |
|---|---|
| "New receipt logged" + full item list, for every receipt | 23:59 daily summary, 23:00 digest; manager still gets the confirmation |
| Price increase 10–20% on a single item | 21:30 bill analysis; manager still gets the Tamil spike question |

Set `DIRECTOR_FEED=full` on Render to bring both back.

## Outlet group — what each shop gets

At Khulafa the cashier is the manager, so each outlet's Telegram group is
registered as its manager (`outlet_managers.chat_id` = the group id). Every
message to a group opens with the cashier on shift — `Rahim,` — see
`cashier_names.py` (morning 07:00–18:59, night 19:00–06:59 MY; `Cashier,` when
no name is set). Change a name with `/cashier SEK20 night Ismath`; list them
with `/cashier`; test every group with `/ping_managers`.

Groups get **tasks**:

| When | Message |
|---|---|
| Live | Receipt confirmation, anomaly note, audit question (big purchase / new supplier / odd item price) |
| Live | Tamil price-spike question with cheaper shops — in the group the bill came from |
| 10:30 | Key stock check |
| 10:45 | Slow items to push today |
| 11:00 | Cook-to-demand plan (how much to cook) |
| 17:00 | Reminder for unanswered questions |
| 18:00 / 00:00 / 02:00 | COOKED / night / LEFT kitchen forms (+ reminders) |
| 20:00 | Tomorrow's order draft |
| 21:00 | Missing supplier bills |

**Money reports stay with the director** (who already gets each in full):
weekly food cost %, weekly overbuying, weekly praise, nightly bill-analysis
note. `GROUP_MONEY_REPORTS` on Render lets some back into groups, e.g.
`GROUP_MONEY_REPORTS=praise` or `all` (keys: `food_cost`, `overbuy`, `praise`,
`bill_analysis`). A manager registered by DM still gets them.

## Delivery gate: `MANAGER_DELIVERY_ENABLED`

While this flag is **off**, every group message above is sent to the
director instead, prefixed `[TEST — would go to … manager]`. It is **on** in
production. An outlet with no `outlet_managers` row falls back to the
director with a `[NO MANAGER REGISTERED]` prefix, so nothing is silently
dropped.
