"""Tomorrow's proposed order: same-weekday medians, confirm marks, edits, saving."""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_proposal as op
import staff_chat as sc

TODAY = date(2026, 9, 24)            # Thursday
TARGET = TODAY + timedelta(days=1)   # Friday


def _row(item, qty, day, **kw):
    return {"outlet_code": "SEK7", "canonical_item": item, "qty": qty,
            "receipt_date": day.isoformat(), **kw}


def _fridays(n):
    return [TARGET - timedelta(days=7 * k) for k in range(1, n + 1)]


def _friday(k):
    """The k-th Friday before the target (1 = last Friday)."""
    return TARGET - timedelta(days=7 * k)


HISTORY = (
    # ayam on all 4 Fridays: 10, 12, 8, 30 -> median 11
    [_row("ayam", q, d) for q, d in zip((10, 12, 8, 30), _fridays(4))]
    # two lines on one Friday add up (5 + 5 = 10), plus 14, 10 -> median 10
    + [_row("ikan", 5, _friday(1)), _row("ikan", 5, _friday(1)),
       _row("ikan", 14, _friday(2)), _row("ikan", 10, _friday(3))]
    # telur on ONE Friday only -> confirm qty
    + [_row("telur", 3, _friday(2), unit="kotak")]
    # sotong only on Mondays -> not proposed for a Friday
    + [_row("sotong", 9, _friday(1) - timedelta(days=4))]
    # ayam five Fridays ago is outside the 4-week window
    + [_row("ayam", 99, _friday(5))]
)


class BuildTests(unittest.TestCase):
    def test_medians_of_last_four_same_weekdays(self):
        lines = {ln["item"]: ln for ln in op.build(HISTORY, target_day=TARGET)}
        self.assertEqual(set(lines), {"ayam", "ikan", "telur"})
        self.assertEqual((lines["ayam"]["qty"], lines["ayam"]["points"], lines["ayam"]["confirm"]),
                         ("11", 4, False))
        self.assertEqual(lines["ayam"]["samples"], [8, 10, 12, 30])
        self.assertEqual((lines["ikan"]["qty"], lines["ikan"]["points"]), ("10", 3))
        self.assertEqual((lines["telur"]["qty"], lines["telur"]["unit"], lines["telur"]["confirm"]),
                         ("3", "kotak", True))

    def test_key_items_first(self):
        self.assertEqual([ln["item"] for ln in op.build(HISTORY, target_day=TARGET)],
                         ["ayam", "ikan", "telur"])

    def test_same_weekday_dates(self):
        self.assertEqual(op.same_weekday_dates(TARGET), _fridays(4))
        self.assertTrue(all(d.weekday() == TARGET.weekday() for d in _fridays(4)))

    def test_bad_rows_ignored(self):
        rows = [_row("ayam", "x", _friday(1)), _row("", 5, _friday(1)),
                _row("ayam", 0, _friday(2)), {"canonical_item": "ayam", "qty": 4}]
        self.assertEqual(op.build(rows, target_day=TARGET), [])

    def test_facts_and_template_mark_confirm_lines(self):
        lines = op.build(HISTORY, target_day=TARGET)
        facts = sc.order_facts(op.draft_lines(lines))
        self.assertTrue(facts["partial"])
        telur = next(i for i in facts["items"] if i["item"] == "Telur")
        self.assertTrue(telur["confirm"])
        text = sc.render_template("order", "bm", facts)
        self.assertIn("Ayam 11kg", text)
        self.assertIn("Telur 3 kotak (sahkan)", text)
        self.assertEqual(sc.fact_check(text, facts, vocabulary={"ayam", "ikan", "telur"},
                                       language="bm", slot="order"), [])
        self.assertIn("confirm those quantities", sc._purpose("order", facts))
        # The fact check still insists every proposed item and number appears.
        self.assertTrue(sc.fact_check(text.replace("Ayam 11kg", "Ayam 15kg"), facts,
                                      vocabulary={"ayam"}, language="bm", slot="order"))


class EditTests(unittest.TestCase):
    lines = op.build(HISTORY, target_day=TARGET)

    def test_reply_changes_one_quantity(self):
        # "ayam 12 bukan 10" -> the reply reader gives {ayam, 12}
        edited, changed = op.apply_edits(self.lines, [{"item": "ayam", "qty": 12, "unit": "kg"}])
        by = {ln["item"]: ln for ln in edited}
        self.assertEqual(by["ayam"]["qty"], "12")
        self.assertTrue(by["ayam"]["edited"])
        self.assertEqual(by["ikan"]["qty"], "10")
        self.assertNotIn("edited", by["ikan"])
        self.assertEqual(changed, ["ayam"])

    def test_confirming_a_confirm_line_clears_the_mark_and_new_items_are_added(self):
        edited, changed = op.apply_edits(self.lines, [
            {"item": "telur", "qty": 3, "unit": "kotak"},       # same qty: no change
            {"item": "chicken", "qty": 11},                      # synonym, same qty
            {"item": "santan", "qty": 2, "unit": "pack"},        # new line
        ])
        by = {ln["item"]: ln for ln in edited}
        self.assertTrue(by["telur"]["confirm"])                  # unchanged qty leaves it
        self.assertEqual(by["santan"]["qty"], "2")
        self.assertEqual(changed, ["santan"])
        self.assertEqual(len(edited), 4)

    def test_rows_for_ok_and_for_changes(self):
        thread = {"id": 9, "outlet_code": "SEK7", "cashier": "Kalai",
                  "shift_date": TODAY.isoformat(), "slot": "order"}
        rows = op.order_rows(thread, self.lines, "[button] ✅ OK", confirmed=True)
        self.assertEqual({r["source"] for r in rows}, {"confirmed"})
        self.assertEqual({r["order_for"] for r in rows}, {TARGET.isoformat()})
        self.assertEqual([r["canonical_item"] for r in rows], ["ayam", "ikan", "telur"])
        edited, _ = op.apply_edits(self.lines, [{"item": "ayam", "qty": 12}])
        rows = op.order_rows(thread, edited, "ayam 12 bukan 10", confirmed=True)
        ayam = next(r for r in rows if r["canonical_item"] == "ayam")
        self.assertEqual((ayam["qty"], ayam["source"], ayam["thread_id"]), (12.0, "reply", 9))
        self.assertEqual(sum(1 for r in rows if r["source"] == "confirmed"), 2)
        # Unconfirmed and unchanged: nothing saved.
        self.assertEqual(op.order_rows(thread, self.lines, "hmm", confirmed=False), [])
        self.assertEqual(op.order_rows(dict(thread, shift_date="bad"), self.lines, "ok",
                                       confirmed=True), [])

    def test_next_weeks_median_sees_the_correction(self):
        thread = {"id": 9, "outlet_code": "SEK7", "cashier": "Kalai",
                  "shift_date": TODAY.isoformat(), "slot": "order"}
        edited, _ = op.apply_edits(self.lines, [{"item": "ayam", "qty": 12}])
        saved = op.order_rows(thread, edited, "ayam 12", confirmed=True)
        history = HISTORY + [{"canonical_item": r["canonical_item"], "qty": r["qty"],
                              "receipt_date": r["order_for"], "unit": r["unit"]} for r in saved]
        nxt = {ln["item"]: ln for ln in op.build(history, target_day=TARGET + timedelta(days=7))}
        # Fridays now: 12 (saved), 10, 12, 8 -> median 11; telur has 2 points -> no confirm.
        self.assertEqual(nxt["ayam"]["qty"], "11")
        self.assertEqual(nxt["ayam"]["points"], 4)
        self.assertFalse(nxt["telur"]["confirm"])


class DirectorTests(unittest.TestCase):
    def test_format(self):
        text = op.format_director("Sek 7", TARGET, op.build(HISTORY, target_day=TARGET))
        self.assertIn("Proposed order — Sek 7 for 2026-09-25 (Friday", text)
        self.assertIn("• Ayam 11kg", text)
        self.assertIn("• Telur 3 kotak  ⚠️ confirm qty (1 data point)", text)
        self.assertIn("1 line(s) need the cashier to confirm", text)
        self.assertIn("nothing bought on a Friday", op.format_director("Sek 7", TARGET, []))


if __name__ == "__main__":
    unittest.main()
