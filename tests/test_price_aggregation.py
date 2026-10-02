"""Unit tests for ``price_aggregation``.

Covers both pure-Python extraction (``classify_and_extract_items``) and
the Supabase persistence wrapper (``save_item_prices``) — the latter is
exercised with a hand-rolled fake client so the tests stay hermetic.

Run with::

    python -m unittest tests.test_price_aggregation
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from price_aggregation import classify_and_extract_items, save_item_prices  # noqa: E402


class FakeSupabaseResult:
    def __init__(self, data):
        self.data = data


class FakeInsertChain:
    """Stand-in for the ``.table(...).insert(...).execute()`` chain, plus
    the read chain the issue-#79 history fetch issues (select/eq/gte/limit
    all no-op and execute returns the canned ``history_rows``)."""

    def __init__(self, table_name, parent):
        self.table_name = table_name
        self.parent = parent
        self._payload = None
        self._is_select = False

    def insert(self, payload):
        self._payload = payload
        self.parent.last_insert_payload = payload
        self.parent.last_table = self.table_name
        self.parent.payloads_by_table.setdefault(self.table_name, []).append(payload)
        return self

    def select(self, *_):
        self._is_select = True
        return self

    def eq(self, *_):
        return self

    def gte(self, *_):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        if self._is_select:
            return FakeSupabaseResult(list(self.parent.history_rows))
        if self.parent.raise_on_execute is not None:
            raise self.parent.raise_on_execute
        # Mirror Supabase behavior: insert echoes back the rows inserted.
        return FakeSupabaseResult(self._payload)


class FakeSupabaseClient:
    def __init__(self):
        self.last_insert_payload = None
        self.last_table = None
        self.raise_on_execute = None
        self.payloads_by_table = {}
        self.history_rows = []

    def table(self, name):
        return FakeInsertChain(name, self)


class ClassifyAndExtractItems(unittest.TestCase):

    def test_clean_items_produce_full_records(self):
        items = [
            {"name": "Ayam", "qty": 30, "price": 19.80},
            {"name": "Telur", "qty": 2, "price": 15.0},
        ]
        result = classify_and_extract_items(items)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["raw_item_name"], "Ayam")
        self.assertAlmostEqual(result[0]["qty"], 30.0)
        self.assertAlmostEqual(result[0]["unit_price"], 19.80)
        self.assertAlmostEqual(result[0]["line_total"], 30 * 19.80)
        self.assertEqual(result[1]["raw_item_name"], "Telur")
        self.assertAlmostEqual(result[1]["line_total"], 30.0)

    def test_line_total_is_qty_times_unit_price(self):
        result = classify_and_extract_items(
            [{"name": "SOS CILI", "qty": 7, "price": 6.30}]
        )
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["line_total"], 44.10)

    def test_raw_item_name_preserves_original_ocr_string(self):
        # Mixed case + whitespace + supplier code -> kept verbatim.
        original = "  Li Agam 4 KG  "
        result = classify_and_extract_items(
            [{"name": original, "qty": 1, "price": 12.5}]
        )
        self.assertEqual(result[0]["raw_item_name"], original)

    def test_canonical_item_uses_canonicalize_item(self):
        # "AYAM" is a known canonical category in canonical_items_v2.json
        # — confirm we delegate to canonicalize_item() rather than echoing
        # the raw name.
        result = classify_and_extract_items(
            [{"name": "AYAM", "qty": 1, "price": 10.0}]
        )
        self.assertEqual(result[0]["canonical_item"], "ayam")

    def test_unknown_item_canonical_is_none_but_record_kept(self):
        # No canonical match -> canonical_item=None, but the row still
        # appears (we collect everything; PR #24 filters downstream).
        result = classify_and_extract_items(
            [{"name": "ZZZ_NONSENSE_PRODUCT", "qty": 1, "price": 1.0}]
        )
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["canonical_item"])
        self.assertEqual(result[0]["raw_item_name"], "ZZZ_NONSENSE_PRODUCT")

    def test_mixed_clean_and_messy_items(self):
        items = [
            {"name": "Ayam", "qty": 30, "price": 19.80},  # clean
            {"name": "Missing qty", "qty": None, "price": 5.0},  # dropped
            {"name": "Missing price", "qty": 3, "price": None},  # dropped
            {"name": None, "qty": 2, "price": 4.0},  # dropped (no name)
            {"name": "  ", "qty": 2, "price": 4.0},  # dropped (blank name)
            {"name": "Telur", "qty": 2, "price": 15.0},  # clean
        ]
        result = classify_and_extract_items(items)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["raw_item_name"], "Ayam")
        self.assertEqual(result[1]["raw_item_name"], "Telur")

    def test_empty_list_returns_empty_list(self):
        self.assertEqual(classify_and_extract_items([]), [])

    def test_all_null_items_returns_empty_list(self):
        items = [
            {"name": "A", "qty": None, "price": None},
            {"name": "B", "qty": None, "price": None},
        ]
        self.assertEqual(classify_and_extract_items(items), [])

    def test_non_list_input_returns_empty_list(self):
        self.assertEqual(classify_and_extract_items(None), [])
        self.assertEqual(classify_and_extract_items("string"), [])
        self.assertEqual(classify_and_extract_items({"name": "x"}), [])

    def test_non_dict_items_skipped(self):
        items = ["bare string", 42, None, {"name": "Ayam", "qty": 1, "price": 5.0}]
        result = classify_and_extract_items(items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["raw_item_name"], "Ayam")

    def test_bool_qty_or_price_rejected(self):
        # ``True``/``False`` are int subclasses in Python; reject them so
        # a stray boolean doesn't masquerade as a real quantity.
        self.assertEqual(
            classify_and_extract_items(
                [{"name": "Ayam", "qty": True, "price": 5.0}]
            ),
            [],
        )
        self.assertEqual(
            classify_and_extract_items(
                [{"name": "Ayam", "qty": 5, "price": False}]
            ),
            [],
        )

    def test_receipt_total_argument_accepted_but_unused(self):
        # Currently unused but reserved for PR #24 reconciliation. Make
        # sure passing it doesn't change behavior or raise.
        items = [{"name": "Ayam", "qty": 1, "price": 5.0}]
        without = classify_and_extract_items(items)
        with_total = classify_and_extract_items(items, receipt_total=5.0)
        self.assertEqual(without, with_total)

    def test_never_raises_on_garbage_input(self):
        # The wrapper around canonicalize_item should swallow whatever
        # weirdness the OCR throws at us — assert no exception even on
        # nested junk.
        try:
            classify_and_extract_items(
                [{"name": "x", "qty": 1, "price": 1.0, "extra": object()}]
            )
        except Exception as e:  # pragma: no cover - safety net
            self.fail(f"classify_and_extract_items raised: {e}")


class SaveItemPrices(unittest.TestCase):

    def _records(self):
        return [
            {
                "raw_item_name": "Ayam",
                "canonical_item": "ayam",
                "qty": 30.0,
                "unit_price": 19.80,
                "line_total": 594.0,
            },
            {
                "raw_item_name": "Telur",
                "canonical_item": "telur",
                "qty": 2.0,
                "unit_price": 15.0,
                "line_total": 30.0,
            },
        ]

    def test_inserts_rows_into_item_prices(self):
        client = FakeSupabaseClient()
        count = save_item_prices(
            client,
            receipt_id=123,
            receipt_date="2026-05-13",
            outlet_code="SEK14",
            chat_id=-100123,
            merchant="BESTARI FARM",
            price_records=self._records(),
        )
        self.assertEqual(count, 2)
        self.assertEqual(client.last_table, "item_prices")
        payload = client.last_insert_payload
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["receipt_id"], 123)
        self.assertEqual(payload[0]["receipt_date"], "2026-05-13")
        self.assertEqual(payload[0]["outlet_code"], "SEK14")
        self.assertEqual(payload[0]["chat_id"], -100123)
        self.assertEqual(payload[0]["merchant"], "BESTARI FARM")
        self.assertEqual(payload[0]["raw_item_name"], "Ayam")
        self.assertEqual(payload[0]["canonical_item"], "ayam")
        self.assertAlmostEqual(payload[0]["qty"], 30.0)
        self.assertAlmostEqual(payload[0]["unit_price"], 19.80)
        self.assertAlmostEqual(payload[0]["line_total"], 594.0)

    def test_empty_records_short_circuits_to_zero(self):
        client = FakeSupabaseClient()
        with self.assertLogs("price_aggregation", level="WARNING"):
            count = save_item_prices(
                client,
                receipt_id=1,
                receipt_date="2026-05-13",
                outlet_code=None,
                chat_id=1,
                merchant="X",
                price_records=[],
            )
        self.assertEqual(count, 0)
        # Empty input should NOT touch the client at all.
        self.assertIsNone(client.last_insert_payload)

    def test_insert_failure_returns_zero_and_logs(self):
        client = FakeSupabaseClient()
        client.raise_on_execute = RuntimeError("connection refused")
        with self.assertLogs("price_aggregation", level="ERROR"):
            count = save_item_prices(
                client,
                receipt_id=42,
                receipt_date="2026-05-13",
                outlet_code="SEK14",
                chat_id=1,
                merchant="X",
                price_records=self._records(),
            )
        self.assertEqual(count, 0)

    def test_none_outlet_code_passes_through(self):
        # Unmapped chats return outlet_code=None — we still want the row.
        client = FakeSupabaseClient()
        count = save_item_prices(
            client,
            receipt_id=7,
            receipt_date="2026-05-13",
            outlet_code=None,
            chat_id=1,
            merchant="X",
            price_records=self._records()[:1],
        )
        self.assertEqual(count, 1)
        self.assertIsNone(client.last_insert_payload[0]["outlet_code"])

    def test_uses_mock_client_via_unittest_mock(self):
        # Sanity check that MagicMock works too (some downstream tests
        # in this repo prefer it over hand-rolled fakes).
        client = MagicMock()
        client.table.return_value.insert.return_value.execute.return_value = (
            FakeSupabaseResult([{"id": 1}, {"id": 2}])
        )
        count = save_item_prices(
            client,
            receipt_id=1,
            receipt_date="2026-05-13",
            outlet_code="SEK14",
            chat_id=1,
            merchant="X",
            price_records=self._records(),
            check_history=False,
        )
        self.assertEqual(count, 2)
        client.table.assert_called_once_with("item_prices")


class SanityGate(unittest.TestCase):
    """Issue #79: implausible rows are quarantined, not inserted."""

    def _garbage_record(self):
        # Receipt 2254's OCR column merge.
        return {
            "raw_item_name": "AIS",
            "canonical_item": "ais",
            "qty": 40250.0,
            "unit_price": 100.0,
            "line_total": 4_025_000.0,
        }

    def _clean_record(self):
        return {
            "raw_item_name": "Ayam",
            "canonical_item": "ayam",
            "qty": 30.0,
            "unit_price": 19.80,
            "line_total": 594.0,
        }

    def test_garbage_row_quarantined_clean_row_inserted(self):
        client = FakeSupabaseClient()
        with self.assertLogs("price_aggregation", level="WARNING"):
            count = save_item_prices(
                client,
                receipt_id=2254,
                receipt_date="2026-06-02",
                outlet_code="SEK14",
                chat_id=1,
                merchant="EVEREST",
                price_records=[self._clean_record(), self._garbage_record()],
                receipt_total=100.0,
                check_history=False,
            )
        self.assertEqual(count, 1)
        inserted = client.payloads_by_table["item_prices"][0]
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0]["raw_item_name"], "Ayam")
        quarantined = client.payloads_by_table["item_price_quarantine"][0]
        self.assertEqual(len(quarantined), 1)
        q = quarantined[0]
        self.assertEqual(q["receipt_id"], 2254)
        self.assertEqual(q["raw_item_name"], "AIS")
        self.assertEqual(q["source"], "ingest")
        self.assertIn("qty_above_ceiling", q["reasons"])
        self.assertIn("line_total_exceeds_receipt_total", q["reasons"])

    def test_all_rows_garbage_returns_zero_no_item_prices_insert(self):
        client = FakeSupabaseClient()
        with self.assertLogs("price_aggregation", level="WARNING"):
            count = save_item_prices(
                client,
                receipt_id=2254,
                receipt_date="2026-06-02",
                outlet_code="SEK14",
                chat_id=1,
                merchant="EVEREST",
                price_records=[self._garbage_record()],
                receipt_total=100.0,
                check_history=False,
            )
        self.assertEqual(count, 0)
        self.assertNotIn("item_prices", client.payloads_by_table)
        self.assertIn("item_price_quarantine", client.payloads_by_table)

    def test_future_dated_receipt_rows_quarantined(self):
        # The 2026-06-21 rows from issue #79 — never valid for a purchase.
        from datetime import date, timedelta

        future = (date.today() + timedelta(days=10)).isoformat()
        client = FakeSupabaseClient()
        with self.assertLogs("price_aggregation", level="WARNING"):
            count = save_item_prices(
                client,
                receipt_id=99,
                receipt_date=future,
                outlet_code="SEK14",
                chat_id=1,
                merchant="X",
                price_records=[self._clean_record()],
                check_history=False,
            )
        self.assertEqual(count, 0)
        q = client.payloads_by_table["item_price_quarantine"][0][0]
        self.assertEqual(q["reasons"], "future_receipt_date")

    def test_history_outlier_quarantined_via_fetched_stats(self):
        client = FakeSupabaseClient()
        # 10 historical AIS rows at ~RM2.50: a new RM100 unit price is 40x.
        client.history_rows = [
            {"qty": 45.0, "unit_price": 2.50} for _ in range(10)
        ]
        spiky = {
            "raw_item_name": "AIS",
            "canonical_item": "ais",
            "qty": 40.0,
            "unit_price": 100.0,
            "line_total": 4000.0,
        }
        with self.assertLogs("price_aggregation", level="WARNING"):
            count = save_item_prices(
                client,
                receipt_id=7,
                receipt_date="2026-06-02",
                outlet_code="SEK14",
                chat_id=1,
                merchant="EVEREST",
                price_records=[spiky],
            )
        self.assertEqual(count, 0)
        q = client.payloads_by_table["item_price_quarantine"][0][0]
        self.assertIn("unit_price_vs_history_median", q["reasons"])

    def test_quarantine_insert_failure_does_not_block_clean_rows(self):
        # If the quarantine table is missing (migration not applied yet),
        # clean rows must still store and nothing may raise.
        client = FakeSupabaseClient()
        original_table = client.table

        def flaky_table(name):
            chain = original_table(name)
            if name == "item_price_quarantine":
                chain.parent = _RaisingParent(client)
            return chain

        class _RaisingParent:
            def __init__(self, real):
                self.raise_on_execute = RuntimeError("relation does not exist")
                self.history_rows = real.history_rows
                self.payloads_by_table = real.payloads_by_table
                self.last_insert_payload = None
                self.last_table = None

        client.table = flaky_table
        with self.assertLogs("price_aggregation", level="WARNING"):
            count = save_item_prices(
                client,
                receipt_id=2254,
                receipt_date="2026-06-02",
                outlet_code="SEK14",
                chat_id=1,
                merchant="EVEREST",
                price_records=[self._clean_record(), self._garbage_record()],
                receipt_total=100.0,
                check_history=False,
            )
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()


class WeighedLineTests(unittest.TestCase):
    """AYAM BERLIAN invoices: per-kg lines must carry the weight as qty.
    The five production bills below all add up to their printed total once
    weights are used (they did not before)."""

    def _sum(self, records):
        return round(sum(r["line_total"] for r in records), 2)

    def test_9970_counts_in_name_weights_in_raw_text(self):
        items = [{"qty": None, "name": "AYAM x30 RM11.5", "price": None},
                 {"qty": None, "name": "AYAM Tandori x10 RM11.5", "price": None},
                 {"qty": None, "name": "W.LEG / WING / DRUMSTICK / THIGH x80 RM11.7", "price": None},
                 {"qty": None, "name": "ISI / MINCED / CHOP / FILLET / B.LEG x4 RM12.2", "price": None}]
        raw = ("Kuantiti Quantity Butiran Particulars KG / Qty Harga U.Price Jumlah (RM) Total (RM) "
               "30 AYAM 47 11.50 540.50 AYAM 10 AYAM Tandori 16.6 11.50 190.90 W.LEG / WING / DRUMSTICK / "
               "THIGH 22.7 11.70 265.59 W.LEG 1P ISI / MINCED / CHOP / FILLET / B.LEG 4kg 12.20 48.80 "
               "Total Jumlah 1045.79")
        recs = classify_and_extract_items(items, 1045.79, raw)
        self.assertEqual([r["qty"] for r in recs], [47.0, 16.6, 22.7, 4.0])
        self.assertEqual(self._sum(recs), 1045.79)

    def test_9931_weight_in_name(self):
        items = [{"qty": None, "name": "AYAM (47.2 KG) RM11.50", "price": None},
                 {"qty": None, "name": "AYAM Tandori (15.10 KG) RM11.50", "price": None},
                 {"qty": None, "name": "W.LFG / WING / DRUMSTICK / THIGH (11.30 KG) RM11.70", "price": None},
                 {"qty": None, "name": "IF / MINCED / CHOP / FILLET / B.LEG (8kg) RM12.20", "price": None}]
        recs = classify_and_extract_items(items, 946.26)
        self.assertEqual([(r["qty"], r["unit_price"]) for r in recs],
                         [(47.2, 11.5), (15.1, 11.5), (11.3, 11.7), (8.0, 12.2)])
        self.assertEqual(self._sum(recs), 946.26)
        self.assertEqual(recs[0]["canonical_item"], "ayam")

    def test_9913_counts_with_bad_date_line(self):
        items = [{"qty": None, "name": "AYAM x50 RM11.5", "price": None},
                 {"qty": None, "name": "W.LEG / WING / DRUMSTICK / THIGH x80 RM11.7", "price": None},
                 {"qty": None, "name": "ISI / MINCED / CHOP / FILLET / B.LEG x2 RM12.2", "price": None}]
        raw = ("TEL: 014 648 7622 TARIKH: 30/18/2020 KG / Qty Harga U.Price Jumlah (RM) Total (RM) "
               "50 AYAM 79.6 11.50 915.40 AYAM AYAM 80 W.LEG / WING / DRUMSTICK / THIGH 22.1 11.70 258.57 "
               "W.LEG 2p ISI / MINCED 4kg 12.20 48.80 ISI Total Jumlah 1222.77")
        recs = classify_and_extract_items(items, 1222.77, raw)
        self.assertEqual([r["qty"] for r in recs], [79.6, 22.1, 4.0])
        self.assertEqual(self._sum(recs), 1222.77)

    def test_9866_alternative_keys(self):
        items = [{"total": 143.91, "quantity": "12.3kg", "unit_price": 11.7,
                  "description": "W.LEG / WING / DRUMSTICK / THIGH"},
                 {"total": 97.6, "quantity": "8kg", "unit_price": 12.2,
                  "description": "W.LEG / WING / DRUMSTICK / THIGH / MINCED / CHOP / FILLET / B.LEG"}]
        recs = classify_and_extract_items(items, 241.51)
        self.assertEqual([(r["raw_item_name"][:6], r["qty"], r["unit_price"]) for r in recs],
                         [("W.LEG ", 12.3, 11.7), ("W.LEG ", 8.0, 12.2)])
        self.assertEqual(self._sum(recs), 241.51)

    def test_9788_bird_counts_replaced_by_weights(self):
        items = [{"qty": 50, "name": "AYAM", "price": 11.5}, {"qty": 10, "name": "AYAM Tandoori", "price": 11.5},
                 {"qty": 120, "name": "W.LEG / WING / DRUMSTICK / THIGH", "price": 11.7},
                 {"qty": 29, "name": "ISI / MINCED / CHOP / FILLET / B.LEG", "price": 12.2}]
        # OCR misread 403.65 as 402.65 on the third line; weight x price wins.
        raw = ("KG / Qty Harga U.Price Jumlah (RM) Total (RM) 50 AYAM 80.5. 11.50 925.75 AYAM 10 AYAM Tandoori "
               "15.0 11.50 172.50 120 W.LEG / WING / DRUMSTICK / THIGH 34.50 11.70 402.65 W.LEG 29 ISI / MINCED "
               "/ CHOP / FILLET / B.LEG 440 12.20 48.80 ISI Total Jumlah 1550.70")
        recs = classify_and_extract_items(items, 1550.70, raw)
        self.assertEqual([r["qty"] for r in recs], [80.5, 15.0, 34.5, 4.0])
        self.assertEqual(self._sum(recs), 1550.70)

    def test_2504_invoice_layout_qty_price_weight_total(self):
        # Vista invoice prints "Qty U/Price Weight Total"; the order count (30)
        # happens to land within 5% of the bill, but the weight column is exact.
        items = [{"qty": 30, "name": "AYAM BERSIH", "price": 11.7}, {"qty": 10, "name": "AYAM BERSIH", "price": 11.7},
                 {"qty": 40, "name": "WHOLE LEG", "price": 11.9}, {"qty": 2, "name": "ISI AYAM", "price": 10.1}]
        raw = ("Item Description Qty U/ Price Weight Disc. Total RM (KG) RM 1. AYAM BERSIH 30 11.70 50.00 585.00 "
               "2. AYAM BERSIH 10 11.70 14.90 174.33 3. WHOLE LEG 40 11.90 12.50 148.75 "
               "4. ISI AYAM 2 10.10 4.00 40.40 Total 948.48")
        recs = classify_and_extract_items(items, 948.48, raw)
        self.assertEqual([r["qty"] for r in recs], [50.0, 14.9, 12.5, 4.0])
        self.assertEqual(self._sum(recs), 948.48)

    def test_pack_sizes_and_matching_bills_untouched(self):
        items = [{"name": "Santan 1 kg", "qty": 3, "price": 6.0}, {"name": "MINYAK 5KG", "qty": 5, "price": 29.0},
                 {"name": "Ikan Kembung (1 KG)", "qty": 3, "price": 10.0}]
        recs = classify_and_extract_items(items, 193.0, "Santan 1 kg 3 6.00 18.00 MINYAK 5KG 5 29.00 145.00")
        self.assertEqual([r["qty"] for r in recs], [3.0, 5.0, 3.0])
        # No bill total: a present qty is never second-guessed.
        recs = classify_and_extract_items([{"name": "AYAM", "qty": 30, "price": 11.5}], None,
                                          "30 AYAM 47 11.50 540.50")
        self.assertEqual(recs[0]["qty"], 30.0)

    def test_raw_text_correction_must_reach_the_total(self):
        # Columns that don't add up to the bill are not trusted.
        items = [{"qty": 30, "name": "AYAM", "price": 11.5}]
        recs = classify_and_extract_items(items, 999.0, "30 AYAM 47 11.50 540.50")
        self.assertEqual(recs[0]["qty"], 30.0)
