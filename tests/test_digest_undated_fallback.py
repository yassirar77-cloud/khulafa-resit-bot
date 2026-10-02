"""Undated supplier bills still count in the digest's weekly outlet spend."""
import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digest_data import _outlet_spending_week  # noqa: E402
from tests.fake_supabase import FakeSupabase  # noqa: E402


class UndatedReceiptFallback(unittest.TestCase):
    def test_undated_bill_counts_on_its_upload_day(self):
        client = FakeSupabase()
        rows = [
            {"outlet": "Vista", "total": "100.00", "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": "2026-09-30", "created_at": "2026-09-30T10:00:00+08:00"},
            # No date on the bill, uploaded inside the week -> counted.
            {"outlet": "Vista", "total": "50.00", "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": None, "created_at": "2026-10-01T09:00:00+08:00"},
            # No date, uploaded before the week -> not counted.
            {"outlet": "Vista", "total": "999.00", "receipt_type": "SUPPLIER_PURCHASE",
             "receipt_date": None, "created_at": "2026-09-01T09:00:00+08:00"},
        ]
        for r in rows:
            client.table("receipts").insert(r).execute()
        now = datetime(2026, 10, 2, 23, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
        out = _outlet_spending_week(client, now)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["amount"], 150.0)
        self.assertEqual(out[0]["receipt_count"], 2)


if __name__ == "__main__":
    unittest.main()
