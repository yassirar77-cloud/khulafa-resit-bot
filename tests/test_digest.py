"""Tests for the daily digest (PR #34)."""

import os
import sys
import types
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import digest  # noqa: E402
import send_daily_digest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MY = ZoneInfo("Asia/Kuala_Lumpur")
NOW = datetime(2026, 5, 25, 23, 0, tzinfo=MY)  # a Sunday


def _pm(item_id, item_name, merch_id, merch_name, line_total, receipt_date, **kw):
    base = {
        "receipt_id": kw.get("receipt_id", item_id * 1000),
        "receipt_date": receipt_date,
        "outlet": kw.get("outlet", "KHULAFA SEK-20"),
        "merchant_canonical_id": merch_id,
        "merchant_display_name": merch_name,
        "item_canonical_id": item_id,
        "item_display_name": item_name,
        "item_category": kw.get("item_category", "spices"),
        "unit_price": kw.get("unit_price"),
        "line_total": line_total,
        "receipt_total": kw.get("receipt_total", line_total),
    }
    return base


EMPTY_DATA = {
    "today": {"count": 0, "total": 0.0, "pending": 0},
    "pm_window_rows": [],
    "data_quality": {"low_confidence": 0, "reparse_pending": 0, "unresolved_merchants": 0},
    "outliers": {"count": 0, "threshold": 5000.0},
    "new_suppliers": [],
}


class Formatting(unittest.TestCase):
    def test_format_rm_thousands_separator(self):
        self.assertEqual(digest.format_rm(1234.5), "RM1,234.50")
        self.assertEqual(digest.format_rm(0), "RM0.00")
        self.assertEqual(digest.format_rm(None), "RM0.00")

    def test_digest_truncates_long_merchants(self):
        long = "SUPER LONG SUPPLIER NAME ENTERPRISE SDN BHD"
        out = digest.truncate(long)
        self.assertEqual(len(out), 25)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(digest.truncate("SHORT"), "SHORT")

    def test_html_escape_handles_amp_lt_gt(self):
        self.assertEqual(digest._html("A & B < C > D"), "A &amp; B &lt; C &gt; D")
        # ampersand escaped first (no double-escaping of the entity)
        self.assertEqual(digest._html("<&>"), "&lt;&amp;&gt;")
        # underscores are NOT special in HTML mode — left as-is
        self.assertEqual(digest._name("protein_seafood"), "protein_seafood")

    def test_parse_mode_attempts(self):
        self.assertEqual(digest.parse_mode_attempts(False), ["HTML", None])
        self.assertEqual(digest.parse_mode_attempts(True), [None])


class Sections(unittest.TestCase):
    def test_digest_renders_with_zero_receipts(self):
        msgs = digest.build_digest_messages(EMPTY_DATA, NOW)
        self.assertTrue(msgs)
        joined = "\n\n".join(msgs)
        self.assertIn("0 receipts processed", joined)
        self.assertIn("KHULAFA DAILY DIGEST", joined)

    def test_digest_includes_all_8_sections(self):
        joined = "\n\n".join(digest.build_digest_messages(EMPTY_DATA, NOW))
        for header in digest.SECTION_HEADERS:
            self.assertIn(header, joined, f"missing section: {header}")
        self.assertEqual(len(digest.SECTION_HEADERS), 8)

    def test_digest_handles_empty_price_alerts_section(self):
        joined = "\n\n".join(digest.build_digest_messages(EMPTY_DATA, NOW))
        self.assertIn("No significant price changes", joined)

    def test_no_unescaped_angle_brackets_for_html_mode(self):
        # After removing the only allowed tags, no bare < or > may remain — those
        # would make Telegram's HTML parser choke (the original bug was "<60").
        data = dict(EMPTY_DATA)
        data["pm_window_rows"] = [_pm(2, "ayam bersih", 11, "BS FROZEN", 800, "2026-05-24")]
        joined = "\n\n".join(digest.build_digest_messages(data, NOW))
        stripped = joined
        for tag in ("<b>", "</b>", "<i>", "</i>"):
            stripped = stripped.replace(tag, "")
        self.assertNotIn("<", stripped)
        self.assertNotIn(">", stripped)

    def test_digest_filters_outliers_above_5000(self):
        data = dict(EMPTY_DATA)
        data["pm_window_rows"] = [
            # phantom: one item line summing > RM5000 (the curry-powder-fish case)
            _pm(1, "curry powder fish", 10, "RK MUBARAKA", 12600, "2026-05-24"),
            _pm(2, "ayam bersih", 11, "BS FROZEN", 800, "2026-05-24"),
        ]
        joined = "\n\n".join(digest.build_digest_messages(data, NOW))
        self.assertNotIn("curry powder fish", joined)   # outlier item dropped
        self.assertIn("ayam bersih", joined)            # normal item kept
        # supplier-side outlier filter too
        self.assertNotIn("RK MUBARAKA", joined)

    def test_outlier_filter_helpers(self):
        rows = [
            _pm(1, "curry powder fish", 10, "RK MUBARAKA", 12600, "2026-05-24"),
            _pm(2, "ayam bersih", 11, "BS FROZEN", 800, "2026-05-24"),
        ]
        items = digest.aggregate_items(rows, 5)
        self.assertEqual([i["name"] for i in items], ["ayam bersih"])
        suppliers = digest.aggregate_suppliers(rows, 5)
        self.assertEqual([s["name"] for s in suppliers], ["BS FROZEN"])

    def test_top_items_excludes_zero_spend(self):
        rows = [
            _pm(1, "asam jawa drink", 10, "KM SETIA", 0, "2026-05-24"),
            _pm(2, "extra joss", 10, "KM SETIA", 0, "2026-05-24"),
            _pm(3, "ayam bersih", 11, "BS FROZEN", 800, "2026-05-24"),
        ]
        items = digest.aggregate_items(rows, 5)
        names = [i["name"] for i in items]
        self.assertEqual(names, ["ayam bersih"])
        self.assertNotIn("asam jawa drink", names)
        self.assertNotIn("extra joss", names)
        # same guard on suppliers (the RM0.00 supplier shouldn't rank)
        suppliers = digest.aggregate_suppliers(rows, 5)
        self.assertEqual([s["name"] for s in suppliers], ["BS FROZEN"])

    def test_long_supplier_name_truncated_in_output(self):
        data = dict(EMPTY_DATA)
        data["pm_window_rows"] = [
            _pm(2, "ayam bersih", 11, "SUPER LONG SUPPLIER NAME ENTERPRISE SDN BHD", 100, NOW.date().isoformat()),
        ]
        joined = "\n\n".join(digest.build_digest_messages(data, NOW))
        self.assertIn("…", joined)
        self.assertNotIn("SUPER LONG SUPPLIER NAME ENTERPRISE SDN BHD", joined)


class PriceAlerts(unittest.TestCase):
    def test_alert_requires_3_in_both_windows_and_10pct(self):
        # recent (last 7 days) avg 60, prior (8-14 days ago) avg 50 -> +20%, 3 each
        recent = [_pm(1, "jintan putih", 10, "BABAS", 60, "2026-05-22", unit_price=60) for _ in range(3)]
        prior = [_pm(1, "jintan putih", 10, "BABAS", 50, "2026-05-14", unit_price=50) for _ in range(3)]
        alerts = digest.price_alerts(recent, prior)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["direction"], "up")
        self.assertAlmostEqual(alerts[0]["pct"], 20.0)

    def test_no_alert_below_threshold_or_count(self):
        # only 5% change -> no alert
        recent = [_pm(1, "x", 10, "BABAS", 0, "2026-05-22", unit_price=52.5) for _ in range(3)]
        prior = [_pm(1, "x", 10, "BABAS", 0, "2026-05-14", unit_price=50) for _ in range(3)]
        self.assertEqual(digest.price_alerts(recent, prior), [])
        # big change but only 2 samples -> no alert
        recent2 = [_pm(1, "x", 10, "BABAS", 0, "2026-05-22", unit_price=80) for _ in range(2)]
        prior2 = [_pm(1, "x", 10, "BABAS", 0, "2026-05-14", unit_price=50) for _ in range(3)]
        self.assertEqual(digest.price_alerts(recent2, prior2), [])


class Splitting(unittest.TestCase):
    def test_split_long_message_at_section_boundary(self):
        blocks = ["A" * 2000, "B" * 2000, "C" * 2000]
        msgs = digest.pack_messages(blocks, limit=4096)
        self.assertEqual(len(msgs), 2)
        for m in msgs:
            self.assertLessEqual(len(m), 4096)
        # blocks stay intact (never torn mid-section)
        self.assertIn("A" * 2000, msgs[0])
        self.assertIn("B" * 2000, msgs[0])
        self.assertEqual(msgs[1], "C" * 2000)

    def test_short_digest_is_one_message(self):
        self.assertEqual(len(digest.build_digest_messages(EMPTY_DATA, NOW)), 1)


# --- send orchestration -----------------------------------------------------

class _FakeQuery:
    def __init__(self, store, table):
        self.store, self.table, self.payload = store, table, None

    def insert(self, payload):
        self.payload = payload
        return self

    def execute(self):
        self.store.setdefault(self.table, []).append(self.payload)
        return types.SimpleNamespace(data=[self.payload])


class FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeQuery(self.store, name)


class SendDigest(unittest.TestCase):
    def test_send_daily_digest_logs_to_digest_log_table(self):
        client = FakeClient()
        sent = []
        summary = send_daily_digest.run(
            client, recipients=[123], now_my=NOW,
            send_fn=lambda r, t, pm: sent.append((r, t, pm)), data=EMPTY_DATA,
        )
        self.assertEqual(summary, {123: "success"})
        self.assertEqual(len(sent), 1)             # one message for the empty digest
        self.assertEqual(sent[0][2], "HTML")        # first attempt is HTML
        logs = client.store.get("digest_log", [])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["recipient"], 123)
        self.assertEqual(logs[0]["status"], "success")

    def test_logs_message_bytes(self):
        client = FakeClient()
        send_daily_digest.run(
            client, recipients=[123], now_my=NOW,
            send_fn=lambda r, t, pm: None, data=EMPTY_DATA,
        )
        full_text = "\n\n".join(digest.build_digest_messages(EMPTY_DATA, NOW))
        self.assertEqual(client.store["digest_log"][0]["message_bytes"], len(full_text.encode("utf-8")))

    def test_send_falls_back_to_plain_text_on_parse_failure(self):
        client = FakeClient()
        attempts = []

        def html_fails(recipient, text, parse_mode):
            attempts.append(parse_mode)
            if parse_mode == "HTML":
                raise RuntimeError("Bad Request: can't parse entities")
            # parse_mode=None (plain) succeeds

        summary = send_daily_digest.run(
            client, recipients=[123], now_my=NOW, send_fn=html_fails, data=EMPTY_DATA,
        )
        self.assertEqual(summary, {123: "success"})       # delivered via fallback
        self.assertEqual(attempts, ["HTML", None])         # tried HTML, then plain
        log = client.store["digest_log"][0]
        self.assertEqual(log["status"], "success")
        self.assertIn("plain text", log["error_msg"])      # fallback noted

    def test_plain_mode_skips_html(self):
        client = FakeClient()
        attempts = []
        send_daily_digest.run(
            client, recipients=[123], now_my=NOW,
            send_fn=lambda r, t, pm: attempts.append(pm), data=EMPTY_DATA, plain=True,
        )
        self.assertEqual(attempts, [None])  # never tries HTML

    def test_send_records_failure(self):
        client = FakeClient()

        def boom(r, t, pm):
            raise RuntimeError("Telegram error: chat not found")

        summary = send_daily_digest.run(
            client, recipients=[999], now_my=NOW, send_fn=boom, data=EMPTY_DATA,
        )
        self.assertEqual(summary, {999: "failed"})
        logs = client.store.get("digest_log", [])
        self.assertEqual(logs[0]["status"], "failed")
        self.assertIn("chat not found", logs[0]["error_msg"])


class Migration(unittest.TestCase):
    def test_digest_log_table(self):
        with open(os.path.join(REPO_ROOT, "migrations", "0013_digest_log.sql")) as f:
            sql = f.read()
        self.assertIn("CREATE TABLE IF NOT EXISTS public.digest_log", sql)
        self.assertIn("status        text CHECK (status IN ('success', 'failed', 'partial'))", sql)
        self.assertIn("recipient     bigint NOT NULL", sql)

    def test_message_bytes_column_migration(self):
        with open(os.path.join(REPO_ROOT, "migrations", "0014_digest_log_message_bytes.sql")) as f:
            sql = f.read()
        self.assertIn("ADD COLUMN IF NOT EXISTS message_bytes integer", sql)


class BotCommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_test_digest_command_owner_only(self):
        idx = self.src.index("async def test_digest_command(")
        self.assertIn("is_reviewer(_command_owner_id(update))", self.src[idx:idx + 600])

    def test_test_digest_registered(self):
        self.assertIn('CommandHandler("test_digest", test_digest_command)', self.src)


if __name__ == "__main__":
    unittest.main()
