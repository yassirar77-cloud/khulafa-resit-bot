"""Tests for the historical OCR re-parse (PR #29c).

Behavioural tests run against the pure helpers in ``reparse``. The bot command
wiring is checked source-level (bot.py can't be imported in CI — runtime deps
+ required env vars), mirroring ``BotGatingTests`` / ``BotReviewFlow``.
"""

import importlib.util
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reparse import (  # noqa: E402
    apply_audit_row,
    audit_insert_payload,
    format_preview,
    format_status,
    propose_corrections,
    should_reprocess,
    summarize_audit_rows,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PVS_SANTAN_RAW = (
    "# PVS SANTAN SDN BHD\n"
    "| Item | Qty | Amount |\n"
    "|---|---|---|\n"
    "| Santan | 1 | 180.00 |\n"
    "GRAND TOTAL: RM 18,000\n"
    "Tarikh: 22/05/2024\n"
)


class ProposeCorrections(unittest.TestCase):
    def test_reparse_pvs_santan_18000_corrects_to_18(self):
        row = {
            "id": 1532,
            "merchant": "PVS SANTAN",
            "total": 18000.0,
            "receipt_date": "2024-05-22",
            "confidence": 50,
            "items": [{"name": "Santan", "qty": 1, "price": 180.0}],
            "raw_text": PVS_SANTAN_RAW,
        }
        result = propose_corrections(row)
        self.assertIsNotNone(result)
        self.assertEqual(result["old_total"], 18000.0)
        self.assertEqual(result["new_total"], 180.0)
        self.assertTrue(result["has_change"])
        # Date in raw_text matches the stored date -> no spurious date change.
        self.assertEqual(result["new_date"], "2024-05-22")
        self.assertIn("total corrected", result["notes"])

    def test_reparse_nasi_lemak_8250_corrects_to_82_50(self):
        row = {
            "id": 911,
            "merchant": "NASI LEMAK",
            "total": 8250.0,
            "receipt_date": "2024-03-01",
            "confidence": 55,
            "items": [{"price": 50.0}, {"price": 32.50}],
            "raw_text": "Nasi Lemak\nTotal RM8,250\n",
        }
        result = propose_corrections(row)
        self.assertEqual(result["new_total"], 82.50)
        self.assertTrue(result["has_change"])

    def test_reparse_everest_9900_corrects_to_99(self):
        row = {
            "id": 1576,
            "merchant": "EVEREST",
            "total": 9900.0,
            "receipt_date": "2024-04-10",
            "confidence": 50,
            "items": [{"name": "Tube Ice", "price": 99.0}],
            "raw_text": "EVEREST\nTotal RM9,900\n",
        }
        result = propose_corrections(row)
        self.assertEqual(result["new_total"], 99.0)
        self.assertTrue(result["has_change"])

    def test_clean_total_no_items_yields_no_change(self):
        row = {
            "id": 1,
            "merchant": "BABAS",
            "total": 25.0,
            "receipt_date": "2026-05-20",
            "confidence": 80,
            "items": [],
            "raw_text": "# BABAS\nTotal RM25.00\n",
        }
        result = propose_corrections(row)
        self.assertIsNotNone(result)
        self.assertFalse(result["has_change"])
        self.assertEqual(result["new_total"], 25.0)
        self.assertEqual(result["notes"], "reviewed, no change")

    def test_clean_total_none_items_yields_no_change(self):
        row = {
            "id": 2, "merchant": "X", "total": 25.0, "receipt_date": "2026-05-20",
            "confidence": 80, "items": None, "raw_text": "X\nTotal RM25.00\n",
        }
        self.assertFalse(propose_corrections(row)["has_change"])

    def test_bestari_2170_already_correct_no_change(self):
        # PR #36 established RM2,170.76 is correct; reparse must not "fix" it.
        row = {
            "id": 1586, "merchant": "BESTARI FARM", "total": 2170.76,
            "receipt_date": "2024-05-21", "confidence": 70,
            "items": [{"price": 2000.0}, {"price": 170.76}],
            "raw_text": "BESTARI\nGRAND TOTAL RM2,170.76\n",
        }
        result = propose_corrections(row)
        self.assertEqual(result["new_total"], 2170.76)
        self.assertFalse(result["has_change"])

    def test_empty_raw_text_skipped(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                row = {"id": 9, "total": 18000.0, "items": [{"price": 180.0}], "raw_text": raw}
                self.assertIsNone(propose_corrections(row))

    def test_audit_insert_payload_drops_has_change(self):
        result = propose_corrections({
            "id": 1, "total": 18000.0, "items": [{"price": 180.0}],
            "receipt_date": "2024-05-22", "raw_text": PVS_SANTAN_RAW,
        })
        payload = audit_insert_payload(result)
        self.assertNotIn("has_change", payload)
        self.assertIn("new_total", payload)


# --- apply_audit_row against a recording fake client ------------------------

class _FakeQuery:
    def __init__(self, recorder, table):
        self._rec = recorder
        self._table = table
        self._op = None
        self._payload = None
        self._eq = None

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        self._rec.append((self._table, self._op, self._payload, self._eq))
        return types.SimpleNamespace(data=[])


class FakeClient:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _FakeQuery(self.calls, name)


class ApplyAuditRow(unittest.TestCase):
    def test_reparse_skips_applied_rows(self):
        client = FakeClient()
        result = apply_audit_row(client, {"id": 5, "receipt_id": 1, "applied": True})
        self.assertFalse(result)
        self.assertEqual(client.calls, [])  # no DB writes on an applied row

    def test_reparse_apply_marks_audit_rows(self):
        client = FakeClient()
        row = {
            "id": 5, "receipt_id": 1532, "new_total": 180.0,
            "new_date": "2024-05-22", "new_merchant": "PVS SANTAN",
            "confidence_new": 100, "applied": False,
        }
        result = apply_audit_row(client, row, applied_by_chat_id=999)
        self.assertTrue(result)
        tables = {c[0] for c in client.calls}
        self.assertEqual(tables, {"receipts", "reparse_audit"})
        receipts_update = next(c for c in client.calls if c[0] == "receipts")
        self.assertEqual(receipts_update[2]["total"], 180.0)
        self.assertEqual(receipts_update[3], ("id", 1532))
        audit_update = next(c for c in client.calls if c[0] == "reparse_audit")
        self.assertTrue(audit_update[2]["applied"])
        self.assertEqual(audit_update[2]["applied_by_chat_id"], 999)
        self.assertEqual(audit_update[3], ("id", 5))


class Idempotency(unittest.TestCase):
    def test_should_reprocess_skips_applied_and_pending(self):
        self.assertFalse(should_reprocess(1, {1}, set()))
        self.assertFalse(should_reprocess(2, set(), {2}))
        self.assertTrue(should_reprocess(3, {1}, {2}))


class SummarizeAndFormat(unittest.TestCase):
    ROWS = [
        {"applied": True, "old_total": 18000, "new_total": 180, "old_date": "2024-05-22", "new_date": "2024-05-22"},
        {"applied": False, "old_total": 8250, "new_total": 82.5, "old_date": "2028-01-01", "new_date": "2024-01-01"},
        {"applied": False, "old_total": 50, "new_total": 50, "old_date": "2028-01-01", "new_date": "2024-01-01"},
    ]

    def test_reparse_status_counts(self):
        c = summarize_audit_rows(self.ROWS)
        self.assertEqual(c["total"], 3)
        self.assertEqual(c["applied"], 1)
        self.assertEqual(c["pending"], 2)
        self.assertEqual(c["total_only"], 1)   # row 0: total changed only
        self.assertEqual(c["date_only"], 1)    # row 2: date changed only
        self.assertEqual(c["both"], 1)         # row 1: total + date
        self.assertIn("Pending:", format_status(c))

    def test_reparse_preview_shows_changes(self):
        rows = [{
            "receipt_id": 1532, "old_total": 18000.0, "new_total": 180.0,
            "old_date": "2024-05-22", "new_date": "2024-05-22",
            "confidence_old": 50, "confidence_new": 100,
        }]
        out = format_preview(rows)
        self.assertIn("#1532", out)
        self.assertIn("RM18,000.00 → RM180.00", out)
        self.assertIn("conf 50 → 100", out)

    def test_preview_empty(self):
        self.assertEqual(format_preview([]), "No pending reparse changes.")


class BotCommandSourceChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_non_owner_command_rejected(self):
        # Each of the four commands gates on the reviewer/owner check.
        self.assertGreaterEqual(
            self.src.count("is_reviewer(_command_owner_id(update))"), 4,
            "all four reparse commands must be owner-gated",
        )
        # The apply-all confirmation callback also rejects non-reviewers.
        self.assertIn("if not is_reviewer(chat_id):", self.src)

    def test_commands_registered(self):
        for cmd, fn in (
            ("reparse_status", "reparse_status_command"),
            ("reparse_preview", "reparse_preview_command"),
            ("reparse_apply", "reparse_apply_command"),
            ("reparse_apply_all", "reparse_apply_all_command"),
        ):
            self.assertIn(f'CommandHandler("{cmd}", {fn})', self.src)
        self.assertIn(
            r'CallbackQueryHandler(reparse_apply_all_callback, pattern=r"^reparse_applyall:(yes|no)$")',
            self.src,
        )

    def test_apply_all_requires_confirmation(self):
        # /reparse_apply_all must NOT apply directly — it sends a Y/N keyboard.
        cmd_idx = self.src.index("async def reparse_apply_all_command(")
        cb_idx = self.src.index("async def reparse_apply_all_callback(")
        cmd_body = self.src[cmd_idx:cb_idx]
        self.assertIn("reparse_applyall:yes", cmd_body)
        self.assertNotIn("_apply_pending_audit_rows", cmd_body)
        # The actual application happens only in the callback after 'yes'.
        cb_body = self.src[cb_idx:cb_idx + 1500]
        self.assertIn("_apply_pending_audit_rows", cb_body)


def _load_script_module():
    path = os.path.join(REPO_ROOT, "scripts", "reparse_ocr_historical.py")
    spec = importlib.util.spec_from_file_location("reparse_ocr_historical", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ScriptFakeQuery:
    """Minimal stand-in for the supabase query builder used by the script."""

    def __init__(self, parent, table):
        self.parent = parent
        self.table = table
        self._op = "select"
        self._eq = {}
        self._payload = None

    def select(self, *a, **k):
        self._op = "select"
        return self

    def or_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def execute(self):
        if self.table == "receipts" and self._op == "select":
            return types.SimpleNamespace(data=list(self.parent.candidates))
        if self.table == "reparse_audit" and self._op == "select":
            ids = self.parent.applied_ids if self._eq.get("applied") else self.parent.pending_ids
            return types.SimpleNamespace(data=[{"receipt_id": i} for i in ids])
        if self.table == "reparse_audit" and self._op == "insert":
            self.parent.inserted.append(self._payload)
            return types.SimpleNamespace(data=[self._payload])
        return types.SimpleNamespace(data=[])


class ScriptFakeClient:
    def __init__(self, candidates, applied_ids=(), pending_ids=()):
        self.candidates = candidates
        self.applied_ids = set(applied_ids)
        self.pending_ids = set(pending_ids)
        self.inserted = []

    def table(self, name):
        return _ScriptFakeQuery(self, name)


def _changing_candidate(rid):
    # 18000 -> 180 is a clean decimal flip, so every such row is a change.
    return {
        "id": rid, "merchant": "M", "total": 18000.0, "receipt_date": "2024-05-22",
        "confidence": 50, "items": [{"price": 180.0}], "raw_text": "M\nTotal RM18,000\n",
    }


class ScriptRunFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _load_script_module()

    def test_dry_run_inserts_nothing(self):
        client = ScriptFakeClient([_changing_candidate(i) for i in range(1, 4)])
        stats = self.script.run(client, dry_run=True)
        self.assertEqual(client.inserted, [])          # nothing written
        self.assertEqual(stats["created"], 3)          # but reports would-create count
        self.assertEqual(stats["evaluated"], 3)

    def test_limit_processes_only_n_candidates(self):
        client = ScriptFakeClient([_changing_candidate(i) for i in range(1, 6)])
        stats = self.script.run(client, limit=2)
        self.assertEqual(stats["evaluated"], 2)
        self.assertEqual(len(client.inserted), 2)      # only first 2 queued

    def test_dry_run_with_limit_combines_correctly(self):
        client = ScriptFakeClient([_changing_candidate(i) for i in range(1, 6)])
        stats = self.script.run(client, dry_run=True, limit=2)
        self.assertEqual(stats["evaluated"], 2)
        self.assertEqual(stats["created"], 2)
        self.assertEqual(client.inserted, [])          # limit AND no writes

    def test_default_behaviour_unchanged(self):
        client = ScriptFakeClient([_changing_candidate(i) for i in range(1, 4)])
        stats = self.script.run(client)
        self.assertEqual(stats["evaluated"], 3)
        self.assertEqual(len(client.inserted), 3)      # all changes queued
        self.assertEqual(stats["created"], 3)


class MigrationFile(unittest.TestCase):
    def test_reparse_audit_migration_exists_and_shaped(self):
        path = os.path.join(REPO_ROOT, "migrations", "0006_reparse_audit.sql")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            ddl = f.read()
        self.assertIn("CREATE TABLE IF NOT EXISTS public.reparse_audit", ddl)
        self.assertIn("idx_reparse_audit_unique_pending", ddl)
        self.assertIn("WHERE applied = false", ddl)


if __name__ == "__main__":
    unittest.main()
