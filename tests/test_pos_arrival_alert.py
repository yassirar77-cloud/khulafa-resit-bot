from datetime import date, timedelta

import pos_arrival_alert as pa

T = date(2026, 10, 11)


def _days(n, start=T):
    return [(start - timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _d(outlet, day, n=1):
    return [{"outlet_code": f"D-{outlet}", "business_date": day}] * n


def _s(outlet, day, n=1):
    return [{"outlet_code": f"S-{outlet}", "shift_business_date": day}] * n


def test_active_outlets_only_active_s_codes():
    rows = [{"code": "S-SEK14", "active": True}, {"code": "S-RAZAK", "active": False},
            {"code": "S-SBESI", "active": True}, {"code": "D-X", "active": True}]
    assert pa.active_outlet_codes(rows) == ["SBESI", "SEK14"]


def test_all_in_no_alert():
    d, s = [], []
    for day in _days(14) + [T.isoformat()]:
        d += _d("SEK14", day, 2)
        s += _s("SEK14", day, 2)
    assert pa.missing_daily(["SEK14"], d, s, T) == []


def test_missing_and_short_files_alert():
    d, s = [], []
    for day in _days(14):
        d += _d("SEK14", day, 2)
        s += _s("SEK14", day, 2)
    d += _d("SEK14", T.isoformat(), 1)          # overnight D-file missing
    assert pa.missing_daily(["SEK14"], d, s, T) == [
        "POS belum terima: SEK14 2026-10-11 (D-file 1/2, S-file 0/2)"]


def test_type_outlet_never_sends_is_not_expected():
    s = []
    for day in _days(14) + [T.isoformat()]:
        s += _s("BISTRO7", day, 2)
    # No D-files in 14 days -> D-file not expected, no alert.
    assert pa.missing_daily(["BISTRO7"], [], s, T) == []


def test_new_outlet_with_little_history_not_alerted():
    d = _d("NEW", _days(1)[0])
    assert pa.missing_daily(["NEW"], d, [], T) == []


def test_monthly_only_on_third_and_matches_subjects():
    subjects = ["MONTHLY REPORT-SEK20   ON Sep -2026",
                "MONTHLY REPORT-Damansara   ON Sep -2026",
                "MONTHLY REPORT-SEK14   ON Aug -2026"]   # wrong month
    outlets = ["DAMANSARA", "SEK14", "SEK20"]
    assert pa.missing_monthly(outlets, subjects, date(2026, 10, 2)) == []
    assert pa.missing_monthly(outlets, subjects, date(2026, 10, 3)) == [
        "POS belum terima: SEK14 laporan bulanan Sep 2026"]


def test_monthly_january_rolls_back_year():
    assert pa.previous_month(date(2027, 1, 3)) == (2026, 12)
    assert pa.monthly_reports_seen(["MONTHLY REPORT-VISTA ON Dec -2026"], 2026, 12) == {"VISTA"}
