"""Tests for the PR #37 digest bug fixes + new food-cost sections."""

import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import digest  # noqa: E402
import digest_data  # noqa: E402
import food_cost_analytics as fca  # noqa: E402

MY = ZoneInfo("Asia/Kuala_Lumpur")
NOW = datetime(2026, 5, 29, 23, 0, tzinfo=MY)

EMPTY_DATA = {
    "today": {"count": 0, "total": 0.0, "pending": 0},
    "pm_window_rows": [],
    "data_quality": {"low_confidence": 0, "reparse_pending": 0, "unresolved_merchants": 0},
    "outliers": {"count": 0, "threshold": 5000.0},
    "new_suppliers": [],
}


def _pm(item_id, item_name, merch_id, merch_name, line_total, receipt_date, **kw):
    return {
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


def _recon(outlet, sales, purchases, pct, **kw):
    row = {
        "outlet_canonical": outlet,
        "sales_total": sales,
        "total_food_purchases": purchases,
        "food_cost_percent": pct,
    }
    row.update(kw)
    return row


class Bug1TopSuppliers(unittest.TestCase):
    def test_top_suppliers_falls_back_to_receipts_when_pm_empty(self):
        # price_movements has no resolved item lines today, but receipts exist.
        data = dict(EMPTY_DATA)
        data["pm_window_rows"] = []
        data["today_suppliers"] = [
            {"name": "BABAS", "amount": 240.0, "line_count": 3},
            {"name": "EVEREST", "amount": 120.0, "line_count": 2},
        ]
        joined = "\n\n".join(digest.build_digest_messages(data, NOW))
        self.assertIn("BABAS", joined)
        self.assertIn("EVEREST", joined)
        self.assertNotIn("(no supplier purchases recorded today)", joined)


class Bug2TopItems(unittest.TestCase):
    def test_top_items_returns_5_across_categories(self):
        rows = [
            _pm(i, f"item{i}", 10, "BABAS", 100 + i, "2026-05-27",
                item_category=cat)
            for i, cat in enumerate(
                ["spices", "protein", "veg", "dairy", "dry", "drinks"], start=1
            )
        ]
        items = digest.aggregate_items(rows, 5)
        self.assertEqual(len(items), 5)
        # Highest spend first; category never filters anything out.
        self.assertEqual(items[0]["name"], "item6")


class Bug3OutletSpending(unittest.TestCase):
    def test_outlet_variants_merge_to_one_canonical(self):
        rows = [
            _pm(1, "x", 10, "BABAS", 100, "2026-05-27", outlet="SEK 20", receipt_id=1),
            _pm(2, "y", 10, "BABAS", 200, "2026-05-27", outlet="KHULAFA SEK-20", receipt_id=2),
            _pm(3, "z", 10, "BABAS", 300, "2026-05-27", outlet="Sek 20", receipt_id=3),
        ]
        outlets = digest.aggregate_outlets(rows)
        self.assertEqual(len(outlets), 1)
        self.assertEqual(outlets[0]["outlet"], "SEK-20")
        self.assertEqual(outlets[0]["amount"], 600.0)
        self.assertEqual(outlets[0]["receipt_count"], 3)

    def test_outlet_spending_data_overrides_pm(self):
        data = dict(EMPTY_DATA)
        data["outlet_spending"] = [{"outlet": "Signature", "amount": 5985.0, "receipt_count": 12}]
        joined = "\n\n".join(digest.build_digest_messages(data, NOW))
        self.assertIn("Signature", joined)
        self.assertIn("RM5,985.00", joined)


class Bug4And5NewSuppliers(unittest.TestCase):
    def setUp(self):
        self.canonicals = [{"id": 1, "display_name": "EVEREST"}]
        self.aliases = [{"alias_text": "EVEREST", "canonical_id": 1}]

    def test_excludes_khulafa_own_outlets(self):
        counts = [("NASI KANDAR KHULAFA", 11), ("REAL SUPPLIER SDN BHD", 4)]
        out = digest_data.filter_new_suppliers(counts, self.aliases, self.canonicals)
        names = [s["name"] for s in out]
        self.assertNotIn("NASI KANDAR KHULAFA", names)
        self.assertIn("REAL SUPPLIER SDN BHD", names)

    def test_excludes_already_canonicalized(self):
        counts = [("EVEREST AISVARAM SDN BHD", 12), ("BRAND NEW VENDOR", 2)]
        out = digest_data.filter_new_suppliers(counts, self.aliases, self.canonicals)
        names = [s["name"] for s in out]
        self.assertNotIn("EVEREST AISVARAM SDN BHD", names)  # resolves to EVEREST
        self.assertIn("BRAND NEW VENDOR", names)


class NewSections(unittest.TestCase):
    def _full_data(self):
        data = dict(EMPTY_DATA)
        data["sales_today"] = {
            "label": "2026-05-29", "outlets": 10, "revenue": 68420.0,
            "customers": 6800, "avg_per_customer": 10.06,
            "takeaway_pct": 40.0, "dine_in_pct": 60.0,
        }
        data["food_cost"] = {
            "label": "2026-05-29",
            "rows": [
                _recon("Vista", 12260.0, 3200.0, 26.1),
                _recon("Jakel", 4040.0, 1540.0, 38.1),
                _recon("SBESI", None, 0.0, None),
            ],
        }
        data["food_cost_anomalies"] = fca.compute_anomalies(
            {"Jakel": 38.1},
            [_recon("Jakel", None, None, 32.5, business_date="2026-05-28")],
        )
        data["cash_alerts"] = [
            {"outlet": "Klang B.Emas", "amount": 200.0, "description": "PAY TO BABAS"},
        ]
        data["top_items_yesterday"] = {
            "label": "2026-05-28",
            "items": [
                {"item_name": "Roti Canai", "qty": 1200, "amount": 1800.0},
                {"item_name": "Teh Tarik", "qty": 900, "amount": 1350.0},
            ],
        }
        return data

    def test_all_new_section_headers_present(self):
        joined = "\n\n".join(digest.build_digest_messages(self._full_data(), NOW))
        for header in digest.NEW_SECTION_HEADERS:
            self.assertIn(header, joined, f"missing section: {header}")

    def test_food_cost_flags_red_outlet(self):
        joined = "\n\n".join(digest.build_digest_messages(self._full_data(), NOW))
        self.assertIn("Jakel", joined)
        self.assertIn("38.1%", joined)
        self.assertIn("INVESTIGATE", joined)
        self.assertIn("🔴", joined)
        self.assertIn("data incomplete", joined)  # SBESI

    def test_cash_alerts_and_sales_render(self):
        joined = "\n\n".join(digest.build_digest_messages(self._full_data(), NOW))
        self.assertIn("RM68,420.00", joined)       # sales revenue
        self.assertIn("RM200.00 to BABAS", joined)  # cash-no-receipt
        self.assertIn("Roti Canai", joined)         # top items yesterday

    def test_empty_new_sections_render_safely(self):
        # EMPTY_DATA (no new keys) still produces a valid one-message digest with
        # no bare angle brackets that would break Telegram HTML.
        msgs = digest.build_digest_messages(EMPTY_DATA, NOW)
        self.assertEqual(len(msgs), 1)
        joined = "\n\n".join(msgs)
        stripped = joined
        for tag in ("<b>", "</b>", "<i>", "</i>"):
            stripped = stripped.replace(tag, "")
        self.assertNotIn("<", stripped)
        self.assertNotIn(">", stripped)

    def test_no_bare_angle_brackets_with_full_data(self):
        joined = "\n\n".join(digest.build_digest_messages(self._full_data(), NOW))
        stripped = joined
        for tag in ("<b>", "</b>", "<i>", "</i>"):
            stripped = stripped.replace(tag, "")
        self.assertNotIn("<", stripped)
        self.assertNotIn(">", stripped)


if __name__ == "__main__":
    unittest.main()
