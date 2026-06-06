"""Integration tests for reconciliation_service against a fake Supabase client
(PR #37), plus a bot wiring assertion."""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

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
        self._negate_next = False

    def _add(self, col, pred):
        if self._negate_next:
            base = pred
            pred = lambda v, base=base: not base(v)  # noqa: E731
            self._negate_next = False
        self._filters.append((col, pred))
        return self

    # --- builders ---
    @property
    def not_(self):
        self._negate_next = True
        return self

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def eq(self, col, val):
        return self._add(col, lambda v, val=val: v == val)

    def in_(self, col, vals):
        vals = set(vals)
        return self._add(col, lambda v, vals=vals: v in vals)

    def is_(self, col, _val):
        # Only the "null" form is used by the code under test.
        return self._add(col, lambda v: v is None)

    def gte(self, col, val):
        return self._add(col, lambda v, val=val: v is not None and str(v) >= str(val))

    def lte(self, col, val):
        return self._add(col, lambda v, val=val: v is not None and str(v) <= str(val))

    def lt(self, col, val):
        return self._add(col, lambda v, val=val: v is not None and str(v) < str(val))

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

    def test_production_outlet_variants_now_match(self):
        # Hotfix regression: receipts arrive with messy outlet strings
        # ("HJ SHARFUDDIN SEK 6") while sales key on the canonical "SEK-6".
        # Before the resolver fix these receipts were dropped, leaving 0 matches
        # and every payout misclassified as cash-no-receipt.
        seed = {
            "merchant_canonical": [{"id": 1, "display_name": "BABAS"}],
            "receipts": [
                {"id": 1001, "total": 60.0, "merchant": "BABAS PRODUCTS",
                 "merchant_canonical_id": 1, "outlet": "HJ SHARFUDDIN SEK 6",
                 "receipt_date": "2026-05-26", "receipt_type": "SUPPLIER_PURCHASE"},
            ],
            "sales_daily_summary": [
                {"id": 5001, "outlet_canonical": "SEK-6", "business_date": "2026-05-26",
                 "day_sales": 600.0},
            ],
            "sales_daily_payouts": [
                {"id": 7001, "summary_id": 5001, "description": "PAY TO BABAS",
                 "vendor_name": "BABAS", "amount": 60.0},
            ],
        }
        client = FakeClient(seed)
        rs.run_reconciliation(client, "2026-05-26")
        row = client.store["purchase_reconciliation"][0]
        self.assertEqual(row["outlet_canonical"], "SEK-6")
        self.assertEqual(row["matched_count"], 1)            # was 0 before the fix
        self.assertEqual(row["unmatched_pos_payouts"], 0)    # no false Type B
        self.assertEqual(row["food_cost_percent"], 10.0)

    def test_idempotent_rerun_overwrites(self):
        client = FakeClient(self._seed())
        rs.run_reconciliation(client, "2026-05-29")
        rs.run_reconciliation(client, "2026-05-29")
        # Still one reconciliation row (UPSERT), and the match log was rewritten,
        # not duplicated.
        self.assertEqual(len(client.store["purchase_reconciliation"]), 1)
        self.assertEqual(len(client.store["purchase_match_log"]), 3)


class IncludeUnknownReceiptsTests(unittest.TestCase):
    """Hotfix: count food spend from un-canonicalised receipts, with safeguards."""

    def _seed(self, receipts):
        return {
            "merchant_canonical": [{"id": 1, "display_name": "BABAS"}],
            "receipts": receipts,
            "sales_daily_summary": [
                {"id": 5001, "outlet_canonical": "Vista", "business_date": "2026-05-26",
                 "day_sales": 1000.0},
            ],
            "sales_daily_payouts": [],
        }

    def test_includes_unknown_receipt_type_in_reconciliation(self):
        # An UNKNOWN-type receipt (merchant not yet canonicalised) must still
        # count toward food cost — this is the whole point of the hotfix.
        client = FakeClient(self._seed([
            {"id": 1, "total": 120.0, "merchant": "SOME NEW SUPPLIER",
             "merchant_canonical_id": None, "outlet": "Vista",
             "receipt_date": "2026-05-26", "receipt_type": "UNKNOWN"},
        ]))
        rs.run_reconciliation(client, "2026-05-26")
        row = client.store["purchase_reconciliation"][0]
        self.assertEqual(row["total_food_purchases"], 120.0)
        self.assertEqual(row["unmatched_receipts"], 1)        # Type C account-only
        self.assertEqual(row["food_cost_percent"], 12.0)

    def test_excludes_staff_advance_receipt_type(self):
        # STAFF_ADVANCE is non-food and must never be fetched/counted.
        client = FakeClient(self._seed([
            {"id": 1, "total": 300.0, "merchant": "KARUNGARAJ",
             "merchant_canonical_id": None, "outlet": "Vista",
             "receipt_date": "2026-05-26", "receipt_type": "STAFF_ADVANCE"},
        ]))
        rs.run_reconciliation(client, "2026-05-26")
        row = client.store["purchase_reconciliation"][0]
        self.assertEqual(row["total_food_purchases"], 0.0)
        self.assertEqual(row["total_receipts"], 0)

    def test_skips_receipts_outside_amount_bounds(self):
        client = FakeClient(self._seed([
            {"id": 1, "total": 2.0, "merchant": "X", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": "2026-05-26", "receipt_type": "UNKNOWN"},
            {"id": 2, "total": 9999.0, "merchant": "Y", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": "2026-05-26", "receipt_type": "UNKNOWN"},
            {"id": 3, "total": 50.0, "merchant": "Z", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": "2026-05-26", "receipt_type": "UNKNOWN"},
        ]))
        rs.run_reconciliation(client, "2026-05-26")
        row = client.store["purchase_reconciliation"][0]
        # Only the RM50 receipt survives the (RM5, RM5000] sanity window.
        self.assertEqual(row["total_food_purchases"], 50.0)
        self.assertEqual(row["total_receipts"], 1)

    def test_logs_warning_for_unresolved_outlet(self):
        client = FakeClient(self._seed([
            {"id": 1, "total": 60.0, "merchant": "X", "merchant_canonical_id": None,
             "outlet": "Some Cafe That Does Not Map", "receipt_date": "2026-05-26",
             "receipt_type": "UNKNOWN"},
        ]))
        with self.assertLogs("reconciliation_service", level="WARNING") as cm:
            rs.run_reconciliation(client, "2026-05-26")
        self.assertTrue(any("did not resolve" in m for m in cm.output))

    def test_flags_unclassified_merchants_in_match_log(self):
        client = FakeClient(self._seed([
            {"id": 1, "total": 120.0, "merchant": "NEW SUPPLIER",
             "merchant_canonical_id": None, "outlet": "Vista",
             "receipt_date": "2026-05-26", "receipt_type": "UNKNOWN"},
            {"id": 2, "total": 40.0, "merchant": "PETTY", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": "2026-05-26", "receipt_type": "PETTY_CASH"},
        ]))
        rs.run_reconciliation(client, "2026-05-26")
        log = client.store["purchase_match_log"]
        by_amount = {r["amount"]: r.get("receipt_classification") for r in log}
        self.assertEqual(by_amount[120.0], "unknown_included")
        self.assertEqual(by_amount[40.0], "petty_cash")

    def test_null_receipt_date_falls_back_to_created_at(self):
        # OCR didn't extract a date; the receipt was uploaded on 2026-05-26 MY
        # (created_at ~11:00 MY = 03:00 UTC). It must still be counted.
        client = FakeClient(self._seed([
            {"id": 1, "total": 75.0, "merchant": "X", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": None, "receipt_type": "UNKNOWN",
             "created_at": "2026-05-26T03:00:00+00:00"},
        ]))
        rs.run_reconciliation(client, "2026-05-26")
        row = client.store["purchase_reconciliation"][0]
        self.assertEqual(row["total_food_purchases"], 75.0)


class OcrDateClampTests(unittest.TestCase):
    """PR #64: a receipt whose OCR date is >3 days in the future is counted on
    its upload day rather than dropped onto a future day no one reconciles."""

    def _seed(self, receipts):
        return {
            "merchant_canonical": [{"id": 1, "display_name": "BABAS"}],
            "receipts": receipts,
            "sales_daily_summary": [
                {"id": 5001, "outlet_canonical": "Vista", "business_date": "2026-05-26",
                 "day_sales": 1000.0},
            ],
            "sales_daily_payouts": [],
        }

    def test_future_dated_receipt_clamped_to_upload_day(self):
        # Uploaded 2026-05-26 (11:00 MY) but OCR read 2026-07-01 (~5 weeks out).
        client = FakeClient(self._seed([
            {"id": 1, "total": 90.0, "merchant": "X", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": "2026-07-01", "receipt_type": "UNKNOWN",
             "created_at": "2026-05-26T03:00:00+00:00"},
        ]))
        result = rs.run_reconciliation(client, "2026-05-26")
        self.assertEqual(result["clamped_receipts"], 1)
        row = client.store["purchase_reconciliation"][0]
        self.assertEqual(row["total_food_purchases"], 90.0)   # counted, not dropped
        self.assertEqual(row["total_receipts"], 1)

    def test_warns_when_clamping(self):
        client = FakeClient(self._seed([
            {"id": 1, "total": 90.0, "merchant": "X", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": "2026-07-01", "receipt_type": "UNKNOWN",
             "created_at": "2026-05-26T03:00:00+00:00"},
        ]))
        with self.assertLogs("reconciliation_service", level="WARNING") as cm:
            rs.run_reconciliation(client, "2026-05-26")
        self.assertTrue(any("clamped" in m for m in cm.output))

    def test_in_window_future_date_not_clamped(self):
        # 2 days ahead is plausible (date roll-over, slow upload) — keep as-is,
        # so it stays on its own day and isn't counted on the upload day.
        client = FakeClient(self._seed([
            {"id": 1, "total": 90.0, "merchant": "X", "merchant_canonical_id": None,
             "outlet": "Vista", "receipt_date": "2026-05-28", "receipt_type": "UNKNOWN",
             "created_at": "2026-05-26T03:00:00+00:00"},
        ]))
        result = rs.run_reconciliation(client, "2026-05-26")
        self.assertEqual(result["clamped_receipts"], 0)
        # The receipt belongs to 2026-05-28, so the upload day's row (created
        # only because a sales summary exists) carries no food purchases.
        row = client.store["purchase_reconciliation"][0]
        self.assertEqual(row["total_receipts"], 0)
        self.assertEqual(row["total_food_purchases"], 0.0)


class DryRunTests(unittest.TestCase):
    """Backfill preview: run_reconciliation(dry_run=True) computes the would-be
    rows (so sales_total can be eyeballed) but writes nothing."""

    def _seed(self):
        return {
            "merchant_canonical": [{"id": 1, "display_name": "BABAS"}],
            "receipts": [
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
            ],
        }

    def test_dry_run_computes_rows_without_writing(self):
        client = FakeClient(self._seed())
        result = rs.run_reconciliation(client, "2026-05-29", dry_run=True)
        self.assertEqual(result["outlets_processed"], 1)
        row = result["rows"][0]
        self.assertEqual(row["outlet_canonical"], "Vista")
        self.assertEqual(row["sales_total"], 600.0)            # re-pulled from summary
        self.assertEqual(row["total_food_purchases"], 60.0)
        self.assertIsNone(row["id"])                            # never persisted
        # Nothing was written to either destination table.
        self.assertNotIn("purchase_reconciliation", client.store)
        self.assertNotIn("purchase_match_log", client.store)

    def test_dry_run_then_apply_writes_same_sales_total(self):
        client = FakeClient(self._seed())
        preview = rs.run_reconciliation(client, "2026-05-29", dry_run=True)["rows"][0]
        applied = rs.run_reconciliation(client, "2026-05-29")["rows"][0]
        self.assertEqual(preview["sales_total"], applied["sales_total"])
        self.assertEqual(len(client.store["purchase_reconciliation"]), 1)


class BackfillScriptTests(unittest.TestCase):
    """scripts/backfill_reconciliation.py: re-pull sales_total over a date range."""

    def setUp(self):
        import backfill_reconciliation
        self.bf = backfill_reconciliation

    def _seed_stale(self):
        # A frozen row: sales_total=NULL though the summary now has the sales.
        return {
            "merchant_canonical": [{"id": 1, "display_name": "BABAS"}],
            "receipts": [
                {"id": 1001, "total": 60.0, "merchant": "BABAS",
                 "merchant_canonical_id": 1, "outlet": "Vista",
                 "receipt_date": "2026-06-04", "receipt_type": "SUPPLIER_PURCHASE"},
            ],
            "sales_daily_summary": [
                {"id": 5001, "outlet_canonical": "Vista", "business_date": "2026-06-04",
                 "day_sales": 15521.50},
            ],
            "sales_daily_payouts": [],
            "purchase_reconciliation": [
                {"id": 9001, "outlet_canonical": "Vista", "business_date": "2026-06-04",
                 "sales_total": None, "total_food_purchases": 60.0},
            ],
        }

    def test_dry_run_previews_fill_and_writes_nothing(self):
        client = FakeClient(self._seed_stale())
        d = self.bf.date(2026, 6, 4)
        stats = self.bf.backfill(client, d, d, dry_run=True)
        self.assertEqual(stats["FILLED"], 1)
        # The stored row is untouched — still NULL after a dry run.
        self.assertIsNone(client.store["purchase_reconciliation"][0]["sales_total"])

    def test_apply_fills_sales_total(self):
        client = FakeClient(self._seed_stale())
        d = self.bf.date(2026, 6, 4)
        self.bf.backfill(client, d, d, dry_run=False)
        rows = client.store["purchase_reconciliation"]
        self.assertEqual(len(rows), 1)                          # UPSERT, not duplicated
        self.assertEqual(rows[0]["sales_total"], 15521.50)

    def test_apply_creates_missing_outlet_row(self):
        # An outlet present in the summary but with no prior recon row (the D.U
        # case) gets a fresh row on apply.
        seed = self._seed_stale()
        seed["sales_daily_summary"].append(
            {"id": 5002, "outlet_canonical": "D.U", "business_date": "2026-06-04",
             "day_sales": 5302.60})
        client = FakeClient(seed)
        d = self.bf.date(2026, 6, 4)
        self.bf.backfill(client, d, d, dry_run=False)
        by_outlet = {r["outlet_canonical"]: r for r in client.store["purchase_reconciliation"]}
        self.assertIn("D.U", by_outlet)
        self.assertEqual(by_outlet["D.U"]["sales_total"], 5302.60)


class ReconcileWindowTests(unittest.TestCase):
    """Part 2 (Option A): the nightly pre-digest refresh covers the full
    food-cost rolling window, not just today + yesterday."""

    def test_reconcile_before_digest_covers_full_rolling_window(self):
        import datetime as _dt

        import digest_data
        import send_daily_digest as sdd

        captured = {}

        def _fake(_client, dates, **_kw):
            captured["dates"] = list(dates)
            return [{"business_date": d, "outlets_processed": 0} for d in dates]

        orig = sdd.reconciliation_service.run_reconciliation_for_dates
        sdd.reconciliation_service.run_reconciliation_for_dates = _fake
        try:
            now_my = _dt.datetime(2026, 6, 6, 23, 0, tzinfo=digest_data.MALAYSIA_TZ)
            sdd._reconcile_before_digest(object(), now_my)
        finally:
            sdd.reconciliation_service.run_reconciliation_for_dates = orig

        self.assertEqual(len(captured["dates"]), digest_data.FOOD_COST_LOOKBACK_DAYS)
        self.assertEqual(captured["dates"][0], "2026-06-06")     # today first
        self.assertEqual(captured["dates"][-1], "2026-05-31")    # 7th day back
        self.assertEqual(len(set(captured["dates"])), len(captured["dates"]))  # distinct


class BotWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_new_commands_registered(self):
        for cmd, handler in [
            ("food_cost_today", "food_cost_today_command"),
            ("food_cost_week", "food_cost_week_command"),
            ("food_cost_month", "food_cost_month_command"),
            ("food_cost_outlet", "food_cost_outlet_command"),
            ("cash_no_receipt_today", "cash_no_receipt_today_command"),
            ("reconcile_now", "reconcile_now_command"),
            ("reconcile_date", "reconcile_date_command"),
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

    def test_0023_adds_receipt_classification(self):
        with open(os.path.join(REPO_ROOT, "migrations",
                               "0023_match_log_receipt_classification.sql")) as f:
            sql = f.read()
        self.assertIn("ADD COLUMN IF NOT EXISTS receipt_classification text", sql)


if __name__ == "__main__":
    unittest.main()
