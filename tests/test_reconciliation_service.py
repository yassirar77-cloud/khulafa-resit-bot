"""Integration tests for reconciliation_service against a fake Supabase client
(PR #37), plus a bot wiring assertion."""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reconciliation_service as rs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Query:
    """Minimal Supabase query stub: select/eq/in_/upsert/insert/delete chains."""

    def __init__(self, store, table):
        self.store = store
        self.table = table
        self._filters = []      # list of (col, predicate)
        self._mode = "select"
        self._payload = None
        self._conflict = None

    # --- builders ---
    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, lambda v, val=val: v == val))
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._filters.append((col, lambda v, vals=vals: v in vals))
        return self

    def gte(self, col, val):
        self._filters.append((col, lambda v, val=val: v is not None and str(v) >= str(val)))
        return self

    def lte(self, col, val):
        self._filters.append((col, lambda v, val=val: v is not None and str(v) <= str(val)))
        return self

    def upsert(self, payload, on_conflict=None):
        self._mode = "upsert"
        self._payload = payload
        self._conflict = (on_conflict or "").split(",") if on_conflict else []
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def delete(self):
        self._mode = "delete"
        return self

    # --- execution ---
    def _matches(self, row):
        return all(pred(row.get(col)) for col, pred in self._filters)

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self._mode == "select":
            return types.SimpleNamespace(data=[r for r in rows if self._matches(r)])
        if self._mode == "delete":
            self.store[self.table] = [r for r in rows if not self._matches(r)]
            return types.SimpleNamespace(data=[])
        if self._mode == "insert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payload:
                p = dict(p)
                p.setdefault("id", self.store["_seq"]())
                rows.append(p)
            return types.SimpleNamespace(data=payload)
        if self._mode == "upsert":
            keys = self._conflict
            existing = None
            for r in rows:
                if keys and all(r.get(k) == self._payload.get(k) for k in keys):
                    existing = r
                    break
            if existing is not None:
                existing.update(self._payload)
                return types.SimpleNamespace(data=[existing])
            new = dict(self._payload)
            new["id"] = self.store["_seq"]()
            rows.append(new)
            return types.SimpleNamespace(data=[new])
        return types.SimpleNamespace(data=[])


class FakeClient:
    def __init__(self, seed=None):
        self.store = dict(seed or {})
        counter = {"n": 100}

        def _seq():
            counter["n"] += 1
            return counter["n"]

        self.store["_seq"] = _seq

    def table(self, name):
        return _Query(self.store, name)


class ReconciliationServiceTests(unittest.TestCase):
    def _seed(self):
        return {
            "merchant_canonical": [
                {"id": 1, "display_name": "BABAS"},
                {"id": 2, "display_name": "EVEREST"},
            ],
            "receipts": [
                # Vista, supplier purchase, BABAS RM60 on the business date.
                {"id": 1001, "total": 60.0, "merchant": "BABAS PRODUCTS",
                 "merchant_canonical_id": 1, "outlet": "Vista",
                 "receipt_date": "2026-05-29", "receipt_type": "SUPPLIER_PURCHASE"},
            ],
            "sales_daily_summary": [
                {"id": 5001, "outlet_canonical": "Vista", "business_date": "2026-05-29",
                 "day_sales": 600.0},
            ],
            "sales_daily_payouts": [
                {"id": 7001, "summary_id": 5001, "description": "PAY TO BABAS",
                 "vendor_name": "BABAS", "amount": 60.0},
                {"id": 7002, "summary_id": 5001, "description": "PAY TO EVEREST",
                 "vendor_name": "EVEREST", "amount": 80.0},  # cash, no receipt -> Type B
                {"id": 7003, "summary_id": 5001, "description": "PAY TO GAS",
                 "vendor_name": "GAS", "amount": 250.0},      # utility -> Type E
            ],
        }

    def test_run_reconciliation_persists_row_and_log(self):
        client = FakeClient(self._seed())
        result = rs.run_reconciliation(client, "2026-05-29")
        self.assertEqual(result["outlets_processed"], 1)

        recon = client.store["purchase_reconciliation"]
        self.assertEqual(len(recon), 1)
        row = recon[0]
        self.assertEqual(row["outlet_canonical"], "Vista")
        self.assertEqual(row["matched_count"], 1)               # BABAS matched
        self.assertEqual(row["unmatched_pos_payouts"], 1)       # EVEREST Type B
        self.assertEqual(row["total_food_purchases"], 140.0)    # 60 matched + 80 cash-no-receipt
        # GAS (utility) excluded; food cost = 140 / 600 = 23.33%.
        self.assertAlmostEqual(float(row["food_cost_percent"]), 23.33, places=1)

        log = client.store["purchase_match_log"]
        types_seen = {r["match_type"] for r in log}
        self.assertEqual(types_seen, {"A_matched", "B_cash_no_receipt", "E_excluded_utility"})
        self.assertTrue(all(r.get("reconciliation_id") == row["id"] for r in log))

    def test_idempotent_rerun_overwrites(self):
        client = FakeClient(self._seed())
        rs.run_reconciliation(client, "2026-05-29")
        rs.run_reconciliation(client, "2026-05-29")
        # Still one reconciliation row (UPSERT), and the match log was rewritten,
        # not duplicated.
        self.assertEqual(len(client.store["purchase_reconciliation"]), 1)
        self.assertEqual(len(client.store["purchase_match_log"]), 3)


class BotWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_new_commands_registered(self):
        for cmd, handler in [
            ("food_cost_today", "food_cost_today_command"),
            ("food_cost_outlet", "food_cost_outlet_command"),
            ("cash_no_receipt_today", "cash_no_receipt_today_command"),
            ("reconcile_now", "reconcile_now_command"),
        ]:
            self.assertIn(f'CommandHandler("{cmd}", {handler})', self.src)

    def test_reconcile_runs_before_digest_cron(self):
        with open(os.path.join(REPO_ROOT, "scripts", "send_daily_digest.py")) as f:
            src = f.read()
        self.assertIn("_reconcile_before_digest(client, now_my)", src)


class Migration(unittest.TestCase):
    def test_0022_creates_both_tables(self):
        with open(os.path.join(REPO_ROOT, "migrations", "0022_purchase_reconciliation.sql")) as f:
            sql = f.read()
        self.assertIn("CREATE TABLE IF NOT EXISTS public.purchase_reconciliation", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS public.purchase_match_log", sql)
        self.assertIn("UNIQUE (outlet_canonical, business_date)", sql)
        self.assertIn("idx_purchase_reconciliation_outlet_date", sql)
        self.assertIn("idx_purchase_match_log_reconciliation", sql)
        for code in ("A_matched", "B_cash_no_receipt", "C_account_only",
                     "D_excluded_staff", "E_excluded_utility"):
            self.assertIn(code, sql)


if __name__ == "__main__":
    unittest.main()
