"""Unit tests for ``missing_bills``.

Hermetic — the detection layer is fed plain row dicts (no DB), and the
DB read is exercised against a tiny fake client following the pattern
from ``test_price_spike_detection``.

Run with::

    python -m unittest tests.test_missing_bills
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from missing_bills import (  # noqa: E402
    find_missing_bills,
    format_missing_bill_message,
    format_owner_summary,
    load_supplier_bill_rows,
    overdue_threshold_days,
)

TODAY = date(2026, 8, 6)
CHAT = -1001


def bills(supplier, dates, chat_id=CHAT, outlet="SEK 20"):
    return [
        {
            "chat_id": chat_id,
            "outlet": outlet,
            "merchant": supplier,
            "receipt_date": d,
        }
        for d in dates
    ]


def every_n_days(n, *, last, count):
    """``count`` dates ending at ``last``, spaced ``n`` days apart."""
    return [last - timedelta(days=n * i) for i in range(count - 1, -1, -1)]


class FindMissingBills(unittest.TestCase):

    def test_regular_supplier_gone_quiet_is_flagged(self):
        # Every 3 days, 10 bills, then 13 days of silence (> 2x gap).
        rows = bills("BESTARI FARM", every_n_days(3, last=TODAY - timedelta(days=13), count=10))
        out = find_missing_bills(rows, today=TODAY)
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["supplier"], "BESTARI FARM")
        self.assertEqual(entry["chat_id"], CHAT)
        self.assertEqual(entry["days_missing"], 13)
        self.assertEqual(entry["median_gap_days"], 3.0)
        self.assertEqual(entry["threshold_days"], 6)
        self.assertEqual(entry["days_overdue"], 7)

    def test_supplier_on_rhythm_is_not_flagged(self):
        rows = bills("BESTARI FARM", every_n_days(3, last=TODAY - timedelta(days=2), count=10))
        self.assertEqual(find_missing_bills(rows, today=TODAY), [])

    def test_silence_within_threshold_is_tolerated(self):
        # Gap 3 -> threshold 6; exactly 6 days quiet is still fine.
        rows = bills("BESTARI FARM", every_n_days(3, last=TODAY - timedelta(days=6), count=10))
        self.assertEqual(find_missing_bills(rows, today=TODAY), [])

    def test_too_few_bills_never_chased(self):
        # 5 bills < _MIN_BILLS: a stall visited a few times is not "regular".
        rows = bills("KEDAI RUNCIT", every_n_days(3, last=TODAY - timedelta(days=20), count=5))
        self.assertEqual(find_missing_bills(rows, today=TODAY), [])

    def test_very_old_silence_is_a_dropped_supplier_not_a_lost_bill(self):
        rows = bills("OLD SUPPLIER", every_n_days(3, last=TODAY - timedelta(days=46), count=10))
        self.assertEqual(find_missing_bills(rows, today=TODAY), [])

    def test_irregular_supplier_not_chased(self):
        # Erratic gaps (cv too high) -> no trustworthy rhythm -> no chase.
        last = TODAY - timedelta(days=20)
        offsets = [70, 69, 40, 39, 38, 12, 1, 0]
        rows = bills("SPORADIC", [last - timedelta(days=o) for o in offsets])
        self.assertEqual(find_missing_bills(rows, today=TODAY), [])

    def test_ocr_spelling_variants_collapse_to_one_supplier(self):
        dates = every_n_days(3, last=TODAY - timedelta(days=13), count=10)
        rows = []
        for i, d in enumerate(dates):
            name = "BESTARI FARM (M) SDN BHD" if i % 2 else "BESTARI FARM"
            rows += bills(name, [d])
        out = find_missing_bills(rows, today=TODAY)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["supplier"], "BESTARI FARM")

    def test_same_supplier_tracked_per_chat(self):
        quiet = every_n_days(3, last=TODAY - timedelta(days=13), count=10)
        active = every_n_days(3, last=TODAY - timedelta(days=1), count=10)
        rows = bills("BESTARI FARM", quiet, chat_id=-1) + bills(
            "BESTARI FARM", active, chat_id=-2
        )
        out = find_missing_bills(rows, today=TODAY)
        self.assertEqual([e["chat_id"] for e in out], [-1])

    def test_ask_today_paces_the_chasing(self):
        # Fixed history; the silence grows as ``today`` moves forward.
        rows = bills(
            "BESTARI FARM",
            every_n_days(3, last=TODAY - timedelta(days=13), count=10),
        )

        def entry_on(day):
            out = find_missing_bills(rows, today=day)
            return out[0] if out else None

        # days_overdue 7 -> (7-1) % 3 == 0 -> ask; next day 8 -> quiet.
        self.assertTrue(entry_on(TODAY)["ask_today"])
        self.assertFalse(entry_on(TODAY + timedelta(days=1))["ask_today"])
        self.assertFalse(entry_on(TODAY + timedelta(days=2))["ask_today"])
        self.assertTrue(entry_on(TODAY + timedelta(days=3))["ask_today"])

    def test_garbage_rows_never_raise(self):
        try:
            self.assertEqual(find_missing_bills(None, today=TODAY), [])
            self.assertEqual(find_missing_bills([], today=TODAY), [])
            self.assertEqual(
                find_missing_bills(["junk", 42, {}], today=TODAY), []
            )
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"find_missing_bills raised: {e}")


class OverdueThreshold(unittest.TestCase):

    def test_daily_supplier_gets_grace_days(self):
        # Gap 1: max(2, 1+2) = 3 — one long weekend never trips it.
        self.assertEqual(overdue_threshold_days(1.0), 3)

    def test_weekly_supplier_waits_two_cycles(self):
        self.assertEqual(overdue_threshold_days(7.0), 14)


class FormatMessages(unittest.TestCase):

    def _entry(self):
        return {
            "chat_id": CHAT,
            "outlet": "SEK 20",
            "supplier": "BESTARI FARM",
            "last_date": date(2026, 7, 24),
            "days_missing": 13,
            "median_gap_days": 3.0,
            "sample_count": 10,
            "threshold_days": 6,
            "days_overdue": 7,
            "ask_today": True,
        }

    def test_chat_question_is_tamil_plus_malay(self):
        out = format_missing_bill_message(self._entry())
        self.assertIn("🧾 BESTARI FARM bill எங்க?", out)
        self.assertIn("~3 நாளுக்கு ஒரு தடவை", out)
        self.assertIn("கடைசி bill: 24/07", out)
        self.assertIn("13 நாளா ஒன்னும் இல்ல", out)
        self.assertIn("ஏன் upload பண்ணல?", out)
        self.assertIn("Supplier மாத்திட்டீங்களா", out)
        self.assertIn("Resit BESTARI FARM dah 13 hari tak upload", out)

    def test_daily_supplier_reads_thinamum(self):
        entry = self._entry()
        entry["median_gap_days"] = 1.0
        self.assertIn("வழக்கமா தினமும் BESTARI FARM", format_missing_bill_message(entry))

    def test_owner_summary_lists_every_quiet_supplier(self):
        chased = self._entry()
        waiting = dict(self._entry(), supplier="ANI TRADING", ask_today=False)
        out = format_owner_summary([chased, waiting])
        self.assertIn("BESTARI FARM", out)
        self.assertIn("❓ asked today", out)
        self.assertIn("ANI TRADING", out)
        self.assertIn("⏳ chased recently", out)

    def test_empty_or_garbage_never_raises(self):
        try:
            self.assertEqual(format_missing_bill_message(None), "")
            self.assertEqual(format_missing_bill_message({}), "")
            self.assertEqual(format_owner_summary([]), "")
            self.assertEqual(format_owner_summary(["junk"]), "")
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"formatters raised: {e}")


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._gte = {}

    def select(self, *_):
        return self

    def gte(self, col, val):
        self._gte[col] = val
        return self

    def order(self, *_, **__):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = [
            r for r in self._rows
            if str(r.get("receipt_date") or "") >= self._gte.get("receipt_date", "")
        ]
        start, end = getattr(self, "_range", (0, len(rows) - 1))
        return FakeResult(rows[start:end + 1])


class FakeSupabase:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "receipts"
        return FakeQuery(self.rows)


class LoadSupplierBillRows(unittest.TestCase):

    def test_filters_non_supplier_types_and_own_outlets(self):
        rows = [
            {"id": 1, "chat_id": CHAT, "outlet": "SEK 20",
             "merchant": "BESTARI FARM", "receipt_date": "2026-08-01",
             "receipt_type": "SUPPLIER_PURCHASE"},
            {"id": 2, "chat_id": CHAT, "outlet": "SEK 20",
             "merchant": "UNCLASSIFIED SHOP", "receipt_date": "2026-08-02",
             "receipt_type": None},
            {"id": 3, "chat_id": CHAT, "outlet": "SEK 20",
             "merchant": "TNB", "receipt_date": "2026-08-02",
             "receipt_type": "UTILITY"},
            {"id": 4, "chat_id": CHAT, "outlet": "SEK 20",
             "merchant": "KHULAFA VISTA", "receipt_date": "2026-08-03",
             "receipt_type": "SUPPLIER_PURCHASE"},
            {"id": 5, "chat_id": None, "outlet": "SEK 20",
             "merchant": "NO CHAT", "receipt_date": "2026-08-03",
             "receipt_type": "SUPPLIER_PURCHASE"},
            {"id": 6, "chat_id": CHAT, "outlet": "SEK 20",
             "merchant": "FUTURE", "receipt_date": "2027-01-01",
             "receipt_type": "SUPPLIER_PURCHASE"},
        ]
        out = load_supplier_bill_rows(FakeSupabase(rows), today=TODAY)
        kept = sorted(r["merchant"] for r in out)
        # The unclassified shop is kept (UNKNOWN is not evidence against it);
        # utility, own-outlet, chatless and future-dated rows are dropped.
        self.assertEqual(kept, ["BESTARI FARM", "UNCLASSIFIED SHOP"])
        self.assertEqual(out[0]["receipt_date"], date(2026, 8, 1))

    def test_db_failure_returns_empty_no_raise(self):
        class Boom:
            def table(self, name):
                raise RuntimeError("boom")

        try:
            self.assertEqual(load_supplier_bill_rows(Boom(), today=TODAY), [])
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"load_supplier_bill_rows raised: {e}")


if __name__ == "__main__":
    unittest.main()
