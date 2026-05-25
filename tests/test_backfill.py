"""Tests for the canonical-merchant backfill (PR #31).

Pure decision/format helpers are tested directly. The batch runner is driven
through an in-memory fake Supabase client that supports the query chains the
backfill uses (select/is_/not_.is_/eq/order/limit, insert with UNIQUE
enforcement, update). bot.py can't be imported in CI, so command wiring is
checked source-level (the established pattern).
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill_canonical as bf  # noqa: E402
from backfill_canonical import (  # noqa: E402
    CONF_APPLY_MIN,
    plan_receipt,
    propose_reclassification,
    run_backfill,
    should_apply,
    should_upgrade_type,
    top_unmatched_from_audit,
)
from merchant_resolver import ALIAS_TABLE, CANONICAL_TABLE  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- in-memory fake Supabase client -----------------------------------------

_UNIQUE_COLUMN = {bf.BACKFILL_AUDIT_TABLE: "receipt_id", ALIAS_TABLE: "alias_text"}


class _FakeQuery:
    def __init__(self, store, table):
        self.store = store
        self.table = table
        self.op = "select"
        self.filters = []
        self._order = None
        self._limit = None
        self.payload = None
        self._negate = False

    @property
    def not_(self):
        self._negate = True
        return self

    def select(self, cols="*", **k):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val, False))
        return self

    def is_(self, col, val):
        neg = self._negate
        self._negate = False
        self.filters.append(("is", col, val, neg))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, row):
        for kind, col, val, neg in self.filters:
            if kind == "eq":
                ok = row.get(col) == val
            else:  # is null / not null
                ok = row.get(col) is None
                if neg:
                    ok = not ok
            if not ok:
                return False
        return True

    def _rows(self):
        return self.store.setdefault(self.table, [])

    def _next_id(self):
        seq = self.store.setdefault("__seq__", {})
        seq[self.table] = seq.get(self.table, 0) + 1
        return seq[self.table]

    def execute(self):
        rows = self._rows()
        if self.op == "insert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = []
            uniq = _UNIQUE_COLUMN.get(self.table)
            for p in payloads:
                rec = dict(p)
                if uniq is not None and any(r.get(uniq) == rec.get(uniq) for r in rows):
                    raise Exception(f"duplicate {uniq}={rec.get(uniq)!r}")
                rec.setdefault("id", self._next_id())
                rows.append(rec)
                inserted.append(dict(rec))
            return types.SimpleNamespace(data=inserted)
        if self.op == "update":
            updated = []
            for row in rows:
                if self._match(row):
                    row.update(self.payload)
                    updated.append(dict(row))
            return types.SimpleNamespace(data=updated)
        # select
        sel = [r for r in rows if self._match(r)]
        if self._order:
            col, desc = self._order
            sel = sorted(sel, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        if self._limit is not None:
            sel = sel[: self._limit]
        return types.SimpleNamespace(data=[dict(r) for r in sel])


class FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeQuery(self.store, name)

    def seed(self, table, rows):
        self.store.setdefault(table, []).extend(dict(r) for r in rows)

    def rows(self, table):
        return self.store.get(table, [])

    def receipt(self, receipt_id):
        for r in self.store.get(bf.RECEIPTS_TABLE, []):
            if r.get("id") == receipt_id:
                return r
        return None

    def audit(self, receipt_id):
        for r in self.store.get(bf.BACKFILL_AUDIT_TABLE, []):
            if r.get("receipt_id") == receipt_id:
                return r
        return None


def make_client(receipts):
    client = FakeClient()
    client.seed(CANONICAL_TABLE, [
        {"id": 1, "display_name": "EVEREST", "category": "supplier"},
        {"id": 15, "display_name": "FOOK LEONG", "category": "supplier"},
        {"id": 3, "display_name": "MEWAH", "category": "supplier"},
    ])
    client.seed(ALIAS_TABLE, [
        {"id": 1, "alias_text": "EVEREST AISVARAM SDN BHD", "canonical_id": 1},
        {"id": 2, "alias_text": "FOOK LEONG", "canonical_id": 15},
        {"id": 3, "alias_text": "MEWAH GROUP", "canonical_id": 3},
    ])
    client.seed(bf.RECEIPTS_TABLE, receipts)
    return client


# --- pure helpers -----------------------------------------------------------

class PureHelpers(unittest.TestCase):
    def test_plan_receipt_skips_null_merchant(self):
        self.assertIsNone(plan_receipt({"id": 1, "merchant": None}, [], []))
        self.assertIsNone(plan_receipt({"id": 1, "merchant": "   "}, [], []))

    def test_should_apply_threshold(self):
        self.assertTrue(should_apply({"matched_canonical_id": 1, "confidence": 80}))
        self.assertFalse(should_apply({"matched_canonical_id": 1, "confidence": 60}))
        self.assertFalse(should_apply({"matched_canonical_id": None, "confidence": 0}))

    def test_should_upgrade_type(self):
        self.assertTrue(should_upgrade_type("UNKNOWN", "SUPPLIER_PURCHASE"))
        self.assertFalse(should_upgrade_type("STAFF_ADVANCE", "SUPPLIER_PURCHASE"))
        self.assertFalse(should_upgrade_type("SUPPLIER_PURCHASE", "SUPPLIER_PURCHASE"))

    def test_top_unmatched_from_audit(self):
        rows = [
            {"matched_canonical_id": None, "confidence": 0, "raw_merchant": "X CORP"},
            {"matched_canonical_id": None, "confidence": 0, "raw_merchant": "X CORP"},
            {"matched_canonical_id": 3, "confidence": 60, "raw_merchant": "Y LOW"},
            {"matched_canonical_id": 1, "confidence": 100, "raw_merchant": "EVEREST"},
        ]
        pairs = top_unmatched_from_audit(rows, limit=30)
        self.assertEqual(pairs[0], ("X CORP", 2))
        self.assertIn(("Y LOW", 1), pairs)  # low-confidence counts as unresolved
        self.assertNotIn("EVEREST", dict(pairs))  # applied match excluded


# --- runner: resolution + apply ---------------------------------------------

class RunBackfill(unittest.TestCase):
    def test_backfill_resolves_exact_match(self):
        client = make_client([
            {"id": 101, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None},
        ])
        stats, tiers, _ = run_backfill(client, apply=False)
        audit = client.audit(101)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["matched_canonical_id"], 1)
        self.assertEqual(audit["confidence"], 100)
        self.assertEqual(audit["match_tier"], "exact")
        # audit-only: receipt not mutated.
        self.assertIsNone(client.receipt(101)["merchant_canonical_id"])
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["applied"], 0)

    def test_backfill_apply_tags_receipt(self):
        client = make_client([
            {"id": 101, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None},
        ])
        run_backfill(client, apply=True)
        self.assertEqual(client.receipt(101)["merchant_canonical_id"], 1)
        self.assertTrue(client.audit(101)["applied"])

    def test_backfill_uses_substring_containment(self):
        client = make_client([
            {"id": 102, "merchant": "FOOK LEONG SEA PRODUCTS SDN BHD", "merchant_canonical_id": None},
        ])
        run_backfill(client, apply=True)
        audit = client.audit(102)
        self.assertEqual(audit["matched_canonical_id"], 15)
        self.assertEqual(audit["match_tier"], "substring")
        self.assertEqual(audit["confidence"], 85)
        self.assertEqual(client.receipt(102)["merchant_canonical_id"], 15)

    def test_backfill_skips_below_80_confidence(self):
        # "MEWAHH" -> fuzzy-canonical vs display "MEWAH" (conf 60) -> not applied.
        client = make_client([
            {"id": 103, "merchant": "MEWAHH", "merchant_canonical_id": None},
        ])
        run_backfill(client, apply=True)
        audit = client.audit(103)
        self.assertEqual(audit["confidence"], 60)
        self.assertEqual(audit["match_tier"], "fuzzy-canonical")
        self.assertFalse(audit["applied"])
        self.assertIsNone(client.receipt(103)["merchant_canonical_id"])

    def test_backfill_skips_null_merchant(self):
        client = make_client([
            {"id": 104, "merchant": None, "merchant_canonical_id": None},
            {"id": 105, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None},
        ])
        stats, _, _ = run_backfill(client, apply=True)
        self.assertEqual(stats["skipped_null"], 1)
        self.assertIsNone(client.audit(104))  # no audit row for null merchant
        self.assertIsNotNone(client.audit(105))

    def test_backfill_idempotent(self):
        client = make_client([
            {"id": 201, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None},
            {"id": 202, "merchant": "ZZZ TOTALLY UNKNOWN VENDOR", "merchant_canonical_id": None},
        ])
        first, _, _ = run_backfill(client, apply=True)
        self.assertEqual(first["evaluated"], 2)
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["applied"], 1)  # only 201 resolved
        # Second run: 201 is now tagged, so only 202 is still a candidate, and
        # its audit row already exists -> no new audit, no new apply.
        second, _, _ = run_backfill(client, apply=True)
        self.assertEqual(second["evaluated"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["applied"], 0)
        self.assertEqual(len(client.rows(bf.BACKFILL_AUDIT_TABLE)), 2)

    def test_backfill_dry_run_writes_nothing(self):
        client = make_client([
            {"id": 301, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None},
        ])
        stats, _, _ = run_backfill(client, dry_run=True, apply=True)
        self.assertEqual(stats["resolved"], 1)  # still evaluated
        self.assertEqual(client.rows(bf.BACKFILL_AUDIT_TABLE), [])
        self.assertIsNone(client.receipt(301)["merchant_canonical_id"])

    def test_backfill_audit_records_match_tier(self):
        client = make_client([
            {"id": 401, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None},
            {"id": 402, "merchant": "FOOK LEONG SEA PRODUCTS SDN BHD", "merchant_canonical_id": None},
            {"id": 403, "merchant": "MEWAHH", "merchant_canonical_id": None},
            {"id": 404, "merchant": "ZZZ UNKNOWN", "merchant_canonical_id": None},
        ])
        run_backfill(client, apply=False)
        self.assertEqual(client.audit(401)["match_tier"], "exact")
        self.assertEqual(client.audit(402)["match_tier"], "substring")
        self.assertEqual(client.audit(403)["match_tier"], "fuzzy-canonical")
        self.assertEqual(client.audit(404)["match_tier"], "none")


# --- reclassify -------------------------------------------------------------

class Reclassify(unittest.TestCase):
    def test_propose_reclassification_keyword_fallback_when_no_category(self):
        # No category on the canonical -> falls back to the keyword classifier.
        receipt = {"id": 1, "merchant": "EVEREST AISVARAM SDN BHD", "receipt_type": "UNKNOWN",
                   "raw_text": "", "items": [], "total": 50}
        canonical = {"id": 1, "display_name": "EVEREST"}
        self.assertEqual(propose_reclassification(receipt, canonical), "SUPPLIER_PURCHASE")

    def test_propose_reclassification_never_downgrades(self):
        receipt = {"id": 2, "merchant": "EVEREST AISVARAM SDN BHD", "receipt_type": "STAFF_ADVANCE",
                   "raw_text": "", "items": [], "total": 50}
        canonical = {"id": 1, "display_name": "EVEREST", "category": "supplier"}
        self.assertIsNone(propose_reclassification(receipt, canonical))

    def test_reclassify_upgrades_unknown_via_canonical_category(self):
        # Strata fee: raw_text has NO rent keyword, so the keyword classifier
        # alone yields UNKNOWN — only the canonical category rescues it.
        receipt = {"id": 3, "merchant": "BADAN PENGURUSAN BERSAMA VISTA ALAM",
                   "receipt_type": "UNKNOWN", "raw_text": "JALAN VISTA, SHAH ALAM",
                   "items": [], "total": 350}
        canonical = {"id": 99, "display_name": "VISTA ALAM JMB", "category": "rent_license"}
        from receipt_classifier import classify_receipt
        keyword_only = classify_receipt(
            ocr_text=receipt["raw_text"], merchant=canonical["display_name"], total=350
        )
        self.assertEqual(keyword_only.receipt_type.value, "UNKNOWN")  # keyword path can't
        self.assertEqual(propose_reclassification(receipt, canonical), "RENT_LICENSE")

    def test_reclassify_internal_transfer_via_category(self):
        receipt = {"id": 4, "merchant": "RESTORAN KHULAFA VISTA", "receipt_type": "UNKNOWN",
                   "raw_text": "", "items": [], "total": 1200}
        canonical = {"id": 41, "display_name": "KHULAFA VISTA", "category": "internal_transfer"}
        self.assertEqual(propose_reclassification(receipt, canonical), "INTERNAL_TRANSFER")

    def test_reclassify_keeps_higher_priority_existing_type(self):
        # Already a more-specific type than the canonical category implies.
        receipt = {"id": 5, "merchant": "EVEREST AISVARAM SDN BHD", "receipt_type": "RENT_LICENSE",
                   "raw_text": "", "items": [], "total": 50}
        canonical = {"id": 1, "display_name": "EVEREST", "category": "supplier"}
        self.assertIsNone(propose_reclassification(receipt, canonical))

    def test_reclassify_only_upgrades_in_run(self):
        client = make_client([
            {"id": 501, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None,
             "receipt_type": "UNKNOWN", "raw_text": "", "items": []},
            {"id": 502, "merchant": "EVEREST AISVARAM SDN BHD", "merchant_canonical_id": None,
             "receipt_type": "STAFF_ADVANCE", "raw_text": "", "items": []},
        ])
        stats, _, _ = run_backfill(client, apply=True, reclassify=True)
        self.assertEqual(client.receipt(501)["receipt_type"], "SUPPLIER_PURCHASE")  # upgraded
        self.assertEqual(client.receipt(502)["receipt_type"], "STAFF_ADVANCE")      # untouched
        self.assertEqual(stats["reclassified"], 1)
        # both still get tagged with the canonical regardless of type
        self.assertEqual(client.receipt(501)["merchant_canonical_id"], 1)
        self.assertEqual(client.receipt(502)["merchant_canonical_id"], 1)


# --- migration + bot wiring (source-level) ----------------------------------

class Migration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "migrations", "0008_backfill_audit.sql")) as f:
            cls.sql = f.read()

    def test_table_and_constraints(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS public.backfill_audit", self.sql)
        self.assertIn("UNIQUE (receipt_id)", self.sql)
        self.assertIn("REFERENCES public.receipts(id)", self.sql)
        self.assertIn("REFERENCES public.merchant_canonical(id)", self.sql)
        for tier in ("exact", "substring", "fuzzy-alias", "fuzzy-canonical", "none"):
            self.assertIn(f"'{tier}'", self.sql)

    def test_internal_transfer_constraint_migration(self):
        with open(os.path.join(REPO_ROOT, "migrations", "0009_receipt_type_internal_transfer.sql")) as f:
            sql = f.read()
        self.assertIn("DROP CONSTRAINT IF EXISTS receipts_receipt_type_check", sql)
        self.assertIn("'INTERNAL_TRANSFER'", sql)
        # all original types preserved
        for t in ("SUPPLIER_PURCHASE", "STAFF_ADVANCE", "UTILITY", "RENT_LICENSE", "PETTY_CASH", "UNKNOWN"):
            self.assertIn(f"'{t}'", sql)


class BotBackfillCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_commands_owner_only(self):
        for fn in (
            "backfill_status_command", "backfill_preview_command",
            "backfill_apply_command", "backfill_apply_all_command",
            "backfill_unmatched_command",
        ):
            idx = self.src.index(f"async def {fn}(")
            body = self.src[idx:idx + 600]
            self.assertIn("is_reviewer(_command_owner_id(update))", body, f"{fn} not owner-gated")

    def test_apply_all_callback_owner_gated(self):
        idx = self.src.index("async def backfill_apply_all_callback(")
        body = self.src[idx:idx + 600]
        self.assertIn("is_reviewer(", body)

    def test_commands_registered(self):
        for cmd, fn in (
            ("backfill_status", "backfill_status_command"),
            ("backfill_preview", "backfill_preview_command"),
            ("backfill_apply", "backfill_apply_command"),
            ("backfill_apply_all", "backfill_apply_all_command"),
            ("backfill_unmatched", "backfill_unmatched_command"),
        ):
            self.assertIn(f'CommandHandler("{cmd}", {fn})', self.src)
        self.assertIn('pattern=r"^backfill_applyall:(yes|no)$"', self.src)


if __name__ == "__main__":
    unittest.main()
