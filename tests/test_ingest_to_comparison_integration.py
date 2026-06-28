"""Integration: real POS email ingest -> kitchen completeness gate -> comparison.

Proves the fix's core claim end-to-end WITHOUT live data: when the overnight
shift's email is ingested through the SAME parse + store path the poll uses, the
kitchen completeness gate flips to COMPLETE and the comparison reconciles that
day. Before the overnight shift is ingested the day stays INCOMPLETE (would show
"⏳ POS belum lengkap"), exactly mirroring the production timeline where the
overnight email arrives later and must be ingested before 09:00 reconciles.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import kitchen_usage as ku
import sales_ingest as si
from tests.fake_supabase import FakeSupabase

MY = ZoneInfo("Asia/Kuala_Lumpur")
OUTLETS = {"S-SEK20": {"canonical_name": "SEK-20", "active": True, "confirmed": True}}


def _shift_content(date_str, hms):
    # Minimal but parser-valid shift-close body: a DATE line (-> close_time, which
    # drives shift_type/business_date) and a TODAY SALES line (-> total_sales).
    return "\n".join([
        "SHIFTNO : 1500",
        "CASHIER : Ali",
        f"DATE : {date_str} {hms}",
        "TODAY SALES        :      1,234.50",
        "NET SALES          :      1,234.50",
    ])


def _email(content, mid):
    return {
        "content": content, "email_type": "S", "outlet_code": "S-SEK20",
        "subject": f"S-SEK20 SHIFTCLOSE ({mid})", "message_id": f"<{mid}@pos>",
        "shift_no_from_subject": "1500", "filename": "s.TXT",
        "received_at": "Thu, 25 Jun 2026 07:05:00 +0800",
    }


def _ingest(fake, content, mid):
    # Drive the REAL ingest path: SupabaseSalesStore over the fake client, exactly
    # as the poll does (process_email -> parse -> store.save into sales_daily).
    store = si.SupabaseSalesStore(fake)
    now = datetime(2026, 6, 25, 7, 5, tzinfo=MY)
    return si.process_email(store, _email(content, mid), now_my=now, outlets=OUTLETS)


def test_real_ingest_makes_overnight_visible_then_comparison_reconciles():
    fake = FakeSupabase()

    # 1) Ingest the DAY shift (closes 24 Jun 19:00 -> business_date 24 Jun).
    status, _ = _ingest(fake, _shift_content("24/Jun/2026", "19:00:04"), "day1")
    assert status == "inserted"
    rows = fake.rows("sales_daily")
    assert len(rows) == 1
    assert rows[0]["shift_type"] == "day"
    assert rows[0]["shift_business_date"] == "2026-06-24"

    # The D-file summary + itemwise (the comparison's quantity source) and a
    # complete kitchen record exist for that day.
    fake._store[ku.SALES_SUMMARY_TABLE] = [
        {"id": 1, "outlet_code": "D-SEK20", "outlet_canonical": "SEK-20",
         "business_date": "2026-06-24", "total_shifts": 2}]
    fake._store[ku.SALES_ITEMWISE_TABLE] = [
        {"id": 10, "summary_id": 1, "item_name": "Ayam Goreng", "qty": 80}]
    fake._store["kitchen_daily_usage"] = [
        {"outlet_code": "SEK20", "business_date": "2026-06-24", "item_code": "ayam_goreng",
         "item_label": "Ayam Goreng", "unit": "pcs", "cooked_qty": 100, "left_qty": 0}]

    # Day shift only -> gate INCOMPLETE (overnight email not ingested yet).
    cov = ku.pos_shift_coverage(fake, "SEK20", "2026-06-24")
    assert cov["has_day"] and not cov["has_overnight"]
    assert cov["complete"] is False
    targets = ku.select_reconcile_target_dates(fake, "SEK20", ["2026-06-24"])
    assert targets["reconcile"] == []
    assert [d for d, _ in targets["pending"]] == ["2026-06-24"]  # -> "belum lengkap"

    # 2) Ingest the OVERNIGHT shift (closes 25 Jun 07:00 -> business_date 24 Jun).
    status2, _ = _ingest(fake, _shift_content("25/Jun/2026", "07:00:04"), "night1")
    assert status2 == "inserted"
    night = [r for r in fake.rows("sales_daily") if r["shift_type"] == "overnight"]
    assert len(night) == 1 and night[0]["shift_business_date"] == "2026-06-24"

    cov2 = ku.pos_shift_coverage(fake, "SEK20", "2026-06-24")
    assert cov2["has_day"] and cov2["has_overnight"]
    assert cov2["complete"] is True  # both shifts in -> now reconcilable

    # 3) The comparison now targets that day and computes Used vs POS + flags.
    targets2 = ku.select_reconcile_target_dates(fake, "SEK20", ["2026-06-24"])
    assert targets2["reconcile"] == ["2026-06-24"]
    evals = ku.evaluate_outlet_day(fake, "SEK20", "2026-06-24")
    ag = next(e for e in evals if e["code"] == "ayam_goreng")
    assert ag["used"] == 100 and ag["pos"] == 80
    assert ku.comparison_already_posted(fake, "SEK20", "2026-06-24") is True


def test_real_ingest_dedups_repeat_email_no_double_row():
    """The poll running every 15 min re-sees the same unread email until it is
    marked seen — ingest must dedup so a shift isn't double-counted."""
    fake = FakeSupabase()
    assert _ingest(fake, _shift_content("24/Jun/2026", "19:00:04"), "day1")[0] == "inserted"
    # same message_id again -> skipped (destination dedup), no second row
    assert _ingest(fake, _shift_content("24/Jun/2026", "19:00:04"), "day1")[0] == "skipped"
    assert len(fake.rows("sales_daily")) == 1
