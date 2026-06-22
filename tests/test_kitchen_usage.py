"""Tests for the Daily Kitchen Usage Log pure logic.

Covers the numpad state machine, Used = Cooked − Left, the dual-gate mismatch
flag, the Bistro-only Ayam Rempah rule, the 18:00→02:00 business_date span,
POS→kg conversion and the mini-summary / digest rendering.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import kitchen_usage as ku

MY = ZoneInfo("Asia/Kuala_Lumpur")


# --- Bistro-only Ayam Rempah -------------------------------------------------

def test_ayam_rempah_only_for_bistro():
    bistro = [it["code"] for it in ku.items_for_outlet("BISTRO7")]
    assert "ayam_rempah" in bistro
    for outlet in ("SEK6", "SEK20", "VISTA", "JAKEL", "D", "KLANG", "SBESI"):
        codes = [it["code"] for it in ku.items_for_outlet(outlet)]
        assert "ayam_rempah" not in codes, outlet
        # every other tracked item is still present
        assert "ayam_goreng" in codes and "kambing" in codes


def test_bistro_match_is_case_insensitive():
    assert "ayam_rempah" in [it["code"] for it in ku.items_for_outlet("bistro7")]


def test_required_codes_count():
    # Bistro has one extra required item (Ayam Rempah).
    assert len(ku.required_codes("BISTRO7")) == len(ku.required_codes("SEK6")) + 1


# --- business_date span (18:00 -> 02:00 next day = same business day) --------

def test_business_date_cooked_evening():
    dt = datetime(2026, 6, 22, 18, 0, tzinfo=MY)
    assert ku.business_date_for(dt).isoformat() == "2026-06-22"


def test_business_date_left_after_midnight():
    dt = datetime(2026, 6, 23, 2, 0, tzinfo=MY)
    assert ku.business_date_for(dt).isoformat() == "2026-06-22"


def test_business_date_cutoff_boundaries():
    # 11:59 still folds back to the previous day; noon is the new day.
    assert ku.business_date_for(datetime(2026, 6, 23, 11, 59, tzinfo=MY)).isoformat() == "2026-06-22"
    assert ku.business_date_for(datetime(2026, 6, 23, 12, 0, tzinfo=MY)).isoformat() == "2026-06-23"


# --- numpad state machine ----------------------------------------------------

def test_numpad_pcs_digits():
    buf = ""
    buf = ku.apply_key(buf, "1", "pcs")
    buf = ku.apply_key(buf, "2", "pcs")
    buf = ku.apply_key(buf, "0", "pcs")
    assert buf == "120"
    assert ku.commit_value(buf, "pcs") == 120


def test_numpad_backspace():
    buf = "35"
    buf = ku.apply_key(buf, "bs", "pcs")
    assert buf == "3"
    buf = ku.apply_key(buf, "bs", "pcs")
    assert buf == ""
    # backspace on empty is a no-op
    assert ku.apply_key("", "bs", "pcs") == ""


def test_numpad_pcs_rejects_dot():
    assert ku.apply_key("3", ".", "pcs") == "3"
    assert ku.commit_value("3", "pcs") == 3


def test_numpad_kg_one_decimal():
    buf = ""
    buf = ku.apply_key(buf, "3", "kg")
    buf = ku.apply_key(buf, ".", "kg")
    buf = ku.apply_key(buf, "5", "kg")
    assert buf == "3.5"
    # a second decimal digit is ignored
    buf = ku.apply_key(buf, "7", "kg")
    assert buf == "3.5"
    # a second dot is ignored
    assert ku.apply_key("3.5", ".", "kg") == "3.5"
    assert ku.commit_value("3.5", "kg") == 3.5


def test_numpad_kg_leading_dot_becomes_zero():
    assert ku.apply_key("", ".", "kg") == "0."
    assert ku.commit_value(ku.apply_key("0.", "5", "kg"), "kg") == 0.5


def test_numpad_collapses_leading_zero():
    assert ku.apply_key("0", "5", "pcs") == "5"


def test_commit_value_empty_is_none():
    assert ku.commit_value("", "pcs") is None
    assert ku.commit_value(".", "kg") is None


def test_pcs_rounds_to_int():
    assert ku.commit_value("12", "pcs") == 12
    assert isinstance(ku.commit_value("12", "pcs"), int)


# --- Used = Cooked − Left ----------------------------------------------------

def test_used_qty_basic():
    assert ku.used_qty(50, 8) == 42
    assert ku.used_qty(3.5, 1.0) == 2.5


def test_used_qty_missing_input():
    assert ku.used_qty(None, 5) is None
    assert ku.used_qty(10, None) is None


# --- POS matching + kg conversion --------------------------------------------

def test_pos_qty_pcs_keyword_match():
    rows = [
        {"item_name": "Ayam Goreng", "qty": 30},
        {"item_name": "Ayam Goreng Berempah", "qty": 5},
        {"item_name": "Ayam Madu", "qty": 12},
        {"item_name": "Nasi Putih", "qty": 100},
    ]
    # ayam_goreng matches both goreng lines (35), not madu/nasi
    assert ku.pos_qty_for_item("ayam_goreng", rows) == 35
    assert ku.pos_qty_for_item("ayam_madu", rows) == 12


def test_pos_qty_kg_conversion():
    # 10 portions of kambing at 180 g = 1.8 kg
    rows = [{"item_name": "Kambing Masak Merah", "qty": 10}]
    assert ku.pos_qty_for_item("kambing", rows) == 1.8
    # 100 portions of daging at 60 g = 6.0 kg
    rows = [{"item_name": "Daging Hitam", "qty": 100}]
    assert ku.pos_qty_for_item("daging", rows) == 6.0


def test_pos_qty_no_match_is_zero():
    assert ku.pos_qty_for_item("ayam_tandoori", [{"item_name": "Roti Canai", "qty": 5}]) == 0.0


# --- dual-gate mismatch flag -------------------------------------------------

def test_flag_pcs_leak_passes_both_gates():
    # Used 100 vs POS 80: Δ20 = 25% (>8%) and 20 pcs (>5) -> LEAK
    assert ku.mismatch_flag(100, 80, "pcs") == "LEAK"


def test_flag_pcs_data_entry_under():
    # Used 60 vs POS 80: Δ20 -> DATA (Used < POS)
    assert ku.mismatch_flag(60, 80, "pcs") == "DATA"


def test_flag_pcs_abs_gate_blocks_small_outlet():
    # Used 4 vs POS 0: huge % but only 4 pcs (<=5) -> no flag
    assert ku.mismatch_flag(4, 0, "pcs") is None
    # Used 55 vs POS 50: 10% (>8%) but Δ5 not >5 -> no flag
    assert ku.mismatch_flag(55, 50, "pcs") is None


def test_flag_pcs_pct_gate_blocks_big_outlet():
    # Used 206 vs POS 200: Δ6 (>5 pcs) but only 3% (<8%) -> no flag
    assert ku.mismatch_flag(206, 200, "pcs") is None


def test_flag_kg_gates():
    # Used 12 vs POS 10: 20% (>10%) and 2 kg (>1.5) -> LEAK
    assert ku.mismatch_flag(12.0, 10.0, "kg") == "LEAK"
    # Used 11 vs POS 10: 10% not >10% -> no flag
    assert ku.mismatch_flag(11.0, 10.0, "kg") is None
    # Used 8 vs POS 10: Δ2 = 20% and 2kg -> DATA
    assert ku.mismatch_flag(8.0, 10.0, "kg") == "DATA"


def test_flag_pos_zero_uses_abs_gate_only():
    # POS 0, Used 20 pcs: % treated as exceeded, abs 20 > 5 -> LEAK
    assert ku.mismatch_flag(20, 0, "pcs") == "LEAK"


def test_flag_none_when_used_missing():
    assert ku.mismatch_flag(None, 10, "pcs") is None


# --- evaluate_usage end to end -----------------------------------------------

def test_evaluate_usage_full():
    rows = [{"item_name": "Ayam Goreng", "qty": 80}]
    ev = ku.evaluate_usage("ayam_goreng", cooked=100, left=0, itemwise_rows=rows)
    assert ev["used"] == 100
    assert ev["pos"] == 80
    assert ev["flag"] == "LEAK"
    assert ev["unit"] == "pcs"
    assert ev["label"] == "Ayam Goreng"


# --- form completeness -------------------------------------------------------

def test_is_form_complete():
    entries = {c: 1 for c in ku.required_codes("SEK6")}
    assert ku.is_form_complete(entries, "SEK6")
    # missing one item
    entries.pop("kambing")
    assert not ku.is_form_complete(entries, "SEK6")
    # a Bistro form needs Ayam Rempah too
    entries_bistro = {c: 1 for c in ku.required_codes("SEK6")}
    assert not ku.is_form_complete(entries_bistro, "BISTRO7")


# --- callback parsing --------------------------------------------------------

def test_parse_callback_roundtrip():
    assert ku.parse_callback("kdu:12:ayam_goreng:open") == {
        "session_id": "12", "item_code": "ayam_goreng", "action": "open",
    }
    assert ku.parse_callback("kdu:5:_form:send")["action"] == "send"
    assert ku.parse_callback("review:5:edit") is None
    assert ku.parse_callback("nonsense") is None


# --- mini summary + digest rollup rendering ----------------------------------

def test_render_mini_summary_flags_and_ok():
    evals = [
        ku.evaluate_usage("ayam_goreng", 100, 0, [{"item_name": "Ayam Goreng", "qty": 80}]),
        ku.evaluate_usage("kambing", 5.0, 4.5, [{"item_name": "Kambing", "qty": 3}]),
    ]
    text = ku.render_mini_summary("SEK-6", "2026-06-22", evals)
    assert "Ringkasan Guna vs POS" in text
    assert "Ayam Goreng" in text
    assert "🔴" in text  # the leak line


def test_render_mini_summary_all_match():
    evals = [ku.evaluate_usage("ayam_goreng", 80, 0, [{"item_name": "Ayam Goreng", "qty": 80}])]
    text = ku.render_mini_summary("SEK-6", "2026-06-22", evals)
    assert "Semua padan" in text


def test_format_value_kg_trims():
    assert ku.format_value(3.0, "kg") == "3"
    assert ku.format_value(3.5, "kg") == "3.5"
    assert ku.format_value(12, "pcs") == "12"
    assert ku.format_value(None, "pcs") == "—"
