"""Self-checking bill reading (ocr_smart): every change must be backed by the
bill's own arithmetic or by the calendar, and a reading that already adds up
must never be made worse."""
import asyncio
import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr_smart as smart  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 11, 21)


def bill(total=100.0, **extra):
    parsed = {
        "merchant": "SAIDA",
        "total": total,
        "items": [
            {"name": "Bawang", "qty": 5, "price": 8.0, "line_total": 40.0},
            {"name": "Jintan", "qty": 2, "price": 30.0, "line_total": 60.0},
        ],
        "raw_text": "SAIDA\nBawang 5 8.00 40.00\nJintan 2 30.00 60.00\nTOTAL 100.00",
    }
    parsed.update(extra)
    return parsed


def run(coro):
    return asyncio.run(coro)


class State(unittest.TestCase):
    def test_match_conflict_unknown(self):
        self.assertEqual(smart.parsed_state(bill()), "match")
        self.assertEqual(smart.parsed_state(bill(total=120.0)), "conflict")
        self.assertEqual(smart.parsed_state(bill(total=None)), "unknown")
        self.assertEqual(smart.parsed_state({"total": 50, "items": [{"name": "x"}]}), "unknown")

    def test_string_total_and_foc_lines(self):
        parsed = bill(total="RM100.00")
        parsed["items"].append({"name": "Ais FOC", "qty": 1, "price": 9.0})
        self.assertEqual(smart.parsed_state(parsed), "match")

    def test_adjustments_count_towards_the_total(self):
        parsed = bill(total=106.0, adjustments=[{"label": "SST", "amount": 6.0}])
        self.assertEqual(smart.parsed_state(parsed), "match")
        self.assertEqual(smart.math_total(parsed), 100.0)


class RepairLines(unittest.TestCase):
    def test_price_decimal_slip_fixed_from_line_total(self):
        items = [{"name": "Santan", "qty": 2, "price": 1150, "line_total": 23.0}]
        fixed, notes = smart.repair_lines(items, 23.0)
        self.assertEqual(fixed[0]["price"], 11.5)
        self.assertEqual(len(notes), 1)
        self.assertEqual(items[0]["price"], 1150)  # input untouched

    def test_qty_decimal_slip(self):
        fixed, notes = smart.repair_lines(
            [{"name": "Ayam", "qty": 472, "price": 11.5, "line_total": 542.8}], 542.8)
        self.assertEqual(fixed[0]["qty"], 47.2)
        self.assertTrue(notes)

    def test_ambiguous_decimal_slip_left_for_the_reread(self):
        items = [{"name": "Minyak", "qty": 10, "price": 1150, "line_total": 1150.0}]
        fixed, notes = smart.repair_lines(items, 1150.0)
        self.assertEqual(fixed, items)
        self.assertEqual(notes, [])

    def test_missing_price_and_missing_qty_filled(self):
        fixed, _ = smart.repair_lines([
            {"name": "Gula", "qty": 4, "price": None, "line_total": 14.0},
            {"name": "Ikan", "qty": None, "price": 12.0, "line_total": 30.0},
        ])
        self.assertEqual(fixed[0]["price"], 3.5)
        self.assertEqual(fixed[1]["qty"], 2.5)

    def test_a_non_decimal_mismatch_is_left_alone(self):
        items = [{"name": "Bawang", "qty": 3, "price": 8.0, "line_total": 40.0}]
        fixed, notes = smart.repair_lines(items, 40.0)
        self.assertEqual(fixed, items)
        self.assertEqual(notes, [])
        self.assertEqual(smart.suspect_lines(items), [0])

    def test_consistent_lines_untouched(self):
        parsed = bill()
        fixed, notes = smart.repair_lines(parsed["items"], 100.0)
        self.assertEqual(fixed, parsed["items"])
        self.assertEqual(notes, [])

    def test_repair_never_breaks_a_bill_that_adds_up(self):
        # The printed line total was the misread; qty x price is right.
        items = [{"name": "Beras", "qty": 2, "price": 50.0, "line_total": 10.0}]
        fixed, notes = smart.repair_lines(items, 100.0)
        self.assertEqual(fixed[0]["price"], 50.0)
        self.assertEqual(notes, [])


class FixDate(unittest.TestCase):
    def test_recent_date_kept(self):
        self.assertEqual(smart.fix_date("2026-11-19", TODAY), ("2026-11-19", None))

    def test_future_day_month_swap(self):
        iso, note = smart.fix_date("2026-12-11", TODAY)
        self.assertEqual(iso, "2026-11-12")
        self.assertIn("2026-12-11", note)

    def test_future_year(self):
        self.assertEqual(smart.fix_date("2027-11-20", TODAY)[0], "2026-11-20")

    def test_old_date_needs_the_transcription_to_agree(self):
        # A genuine late upload stays; the same date with the bill itself
        # showing 09/11/2026 is a day/month mix-up.
        self.assertEqual(smart.fix_date("2026-09-11", TODAY), ("2026-09-11", None))
        iso, note = smart.fix_date("2026-09-11", TODAY, "Tarikh: 09/11/2026")
        self.assertEqual(iso, "2026-11-09")
        self.assertTrue(note)

    def test_last_year_typo_with_transcription(self):
        iso, _ = smart.fix_date("2025-11-20", TODAY, "Date 20/11/26")
        self.assertEqual(iso, "2026-11-20")

    def test_no_believable_candidate_means_no_change(self):
        self.assertEqual(smart.fix_date("2027-03-25", TODAY), ("2027-03-25", None))
        self.assertEqual(smart.fix_date(None, TODAY), (None, None))
        self.assertEqual(smart.fix_date("garbage", TODAY), ("garbage", None))


class PreCheck(unittest.TestCase):
    def test_clean_bill_is_untouched(self):
        parsed = bill(receipt_date="2026-11-20")
        before = {k: (list(v) if isinstance(v, list) else v) for k, v in parsed.items()}
        self.assertEqual(smart.pre_check(parsed, TODAY), [])
        self.assertEqual(parsed, before)

    def test_total_decimal_point(self):
        parsed = bill(total=10000.0)
        notes = smart.pre_check(parsed, TODAY)
        self.assertEqual(parsed["total"], 100.0)
        self.assertTrue(any("decimal" in n for n in notes))

    def test_weighed_line_takes_the_weight_when_that_adds_up(self):
        parsed = {
            "total": 542.8,
            "items": [{"name": "AYAM", "qty": 30, "price": 11.5, "line_total": 542.8}],
            "raw_text": "30 AYAM 47.2 11.50 542.80",
        }
        smart.pre_check(parsed, TODAY)
        self.assertEqual(parsed["items"][0]["qty"], 47.2)
        self.assertEqual(smart.parsed_state(parsed), "match")

    def test_date_fix_sets_both_keys(self):
        parsed = bill(date="2026-12-11")
        smart.pre_check(parsed, TODAY)
        self.assertEqual(parsed["receipt_date"], "2026-11-12")
        self.assertEqual(parsed["date"], "2026-11-12")


class ArbitrateCorrections(unittest.TestCase):
    def test_total_correction_refused_when_the_bill_adds_up(self):
        out, refused = smart.arbitrate_corrections(
            bill(), {"total": 106.0, "merchant": "SAIDA TRADING"})
        self.assertEqual(out, {"merchant": "SAIDA TRADING"})
        self.assertEqual(len(refused), 1)

    def test_correction_that_makes_the_bill_add_up_is_applied(self):
        out, refused = smart.arbitrate_corrections(bill(total=160.0), {"total": "RM100.00"})
        self.assertEqual(out, {"total": "RM100.00"})
        self.assertEqual(refused, [])

    def test_smallest_fix_wins(self):
        # The total alone fixes it; the items "correction" would break it.
        parsed = bill(total=160.0)
        out, refused = smart.arbitrate_corrections(parsed, {
            "total": 100.0,
            "items": [{"name": "Bawang", "qty": 5, "price": 9.0},
                      {"name": "Jintan", "qty": 2, "price": 30.0}],
        })
        self.assertEqual(list(out), ["total"])
        self.assertTrue(refused)

    def test_partial_item_list_refused(self):
        parsed = bill(total=120.0)
        out, refused = smart.arbitrate_corrections(
            parsed, {"items": [{"name": "Bawang", "qty": 5, "price": 9.0}]})
        self.assertNotIn("items", out)
        self.assertIn("1 lines would replace 2", refused[0])

    def test_unverifiable_correction_applied_as_before(self):
        parsed = {"total": 50.0, "items": [{"name": "x"}]}
        out, refused = smart.arbitrate_corrections(parsed, {"total": 55.0})
        self.assertEqual(out, {"total": 55.0})
        self.assertEqual(refused, [])

    def test_bad_input(self):
        self.assertEqual(smart.arbitrate_corrections(bill(), None), ({}, []))
        out, refused = smart.arbitrate_corrections(bill(), {"items": [], "date": "20/11/26"})
        self.assertEqual(out, {"date": "20/11/26"})
        self.assertEqual(refused, ["items (empty)"])


class Reread(unittest.TestCase):
    def test_prompt_names_the_gap_and_the_suspect_line(self):
        parsed = bill(total=120.0)
        parsed["items"][0]["line_total"] = 60.0
        prompt = smart.build_reread_prompt(parsed)
        self.assertIn("add up to 100.00", prompt)
        self.assertIn("120.00", prompt)
        self.assertIn("line total: 1.", prompt)

    def test_accepts_a_line_fix_that_keeps_the_total(self):
        parsed = bill(total=116.0)  # really 7 x 8 = 56, first read 5 x 8
        ok, notes = smart.accept_reread(parsed, {
            "total": 116.0,
            "items": [{"name": "Bawang", "qty": 7, "price": 8.0, "total": 56.0},
                      {"name": "Jintan", "qty": 2, "price": 30.0, "total": 60.0}],
        })
        self.assertTrue(ok)
        self.assertEqual(parsed["items"][0]["qty"], 7)
        self.assertEqual(smart.parsed_state(parsed), "match")
        self.assertTrue(notes)

    def test_accepts_a_total_fix_that_keeps_the_lines(self):
        parsed = bill(total=700.0)
        ok, notes = smart.accept_reread(parsed, {"total": 100.0, "items": parsed["items"]})
        self.assertTrue(ok)
        self.assertEqual(parsed["total"], 100.0)
        self.assertEqual(notes, ["total 700→100"])

    def test_rejects_when_both_sides_change(self):
        parsed = bill(total=120.0)
        before = dict(parsed)
        ok, _ = smart.accept_reread(parsed, {
            "total": 90.0, "items": [{"name": "Bawang", "qty": 3, "price": 30.0}]})
        self.assertFalse(ok)
        self.assertEqual(parsed, before)

    def test_rejects_when_it_still_does_not_add_up(self):
        parsed = bill(total=120.0)
        self.assertFalse(smart.accept_reread(parsed, {"total": 120.0, "items": parsed["items"]})[0])

    def test_rejects_a_line_that_contradicts_its_own_line_total(self):
        parsed = bill(total=120.0)
        ok, _ = smart.accept_reread(parsed, {
            "total": 120.0,
            "items": [{"name": "Bawang", "qty": 5, "price": 12.0, "total": 40.0},
                      {"name": "Jintan", "qty": 2, "price": 30.0, "total": 60.0}],
        })
        self.assertFalse(ok)

    def test_tax_line_accepted_only_when_the_first_reading_saw_it(self):
        answer = {"total": 106.0, "adjustments": [{"label": "SST 6%", "amount": 6.0}]}
        invented = bill(total=106.0)
        answer["items"] = invented["items"]
        self.assertFalse(smart.accept_reread(invented, answer)[0])

        printed = bill(total=106.0)
        printed["raw_text"] += "\nSST 6% 6.00"
        ok, notes = smart.accept_reread(printed, answer)
        self.assertTrue(ok)
        self.assertEqual(printed["adjustments"], [{"label": "SST 6%", "amount": 6.0}])
        self.assertEqual(smart.math_total(printed), 100.0)
        self.assertIn("SST 6% +6.00", notes)

    def test_rounding_of_a_few_sen_needs_no_label(self):
        parsed = bill(total=100.05)
        parsed["items"][0]["price"] = 8.02  # 40.10 + 60 = 100.10 vs 100.05
        parsed["items"][0]["line_total"] = 40.1
        self.assertEqual(smart.parsed_state(parsed), "match")  # within 5 sen already

    def test_oversized_adjustment_rejected(self):
        parsed = bill(total=200.0)
        parsed["raw_text"] += "\ndiscount"
        ok, _ = smart.accept_reread(parsed, {
            "total": 200.0, "items": parsed["items"],
            "adjustments": [{"label": "discount", "amount": 100.0}]})
        self.assertFalse(ok)

    def test_garbage_answers(self):
        for answer in (None, "x", {}, {"total": "abc", "items": []}, {"total": 5, "items": "no"}):
            self.assertFalse(smart.accept_reread(bill(total=120.0), answer)[0])


class PostCheck(unittest.TestCase):
    def test_no_reread_when_the_bill_adds_up(self):
        calls = []

        async def reread(prompt):
            calls.append(prompt)

        result = run(smart.post_check(bill(), reread))
        self.assertEqual(calls, [])
        self.assertEqual(result, {"notes": [], "reread": None, "state": "match"})

    def test_reread_accepted(self):
        parsed = bill(total=700.0)

        async def reread(prompt):
            return {"total": 100.0, "items": parsed["items"]}

        result = run(smart.post_check(parsed, reread))
        self.assertEqual(result["reread"], "accepted")
        self.assertEqual(result["state"], "match")
        self.assertEqual(parsed["total"], 100.0)

    def test_reread_rejected_leaves_the_reading_alone(self):
        parsed = bill(total=120.0)

        async def reread(prompt):
            return {"total": 77.0, "items": [{"name": "?", "qty": 1, "price": 77.0}]}

        result = run(smart.post_check(parsed, reread))
        self.assertEqual(result["reread"], "rejected")
        self.assertEqual(parsed["total"], 120.0)
        self.assertEqual(len(parsed["items"]), 2)

    def test_reread_failure_is_swallowed(self):
        async def reread(prompt):
            raise RuntimeError("api down")

        result = run(smart.post_check(bill(total=120.0), reread))
        self.assertEqual(result["reread"], "failed")
        self.assertEqual(result["state"], "conflict")

    def test_reread_switch(self):
        async def reread(prompt):
            raise AssertionError("must not be called")

        with mock.patch.dict(os.environ, {"OCR_REREAD": "off"}):
            self.assertIsNone(run(smart.post_check(bill(total=120.0), reread))["reread"])
        with mock.patch.dict(os.environ, {"OCR_SELF_CHECK": "off"}):
            self.assertFalse(smart.enabled())
            self.assertFalse(smart.reread_enabled())

    def test_nothing_to_check_means_no_reread(self):
        async def reread(prompt):
            raise AssertionError("must not be called")

        parsed = {"total": 50.0, "items": [{"name": "Sewa"}]}
        self.assertIsNone(run(smart.post_check(parsed, reread))["reread"])


class Summarize(unittest.TestCase):
    def test_short_and_capped(self):
        self.assertEqual(smart.summarize([]), "")
        self.assertEqual(smart.summarize(["a", "b"]), "a; b")
        self.assertEqual(smart.summarize(["a", "b", "c", "d", "e"]), "a; b; c; +2 more")


class BotWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            src = f.read()
        start = src.index("async def run_verification(")
        cls.body = src[start:src.index("def _review_keyboard(")]

    def test_steps_run_in_order(self):
        order = [self.body.index(marker) for marker in (
            "ocr_smart.pre_check(", "verify_extraction(", "ocr_smart.arbitrate_corrections(",
            "_apply_corrections(parsed", "ocr_smart.post_check(", "resolve_confidence(")]
        self.assertEqual(order, sorted(order))

    def test_confidence_uses_the_adjusted_total(self):
        self.assertIn("ocr_smart.math_total(parsed)", self.body)

    def test_kill_switch_is_honoured(self):
        self.assertIn("ocr_smart.enabled()", self.body)


if __name__ == "__main__":
    unittest.main()
