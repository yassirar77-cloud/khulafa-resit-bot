// node --test stok_dapur/tests/core.test.js   (run by tests/test_stok_dapur_js.py)
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../core.js");

test("business date: noon fold in Asia/Kuala_Lumpur", () => {
  assert.equal(C.businessDate("2026-06-22T10:00:00Z"), "2026-06-22"); // 18:00 MYT
  assert.equal(C.businessDate("2026-06-22T18:00:00Z"), "2026-06-22"); // 02:00 MYT next day -> evening's day
  assert.equal(C.businessDate("2026-06-23T03:59:00Z"), "2026-06-22"); // 11:59 MYT
  assert.equal(C.businessDate("2026-06-23T04:00:00Z"), "2026-06-23"); // 12:00 MYT
});

test("keypad: whole numbers", () => {
  let b = "";
  for (const k of ["0", "1", "2"]) b = C.applyKey(b, k, 0);
  assert.equal(b, "12", "no leading zero");
  assert.equal(C.applyKey(b, ".", 0), "12", "no dot for pcs/beg");
  assert.equal(C.applyKey(b, "back", 0), "1");
  let big = "";
  for (let i = 0; i < 9; i++) big = C.applyKey(big, "9", 0);
  assert.equal(big, "999999", "max 6 digits");
});

test("keypad: kg with 2 decimals", () => {
  let b = "";
  for (const k of [".", "5"]) b = C.applyKey(b, k, 2);
  assert.equal(b, "0.5");
  b = C.applyKey(C.applyKey(b, "2", 2), "9", 2);
  assert.equal(b, "0.52", "third decimal ignored");
  assert.equal(C.applyKey(b, ".", 2), "0.52", "one dot only");
  assert.equal(C.bufferValue("3."), 3);
  assert.equal(C.bufferValue(""), null);
});

test("+/- and pack chips never go below zero", () => {
  assert.equal(C.step("", 80, 0), "80", "+1 beg = 80 pcs");
  assert.equal(C.step("80", 80, 0), "160");
  assert.equal(C.step("1", -1, 0), "0");
  assert.equal(C.step("0", -1, 0), "0");
  assert.equal(C.step("1.25", 1, 2), "2.25");
});

test("terima uses the bill unit, guna/baki the counting unit", () => {
  const leg = { unit: "pcs", decimals: 0, terima_unit: "kg", terima_decimals: 2 };
  assert.deepEqual(C.entryUnit(leg, "terima"), { unit: "kg", decimals: 2 });
  assert.deepEqual(C.entryUnit(leg, "baki"), { unit: "pcs", decimals: 0 });
  const gula = { unit: "karung", decimals: 0, terima_unit: null };
  assert.deepEqual(C.entryUnit(gula, "terima"), { unit: "karung", decimals: 0 });
});

test("names fall back to BM", () => {
  const it = { name_bm: "Halia", name_ta: "இஞ்சி", name_bn: null };
  assert.equal(C.itemName(it, "ta"), "இஞ்சி");
  assert.equal(C.itemName(it, "bn"), "Halia");
});

test("buildBatch drops untouched items and keeps the receipt link", () => {
  const b = C.buildBatch({
    kind: "terima", staffId: "s1", deviceId: "d1", supplier: "BESTARI FARM (M) SDN BHD", receiptId: 17860,
    draft: { ayam_p8: { qty: 14.6, prefill_qty: 14.6 }, ayam_hati: { qty: null, prefill_qty: 1 }, halia: { qty: 0 } },
    now: new Date("2026-11-12T10:00:00Z"),
  });
  assert.match(b.id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(b.items, [{ item_code: "ayam_p8", qty: 14.6, prefill_qty: 14.6 }, { item_code: "halia", qty: 0 }]);
  assert.equal(b.receipt_id, 17860);
  assert.equal(b.captured_at, "2026-11-12T10:00:00.000Z");
  assert.equal(C.buildBatch({ kind: "baki", draft: { x: { qty: null } } }), null, "nothing filled -> nothing to send");
});

test("each batch id is unique (offline retries stay idempotent)", () => {
  const ids = new Set(Array.from({ length: 500 }, () => C.uuid()));
  assert.equal(ids.size, 500);
});

test("sync outcome: what stays in the offline queue", () => {
  assert.equal(C.syncOutcome(200, { status: "ok" }), "done");
  assert.equal(C.syncOutcome(200, { status: "exists" }), "done", "already saved by an earlier retry");
  assert.equal(C.syncOutcome(0, null), "retry", "no network");
  assert.equal(C.syncOutcome(503, {}), "retry");
  assert.equal(C.syncOutcome(401, {}), "retry", "token expired: refresh + retry");
  assert.equal(C.syncOutcome(400, { code: "22023", message: "bad qty for halia" }), "stuck");
  assert.equal(C.syncOutcome(403, { code: "42501", message: "new row violates row-level security policy" }), "stuck");
});

test("offline PIN verifier: same PIN + salt matches, other PIN does not", async () => {
  const salt = Buffer.from("0123456789abcdef").toString("base64");
  const a = await C.pinVerifier("2580", salt);
  assert.equal(a, await C.pinVerifier("2580", salt));
  assert.notEqual(a, await C.pinVerifier("2581", salt));
});
