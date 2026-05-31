"""Tests for risk-weighted merchant auto-resolution (PR #68).

Pure stages (normalise / score / decide / queue / format) are tested directly.
The DB layer (auto-resolve, undo, one-pass backfill) runs against the in-memory
FakeSupabase double with a recording reconcile stub, so the reconcile-on-write
contract is asserted without Supabase. The migration and bot wiring are checked
source-level (bot.py can't be imported in CI).
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import merchant_auto_resolve as mar  # noqa: E402
from merchant_auto_resolve import (  # noqa: E402
    AUTO_RESOLVE_CONF_CUTOFF,
    DECISION_AUTO,
    DECISION_DEFER,
    DECISION_ESCALATE,
    ESCALATION_RISK_THRESHOLD,
    best_canonical,
    decide,
    fetch_review_queue,
    format_review_digest_line,
    format_review_queue,
    match_confidence,
    normalize_merchant_name,
    rank_review_queue,
    resolve_all,
    risk_score,
    undo_resolution,
)
from tests.fake_supabase import FakeSupabase  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANONICALS = [
    {"id": 1, "display_name": "EVEREST"},
    {"id": 2, "display_name": "BABAS"},
    {"id": 3, "display_name": "MEWAH"},
    {"id": 15, "display_name": "FOOK LEONG"},
]
ALIASES = [
    {"alias_text": "EVEREST AISVARAM SDN BHD", "canonical_id": 1},
    {"alias_text": "EVEREST", "canonical_id": 1},
    {"alias_text": "BABAS PRODUCTS (M) SDN BHD", "canonical_id": 2},
    {"alias_text": "MEWAH GROUP", "canonical_id": 3},
    {"alias_text": "FOOK LEONG SEAFOOD SDN BHD", "canonical_id": 15},
]


# === Part 1: normalisation ===================================================

class NormalizeMerchantName(unittest.TestCase):
    def test_uppercases_strips_punct_and_collapses_ws(self):
        self.assertEqual(
            normalize_merchant_name("  babas   products,  (m). "),
            "BABAS PRODUCTS M",
        )

    def test_strips_sdn_bhd_full_variant(self):
        self.assertEqual(
            normalize_merchant_name("EVEREST AISVARAM SDN. BHD."),
            "EVEREST AISVARAM",
        )

    def test_strips_truncated_sdn_bh(self):
        # OCR clipped the final D of BHD.
        self.assertEqual(
            normalize_merchant_name("EVEREST AISVARAM SDN. BH"),
            "EVEREST AISVARAM",
        )

    def test_strips_berhad_and_sendirian_berhad(self):
        self.assertEqual(normalize_merchant_name("TENAGA NASIONAL BERHAD"), "TENAGA NASIONAL")
        self.assertEqual(normalize_merchant_name("FOO SENDIRIAN BERHAD"), "FOO")

    def test_pure_and_idempotent(self):
        raw = "Fook Leong Seafood Sdn. Bhd.!!"
        once = normalize_merchant_name(raw)
        self.assertEqual(once, normalize_merchant_name(once))  # idempotent
        self.assertEqual(raw, "Fook Leong Seafood Sdn. Bhd.!!")  # input untouched

    def test_empty_and_none(self):
        self.assertEqual(normalize_merchant_name(""), "")
        self.assertEqual(normalize_merchant_name("   "), "")
        self.assertEqual(normalize_merchant_name(None), "")


# === Part 2: confidence scorer ===============================================

class MatchConfidence(unittest.TestCase):
    def test_exact_after_normalisation_is_one(self):
        self.assertEqual(match_confidence("EVEREST", "EVEREST"), 1.0)

    def test_anchored_containment_is_high(self):
        # Clean OCR with a legal suffix the normaliser strips: a confident match.
        conf = match_confidence("EVEREST AISVARAM SDN. BHD.", "EVEREST",
                                ["EVEREST AISVARAM SDN BHD"])
        self.assertGreaterEqual(conf, AUTO_RESOLVE_CONF_CUTOFF)

    def test_typo_is_below_cutoff(self):
        # A 1-char typo is NOT a word-bounded containment -> stays below the
        # auto-resolve cutoff so the risk model gets to weigh in.
        conf = match_confidence("MEWAHH GROUPP", "MEWAH", ["MEWAH GROUP"])
        self.assertLess(conf, AUTO_RESOLVE_CONF_CUTOFF)

    def test_unrelated_is_low(self):
        self.assertLess(match_confidence("RANDOM XYZ VENDOR", "EVEREST", ["EVEREST"]), 0.5)

    def test_best_canonical_picks_highest(self):
        cid, conf = best_canonical("FOOK LEONG SEA PRODUCTS SDN BHD", ALIASES, CANONICALS)
        self.assertEqual(cid, 15)
        self.assertGreaterEqual(conf, AUTO_RESOLVE_CONF_CUTOFF)

    def test_best_canonical_no_overlap_returns_none(self):
        # Incidental character overlap stays below the floor, so no canonical is
        # attached even though the raw similarity is fractionally above zero.
        cid, conf = best_canonical("ZZZ QQQ WWW", ALIASES, CANONICALS)
        self.assertIsNone(cid)
        self.assertLess(conf, mar.MIN_CANDIDATE_CONFIDENCE)


# === Part 3: the CORE risk-weighted decision =================================

class RiskFormula(unittest.TestCase):
    def test_risk_is_one_minus_conf_times_rm(self):
        self.assertAlmostEqual(risk_score(0.7, 420.0), 0.3 * 420.0)
        self.assertAlmostEqual(risk_score(1.0, 5000.0), 0.0)


class DecisionWorkedExamples(unittest.TestCase):
    """The four worked examples from the brief — these must hold."""

    def test_1_high_conf_large_rm_auto_resolves(self):
        decision, risk = decide(0.96, 1240.50)
        self.assertEqual(decision, DECISION_AUTO)
        self.assertAlmostEqual(risk, 0.04 * 1240.50)

    def test_2_high_conf_tiny_rm_auto_resolves(self):
        decision, _ = decide(0.92, 6.00)
        self.assertEqual(decision, DECISION_AUTO)

    def test_3_low_conf_low_risk_defers(self):
        # risk = 0.30 * 18 = 5.4 < 50 -> silent long tail.
        decision, risk = decide(0.70, 18.00)
        self.assertEqual(decision, DECISION_DEFER)
        self.assertLess(risk, ESCALATION_RISK_THRESHOLD)

    def test_4_low_conf_high_rm_escalates(self):
        # risk = 0.35 * 700 = 245 >= 200 -> owner review (the high-value tail).
        decision, risk = decide(0.65, 700.00)
        self.assertEqual(decision, DECISION_ESCALATE)
        self.assertGreaterEqual(risk, ESCALATION_RISK_THRESHOLD)

    def test_mid_risk_defers_after_production_tune(self):
        # risk = 0.35 * 420 = 147 < 200 -> defers under the tuned threshold
        # (this exact case escalated under the original RM50).
        decision, risk = decide(0.65, 420.00)
        self.assertEqual(decision, DECISION_DEFER)
        self.assertLess(risk, ESCALATION_RISK_THRESHOLD)

    def test_threshold_boundary_escalates(self):
        # Exactly at the threshold escalates (>=), so the conservative tuning
        # never lets a borderline-risk match slip into a silent defer.
        decision, risk = decide(0.50, ESCALATION_RISK_THRESHOLD / 0.5)
        self.assertEqual(decision, DECISION_ESCALATE)
        self.assertAlmostEqual(risk, ESCALATION_RISK_THRESHOLD)

    def test_constants_are_conservative(self):
        # Sanity-pin the exposed knobs so a careless retune is caught in review.
        self.assertEqual(AUTO_RESOLVE_CONF_CUTOFF, 0.90)
        self.assertEqual(ESCALATION_RISK_THRESHOLD, 200.0)


# === Part 5: review queue + digest line ======================================

class ReviewQueue(unittest.TestCase):
    def test_ranked_by_rm_descending(self):
        rows = [
            {"id": 1, "raw_merchant": "A", "rm_at_stake": 30, "risk": 10},
            {"id": 2, "raw_merchant": "B", "rm_at_stake": 900, "risk": 300},
            {"id": 3, "raw_merchant": "C", "rm_at_stake": 120, "risk": 40},
        ]
        ranked = rank_review_queue(rows)
        self.assertEqual([r["id"] for r in ranked], [2, 3, 1])

    def test_format_queue_lists_highest_first(self):
        rows = rank_review_queue([
            {"id": 7, "raw_merchant": "BIG VENDOR", "rm_at_stake": 900, "risk": 315,
             "confidence": 0.65, "canonical_id": 2},
            {"id": 8, "raw_merchant": "SMALL VENDOR", "rm_at_stake": 60, "risk": 21,
             "confidence": 0.65, "canonical_id": None},
        ])
        out = format_review_queue(rows, {2: "BABAS"})
        self.assertIn("BIG VENDOR", out)
        self.assertIn("RM900.00", out)
        self.assertIn("best guess: BABAS", out)
        self.assertLess(out.index("BIG VENDOR"), out.index("SMALL VENDOR"))

    def test_digest_line_only_when_non_empty(self):
        self.assertIsNone(format_review_digest_line([]))
        line = format_review_digest_line([
            {"raw_merchant": "BIG VENDOR", "rm_at_stake": 900},
            {"raw_merchant": "X", "rm_at_stake": 100},
        ])
        self.assertIn("2 merchant", line)
        self.assertIn("BIG VENDOR", line)
        self.assertIn("/merchant_review", line)


# === Part 4/6: DB layer (FakeSupabase + recording reconcile) =================

def _seed_db():
    db = FakeSupabase()
    for c in CANONICALS:
        db.table("merchant_canonical").insert(
            {"id": c["id"], "display_name": c["display_name"]}
        ).execute()
    for a in ALIASES:
        db.table("merchant_alias").insert(dict(a)).execute()
    return db


class _RecordingReconcile:
    def __init__(self):
        self.calls = []

    def __call__(self, _client, dates):
        self.calls.append(list(dates))


class ResolveAll(unittest.TestCase):
    def setUp(self):
        self.db = _seed_db()
        self.reconcile = _RecordingReconcile()

    def _add_receipt(self, rid, merchant, total, business_date):
        self.db.table("receipts").insert({
            "id": rid, "merchant": merchant, "total": total,
            "receipt_date": business_date, "created_at": business_date + "T02:00:00+00:00",
            "merchant_canonical_id": None,
        }).execute()

    def test_auto_resolve_tags_writes_alias_log_and_reconciles(self):
        # Clean match, large RM -> auto-resolve.
        self._add_receipt(101, "FOOK LEONG SEA PRODUCTS SDN BHD", 800.0, "2026-05-20")
        stats = resolve_all(self.db, actor=42, now="2026-05-30T00:00:00+00:00",
                            reconcile_fn=self.reconcile)
        self.assertEqual(stats["auto_resolved"], 1)
        self.assertEqual(stats["receipts_tagged"], 1)
        # Receipt tagged to FOOK LEONG (#15).
        receipt = self.db.rows("receipts")[0]
        self.assertEqual(receipt["merchant_canonical_id"], 15)
        # Alias written via auto_resolved.
        new_alias = [a for a in self.db.rows("merchant_alias")
                     if a["alias_text"] == "FOOK LEONG SEA PRODUCTS SDN BHD"]
        self.assertEqual(len(new_alias), 1)
        self.assertEqual(new_alias[0]["created_via"], "auto_resolved")
        # Log row written, active, with undo metadata.
        log = self.db.rows("merchant_resolution_log")[0]
        self.assertEqual(log["decision"], "auto_resolved")
        self.assertEqual(log["status"], "active")
        self.assertEqual(log["receipt_ids"], [101])
        self.assertEqual(log["affected_dates"], ["2026-05-20"])
        # Reconcile fired once for the affected date.
        self.assertEqual(self.reconcile.calls, [["2026-05-20"]])

    def test_escalate_logs_without_tagging(self):
        # Below cutoff + high RM -> escalate, receipt left untagged.
        self._add_receipt(201, "MEWAHH GROUPP TRADING XYZ", 2000.0, "2026-05-21")
        stats = resolve_all(self.db, reconcile_fn=self.reconcile)
        self.assertEqual(stats["escalated"], 1)
        self.assertEqual(stats["auto_resolved"], 0)
        self.assertIsNone(self.db.rows("receipts")[0]["merchant_canonical_id"])
        log = self.db.rows("merchant_resolution_log")[0]
        self.assertEqual(log["decision"], "escalated")
        self.assertEqual(self.reconcile.calls, [])  # no reconcile on escalate

    def test_defer_logs_silently(self):
        # Below cutoff + tiny RM -> defer.
        self._add_receipt(301, "MEWAHH GROUPP TRADING XYZ", 9.0, "2026-05-22")
        stats = resolve_all(self.db, reconcile_fn=self.reconcile)
        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(self.db.rows("merchant_resolution_log")[0]["decision"], "deferred")

    def test_rerunnable_skips_already_tagged(self):
        self._add_receipt(401, "FOOK LEONG SEA PRODUCTS SDN BHD", 800.0, "2026-05-20")
        resolve_all(self.db, reconcile_fn=self.reconcile)
        second = resolve_all(self.db, reconcile_fn=self.reconcile)
        # Nothing left to do on the second pass.
        self.assertEqual(second["auto_resolved"], 0)
        self.assertEqual(second["receipts_tagged"], 0)
        # Exactly one auto_resolved log row across both passes.
        auto = [r for r in self.db.rows("merchant_resolution_log")
                if r["decision"] == "auto_resolved"]
        self.assertEqual(len(auto), 1)

    def test_review_queue_reads_back_escalations_by_rm(self):
        self._add_receipt(501, "ALPHA UNKNOWN VENDOR ONE", 400.0, "2026-05-21")
        self._add_receipt(502, "BETA UNKNOWN VENDOR TWO", 900.0, "2026-05-21")
        resolve_all(self.db, reconcile_fn=self.reconcile)
        queue = fetch_review_queue(self.db)
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]["raw_merchant"], "BETA UNKNOWN VENDOR TWO")  # higher RM first

    def test_undo_reverses_and_rereconciles(self):
        self._add_receipt(601, "FOOK LEONG SEA PRODUCTS SDN BHD", 800.0, "2026-05-20")
        resolve_all(self.db, reconcile_fn=self.reconcile)
        log_id = self.db.rows("merchant_resolution_log")[0]["id"]
        self.reconcile.calls.clear()

        row = undo_resolution(self.db, log_id, now="2026-05-30T01:00:00+00:00",
                              reconcile_fn=self.reconcile)
        self.assertIsNotNone(row)
        # Receipt untagged, alias removed, log marked undone.
        self.assertIsNone(self.db.rows("receipts")[0]["merchant_canonical_id"])
        self.assertEqual(
            [a for a in self.db.rows("merchant_alias")
             if a["alias_text"] == "FOOK LEONG SEA PRODUCTS SDN BHD"],
            [],
        )
        log = [r for r in self.db.rows("merchant_resolution_log") if r["id"] == log_id][0]
        self.assertEqual(log["status"], "undone")
        # Re-reconciled the affected date so food cost reverts.
        self.assertEqual(self.reconcile.calls, [["2026-05-20"]])

    def test_undo_is_idempotent(self):
        self._add_receipt(701, "FOOK LEONG SEA PRODUCTS SDN BHD", 800.0, "2026-05-20")
        resolve_all(self.db, reconcile_fn=self.reconcile)
        log_id = self.db.rows("merchant_resolution_log")[0]["id"]
        self.assertIsNotNone(undo_resolution(self.db, log_id, reconcile_fn=self.reconcile))
        self.assertIsNone(undo_resolution(self.db, log_id, reconcile_fn=self.reconcile))


# === migration + bot wiring (source-level) ===================================

class Migration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "migrations", "0025_merchant_resolution_log.sql")) as f:
            cls.sql = f.read()

    def test_creates_log_table_with_decision_and_status(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS public.merchant_resolution_log", self.sql)
        self.assertIn("'auto_resolved', 'escalated', 'deferred'", self.sql)
        self.assertIn("'active', 'undone'", self.sql)

    def test_stores_undo_metadata(self):
        self.assertIn("receipt_ids", self.sql)
        self.assertIn("affected_dates", self.sql)
        self.assertIn("alias_id", self.sql)

    def test_widens_alias_created_via_for_auto_resolved(self):
        self.assertIn("auto_resolved", self.sql)
        self.assertIn("merchant_alias_created_via_check", self.sql)


class BotWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_commands_registered(self):
        for cmd, fn in (
            ("merchant_review", "merchant_review_command"),
            ("merchant_resolve_now", "merchant_resolve_now_command"),
            ("merchant_undo", "merchant_undo_command"),
        ):
            self.assertIn(f'CommandHandler("{cmd}", {fn})', self.src)

    def test_commands_owner_gated(self):
        for fn in ("merchant_review_command", "merchant_resolve_now_command",
                   "merchant_undo_command"):
            idx = self.src.index(f"async def {fn}(")
            body = self.src[idx:idx + 600]
            self.assertIn("is_reviewer(_command_owner_id(update))", body,
                          f"{fn} is not owner-gated")


if __name__ == "__main__":
    unittest.main()
