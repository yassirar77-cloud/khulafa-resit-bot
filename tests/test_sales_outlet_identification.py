"""Outlet-identification tests for PR #35.

Identity comes from the email SUBJECT, never the TXT header. Unknown/unconfirmed
codes log and continue rather than crashing the batch.

Run with::

    python -m unittest tests.test_sales_outlet_identification
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sales_ingest import build_sales_record, process_email  # noqa: E402
from sales_parser import (  # noqa: E402
    canonical_outlet_for_code,
    extract_outlet_from_subject,
    parse_shift_close,
    read_shift_close_file,
)
from tests.sales_fixtures import FIXTURE_DIR, by_code, write_all  # noqa: E402
from datetime import datetime  # noqa: E402


def setUpModule():
    if not os.path.isdir(FIXTURE_DIR) or not os.listdir(FIXTURE_DIR):
        write_all()


NOW = datetime(2026, 5, 26, 20, 0, 0)


class _FakeStore:
    def __init__(self):
        self.saved = []
        self._keys = set()

    def exists(self, *key):
        return key in self._keys

    def save(self, record):
        self.saved.append(record)
        self._keys.add(record["key"])
        return len(self.saved)


def _klang_content():
    return read_shift_close_file(os.path.join(FIXTURE_DIR, by_code("S-KLANG")["filename"]))


class OutletIdentificationTests(unittest.TestCase):
    def test_identifies_outlet_from_subject_not_header(self):
        # BISTRO7 file (header says "BISTRO 7") delivered under a KLANG subject:
        # identity must follow the subject, header is debug-only.
        content = read_shift_close_file(os.path.join(FIXTURE_DIR, by_code("S-BISTRO7")["filename"]))
        subject = "S-KLANG SHIFTCLOSE (1499)"
        code = extract_outlet_from_subject(subject)
        self.assertEqual(code, "S-KLANG")
        self.assertEqual(canonical_outlet_for_code(code), "Klang B.Emas")
        parsed = parse_shift_close(content)
        self.assertEqual(parsed["header_outlet_raw"], "BISTRO 7")
        self.assertNotEqual(parsed["header_outlet_raw"], "Klang B.Emas")

    def test_unknown_outlet_subject_logs_warning_continues_ingest(self):
        store = _FakeStore()
        email_dict = {
            "subject": "S-FOO SHIFTCLOSE (1)",
            "outlet_code": "S-FOO",
            "content": _klang_content(),
            "message_id": "<foo>",
        }
        with self.assertLogs("sales_parser", level="WARNING") as cm:
            status, _ = process_email(store, email_dict, now_my=NOW)
        self.assertEqual(status, "inserted")
        self.assertTrue(any("S-FOO" in m for m in cm.output))
        # Stored under the raw code so the shift is not lost.
        self.assertEqual(store.saved[0]["parent"]["outlet_canonical"], "S-FOO")

    def test_outlet_S_KLANG_maps_to_Klang_BEmas(self):
        self.assertEqual(canonical_outlet_for_code("S-KLANG"), "Klang B.Emas")

    def test_outlet_S_SEK14_maps_to_Signature(self):
        self.assertEqual(canonical_outlet_for_code("S-SEK14"), "Signature")

    def test_outlet_SBESI_logs_warning_uses_canonical(self):
        with self.assertLogs("sales_parser", level="WARNING") as cm:
            canonical = canonical_outlet_for_code("S-SBESI")
        self.assertEqual(canonical, "SBESI")
        self.assertTrue(any("UNCONFIRMED" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
