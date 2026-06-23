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
    for outlet in ("SEK6", "SEK20", "VISTA", "JAKEL", "D", "KLANG", "KLRAZAK"):
        codes = [it["code"] for it in ku.items_for_outlet(outlet)]
        assert "ayam_rempah" not in codes, outlet
        # every other tracked item is still present
        assert "ayam_goreng" in codes and "kambing" in codes


def test_bistro_match_is_case_insensitive():
    assert "ayam_rempah" in [it["code"] for it in ku.items_for_outlet("bistro7")]


def test_required_codes_count():
    # Bistro has one extra required item (Ayam Rempah).
    assert len(ku.required_codes("BISTRO7")) == len(ku.required_codes("SEK6")) + 1


# --- KITCHEN_LOG_ENABLED safety gate -----------------------------------------

def test_kitchen_log_enabled_default_off(monkeypatch):
    monkeypatch.delenv("KITCHEN_LOG_ENABLED", raising=False)
    assert ku.kitchen_log_enabled() is False


def test_kitchen_log_enabled_truthy_values(monkeypatch):
    for val in ("true", "TRUE", "1", "yes", "on", "Y"):
        monkeypatch.setenv("KITCHEN_LOG_ENABLED", val)
        assert ku.kitchen_log_enabled() is True, val


def test_kitchen_log_enabled_falsy_values(monkeypatch):
    for val in ("", "false", "0", "no", "off", "nope"):
        monkeypatch.setenv("KITCHEN_LOG_ENABLED", val)
        assert ku.kitchen_log_enabled() is False, val


# --- missing-table (PGRST205) detection + graceful poster --------------------

class _PGRSTError(Exception):
    """Mimics postgrest APIError: has .code and .message attributes."""
    def __init__(self, code="PGRST205",
                 message="Could not find the table 'public.kitchen_log_session' "
                         "in the schema cache"):
        super().__init__(message)
        self.code = code
        self.message = message


def test_is_missing_table_error_detects_pgrst205():
    assert ku._is_missing_table_error(_PGRSTError()) is True
    # plain Exception carrying the message text (no .code attr)
    assert ku._is_missing_table_error(
        Exception("Could not find the table 'public.kitchen_log_session' "
                  "in the schema cache")
    ) is True
    # unrelated errors are NOT swallowed as missing-table
    assert ku._is_missing_table_error(ValueError("boom")) is False
    assert ku._is_missing_table_error(Exception("network timeout")) is False


def test_poster_skips_gracefully_when_table_missing(monkeypatch, caplog):
    import asyncio
    import logging
    import types

    import config.kitchen_groups as kg

    # One group resolved, so the loop runs; bypass receipts resolution.
    monkeypatch.setattr(kg, "configured_groups", lambda client=None: [(-1, "VISTA")])
    monkeypatch.setenv("KITCHEN_LOG_ENABLED", "true")

    class _RaisingQuery:
        def __init__(self, name):
            self.name = name
        def select(self, *a, **k):
            return self
        def insert(self, *a, **k):
            return self
        def eq(self, *a, **k):
            return self
        def limit(self, *a, **k):
            return self
        def execute(self):
            if self.name == "kitchen_log_session":
                raise _PGRSTError()
            return types.SimpleNamespace(data=[])

    class _RaisingSupabase:
        def table(self, name):
            return _RaisingQuery(name)

    monkeypatch.setattr(ku, "_supabase", _RaisingSupabase())

    sent = []

    class _Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return types.SimpleNamespace(message_id=1)

    app = types.SimpleNamespace(bot=_Bot())

    with caplog.at_level(logging.INFO, logger="kitchen_usage"):
        # Must NOT raise — a missing table is logged and skipped, scheduler lives.
        asyncio.run(ku._post_forms(app, ku.PHASE_COOKED))

    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "kitchen COOKED poster fired, enabled=True" in msgs
    assert "schema cache" in msgs  # the clear missing-table error
    assert sent == []  # never reached send_message (session create failed first)


def test_post_one_form_bypasses_gate_and_posts(monkeypatch):
    import asyncio
    import types

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    monkeypatch.setattr(ku, "_supabase", fake)
    # Flag OFF — post_one_form is a manual trigger and must still post.
    monkeypatch.delenv("KITCHEN_LOG_ENABLED", raising=False)

    sent = []

    class _Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)
            return types.SimpleNamespace(message_id=77)

    app = types.SimpleNamespace(bot=_Bot())

    posted = asyncio.run(ku.post_one_form(app, -1, "VISTA", ku.PHASE_COOKED))
    assert posted is True
    assert len(sent) == 1 and sent[0]["chat_id"] == -1
    # A session row was created and stamped with the message_id.
    sessions = fake.rows("kitchen_log_session")
    assert len(sessions) == 1
    assert sessions[0]["outlet_code"] == "VISTA"
    assert sessions[0]["phase"] == "cooked"
    assert sessions[0]["message_id"] == 77

    # Re-posting after it's submitted should be a no-op (returns False).
    fake._store["kitchen_log_session"][0]["status"] = "submitted"
    again = asyncio.run(ku.post_one_form(app, -1, "VISTA", ku.PHASE_COOKED))
    assert again is False
    assert len(sent) == 1  # no second send


# --- finalize_submission: promote entries to kitchen_daily_usage -------------

def _bistro_cooked_session():
    return {
        "id": 1, "chat_id": -1, "outlet_code": "BISTRO7",
        "business_date": "2026-06-22", "phase": "cooked", "status": "open",
        "entries": {
            "ayam_goreng": 50, "ayam_bawang": 40, "ayam_rempah": 50,
            "ayam_kicap": 5, "ayam_madu": 18, "ayam_tandoori": 20,
            "ikan_goreng": 6, "ikan_kari": 5, "telur_ikan": 4,
            "kambing": 5, "daging": 5,
        },
    }


def test_finalize_cooked_promotes_rows(monkeypatch, caplog):
    import logging

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [_bistro_cooked_session()]
    session = dict(fake._store["kitchen_log_session"][0])

    with caplog.at_level(logging.INFO, logger="kitchen_usage"):
        ku.finalize_submission(fake, session, submitter="Chef")

    usage = fake.rows("kitchen_daily_usage")
    # BISTRO7 has all 11 items (incl. ayam_rempah).
    assert len(usage) == 11
    rempah = next(r for r in usage if r["item_code"] == "ayam_rempah")
    assert rempah["cooked_qty"] == 50
    assert rempah["cooked_by"] == "Chef"
    assert rempah["cooked_at"] is not None
    assert rempah.get("left_qty") is None       # COOKED leaves left_qty NULL
    assert rempah["business_date"] == "2026-06-22"
    # session marked submitted
    assert fake.rows("kitchen_log_session")[0]["status"] == "submitted"
    # promotion logging present
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "promoting 11 entries to kitchen_daily_usage for BISTRO7" in msgs
    assert "promotion done — 11/11 rows written" in msgs


def test_finalize_falls_back_to_manual_upsert_when_on_conflict_unsupported(caplog):
    """The production bug: a hand-built table missing the UNIQUE constraint makes
    the native ON CONFLICT upsert fail. finalize must fall back to manual
    insert/update so rows still land."""
    import logging

    from tests.fake_supabase import FakeSupabase

    class _NoConstraint(FakeSupabase):
        def table(self, name):
            q = super().table(name)
            if name == "kitchen_daily_usage":
                def _boom(*a, **k):
                    raise Exception("no unique or exclusion constraint matching "
                                    "the ON CONFLICT specification")
                q.upsert = _boom  # type: ignore[assignment]
            return q

    fake = _NoConstraint()
    fake._store["kitchen_log_session"] = [_bistro_cooked_session()]
    session = dict(fake._store["kitchen_log_session"][0])

    with caplog.at_level(logging.WARNING, logger="kitchen_usage"):
        ku.finalize_submission(fake, session, submitter="Chef")

    usage = fake.rows("kitchen_daily_usage")
    assert len(usage) == 11  # written via the manual fallback
    assert all(r.get("cooked_qty") is not None and r.get("left_qty") is None for r in usage)
    assert fake.rows("kitchen_log_session")[0]["status"] == "submitted"
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "falling back to manual" in msgs


def test_finalize_raises_and_leaves_session_open_when_all_writes_fail(caplog):
    import logging

    from tests.fake_supabase import FakeSupabase

    class _AllWritesFail(FakeSupabase):
        def table(self, name):
            q = super().table(name)
            if name == "kitchen_daily_usage":
                def _boom(*a, **k):
                    raise Exception("could not find the table "
                                    "'public.kitchen_daily_usage' in the schema cache")
                q.upsert = _boom      # type: ignore[assignment]
                q.insert = _boom      # type: ignore[assignment]
                q.select = _boom      # type: ignore[assignment]
            return q

    fake = _AllWritesFail()
    fake._store["kitchen_log_session"] = [_bistro_cooked_session()]
    session = dict(fake._store["kitchen_log_session"][0])

    with caplog.at_level(logging.INFO, logger="kitchen_usage"):
        try:
            ku.finalize_submission(fake, session, submitter="Chef")
            raised = False
        except ku.KitchenPromotionError:
            raised = True

    assert raised is True
    # No usage rows, and the session stays OPEN (not submitted) for retry.
    assert fake.rows("kitchen_daily_usage") == []
    assert fake.rows("kitchen_log_session")[0]["status"] == "open"
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "0/11 rows written" in msgs


def test_hantar_callback_end_to_end_promotes_rows(monkeypatch):
    """End-to-end through the REAL handler: a Hantar tap on a complete BISTRO7
    COOKED form must write 11 kitchen_daily_usage rows even when the table lacks
    the ON CONFLICT unique constraint (the production scenario)."""
    import asyncio
    import types

    from tests.fake_supabase import FakeSupabase

    class _NoConstraint(FakeSupabase):
        def table(self, name):
            q = super().table(name)
            if name == "kitchen_daily_usage":
                def _boom(*a, **k):
                    raise Exception("no unique or exclusion constraint matching "
                                    "the ON CONFLICT specification")
                q.upsert = _boom  # type: ignore[assignment]
            return q

    fake = _NoConstraint()
    sess = _bistro_cooked_session()
    sess["id"] = "s1"  # string id so the fake's .eq('id', '<str>') matches
    fake._store["kitchen_log_session"] = [sess]
    monkeypatch.setattr(ku, "_supabase", fake)

    edits, replies = [], []

    class _Query:
        data = "kdu:s1:_form:send"
        from_user = types.SimpleNamespace(full_name="Chef", username=None, id=999)
        message = types.SimpleNamespace(
            reply_text=lambda *a, **k: _async_append(replies, a[0] if a else k.get("text"))
        )
        async def answer(self, *a, **k):
            return None
        async def edit_message_text(self, text, **k):
            edits.append(text)

    class _Bot:
        async def send_message(self, **k):
            return types.SimpleNamespace(message_id=1)

    update = types.SimpleNamespace(callback_query=_Query())
    context = types.SimpleNamespace(bot=_Bot())

    asyncio.run(ku.handle_kitchen_callback(update, context))

    usage = fake.rows("kitchen_daily_usage")
    assert len(usage) == 11
    assert all(r.get("cooked_qty") is not None and r.get("left_qty") is None for r in usage)
    assert {r["item_code"] for r in usage} == set(ku.required_codes("BISTRO7"))
    assert fake.rows("kitchen_log_session")[0]["status"] == "submitted"
    assert any("Tersimpan" in e for e in edits)  # success shown, not an error reply


def _async_append(store, value):
    async def _coro():
        store.append(value)
    return _coro()


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
    assert ev["source"] == "pos"


# --- Telur Ikan: consumption vs PURCHASE (not POS) ---------------------------

def test_telur_ikan_not_in_pos_or_portion_config():
    # No portion-size guess and no POS keyword entry for Telur Ikan anymore.
    assert "telur_ikan" not in ku.KG_PORTION_GRAMS
    assert "telur_ikan" not in ku.ITEM_POS_KEYWORDS
    # Kambing/Daging POS portions are untouched.
    assert ku.KG_PORTION_GRAMS["kambing"] == 180.0
    assert ku.KG_PORTION_GRAMS["daging"] == 60.0


def test_compare_source():
    assert ku.compare_source("telur_ikan") == "purchase"
    for code in ("ayam_goreng", "kambing", "daging", "ikan_kari"):
        assert ku.compare_source(code) == "pos"


def test_purchased_kg_sums_matching_lines():
    receipts = [
        {"items": [{"name": "TELUR IKAN SEJUK BEKU", "qty": 4, "price": 120}]},
        {"items": [{"name": "Telur Ikan", "qty": 2.5, "price": 80},
                   {"name": "Ayam 4 KG", "qty": 4, "price": 40}]},
    ]
    assert ku.purchased_kg_from_receipts(receipts) == 6.5


def test_purchased_kg_ignores_eggs_and_other_fish():
    receipts = [
        {"items": [{"name": "Telur Ayam", "qty": 30, "price": 15},   # eggs, not roe
                   {"name": "Ikan Tenggiri", "qty": 5, "price": 90}]},  # other fish
    ]
    # No telur-ikan line at all -> None (not 0), so the digest shows "tiada rekod beli".
    assert ku.purchased_kg_from_receipts(receipts) is None


def test_purchased_kg_none_when_no_receipts():
    assert ku.purchased_kg_from_receipts([]) is None
    assert ku.purchased_kg_from_receipts(None) is None


def test_evaluate_telur_ikan_vs_purchase_flags():
    # Used 8 kg vs bought 5 kg: Δ3 = 60% (>10%) and 3 kg (>1.5) -> LEAK.
    ev = ku.evaluate_usage("telur_ikan", cooked=10.0, left=2.0,
                           itemwise_rows=[], purchased_kg=5.0)
    assert ev["source"] == "purchase"
    assert ev["used"] == 8.0
    assert ev["pos"] == 5.0
    assert ev["flag"] == "LEAK"


def test_evaluate_telur_ikan_ignores_pos_rows():
    # Even if POS-looking ikan rows are passed, Telur Ikan never reads them.
    pos_rows = [{"item_name": "Telur Ikan Goreng", "qty": 999}]
    ev = ku.evaluate_usage("telur_ikan", cooked=3.0, left=1.0,
                           itemwise_rows=pos_rows, purchased_kg=2.0)
    assert ev["pos"] == 2.0  # purchased, not the 999 POS qty


def test_evaluate_telur_ikan_no_purchase_no_flag():
    # No same-day purchase -> pos None, no flag (approach b: don't false-alarm).
    ev = ku.evaluate_usage("telur_ikan", cooked=5.0, left=0.5,
                           itemwise_rows=[], purchased_kg=None)
    assert ev["pos"] is None
    assert ev["flag"] is None
    assert ev["used"] == 4.5


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


def test_render_mini_summary_telur_ikan_purchase_wording():
    # With a same-day purchase -> "guna vs beli"; the Telur line says no "POS".
    evals = [ku.evaluate_usage("telur_ikan", 5.0, 1.0, [], purchased_kg=4.0)]
    text = ku.render_mini_summary("SEK-6", "2026-06-22", evals)
    telur_line = next(ln for ln in text.splitlines() if "Telur Ikan" in ln)
    assert "beli" in telur_line
    assert "POS" not in telur_line


def test_render_mini_summary_telur_ikan_no_purchase():
    evals = [ku.evaluate_usage("telur_ikan", 5.0, 1.0, [], purchased_kg=None)]
    text = ku.render_mini_summary("SEK-6", "2026-06-22", evals)
    assert "tiada rekod beli" in text


def test_digest_block_telur_ikan_lines():
    import digest
    usage = [{
        "outlet_code": "SEK6", "complete": True,
        "items": [
            {"item_code": "telur_ikan", "item_label": "Telur Ikan", "unit": "kg",
             "used_qty": 4.0, "pos_qty": 3.0, "mismatch_flag": None},
        ],
    }]
    text = digest._kitchen_usage_block(usage)
    telur_line = next(ln for ln in text.splitlines() if "Telur Ikan" in ln)
    assert "beli" in telur_line
    assert "POS" not in telur_line  # the line itself, not the section header

    usage[0]["items"][0]["pos_qty"] = None
    text2 = digest._kitchen_usage_block(usage)
    assert "tiada rekod beli" in text2


def test_format_value_kg_trims():
    assert ku.format_value(3.0, "kg") == "3"
    assert ku.format_value(3.5, "kg") == "3.5"
    assert ku.format_value(12, "pcs") == "12"
    assert ku.format_value(None, "pcs") == "—"


# --- chat_id -> outlet_code resolution from receipts -------------------------

import importlib  # noqa: E402

from tests.fake_supabase import FakeSupabase  # noqa: E402


def _fresh_kitchen_groups():
    # Reload so the process-level resolution cache starts empty per test.
    import config.kitchen_groups as kg
    importlib.reload(kg)
    return kg


def test_outlet_code_from_text_titles():
    kg = _fresh_kitchen_groups()
    cases = {
        "Khulafa Bistro Resit": "BISTRO7",
        "Khulafa Sek 20 Resit": "SEK20",
        "Khulafa Signature Resit": "SEK14",
        "Khulafa Sek 15 Receipt": "SEK15",
        # The Sharfuddin/Klang Bayu Emas group IS the KLANG outlet, not SEK6.
        "Hj Sharfuddin Klang Bayu Emas": "KLANG",
        "Khulafa Vista Resit": "VISTA",
        "Khulafa Jakel Receipt": "JAKEL",
        "Khulafa Damansara Uptown": "D",
        "Khulafa Klang Bayu Emas": "KLANG",
        "Khulafa KL Razak": "KLRAZAK",
        # "Kl Sg Besi" is the SAME outlet as K.L Razak -> KLRAZAK, not SBESI.
        "Kl Sg Besi": "KLRAZAK",
        "Khulafa Sungai Besi": "KLRAZAK",
        # SEK6 only matches a genuine Jalan Murai / Sek 6 titled group.
        "Khulafa Sek 6 Jalan Murai": "SEK6",
    }
    for text, code in cases.items():
        assert kg.outlet_code_from_text(text) == code, text
    # SBESI must never be emitted as a kitchen outlet_code anymore.
    assert "SBESI" not in {kg.outlet_code_from_text(t) for t in cases}
    assert kg.outlet_code_from_text("UNKNOWN") is None
    assert kg.outlet_code_from_text("") is None
    assert kg.outlet_code_from_text(None) is None


def test_kl_sg_besi_resolves_to_klrazak_not_sbesi():
    # Regression: group -5163000846 "Kl Sg Besi" is K.L Razak.
    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    fake._store["receipts"] = [{"chat_id": -5163000846, "outlet": "Kl Sg Besi"}]
    groups = dict(kg.configured_groups(fake))
    assert groups == {-5163000846: "KLRAZAK"}
    assert "SBESI" not in groups.values()


def test_sharfuddin_klang_not_sek6():
    # Regression: the one Klang-ish group must not be inverted onto SEK6.
    kg = _fresh_kitchen_groups()
    assert kg.outlet_code_from_text("Hj Sharfuddin Klang Bayu Emas") == "KLANG"
    # ...and SEK6 doesn't resolve from a bare Sharfuddin/Klang title.
    assert kg.outlet_code_from_text("Hj Sharfuddin Klang Bayu Emas") != "SEK6"


def test_resolve_groups_from_receipts():
    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    fake._store["receipts"] = [
        {"chat_id": -1001, "outlet": "Bistro"},
        {"chat_id": -1001, "outlet": "Bistro"},
        {"chat_id": -1002, "outlet": "SEK 20"},
        {"chat_id": -1003, "outlet": "Signature"},
        {"chat_id": -1004, "outlet": "Hj Sharfuddin Klang Bayu Emas"},
        {"chat_id": 555, "outlet": "Bistro"},      # private DM -> skipped
        {"chat_id": -1009, "outlet": "UNKNOWN"},   # unresolved -> skipped
    ]
    groups = dict(kg.configured_groups(fake))
    assert groups == {
        -1001: "BISTRO7",
        -1002: "SEK20",
        -1003: "SEK14",
        -1004: "KLANG",  # Sharfuddin/Klang Bayu Emas -> KLANG
    }
    # outlet_for_chat reads the cached resolution.
    assert kg.outlet_for_chat(-1002) == "SEK20"
    assert kg.outlet_for_chat(999) is None


def test_resolve_groups_busiest_chat_wins_per_outlet():
    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    fake._store["receipts"] = [
        {"chat_id": -2001, "outlet": "Vista"},
        {"chat_id": -2001, "outlet": "Vista"},
        {"chat_id": -2001, "outlet": "Vista"},
        {"chat_id": -2002, "outlet": "Vista"},  # fewer receipts -> loses
    ]
    groups = dict(kg.configured_groups(fake))
    assert groups == {-2001: "VISTA"}


def test_manual_override_wins():
    kg = _fresh_kitchen_groups()
    kg.KITCHEN_GROUPS = {-3001: "JAKEL"}
    fake = FakeSupabase()
    fake._store["receipts"] = [{"chat_id": -3001, "outlet": "Vista"}]
    groups = dict(kg.configured_groups(fake))
    assert groups[-3001] == "JAKEL"


def test_resolve_groups_force_positional_and_keyword():
    # Regression for the /kitchen_groups_debug TypeError: force is a
    # positional-or-keyword arg, so both call styles must work.
    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    fake._store["receipts"] = [{"chat_id": -1001, "outlet": "Bistro"}]
    assert kg.resolve_groups(fake, True) == {-1001: "BISTRO7"}    # positional
    assert kg.resolve_groups(fake, force=True) == {-1001: "BISTRO7"}  # keyword


def test_resolve_groups_force_bypasses_cache():
    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    fake._store["receipts"] = [{"chat_id": -1001, "outlet": "Bistro"}]
    # First call populates and caches.
    assert kg.resolve_groups(fake) == {-1001: "BISTRO7"}
    # New receipts arrive for a second outlet.
    fake._store["receipts"].append({"chat_id": -1002, "outlet": "Vista"})
    # Cached call still shows the stale single mapping...
    assert kg.resolve_groups(fake) == {-1001: "BISTRO7"}
    # ...force=True re-reads receipts fresh and now includes Vista.
    assert kg.resolve_groups(fake, force=True) == {-1001: "BISTRO7", -1002: "VISTA"}


def test_missing_outlets_listed_in_expected_order():
    kg = _fresh_kitchen_groups()
    # Only two outlets resolve; the other eight should be reported missing in
    # EXPECTED_CODES order.
    mapping = {-1: "BISTRO7", -2: "KLANG"}
    missing = kg.missing_outlets(mapping)
    assert "BISTRO7" not in missing and "KLANG" not in missing
    assert missing == [c for c in kg.EXPECTED_CODES if c not in ("BISTRO7", "KLANG")]


def test_log_resolution_summary_warns_when_missing(caplog):
    import logging

    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    # Only 3 of the 10 expected outlets have receipts -> WARNING with the rest.
    fake._store["receipts"] = [
        {"chat_id": -1001, "outlet": "Bistro"},
        {"chat_id": -1002, "outlet": "SEK 20"},
        {"chat_id": -1004, "outlet": "Hj Sharfuddin Klang Bayu Emas"},
    ]
    with caplog.at_level(logging.WARNING, logger="config.kitchen_groups"):
        result = kg.log_resolution_summary(fake)
    assert "SEK6" in result["missing"]
    assert "KLRAZAK" in result["missing"]
    assert "BISTRO7" not in result["missing"]
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Kitchen groups resolved: 3/10" in msgs
    assert "missing:" in msgs


def test_log_resolution_summary_info_when_all_present(caplog):
    import logging

    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    titles = {
        "BISTRO7": "Bistro", "SEK20": "SEK 20", "SEK14": "Signature",
        "SEK15": "SEK 15", "SEK6": "Sek 6 Jalan Murai", "VISTA": "Vista",
        "JAKEL": "Jakel", "D": "Damansara", "KLANG": "Hj Sharfuddin Klang Bayu Emas",
        "KLRAZAK": "KL Razak",
    }
    fake._store["receipts"] = [
        {"chat_id": -(i + 1), "outlet": title} for i, title in enumerate(titles.values())
    ]
    with caplog.at_level(logging.INFO, logger="config.kitchen_groups"):
        result = kg.log_resolution_summary(fake)
    assert result["missing"] == []
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Kitchen groups resolved: 10/10 — all present" in msgs


def test_diagnostic_dump_shape():
    kg = _fresh_kitchen_groups()
    fake = FakeSupabase()
    fake._store["receipts"] = [
        {"chat_id": -1001, "outlet": "Bistro"},
        {"chat_id": -1001, "outlet": "Bistro"},
        {"chat_id": -1009, "outlet": "UNKNOWN"},  # group chat, unresolved code
        {"chat_id": 42, "outlet": "Bistro"},       # private DM -> skipped
    ]
    rows = kg.diagnostic_dump(fake)
    chat_ids = {r["chat_id"] for r in rows}
    assert chat_ids == {-1001, -1009}  # DM excluded
    by_id = {r["chat_id"]: r for r in rows}
    assert by_id[-1001]["code"] == "BISTRO7"
    assert by_id[-1001]["count"] == 2
    assert by_id[-1009]["code"] is None  # seen but unresolved
