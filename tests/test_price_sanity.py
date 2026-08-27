"""Unit tests for ``price_sanity`` — the issue #79 ingestion gate.

Pure rule evaluation, in-memory median stats, the history fetcher against
a hand-rolled fake client, and the /price_quarantine formatter.

Run with::

    python -m unittest tests.test_price_sanity
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import price_sanity  # noqa: E402
from price_sanity import (  # noqa: E402
    REASON_EXCEEDS_RECEIPT,
    REASON_FUTURE_DATE,
    REASON_LINE_TOTAL_CEILING,
    REASON_NON_POSITIVE,
    REASON_PRICE_CEILING,
    REASON_PRICE_VS_HISTORY,
    REASON_QTY_CEILING,
    REASON_QTY_VS_HISTORY,
    evaluate_record,
    fetch_history_stats,
    format_quarantine_rows,
    median_stats,
    partition_records,
)

TODAY = date(2026, 6, 11)


def _rec(qty=5.0, unit_price=2.50, line_total=None):
    return {
        "raw_item_name": "AIS",
        "canonical_item": "ais",
        "qty": qty,
        "unit_price": unit_price,
        "line_total": qty * unit_price if line_total is None else line_total,
    }


class EvaluateRecord(unittest.TestCase):

    def test_clean_row_passes(self):
        self.assertEqual(
            evaluate_record(_rec(), receipt_date="2026-06-02", today=TODAY), []
        )

    def test_receipt_2254_column_merge_rejected(self):
        # The live finding from issue #79: qty=40250, unit_price=100,
        # line_total=RM4,025,000 on a receipt whose siblings totalled ~RM100.
        reasons = evaluate_record(
            _rec(qty=40250.0, unit_price=100.0, line_total=4_025_000.0),
            receipt_date="2026-06-02",
            receipt_total=100.0,
            today=TODAY,
        )
        self.assertIn(REASON_QTY_CEILING, reasons)
        self.assertIn(REASON_LINE_TOTAL_CEILING, reasons)
        self.assertIn(REASON_EXCEEDS_RECEIPT, reasons)

    def test_future_receipt_date_rejected(self):
        # The 2026-06-21 row that sat in item_prices 10 days early.
        reasons = evaluate_record(
            _rec(), receipt_date="2026-06-21", today=TODAY
        )
        self.assertEqual(reasons, [REASON_FUTURE_DATE])

    def test_today_is_not_future(self):
        self.assertEqual(
            evaluate_record(_rec(), receipt_date=TODAY.isoformat(), today=TODAY),
            [],
        )

    def test_unparseable_date_passes_date_check(self):
        # Garbage dates are someone else's problem (date_utils clamps them
        # upstream) — the gate only rejects a *confirmed* future date.
        self.assertEqual(
            evaluate_record(_rec(), receipt_date="not-a-date", today=TODAY), []
        )

    def test_unit_price_ceiling(self):
        reasons = evaluate_record(
            _rec(qty=1.0, unit_price=6000.0), today=TODAY
        )
        self.assertIn(REASON_PRICE_CEILING, reasons)

    def test_non_positive_rejected(self):
        self.assertIn(
            REASON_NON_POSITIVE,
            evaluate_record(_rec(qty=0.0), today=TODAY),
        )
        self.assertIn(
            REASON_NON_POSITIVE,
            evaluate_record(_rec(unit_price=-2.5), today=TODAY),
        )

    def test_line_exceeds_receipt_total_only_above_floor(self):
        # RM250 line on a receipt whose total was misread to RM40 must NOT
        # quarantine (below the RM1,000 floor)…
        below = evaluate_record(
            _rec(qty=100.0, unit_price=2.50), receipt_total=40.0, today=TODAY
        )
        self.assertEqual(below, [])
        # …but a RM2,500 line on a RM100 receipt must.
        above = evaluate_record(
            _rec(qty=1000.0, unit_price=2.50), receipt_total=100.0, today=TODAY
        )
        self.assertIn(REASON_EXCEEDS_RECEIPT, above)

    def test_history_price_outlier(self):
        history = {"median_price": 2.50, "price_samples": 10,
                   "median_qty": 45.0, "qty_samples": 10}
        reasons = evaluate_record(
            _rec(qty=40.0, unit_price=100.0), history=history, today=TODAY
        )
        self.assertIn(REASON_PRICE_VS_HISTORY, reasons)

    def test_history_qty_outlier(self):
        history = {"median_price": 2.50, "price_samples": 10,
                   "median_qty": 45.0, "qty_samples": 10}
        reasons = evaluate_record(
            _rec(qty=950.0, unit_price=2.50), history=history, today=TODAY
        )
        self.assertIn(REASON_QTY_VS_HISTORY, reasons)

    def test_history_needs_min_samples(self):
        # qty 60 is >20x the median of 2 and price RM30 is >10x the median
        # of RM2.50, but with only 2 samples the median isn't trusted yet.
        history = {"median_price": 2.50, "price_samples": 2,
                   "median_qty": 2.0, "qty_samples": 2}
        self.assertEqual(
            evaluate_record(
                _rec(qty=60.0, unit_price=30.0), history=history, today=TODAY
            ),
            [],
        )

    def test_genuine_price_hike_passes_history_check(self):
        # 2x is a real-world hike, not garbage — must survive the 10x bound.
        history = {"median_price": 18.0, "price_samples": 10,
                   "median_qty": 8.0, "qty_samples": 10}
        self.assertEqual(
            evaluate_record(
                _rec(qty=8.0, unit_price=36.0), history=history, today=TODAY
            ),
            [],
        )


class PartitionRecords(unittest.TestCase):

    def test_splits_clean_and_rejected(self):
        good = _rec()
        bad = _rec(qty=40250.0, unit_price=100.0, line_total=4_025_000.0)
        clean, rejected = partition_records(
            [good, bad], receipt_date="2026-06-02", receipt_total=100.0,
            today=TODAY,
        )
        self.assertEqual(clean, [good])
        self.assertEqual(len(rejected), 1)
        rec, reasons = rejected[0]
        self.assertIs(rec, bad)
        self.assertIn(REASON_QTY_CEILING, reasons)

    def test_history_by_item_scopes_per_canonical(self):
        history = {"ais": {"median_price": 2.50, "price_samples": 10,
                           "median_qty": 45.0, "qty_samples": 10}}
        spiky = _rec(qty=40.0, unit_price=100.0)
        clean, rejected = partition_records(
            [spiky], history_by_item=history, today=TODAY
        )
        self.assertEqual(clean, [])
        self.assertEqual(len(rejected), 1)

    def test_non_dict_entries_skipped(self):
        clean, rejected = partition_records(
            ["garbage", None, _rec()], today=TODAY
        )
        self.assertEqual(len(clean), 1)
        self.assertEqual(rejected, [])


class MedianStats(unittest.TestCase):

    def test_medians_from_clean_rows(self):
        rows = [
            {"qty": 40.0, "unit_price": 2.50},
            {"qty": 45.0, "unit_price": 2.50},
            {"qty": 50.0, "unit_price": 3.00},
        ]
        stats = median_stats(rows)
        self.assertAlmostEqual(stats["median_price"], 2.50)
        self.assertAlmostEqual(stats["median_qty"], 45.0)
        self.assertEqual(stats["price_samples"], 3)

    def test_garbage_rows_excluded_from_median(self):
        # A poisoned historical row (qty 40250) must not stretch the median
        # that is supposed to catch the next poisoned row.
        rows = [
            {"qty": 40.0, "unit_price": 2.50},
            {"qty": 45.0, "unit_price": 2.50},
            {"qty": 40250.0, "unit_price": 100_000.0},
        ]
        stats = median_stats(rows)
        self.assertEqual(stats["qty_samples"], 2)
        self.assertEqual(stats["price_samples"], 2)
        self.assertAlmostEqual(stats["median_qty"], 42.5)

    def test_empty_rows(self):
        stats = median_stats([])
        self.assertIsNone(stats["median_price"])
        self.assertEqual(stats["price_samples"], 0)


class _FakeQuery:
    def __init__(self, parent):
        self.parent = parent

    def select(self, *_):
        return self

    def eq(self, _col, value):
        self.parent.queried_items.append(value)
        return self

    def gte(self, *_):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        if self.parent.raise_on_execute:
            raise RuntimeError("boom")

        class R:
            data = self.parent.rows

        return R()


class _FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.queried_items = []
        self.raise_on_execute = False

    def table(self, _name):
        return _FakeQuery(self)


class FetchHistoryStats(unittest.TestCase):

    def test_stats_per_item(self):
        client = _FakeClient(
            [{"qty": 40.0, "unit_price": 2.50}, {"qty": 50.0, "unit_price": 3.50}]
        )
        stats = fetch_history_stats(client, ["ais", "ais", None, "", 42])
        self.assertEqual(client.queried_items, ["ais"])  # deduped, non-strings dropped
        self.assertIn("ais", stats)
        self.assertAlmostEqual(stats["ais"]["median_price"], 3.00)

    def test_query_failure_yields_no_stats_and_never_raises(self):
        client = _FakeClient([])
        client.raise_on_execute = True
        self.assertEqual(fetch_history_stats(client, ["ais"]), {})


class FormatQuarantineRows(unittest.TestCase):

    def test_empty_state_message(self):
        self.assertIn("kosong", format_quarantine_rows([]))

    def test_rows_render_reasons_and_values(self):
        text = format_quarantine_rows([
            {
                "id": 1,
                "receipt_id": 2254,
                "receipt_date": "2026-06-02",
                "outlet_code": "SEK14",
                "merchant": "EVEREST",
                "raw_item_name": "AIS",
                "qty": 40250,
                "unit_price": 100,
                "line_total": 4025000,
                "reasons": "qty_above_ceiling,line_total_above_ceiling",
                "source": "ingest",
            }
        ])
        self.assertIn("resit 2254", text)
        self.assertIn("40,250", text)
        self.assertIn("qty_above_ceiling", text)
        self.assertIn("EVEREST", text)


if __name__ == "__main__":
    unittest.main()
