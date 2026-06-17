"""Hermetic tests for scripts/repair_corrupt_dates over a fake Supabase."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import repair_corrupt_dates as rcd  # noqa: E402
from tests.fake_supabase import FakeSupabase  # noqa: E402

# Pin "today(MY)" so the corruption rules are deterministic.
_TODAY = __import__("datetime").date(2026, 6, 17)


class RepairCorruptDatesTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()
        self._orig_today = rcd._today_my
        rcd._today_my = lambda: _TODAY  # type: ignore[assignment]

    def tearDown(self):
        rcd._today_my = self._orig_today  # type: ignore[assignment]

    def _seed(self, table, rows):
        for r in rows:
            self.fake.table(table).insert(r).execute()

    def test_dry_run_changes_nothing(self):
        self._seed("receipts", [
            {"merchant": "DIAMOND BALL", "receipt_date": "2029-05-29",
             "created_at": "2026-05-29T02:00:00+00:00"},
        ])
        rcd.run(self.fake, apply=False, max_drift_days=60)
        self.assertEqual(self.fake.rows("receipts")[0]["receipt_date"], "2029-05-29")

    def test_apply_corrects_future_and_stale_and_is_idempotent(self):
        self._seed("receipts", [
            # future -> ingestion day
            {"merchant": "INBOIS", "receipt_date": "2026-08-22",
             "created_at": "2026-05-22T02:00:00+00:00"},
            # stale year -> ingestion day
            {"merchant": "AYAM CO", "receipt_date": "2024-05-08",
             "created_at": "2026-05-08T02:00:00+00:00"},
            # plausible -> untouched
            {"merchant": "BESTARI", "receipt_date": "2026-06-14",
             "created_at": "2026-06-15T02:00:00+00:00"},
        ])
        self._seed("item_prices", [
            {"merchant": "INBOIS", "canonical_item": "gas", "receipt_date": "2026-12-16",
             "created_at": "2026-06-10T02:00:00+00:00"},
        ])
        totals = rcd.run(self.fake, apply=True, max_drift_days=60)
        self.assertEqual(totals["written"], 3)

        recs = {r["merchant"]: r["receipt_date"] for r in self.fake.rows("receipts")}
        self.assertEqual(recs["INBOIS"], "2026-05-22")   # future, no year-fix -> ingested
        self.assertEqual(recs["AYAM CO"], "2026-05-08")  # year fix keeps 05-08
        self.assertEqual(recs["BESTARI"], "2026-06-14")  # plausible untouched
        self.assertEqual(self.fake.rows("item_prices")[0]["receipt_date"], "2026-06-10")
        self.assertEqual(totals["year_fix"], 1)          # AYAM CO 2024-05-08 -> 2026-05-08
        self.assertEqual(totals["ingest_fallback"], 2)   # INBOIS future x2

        # Idempotent: nothing left to repair.
        again = rcd.run(self.fake, apply=True, max_drift_days=60)
        self.assertEqual(again["written"], 0)

    def test_unfixable_without_created_at_is_flagged_not_changed(self):
        self._seed("receipts", [
            {"merchant": "VICTORY", "receipt_date": "2026-06-26", "created_at": None},
        ])
        totals = rcd.run(self.fake, apply=True, max_drift_days=60)
        self.assertEqual(totals["written"], 0)
        self.assertEqual(totals["flagged"], 1)
        self.assertEqual(self.fake.rows("receipts")[0]["receipt_date"], "2026-06-26")

    def test_ambiguous_old_date_flagged_not_rewritten(self):
        # Real-looking old date far from ingestion (late upload?) -> don't guess.
        self._seed("item_prices", [
            {"merchant": "EVEREST", "canonical_item": "ais_batu",
             "receipt_date": "2026-03-15", "created_at": "2026-05-31T02:00:00+00:00"},
        ])
        totals = rcd.run(self.fake, apply=True, max_drift_days=60)
        self.assertEqual(totals["written"], 0)
        self.assertEqual(totals["flagged"], 1)
        self.assertEqual(self.fake.rows("item_prices")[0]["receipt_date"], "2026-03-15")


    def test_apply_writes_journal_with_old_value_and_revert_restores(self):
        import json
        import tempfile
        self._seed("receipts", [
            {"merchant": "INBOIS", "receipt_date": "2026-08-22",
             "created_at": "2026-05-22T02:00:00+00:00"},          # future -> ingested
            {"merchant": "BESTARI", "receipt_date": "2026-06-14",
             "created_at": "2026-06-15T02:00:00+00:00"},          # plausible, untouched
        ])
        journal = os.path.join(tempfile.mkdtemp(), "journal.jsonl")
        rcd.run(self.fake, apply=True, max_drift_days=60, journal_path=journal)

        # Journal captures the OLD value per applied row (the rollback contract).
        with open(journal, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["table"], "receipts")
        self.assertEqual(entries[0]["old"], "2026-08-22")
        self.assertEqual(entries[0]["new"], "2026-05-22")

        # The write happened...
        recs = {r["merchant"]: r["receipt_date"] for r in self.fake.rows("receipts")}
        self.assertEqual(recs["INBOIS"], "2026-05-22")

        # ...and --revert puts the OLD value back, idempotently.
        stats = rcd.revert(self.fake, journal)
        self.assertEqual(stats["restored"], 1)
        recs = {r["merchant"]: r["receipt_date"] for r in self.fake.rows("receipts")}
        self.assertEqual(recs["INBOIS"], "2026-08-22")
        # Re-revert is a no-op (current no longer equals the new value).
        again = rcd.revert(self.fake, journal)
        self.assertEqual(again["restored"], 0)
        self.assertEqual(again["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
