"""/summary counts undated bills on their upload day (source-level check,
like test_bot_review_flow: bot.py needs live credentials to import)."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SummaryUndatedFallback(unittest.TestCase):
    def test_fetch_today_receipts_reads_undated_bills_by_upload_day(self):
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as f:
            src = f.read()
        body = re.search(r"def fetch_today_receipts\(.*?\n(?=\n\n(?:async )?def )", src, re.S).group(0)
        self.assertIn('.eq("receipt_date", today_iso)', body)
        self.assertIn('.is_("receipt_date", "null")', body)
        self.assertIn("upload_window(today_iso, today_iso)", body)
        self.assertIn('.gte("created_at", gte)', body)


if __name__ == "__main__":
    unittest.main()
