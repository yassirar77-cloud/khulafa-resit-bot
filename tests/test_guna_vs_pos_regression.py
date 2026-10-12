"""Regression tests for the "📊 Ringkasan Guna vs POS" bugs reported on the
live D.U and SEK-14 summaries for business day 2026-10-08.

Fixtures are the real kitchen key-ins and D-file itemwise rows
(tests/fixtures/guna_vs_pos_oct2026.py).
"""
from datetime import datetime

import pytest

import kitchen_usage as ku
import pos_dates
from pos_names import name_issues, normalize_pos_name
from tests.fixtures.guna_vs_pos_oct2026 import BEFORE_8_OCT, ITEMWISE, KITCHEN

# Business day -> its D-file, once dates are derived correctly.
D_FILE = {("D", "2026-10-08"): 1221, ("SEK14", "2026-10-08"): 1219,
          ("D", "2026-10-09"): 1230, ("SEK14", "2026-10-09"): 1228}


def _evals(outlet, day):
    rows = ITEMWISE[D_FILE[(outlet, day)]]
    return {code: ku.evaluate_usage(code, c, l, rows)
            for code, (c, l) in KITCHEN[(outlet, day)].items()}


def _mark(ev):
    full = ku.render_mini_summary_full("X", "d", [ev])
    return full.splitlines()[3][:2].strip()


# --- BUG 1: status icons ------------------------------------------------------

@pytest.mark.parametrize("used,pos,unit", [
    (5, 0, "pcs"),     # D.U Ayam Kicap 5 vs 0 (was ✅)
    (1, 0, "pcs"),     # D.U Ayam Madu 1 vs 0 (was ✅)
    (4, 1, "pcs"),     # D.U Ikan Goreng 4 vs 1 (was ✅)
    (1.8, 0.3, "kg"),  # D.U Daging 1.8 vs 0.3 (was ✅)
    (3, 0, "pcs"),     # SEK-14 Ayam Madu 3 vs 0 (was ✅)
])
def test_reported_false_greens_are_red(used, pos, unit):
    assert ku.mismatch_flag(used, pos, unit) == "LEAK"


def test_old_stored_values_now_red():
    # Re-flag the exact numbers the live bot stored for D.U 8 Oct.
    for code, (cooked, left) in KITCHEN[("D", "2026-10-08")].items():
        pos, old_flag = BEFORE_8_OCT["D"][code]
        if code in ("ayam_kicap", "ayam_madu", "ikan_goreng", "daging"):
            used = round(cooked - left, 2)
            assert old_flag is None
            assert ku.mismatch_flag(used, pos, ku.ITEM_BY_CODE[code]["unit"]) == "LEAK"


# --- BUG 2: negative guna -------------------------------------------------------

def test_negative_guna_is_bad_data_never_a_status():
    ev = _evals("SEK14", "2026-10-08")
    assert ev["kambing"]["used"] == -495.0 and ev["kambing"]["flag"] == "BAD"
    assert ev["telur_ikan"]["used"] == -23.0 and ev["telur_ikan"]["flag"] == "BAD"
    for render in (ku.render_mini_summary, ku.render_mini_summary_full):
        text = render("SEK-14", "2026-10-08", list(ev.values()))
        kambing = next(ln for ln in text.splitlines() if "Kambing" in ln)
        telur = next(ln for ln in text.splitlines() if "Telur Ikan" in ln)
        assert kambing == "❌ Kambing: Data salah – sila semak"
        assert telur == "❌ Telur Ikan: Data salah – sila semak"
        assert "-495" not in text and "⚠️ Kambing" not in text and "➖" not in text


def test_leftover_above_cooked_is_rejected():
    assert ku.left_exceeds_cooked(500, 5.0)          # kambing 5 kg, "500"
    assert ku.left_exceeds_cooked(24.0, 1.0)         # telur ikan
    assert not ku.left_exceeds_cooked(0.5, 5.0)
    assert not ku.left_exceeds_cooked(5.0, 5.0)
    assert not ku.left_exceeds_cooked(6, 5, carry=2)  # yesterday's leftover on hand
    assert not ku.left_exceeds_cooked(3, None)        # cooked unknown: no check
    bad = ku.left_violations({"kambing": 500.0, "ayam_goreng": 17},
                             {"kambing": {"cooked": 5.0, "carry": 0},
                              "ayam_goreng": {"cooked": 205, "carry": 0}}, "SEK14")
    assert [v["code"] for v in bad] == ["kambing"]
    text = ku.left_violation_text(bad[0])
    bm, tamil = text.splitlines()
    assert "baki 500 kg lebih dari masak 5 kg" in bm and "sila taip semula" in bm
    assert "மீதம்" in tamil and "மறுபடி" in tamil


def test_kg_value_50_or_more_asks_about_grams():
    assert ku.grams_suspect(500, "kg") and ku.grams_suspect(50, "kg")
    assert not ku.grams_suspect(49.9, "kg") and not ku.grams_suspect(500, "pcs")
    assert ku.grams_to_kg(500) == 0.5
    import kitchen_texts
    assert kitchen_texts.bm_tamil("grams_ask", label="Kambing", g="500", kg="0.5") \
        .startswith("Kambing: Maksud 500 g = 0.5 kg?")


def test_grams_keyboard_callbacks_fit_telegram_limit():
    kb = ku.build_grams_keyboard(987654321, "ayam_tandoori", "12345.5")
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
            assert ku.parse_callback(b.callback_data) is not None


# --- BUG 3: usage date vs POS window -----------------------------------------------

def test_sek14_8_oct_d_file_is_dated_8_oct():
    # "D-SEK14 ON 08/Oct/2026 07:00:02" arrived 9 Oct 07:00 -> business day 8 Oct
    # (it lists shifts 2094 + 2095). The old rule booked it to 7 Oct.
    assert str(pos_dates.pos_business_date(datetime(2026, 10, 8, 7, 0, 2),
                                           "2026-10-09T07:00:15+08:00")) == "2026-10-08"
    # The D-file the live summary actually used (2096+2097) is 9 Oct.
    assert str(pos_dates.pos_business_date(datetime(2026, 10, 9, 7, 0, 3),
                                           "2026-10-10T07:00:16+08:00")) == "2026-10-09"
    # Its overnight S-file closed for real at 07:00 on 10 Oct.
    assert pos_dates.real_close_time(datetime(2026, 10, 9, 7, 0, 3),
                                     "2026-10-09T23:00:06+00:00") == datetime(2026, 10, 10, 7, 0, 3)


def test_live_8_oct_pos_figures_were_9_oct_sales():
    # The reported "Ayam Goreng 159" = 40 + 55 + 61 + 3 is 9 Oct's D-file.
    nine = ITEMWISE[1228]
    names = {r["item_name"]: r["qty"] for r in nine}
    assert names["Ayam Goreng"] + names["Nasi Ayam"] + names["Nasi Ayam Sayur"] \
        + names["Nasi Separuh Ayam"] == 159
    # The real 8 Oct figure comes from the 8 Oct D-file.
    assert ku.pos_qty_for_item("ayam_goreng", ITEMWISE[1219]) == 148


def test_shift_fallback_refuses_two_different_pos_days():
    # Live: day 2094 (8 Oct) + overnight 2097 (9 Oct) were summed as "8 Oct".
    assert not ku._shifts_are_one_day([
        {"shift_type": "day", "shift_no": "2094"},
        {"shift_type": "overnight", "shift_no": "2097"}])
    assert ku._shifts_are_one_day([
        {"shift_type": "day", "shift_no": "2094"},
        {"shift_type": "overnight", "shift_no": "2095"}])
    # SEK-15 runs three shifts: 3743 day, 3744 + 3745 night.
    assert ku._shifts_are_one_day([
        {"shift_type": "day", "shift_no": "3743"},
        {"shift_type": "overnight", "shift_no": "3744"},
        {"shift_type": "overnight", "shift_no": "3745"}])


def test_summary_footer_shows_both_windows():
    extras = {"unmapped": {}, "window": {
        "kitchen": "8/10 18:14 → 9/10 02:17",
        "pos": "shift 2094+2095 (tutup 8/10 19:00, 9/10 07:00)"}}
    for render in (ku.render_mini_summary, ku.render_mini_summary_full):
        text = render("SEK-14", "2026-10-08", [], extras=extras)
        assert text.splitlines()[-1] == (
            "Dapur: 8/10 18:14 → 9/10 02:17 | POS: shift 2094+2095 (tutup 8/10 19:00, 9/10 07:00)")


# --- BUG 4: POS mapping ------------------------------------------------------------

def test_sek14_ayam_bawang_counts_every_variant_incl_typo():
    # 9 Oct full day: 5 + 21 + 25 + 1 + Briyani 5 + "Briyani Awam Bawang" 4 = 61
    assert ku.pos_qty_for_item("ayam_bawang", ITEMWISE[1228]) == 61
    # 8 Oct (the right day): 3 + 10 + 20 + Briyani 5 + Awam 2 = 40
    assert ku.pos_qty_for_item("ayam_bawang", ITEMWISE[1219]) == 40


def test_rendang_kari_and_65_have_their_own_buttons():
    rows = ITEMWISE[1228]
    assert ku.pos_qty_for_item("ayam_kicap", rows) == 0
    assert ku.pos_qty_for_item("ayam_madu", rows) == 0
    # Rendang 2 + Rendang 1pc 1 + Nasi Ayam Rendang 2 + Kari 3 + 4 + 2
    assert ku.pos_qty_for_item("ayam_kari_rendang", rows) == 14
    # Ayam 65 3 + Nasiputih+65 Sayur 6
    assert ku.pos_qty_for_item("ayam_65", rows) == 9
    assert ku.unmapped_ayam_counts(rows) == {}


def test_buttons_without_kitchen_rows_show_pos_only():
    ev = {"code": "ayam_65", "label": "Ayam 65", "unit": "pcs", "cooked": None, "left": None,
          "used": None, "pos": 9, "flag": None, "source": "pos", "no_kitchen": True}
    full = ku.render_mini_summary_full("SEK-14", "2026-10-09", [ev])
    assert "➖ Ayam 65: tiada rekod dapur vs POS 9 pcs" in full
    group = ku.render_mini_summary("SEK-14", "2026-10-09", [ev])
    assert "➖ Ayam 65: tiada rekod dapur" in group and "9" not in group.split("\n", 3)[3]


def test_telur_ikan_is_compared_against_pos_not_purchases():
    assert ku.compare_source("telur_ikan") == "pos"
    assert ku.pos_qty_for_item("telur_ikan", ITEMWISE[1228]) == 2  # "Telur Ikan M"
    ev = ku.evaluate_usage("telur_ikan", 3, 1, ITEMWISE[1228])
    text = ku.render_mini_summary_full("SEK-14", "2026-10-09", [ev])
    assert "tiada rekod beli" not in text and "guna 2 vs POS 2 pcs" in text


def test_tandoori_is_ayam_tandoori_only():
    # SEK-14 9 Oct: Ayam Tandoori 13; "Ayam 65" 3 is NOT tandoori.
    assert ku.pos_qty_for_item("ayam_tandoori", ITEMWISE[1228]) == 13
    assert ku.pos_qty_for_item("ayam_tandoori", [
        {"item_name": "Ayam Tandoori Staff", "category": "NAAN", "qty": 3},
        {"item_name": "Ayam Tandori", "category": "NAAN", "qty": 2}]) == 2


def test_name_normalisation():
    assert normalize_pos_name("  Ikan  Goreng ") == "ikan goreng"
    assert normalize_pos_name("Ayam_goreng") == "ayam goreng"
    assert normalize_pos_name("Briyani Awam Bawang") == "briyani ayam bawang"
    assert normalize_pos_name("Mee Gorng Tampa") == "mee goreng tampa"
    assert normalize_pos_name("Nasi Sepuruh Ayam Besar") == "nasi separuh ayam besar"
    # Whole words only: "awam" inside another word is left alone.
    assert normalize_pos_name("Kawamura") == "kawamura"
    assert ku.pos_qty_for_item("ayam_goreng", [{"item_name": "Ayam_goreng", "qty": 3}]) == 3
    assert ku.pos_qty_for_item("ayam_goreng", [{"item_name": "Nasi Sepuruh Ayam Besar", "qty": 4}]) == 4
    assert "double space" in name_issues("Ikan  Goreng")
    assert "leading/trailing space" in name_issues(" Ayam")
    assert "typo 'awam'→'ayam'" in name_issues("Briyani Awam Bawang")


def test_s_file_ingest_keeps_typo_names():
    import sales_parser
    assert sales_parser.is_nasi_kandar_item("Briyani Awam Bawang")


# --- end to end: corrected 8 Oct ------------------------------------------------------

def test_corrected_d_u_8_oct():
    ev = _evals("D", "2026-10-08")
    flags = {c: e["flag"] for c, e in ev.items()}
    assert flags["ayam_kicap"] == flags["ayam_madu"] == "LEAK"
    assert ev["ayam_goreng"]["pos"] == 94 and flags["ayam_goreng"] == "LEAK"
    assert ev["ayam_bawang"]["pos"] == 28 and flags["ayam_bawang"] is None
    assert ev["daging"]["pos"] == 0.48 and flags["daging"] == "LEAK"


def test_corrected_sek14_8_oct():
    ev = _evals("SEK14", "2026-10-08")
    assert ev["ayam_goreng"]["pos"] == 148
    assert ev["ayam_bawang"]["pos"] == 40 and ev["ayam_bawang"]["flag"] is None
    assert ev["ayam_tandoori"]["pos"] == 9 and ev["ayam_tandoori"]["flag"] == "LEAK"
    assert ev["ayam_madu"]["flag"] == "LEAK"


def test_gravy_items_count_toward_nothing_and_65_names_kept():
    """Owner, Oct 2026: Kari Bungkus / Kua Kari / Nasi Putih (Kari) and SEK-15
    "KARI AYAM/KAMBING" (avg RM2.59 < RM4) are gravy; Chicken 65 and Ayam
    Masala/65 stay in the Ayam 65 button."""
    gravy = ["Kari Bungkus", "Kua Kari", "Nasi Putih ( Kari )", "Nasi Putih (Kari)",
             "KARI AYAM/KAMBING"]
    for name in gravy:
        rows = [{"item_name": name, "qty": 10, "category": None}]
        assert all(ku.pos_pcs_for_item(code, rows) == 0
                   for code in ku.ITEM_POS_KEYWORDS), name
    assert ku.pos_pcs_for_item("ayam_kari_rendang",
                               [{"item_name": "Ayam Kari", "qty": 3}]) == 3
    for name in ("Chicken 65", "Ayam Masala/65"):
        assert ku.pos_pcs_for_item("ayam_65", [{"item_name": name, "qty": 2}]) == 2, name
