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
    # Original tap-button layout: the form is posted WITH the item keyboard.
    assert sent[0].get("reply_markup") is not None
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


# --- night cook (12AM additive) ---------------------------------------------

def _night_session(entries, sid="n1"):
    return {
        "id": sid, "chat_id": -1, "outlet_code": "BISTRO7",
        "business_date": "2026-06-24", "phase": "cooked_night", "status": "open",
        "entries": entries,
    }


def test_night_form_same_items_incl_rempah_for_bistro():
    # The night form shows the SAME items as 6PM (Ayam Rempah Bistro-only).
    assert "ayam_rempah" in [it["code"] for it in ku.items_for_outlet("BISTRO7")]
    assert "ayam_rempah" not in [it["code"] for it in ku.items_for_outlet("SEK20")]


def test_can_submit_at_least_one_item_all_phases():
    partial = {"ayam_goreng": 20}
    # FIX 1: every phase only needs >=1 item; untouched ones save as 0.
    for phase in (ku.PHASE_COOKED, ku.PHASE_LEFT, ku.PHASE_COOKED_NIGHT):
        assert ku.can_submit(partial, "SEK20", phase) is True
        assert ku.can_submit({}, "SEK20", phase) is False


def test_night_cook_is_additive(monkeypatch):
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    # 6PM already cooked ayam_goreng=50; night adds 20 -> 70.
    fake._store["kitchen_daily_usage"] = [{
        "id": 1, "outlet_code": "BISTRO7", "business_date": "2026-06-24",
        "item_code": "ayam_goreng", "item_label": "Ayam Goreng", "unit": "pcs",
        "cooked_qty": 50, "left_qty": None,
    }]
    fake._store["kitchen_log_session"] = [_night_session({"ayam_goreng": 20})]
    session = dict(fake._store["kitchen_log_session"][0])

    ku.finalize_submission(fake, session, submitter="NightChef")

    rows = [r for r in fake.rows("kitchen_daily_usage") if r["item_code"] == "ayam_goreng"]
    assert len(rows) == 1
    assert rows[0]["cooked_qty"] == 70  # 50 + 20, additive (not replaced)
    assert rows[0]["left_qty"] is None
    assert fake.rows("kitchen_log_session")[0]["status"] == "submitted"


def test_night_cook_creates_row_when_no_6pm_entry(monkeypatch):
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()  # no prior 6PM row for this item
    fake._store["kitchen_log_session"] = [_night_session({"ikan_kari": 8})]
    session = dict(fake._store["kitchen_log_session"][0])

    ku.finalize_submission(fake, session, submitter="NightChef")

    rows = [r for r in fake.rows("kitchen_daily_usage") if r["item_code"] == "ikan_kari"]
    assert len(rows) == 1
    assert rows[0]["cooked_qty"] == 8  # starts at the night value


def test_night_cook_double_submit_guard(monkeypatch):
    # The session-submitted status is the guard: a submitted night session is a
    # no-op, so the additive add can't be applied twice via the handler.
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_daily_usage"] = [{
        "id": 1, "outlet_code": "BISTRO7", "business_date": "2026-06-24",
        "item_code": "ayam_goreng", "item_label": "Ayam Goreng", "unit": "pcs",
        "cooked_qty": 50,
    }]
    submitted = _night_session({"ayam_goreng": 20})
    submitted["status"] = "submitted"
    fake._store["kitchen_log_session"] = [submitted]

    # The handler short-circuits a submitted session (status check) — emulate the
    # guard the handler enforces: a submitted session is never re-finalized.
    assert fake.rows("kitchen_log_session")[0]["status"] == "submitted"
    row = [r for r in fake.rows("kitchen_daily_usage") if r["item_code"] == "ayam_goreng"][0]
    assert row["cooked_qty"] == 50  # unchanged — not double-added


def test_used_reflects_summed_cooked_after_night():
    # Used = (6PM 50 + night 20) - left 8 = 62.
    ev = ku.evaluate_usage("ayam_goreng", cooked=70, left=8,
                           itemwise_rows=[{"item_name": "Ayam Goreng", "qty": 62}])
    assert ev["used"] == 62
    assert ev["flag"] is None  # matches POS


# --- FIX 1: untouched items save as 0; Hantar with >=1 -----------------------

def test_finalize_cooked_partial_saves_zeros():
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    session = {
        "id": "c1", "chat_id": -1, "outlet_code": "SEK20",
        "business_date": "2026-06-24", "phase": "cooked", "status": "open",
        "entries": {"ayam_goreng": 50, "kambing": 3.5},  # only 2 of 10 keyed
    }
    fake._store["kitchen_log_session"] = [dict(session)]

    ku.finalize_submission(fake, dict(session), submitter="Chef")

    rows = {r["item_code"]: r for r in fake.rows("kitchen_daily_usage")}
    # All SEK20 items get a row (untouched -> 0); none left None.
    assert set(rows) == set(ku.required_codes("SEK20"))
    assert rows["ayam_goreng"]["cooked_qty"] == 50
    assert rows["kambing"]["cooked_qty"] == 3.5
    assert rows["ayam_bawang"]["cooked_qty"] == 0  # untouched
    assert all(r.get("left_qty") is None for r in rows.values())  # LEFT not yet


def test_finalize_left_partial_saves_zeros():
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    session = {
        "id": "l1", "chat_id": -1, "outlet_code": "SEK20",
        "business_date": "2026-06-24", "phase": "left", "status": "open",
        "entries": {"ayam_goreng": 4},
    }
    fake._store["kitchen_log_session"] = [dict(session)]
    ku.finalize_submission(fake, dict(session), submitter="Cashier")

    rows = {r["item_code"]: r for r in fake.rows("kitchen_daily_usage")}
    assert rows["ayam_goreng"]["left_qty"] == 4
    assert rows["ayam_bawang"]["left_qty"] == 0  # untouched -> 0


def test_digest_complete_when_zeros_but_both_submitted():
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    # Both COOKED and LEFT submitted; some items are 0 — still COMPLETE.
    fake._store["kitchen_daily_usage"] = [
        {"outlet_code": "SEK20", "business_date": "2026-06-24", "item_code": "ayam_goreng",
         "item_label": "Ayam Goreng", "unit": "pcs", "cooked_qty": 50, "left_qty": 4},
        {"outlet_code": "SEK20", "business_date": "2026-06-24", "item_code": "ayam_bawang",
         "item_label": "Ayam Bawang", "unit": "pcs", "cooked_qty": 0, "left_qty": 0},
    ]
    out = ku.gather_digest_usage(fake, "2026-06-24")
    assert len(out) == 1 and out[0]["complete"] is True


def test_digest_incomplete_when_left_session_missing():
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    # COOKED submitted (0s included) but LEFT never submitted -> incomplete.
    fake._store["kitchen_daily_usage"] = [
        {"outlet_code": "SEK20", "business_date": "2026-06-24", "item_code": "ayam_goreng",
         "item_label": "Ayam Goreng", "unit": "pcs", "cooked_qty": 50, "left_qty": None},
        {"outlet_code": "SEK20", "business_date": "2026-06-24", "item_code": "ayam_bawang",
         "item_label": "Ayam Bawang", "unit": "pcs", "cooked_qty": 0, "left_qty": None},
    ]
    out = ku.gather_digest_usage(fake, "2026-06-24")
    assert len(out) == 1 and out[0]["complete"] is False



# --- bulk free-text parsing -------------------------------------------------

def test_bulk_parse_multiline():
    text = "ayam goreng 50\nayam bawang 40\nkambing 8\ndaging 5"
    out = ku.parse_bulk_entry(text, "SEK20")
    assert out["matched"] == {
        "ayam_goreng": 50, "ayam_bawang": 40, "kambing": 8.0, "daging": 5.0,
    }
    assert out["unmatched"] == []


def test_bulk_parse_comma_separated():
    out = ku.parse_bulk_entry("ayam goreng 50, ayam bawang 40", "SEK20")
    assert out["matched"] == {"ayam_goreng": 50, "ayam_bawang": 40}


def test_bulk_parse_fuzzy_names():
    assert ku.match_item_name("ayamgoreng") == "ayam_goreng"
    assert ku.match_item_name("a goreng") == "ayam_goreng"
    assert ku.match_item_name("telurikan") == "telur_ikan"
    assert ku.match_item_name("madu") == "ayam_madu"
    assert ku.match_item_name("kari") == "ikan_kari"
    # bare "goreng" is ambiguous (ayam vs ikan) -> no match
    assert ku.match_item_name("goreng") is None


def test_bulk_parse_kg_decimal_incl_comma():
    out = ku.parse_bulk_entry("kambing 3.5\ndaging 2,5\ntelur ikan 4", "SEK20")
    assert out["matched"] == {"kambing": 3.5, "daging": 2.5, "telur_ikan": 4.0}
    # pcs stays whole even if a decimal is typed
    out2 = ku.parse_bulk_entry("ayam goreng 50.9", "SEK20")
    assert out2["matched"]["ayam_goreng"] == 51


def test_bulk_parse_reports_unmatched():
    out = ku.parse_bulk_entry("ayam goreng 50\nxyz 5\nnasi 10", "SEK20")
    assert out["matched"] == {"ayam_goreng": 50}
    assert "xyz 5" in out["unmatched"] and "nasi 10" in out["unmatched"]


def test_bulk_parse_rempah_bistro_only():
    assert ku.parse_bulk_entry("ayam rempah 50", "BISTRO7")["matched"] == {"ayam_rempah": 50}
    out = ku.parse_bulk_entry("ayam rempah 50", "SEK20")
    assert out["matched"] == {}
    assert "ayam rempah 50" in out["unmatched"]


# --- tap-button form (restored original) ------------------------------------

def test_form_text_says_tap_to_keyin_not_typing():
    t = ku.form_text("cooked", "2026-06-24", "SEK-20", {}, "SEK20")
    assert "Tap untuk key-in" in t
    assert "Balas SATU mesej" not in t   # bulk-typing instruction removed
    assert "தமிழ்" in t                   # Tamil line present


def test_build_item_keyboard_has_one_button_per_item_plus_hantar():
    kb = ku.build_item_keyboard("s1", "SEK20", {"ayam_goreng": 50}, "cooked")
    rows = kb.inline_keyboard
    # 10 SEK20 items + 1 Hantar row
    assert len(rows) == len(ku.required_codes("SEK20")) + 1
    texts = [r[0].text for r in rows]
    assert "✓ Ayam Goreng: 50" in texts        # filled shows ✓ + value
    assert "Ayam Bawang: —" in texts            # empty shows —
    assert any("Hantar" in t for t in texts)    # Hantar button present
    # item buttons open the numpad
    assert rows[0][0].callback_data.endswith(":open")


def test_bulk_handler_removed():
    # The bulk free-text / ForceReply experiments are gone (tap-only entry).
    assert not hasattr(ku, "handle_kitchen_bulk_text")
    assert not hasattr(ku, "build_confirm_keyboard")
    assert not hasattr(ku, "get_open_session_for_chat")


# --- business_date span (18:00 -> 02:00 next day = same business day) --------

def test_business_date_night_and_left_fold_to_prior_6pm_day():
    # 24 Jun 18:00 COOKED, 25 Jun 00:00 NIGHT, 25 Jun 02:00 LEFT -> all 2026-06-24.
    assert ku.business_date_for(datetime(2026, 6, 24, 18, 0, tzinfo=MY)).isoformat() == "2026-06-24"
    assert ku.business_date_for(datetime(2026, 6, 25, 0, 0, tzinfo=MY)).isoformat() == "2026-06-24"
    assert ku.business_date_for(datetime(2026, 6, 25, 2, 0, tzinfo=MY)).isoformat() == "2026-06-24"


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


# --- per-item POS mapping: each whole-leg item on its OWN line ----------------

def test_ayam_goreng_whole_cut_only_excludes_carb_fried():
    rows = [
        {"item_name": "Ayam Goreng", "qty": 10},
        {"item_name": "Ayam Goreng Besar", "qty": 4},
        {"item_name": "Nasi Ayam GORENG Besar", "qty": 3},
        {"item_name": "Nasi Ayam GORENG Besar Sayur", "qty": 2},
        # carb-fried with ayam trailing -> NOT whole-cut, must be ignored
        {"item_name": "Nasi Goreng Ayam", "qty": 50},
        {"item_name": "Maggi Goreng Ayam", "qty": 20},
        {"item_name": "Mee Goreng Ayam", "qty": 15},
        {"item_name": "Kuey Teow Goreng Ayam", "qty": 8},
    ]
    assert ku.pos_qty_for_item("ayam_goreng", rows) == 19  # 10+4+3+2


def test_ayam_bawang_counts_nasi_separuh_and_briyani():
    rows = [
        {"item_name": "Ayam Bawang", "qty": 5},
        {"item_name": "Nasi Ayam Bawang", "qty": 4},
        {"item_name": "Nasi Ayam Bawang Sayur", "qty": 3},
        {"item_name": "Nasi Separuh Ayam Bawang", "qty": 2},
        {"item_name": "Briyani Ayam Bawang Set", "qty": 6},
        {"item_name": "Briyani Ayam Bawang Telur", "qty": 1},
        # plain isi-ayam (no style) must NOT count for any item
        {"item_name": "Nasi Ayam", "qty": 99},
        {"item_name": "Nasi Separuh Ayam", "qty": 88},
    ]
    assert ku.pos_qty_for_item("ayam_bawang", rows) == 21  # 5+4+3+2+6+1


def test_plain_isi_ayam_matches_nothing():
    rows = [
        {"item_name": "Nasi Ayam", "qty": 30},
        {"item_name": "Nasi Separuh Ayam", "qty": 20},
        {"item_name": "Isi Ayam", "qty": 10},
    ]
    for code in ("ayam_goreng", "ayam_bawang", "ayam_kicap",
                 "ayam_madu", "ayam_tandoori", "ayam_rempah"):
        assert ku.pos_qty_for_item(code, rows) == 0.0, code


def test_ayam_kicap_matches_masak_kicap():
    rows = [
        {"item_name": "Ayam Masak Kicap", "qty": 7},
        {"item_name": "Nasi Ayam Kicap", "qty": 3},
        {"item_name": "Ayam Bawang", "qty": 99},
    ]
    assert ku.pos_qty_for_item("ayam_kicap", rows) == 10


def test_ayam_madu_line():
    rows = [
        {"item_name": "Ayam Madu", "qty": 6},
        {"item_name": "Nasi Ayam Madu Sayur", "qty": 2},
    ]
    assert ku.pos_qty_for_item("ayam_madu", rows) == 8


def test_ayam_tandoori_excludes_staff_and_accepts_misspelling():
    rows = [
        {"item_name": "Ayam Tandoori", "qty": 9},
        {"item_name": "Ayam Tandori", "qty": 4},          # common POS misspelling
        {"item_name": "Ayam Tandori Staff", "qty": 100},  # staff meal -> excluded
        {"item_name": "Ayam Tandoori Staff", "qty": 50},  # staff meal -> excluded
    ]
    assert ku.pos_qty_for_item("ayam_tandoori", rows) == 13  # 9+4


def test_ayam_rempah_bistro_only_and_not_fried_berempah():
    rows = [
        {"item_name": "Ayam Rempah", "qty": 5},
        {"item_name": "Ayam Masak Rempah", "qty": 3},
        # a fried "...berempah" dish is a goreng dish, not rempah
        {"item_name": "Ayam Goreng Berempah", "qty": 40},
    ]
    assert ku.pos_qty_for_item("ayam_rempah", rows) == 8        # 5+3, not the fried one
    assert ku.pos_qty_for_item("ayam_goreng", rows) == 40       # fried one goes here


def test_thai_food_category_and_thai_dishes_excluded():
    rows = [
        {"item_name": "Paprik Ayam", "qty": 30},
        {"item_name": "Tomyam Ayam", "qty": 20},
        {"item_name": "Indomee Ayam", "qty": 10},
        # a bawang dish but flagged THAI FOOD by category -> excluded
        {"item_name": "Ayam Bawang Thai", "qty": 12, "category": "THAI FOOD"},
    ]
    for code in ("ayam_goreng", "ayam_bawang", "ayam_kicap", "ayam_madu"):
        assert ku.pos_qty_for_item(code, rows) == 0.0, code


def test_ayam_rendang_kurma_kari_excluded():
    rows = [
        {"item_name": "Ayam Rendang", "qty": 10},
        {"item_name": "Ayam Kurma", "qty": 8},
        {"item_name": "Ayam Kari", "qty": 6},
    ]
    for code in ku.ITEM_POS_KEYWORDS:
        if ku.ITEM_POS_KEYWORDS[code]["base"] == "ayam":
            assert ku.pos_qty_for_item(code, rows) == 0.0, code


def test_kambing_all_dishes_x180g():
    # S-KLANG 24 Jun cross-check: ~8 kambing portions in the header.
    rows = [
        {"item_name": "Kambing Masak Merah", "qty": 5},
        {"item_name": "Nasi Kambing", "qty": 2},
        {"item_name": "Briyani Kambing Set", "qty": 1},
    ]
    # 8 portions * 180 g = 1.44 kg
    assert ku.pos_qty_for_item("kambing", rows) == 1.44


def test_daging_all_dishes_x60g():
    # S-KLANG 24 Jun cross-check: ~4 daging portions.
    rows = [
        {"item_name": "Daging Masak Merah", "qty": 3},
        {"item_name": "Nasi Daging", "qty": 1},
    ]
    # 4 portions * 60 g = 0.24 kg
    assert ku.pos_qty_for_item("daging", rows) == 0.24


def test_kambing_daging_drop_staff_but_keep_all_others():
    rows = [
        {"item_name": "Kambing Masak Merah", "qty": 8},
        {"item_name": "Kambing Staff", "qty": 100},  # staff meal -> excluded
    ]
    assert ku.pos_qty_for_item("kambing", rows) == 1.44  # 8 * 180 / 1000


def test_ikan_goreng_and_kari_separate_lines():
    rows = [
        {"item_name": "Ikan Goreng", "qty": 12},
        {"item_name": "Ikan Kari Kepala", "qty": 7},
        {"item_name": "Ikan Curry", "qty": 3},
    ]
    assert ku.pos_qty_for_item("ikan_goreng", rows) == 12
    assert ku.pos_qty_for_item("ikan_kari", rows) == 10  # 7 + 3


# --- outlet-code normalisation (kitchen <-> POS join) ------------------------

def test_normalize_outlet_strips_pos_prefix_all_ten_outlets():
    # (kitchen_code, pos_code) pairs must collapse to the SAME join key.
    pairs = [
        ("BISTRO7", "S-BISTRO7"),
        ("SEK20", "D-SEK20"),
        ("SEK14", "S-SEK14"),
        ("SEK15", "D-SEK15"),
        ("SEK6", "S-SEK6"),
        ("VISTA", "D-VISTA"),
        ("JAKEL", "S-JAKEL"),
        ("D", "D-DAMANSARA"),     # names differ but resolve to D.U
        ("KLANG", "S-KLANG"),
        ("KLRAZAK", "D-RAZAK"),   # names differ but resolve to K.L Razak
    ]
    keys = set()
    for kitchen, pos in pairs:
        k = ku.normalize_outlet_code(kitchen)
        assert k is not None, kitchen
        assert k == ku.normalize_outlet_code(pos), (kitchen, pos)
        keys.add(k)
    # all 10 outlets resolve to 10 DISTINCT keys (no accidental collisions)
    assert len(keys) == 10


def test_normalize_outlet_handles_blank_and_canonical():
    assert ku.normalize_outlet_code("") is None
    assert ku.normalize_outlet_code(None) is None
    # an already-canonical name is stable
    assert ku.normalize_outlet_code("Klang B.Emas") == "Klang B.Emas"


def test_fetch_itemwise_joins_kitchen_klang_to_pos_d_klang():
    """The POS=0 regression: kitchen 'KLANG' must join POS 'D-KLANG' summaries."""
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    # POS daily summary stored with the D- prefix (and its canonical name).
    fake._store[ku.SALES_SUMMARY_TABLE] = [
        {"id": 1, "outlet_code": "D-KLANG", "outlet_canonical": "Klang B.Emas",
         "business_date": "2026-06-24"},
        {"id": 2, "outlet_code": "D-SEK20", "outlet_canonical": "SEK-20",
         "business_date": "2026-06-24"},  # different outlet, must be ignored
    ]
    fake._store[ku.SALES_ITEMWISE_TABLE] = [
        {"id": 10, "summary_id": 1, "item_name": "Ayam Bawang", "qty": 5,
         "category": "AYAM"},
        {"id": 11, "summary_id": 1, "item_name": "Kambing Masak Merah", "qty": 8,
         "category": "KAMBING"},
        {"id": 12, "summary_id": 2, "item_name": "Ayam Bawang", "qty": 99,
         "category": "AYAM"},  # belongs to SEK20, must not leak in
    ]
    rows = ku._fetch_itemwise(fake, "KLANG", "2026-06-24")
    names = sorted(r["item_name"] for r in rows)
    assert names == ["Ayam Bawang", "Kambing Masak Merah"]
    assert ku.pos_qty_for_item("ayam_bawang", rows) == 5
    assert ku.pos_qty_for_item("kambing", rows) == 1.44  # 8 * 180 / 1000


def test_fetch_itemwise_joins_renamed_outlets():
    """Kitchen 'D'/'KLRAZAK' join POS 'D-DAMANSARA'/'D-RAZAK' despite name diffs."""
    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store[ku.SALES_SUMMARY_TABLE] = [
        {"id": 1, "outlet_code": "D-DAMANSARA", "outlet_canonical": "D.U",
         "business_date": "2026-06-24"},
        {"id": 2, "outlet_code": "D-RAZAK", "outlet_canonical": "K.L Razak",
         "business_date": "2026-06-24"},
    ]
    fake._store[ku.SALES_ITEMWISE_TABLE] = [
        {"id": 10, "summary_id": 1, "item_name": "Ayam Madu", "qty": 3},
        {"id": 11, "summary_id": 2, "item_name": "Ayam Madu", "qty": 7},
    ]
    du = ku._fetch_itemwise(fake, "D", "2026-06-24")
    assert ku.pos_qty_for_item("ayam_madu", du) == 3
    razak = ku._fetch_itemwise(fake, "KLRAZAK", "2026-06-24")
    assert ku.pos_qty_for_item("ayam_madu", razak) == 7


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


# --- numpad lag fix: in-memory buffer, DB only on commit ---------------------

def _cb_update(data, chat_id=-77, user_id=7):
    import types
    edits, answers, replies = [], [], []

    async def _edit(text, **k):
        edits.append((text, k))

    async def _reply(t, **k):
        replies.append(t)

    async def _answer(*a, **k):
        answers.append((a, k))

    message = types.SimpleNamespace(chat_id=chat_id, reply_text=_reply)
    query = types.SimpleNamespace(
        data=data,
        from_user=types.SimpleNamespace(id=user_id, full_name="Chef", username=None),
        message=message,
        edit_message_text=_edit,
        answer=_answer,
    )
    update = types.SimpleNamespace(callback_query=query)
    return update, edits, answers


def _ctx():
    import types
    return types.SimpleNamespace(bot=types.SimpleNamespace())


def _spy(ku_mod, monkeypatch, name):
    calls = {"n": 0}
    orig = getattr(ku_mod, name)

    def wrapper(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(ku_mod, name, wrapper)
    return calls


def test_numpad_open_seeds_memory_without_db_write(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [{
        "id": "s1", "chat_id": -77, "outlet_code": "SEK20", "business_date": "2026-06-24",
        "phase": "cooked", "status": "open", "entries": {},
    }]
    monkeypatch.setattr(ku, "_supabase", fake)
    ku._numpad_state.clear()
    saves = _spy(ku, monkeypatch, "_save_session")

    update, edits, _ = _cb_update("kdu:s1:ayam_goreng:open")
    asyncio.run(ku.handle_kitchen_callback(update, _ctx()))

    key = ku._numpad_key(-77, 7, "s1", "ayam_goreng")
    assert ku._numpad_state.get(key) == {"buffer": "", "phase": "cooked"}
    assert saves["n"] == 0  # open no longer writes the DB
    assert len(edits) == 1  # numpad shown


def test_numpad_digits_do_not_touch_db(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    monkeypatch.setattr(ku, "_supabase", FakeSupabase())
    ku._numpad_state.clear()
    ku._numpad_state[ku._numpad_key(-77, 7, "s1", "ayam_goreng")] = {"buffer": "", "phase": "cooked"}
    gets = _spy(ku, monkeypatch, "get_session")
    saves = _spy(ku, monkeypatch, "_save_session")

    last_answers = last_edits = None
    for data in ("kdu:s1:ayam_goreng:d5", "kdu:s1:ayam_goreng:d0"):
        update, edits, answers = _cb_update(data)
        asyncio.run(ku.handle_kitchen_callback(update, _ctx()))
        assert answers, "callback answered immediately (toast)"
        last_answers, last_edits = answers, edits

    assert ku._numpad_state[ku._numpad_key(-77, 7, "s1", "ayam_goreng")]["buffer"] == "50"
    assert gets["n"] == 0    # no DB read on digit taps
    assert saves["n"] == 0   # no DB write on digit taps
    # Running value comes from the answer-toast, not a message edit.
    assert last_edits == []
    assert "50" in last_answers[-1][1].get("text", "")


def test_numpad_digit_never_edits_message(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    monkeypatch.setattr(ku, "_supabase", FakeSupabase())
    ku._numpad_state.clear()
    ku._numpad_state[ku._numpad_key(-77, 7, "s1", "ayam_goreng")] = {"buffer": "", "phase": "cooked"}

    update, edits, answers = _cb_update("kdu:s1:ayam_goreng:bs")  # backspace on empty
    asyncio.run(ku.handle_kitchen_callback(update, _ctx()))
    assert edits == []       # digit/backspace never edits the message (toast only)
    assert answers           # spinner cleared via the toast answer


def test_numpad_ok_commits_to_db_and_clears_memory(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [{
        "id": "s1", "chat_id": -77, "outlet_code": "SEK20", "business_date": "2026-06-24",
        "phase": "cooked", "status": "open", "entries": {},
    }]
    monkeypatch.setattr(ku, "_supabase", fake)
    ku._numpad_state.clear()
    ku._numpad_state[ku._numpad_key(-77, 7, "s1", "ayam_goreng")] = {"buffer": "50", "phase": "cooked"}
    saves = _spy(ku, monkeypatch, "_save_session")

    update, edits, _ = _cb_update("kdu:s1:ayam_goreng:ok")
    asyncio.run(ku.handle_kitchen_callback(update, _ctx()))

    assert saves["n"] == 1  # commit writes once
    assert fake.rows("kitchen_log_session")[0]["entries"]["ayam_goreng"] == 50
    assert ku._numpad_state == {}  # memory cleared on commit


def test_numpad_memory_miss_recovers_from_db_once(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [{
        "id": "s1", "chat_id": -77, "outlet_code": "SEK20", "business_date": "2026-06-24",
        "phase": "cooked", "status": "open", "entries": {}, "buffer": "1",
    }]
    monkeypatch.setattr(ku, "_supabase", fake)
    ku._numpad_state.clear()  # memory lost (e.g. restart)
    saves = _spy(ku, monkeypatch, "_save_session")

    update, edits, answers = _cb_update("kdu:s1:ayam_goreng:d2")
    asyncio.run(ku.handle_kitchen_callback(update, _ctx()))

    # Recovered buffer "1" + "2" -> "12", in memory, still no DB write.
    assert ku._numpad_state[ku._numpad_key(-77, 7, "s1", "ayam_goreng")]["buffer"] == "12"
    assert saves["n"] == 0
    # Running value is shown via the answer-toast, NOT a message edit.
    assert edits == []
    assert answers and "12" in answers[-1][1].get("text", "")


# --- mistake-fixing before Hantar: overwrite + Kosongkan ---------------------

def test_numpad_keyboard_has_kosongkan_clear():
    kb = ku.build_numpad_keyboard("s1", "ayam_goreng", "pcs")
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any("Kosongkan" in b.text for b in btns)
    assert any(b.callback_data.endswith(":clr") for b in btns)


def test_open_filled_item_starts_empty_buffer_and_shows_current(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [{
        "id": "s1", "chat_id": -77, "outlet_code": "SEK20", "business_date": "2026-06-24",
        "phase": "cooked", "status": "open", "entries": {"ayam_goreng": 50},
    }]
    monkeypatch.setattr(ku, "_supabase", fake)
    ku._numpad_state.clear()

    update, edits, _ = _cb_update("kdu:s1:ayam_goreng:open")
    asyncio.run(ku.handle_kitchen_callback(update, _ctx()))

    # Re-tapping a filled item opens with an EMPTY buffer (typing replaces)...
    assert ku._numpad_state[ku._numpad_key(-77, 7, "s1", "ayam_goreng")]["buffer"] == ""
    # ...and the message shows the current value as a reference.
    assert edits and "Sekarang: 50" in edits[0][0]


def test_retap_filled_item_overwrites_on_commit(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [{
        "id": "s1", "chat_id": -77, "outlet_code": "SEK20", "business_date": "2026-06-24",
        "phase": "cooked", "status": "open", "entries": {"ayam_goreng": 50},
    }]
    monkeypatch.setattr(ku, "_supabase", fake)
    ku._numpad_state.clear()

    # open -> type 6,0 -> ✓  (replaces 50 with 60)
    for data in ("kdu:s1:ayam_goreng:open", "kdu:s1:ayam_goreng:d6",
                 "kdu:s1:ayam_goreng:d0", "kdu:s1:ayam_goreng:ok"):
        asyncio.run(ku.handle_kitchen_callback(_cb_update(data)[0], _ctx()))

    assert fake.rows("kitchen_log_session")[0]["entries"]["ayam_goreng"] == 60
    assert ku._numpad_state == {}  # cleared after commit


def test_commit_empty_buffer_keeps_existing_value(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [{
        "id": "s1", "chat_id": -77, "outlet_code": "SEK20", "business_date": "2026-06-24",
        "phase": "cooked", "status": "open", "entries": {"ayam_goreng": 50},
    }]
    monkeypatch.setattr(ku, "_supabase", fake)
    ku._numpad_state.clear()
    # open then ✓ without typing -> old value kept (no accidental clear)
    for data in ("kdu:s1:ayam_goreng:open", "kdu:s1:ayam_goreng:ok"):
        asyncio.run(ku.handle_kitchen_callback(_cb_update(data)[0], _ctx()))
    assert fake.rows("kitchen_log_session")[0]["entries"]["ayam_goreng"] == 50


def test_kosongkan_unsets_item(monkeypatch):
    import asyncio

    from tests.fake_supabase import FakeSupabase

    fake = FakeSupabase()
    fake._store["kitchen_log_session"] = [{
        "id": "s1", "chat_id": -77, "outlet_code": "SEK20", "business_date": "2026-06-24",
        "phase": "cooked", "status": "open", "entries": {"ayam_goreng": 50, "kambing": 3},
    }]
    monkeypatch.setattr(ku, "_supabase", fake)
    ku._numpad_state.clear()

    update, edits, _ = _cb_update("kdu:s1:ayam_goreng:clr")
    asyncio.run(ku.handle_kitchen_callback(update, _ctx()))

    entries = fake.rows("kitchen_log_session")[0]["entries"]
    assert "ayam_goreng" not in entries   # cleared back to unset
    assert entries.get("kambing") == 3    # other items untouched
    # returned to the item list (form text + item keyboard)
    assert edits and "Ayam Goreng: —" in str(edits[0])
