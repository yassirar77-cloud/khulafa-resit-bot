"""Tests for the item-resolution backfill (PR #32b).

Pure helpers plus the runner driven by an in-memory fake Supabase client that
supports the chains the backfill uses: receipts select with not_.is_, batch
insert into item_resolutions with a composite UNIQUE(receipt_id, item_index),
and item_alias insert with UNIQUE(alias_text). bot.py command wiring is checked
source-level.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill_items as bi  # noqa: E402
from backfill_items import (  # noqa: E402
    iter_item_names,
    plan_item,
    run_item_backfill,
    top_unmatched_from_resolutions,
)
from item_resolver import ALIAS_TABLE, CANONICAL_TABLE  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Composite / single UNIQUE keys per table for the fake.
_UNIQUE = {
    bi.ITEM_RESOLUTIONS_TABLE: ("receipt_id", "item_index"),
    ALIAS_TABLE: ("alias_text",),
}


class _FakeQuery:
    def __init__(self, store, table):
        self.store, self.table = store, table
        self.op, self.payload, self.filters, self._negate = "select", None, [], False
        self._order = self._limit = None

    @property
    def not_(self):
        self._negate = True
        return self

    def select(self, *a, **k):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val, False))
        return self

    def is_(self, col, val):
        neg, self._negate = self._negate, False
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
            else:
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
            uniq = _UNIQUE.get(self.table)
            inserted = []
            # Validate the whole batch first (mimic a transactional insert).
            if uniq:
                for p in payloads:
                    key = tuple(p.get(c) for c in uniq)
                    if any(tuple(r.get(c) for c in uniq) == key for r in rows):
                        raise Exception(f"duplicate {uniq}={key}")
            for p in payloads:
                rec = dict(p)
                rec.setdefault("id", self._next_id())
                rows.append(rec)
                inserted.append(dict(rec))
            return types.SimpleNamespace(data=inserted)
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


def make_client(receipts):
    client = FakeClient()
    client.seed(CANONICAL_TABLE, [
        {"id": 1, "display_name": "ayam bersih"},
        {"id": 17, "display_name": "jintan putih"},
        {"id": 36, "display_name": "santan"},
        {"id": 9, "display_name": "kambing"},
    ])
    client.seed(ALIAS_TABLE, [
        {"id": 1, "alias_text": "AYAM BERSIH", "canonical_id": 1},
        {"id": 2, "alias_text": "ayam bersih", "canonical_id": 1},
        {"id": 3, "alias_text": "JINTAN PUTIH", "canonical_id": 17},
        {"id": 4, "alias_text": "SANTAN", "canonical_id": 36},
        {"id": 5, "alias_text": "KAMBING", "canonical_id": 9},
    ])
    client.seed(bi.RECEIPTS_TABLE, receipts)
    return client


def _res(client, receipt_id, item_index):
    for r in client.rows(bi.ITEM_RESOLUTIONS_TABLE):
        if r.get("receipt_id") == receipt_id and r.get("item_index") == item_index:
            return r
    return None


# --- pure helpers -----------------------------------------------------------

class PureHelpers(unittest.TestCase):
    def test_iter_item_names_dicts_and_strings(self):
        r = {"items": [{"name": "AYAM BERSIH"}, "JINTAN PUTIH", {"qty": 2}]}
        self.assertEqual(list(iter_item_names(r)), [(0, "AYAM BERSIH"), (1, "JINTAN PUTIH"), (2, None)])

    def test_iter_item_names_null_or_empty(self):
        self.assertEqual(list(iter_item_names({"items": None})), [])
        self.assertEqual(list(iter_item_names({"items": []})), [])
        self.assertEqual(list(iter_item_names({"items": "notalist"})), [])

    def test_plan_item_skips_empty_name(self):
        self.assertIsNone(plan_item(1, 0, "", [], []))
        self.assertIsNone(plan_item(1, 0, None, [], []))

    def test_plan_item_low_confidence_has_null_canonical(self):
        canon = [{"id": 9, "display_name": "kambing"}]
        aliases = [{"alias_text": "KAMBING", "canonical_id": 9}]
        plan = plan_item(5, 2, "kambxyz", aliases, canon)  # conf 60 -> fuzzy-canonical
        self.assertEqual(plan["match_confidence"], 60)
        self.assertIsNone(plan["canonical_id"])
        self.assertEqual(plan["match_tier"], "low_confidence")

    def test_top_unmatched_from_resolutions(self):
        rows = [
            {"canonical_id": None, "raw_name": "MYSTERY"},
            {"canonical_id": None, "raw_name": "MYSTERY"},
            {"canonical_id": 1, "raw_name": "AYAM BERSIH"},
        ]
        pairs = top_unmatched_from_resolutions(rows)
        self.assertEqual(pairs, [("MYSTERY", 2)])


# --- runner -----------------------------------------------------------------

class RunBackfill(unittest.TestCase):
    def test_backfill_resolves_high_confidence_items(self):
        client = make_client([
            {"id": 100, "items": [{"name": "AYAM BERSIH"}, {"name": "JINTAN PUTIH 1KG"}]},
        ])
        stats, _, _ = run_item_backfill(client, dry_run=False)
        self.assertEqual(stats["resolved"], 2)
        self.assertEqual(stats["written"], 2)
        r0, r1 = _res(client, 100, 0), _res(client, 100, 1)
        self.assertEqual((r0["canonical_id"], r0["match_tier"], r0["match_confidence"]), (1, "exact", 100))
        self.assertEqual((r1["canonical_id"], r1["match_tier"], r1["match_confidence"]), (17, "substring", 85))

    def test_backfill_skips_null_items_array(self):
        client = make_client([
            {"id": 200, "items": None},
            {"id": 201, "items": []},
            {"id": 202, "items": [{"name": "AYAM BERSIH"}]},
        ])
        stats, _, _ = run_item_backfill(client, dry_run=False)
        # Only the one real item is written.
        self.assertEqual(len(client.rows(bi.ITEM_RESOLUTIONS_TABLE)), 1)
        self.assertIsNotNone(_res(client, 202, 0))
        self.assertIsNone(_res(client, 200, 0))

    def test_backfill_records_unresolved_with_null_canonical(self):
        client = make_client([
            {"id": 300, "items": [{"name": "ZZZ MYSTERY ITEM"}, {"name": "kambxyz"}]},
        ])
        run_item_backfill(client, dry_run=False)
        none_row = _res(client, 300, 0)
        low_row = _res(client, 300, 1)
        self.assertIsNone(none_row["canonical_id"])
        self.assertEqual(none_row["match_tier"], "none")
        self.assertIsNone(low_row["canonical_id"])
        self.assertEqual(low_row["match_tier"], "low_confidence")
        self.assertEqual(low_row["match_confidence"], 60)

    def test_backfill_skips_empty_item_name(self):
        client = make_client([
            {"id": 400, "items": [{"name": ""}, {"name": "   "}, {"name": "AYAM BERSIH"}]},
        ])
        stats, _, _ = run_item_backfill(client, dry_run=False)
        self.assertEqual(stats["skipped_empty"], 2)
        self.assertEqual(stats["items"], 1)
        self.assertEqual(len(client.rows(bi.ITEM_RESOLUTIONS_TABLE)), 1)

    def test_backfill_idempotent(self):
        client = make_client([
            {"id": 500, "items": [{"name": "AYAM BERSIH"}, {"name": "ZZZ UNKNOWN"}]},
        ])
        first, _, _ = run_item_backfill(client, dry_run=False)
        self.assertEqual(first["written"], 2)
        second, _, _ = run_item_backfill(client, dry_run=False)
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["already"], 2)
        self.assertEqual(len(client.rows(bi.ITEM_RESOLUTIONS_TABLE)), 2)

    def test_backfill_dry_run_writes_nothing(self):
        client = make_client([
            {"id": 600, "items": [{"name": "AYAM BERSIH"}, {"name": "JINTAN PUTIH 1KG"}]},
        ])
        stats, _, _ = run_item_backfill(client, dry_run=True)
        self.assertEqual(stats["resolved"], 2)   # still evaluated
        self.assertEqual(stats["written"], 0)
        self.assertEqual(client.rows(bi.ITEM_RESOLUTIONS_TABLE), [])
        # no fuzzy aliases cached either
        self.assertEqual(len(client.rows(ALIAS_TABLE)), 5)

    def test_backfill_handles_jsonb_array_correctly(self):
        client = make_client([
            {"id": 700, "items": [
                {"name": "AYAM BERSIH"},
                {"name": "JINTAN PUTIH 1KG"},
                "SANTAN",
            ]},
        ])
        run_item_backfill(client, dry_run=False)
        self.assertEqual(_res(client, 700, 0)["raw_name"], "AYAM BERSIH")
        self.assertEqual(_res(client, 700, 1)["raw_name"], "JINTAN PUTIH 1KG")
        self.assertEqual(_res(client, 700, 2)["raw_name"], "SANTAN")
        self.assertEqual(_res(client, 700, 2)["canonical_id"], 36)

    def test_backfill_caches_fuzzy_alias_for_substring(self):
        client = make_client([
            {"id": 800, "items": [{"name": "JINTAN PUTIH 1KG"}]},  # substring -> 85
        ])
        run_item_backfill(client, dry_run=False)
        new = [a for a in client.rows(ALIAS_TABLE) if a.get("created_via") == "fuzzy_auto"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["alias_text"], "JINTAN PUTIH 1KG")
        self.assertEqual(new[0]["canonical_id"], 17)


# --- bot wiring (source-level) ----------------------------------------------

class BotCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_commands_owner_only(self):
        for fn in ("item_backfill_status_command", "item_backfill_unmatched_command"):
            idx = self.src.index(f"async def {fn}(")
            self.assertIn("is_reviewer(_command_owner_id(update))", self.src[idx:idx + 600], f"{fn} not gated")

    def test_commands_registered(self):
        self.assertIn('CommandHandler("item_backfill_status", item_backfill_status_command)', self.src)
        self.assertIn('CommandHandler("item_backfill_unmatched", item_backfill_unmatched_command)', self.src)


if __name__ == "__main__":
    unittest.main()
