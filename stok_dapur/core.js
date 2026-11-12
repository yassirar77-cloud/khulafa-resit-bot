/* Khulafa Stok Dapur — pure logic shared by the kitchen app (no DOM, no network).
   Loaded as a classic <script> in the browser (window.StokCore) and as a
   CommonJS module by the node tests (stok_dapur/tests/core.test.js). */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.StokCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---- business date: before 12:00 Asia/Kuala_Lumpur = previous day ------
  // (same fold as stok_business_date() in SQL and kitchen_usage.business_date_for)
  function businessDate(d) {
    const t = (d instanceof Date ? d.getTime() : Date.parse(d)) + 8 * 3600e3 - 12 * 3600e3;
    return new Date(t).toISOString().slice(0, 10);
  }

  // ---- keypad buffer ---------------------------------------------------------
  // buffer is the typed string ("", "12", "3.", "3.5"); decimals = allowed places.
  const MAX_DIGITS = 6;
  function applyKey(buffer, key, decimals) {
    buffer = buffer || "";
    if (key === "back") return buffer.slice(0, -1);
    if (key === "clear") return "";
    if (key === ".") {
      if (!decimals || buffer.includes(".")) return buffer;
      return buffer === "" ? "0." : buffer + ".";
    }
    if (!/^\d$/.test(key)) return buffer;
    const [intPart, frac] = buffer.split(".");
    if (frac !== undefined) {
      if (frac.length >= decimals) return buffer;
      return buffer + key;
    }
    if (intPart.replace(/^0+/, "").length >= MAX_DIGITS) return buffer;
    if (buffer === "0") return key; // no leading zeros
    return buffer + key;
  }

  function bufferValue(buffer) {
    if (buffer === "" || buffer == null) return null;
    const v = Number(buffer.endsWith(".") ? buffer.slice(0, -1) : buffer);
    return Number.isFinite(v) ? v : null;
  }

  function round(v, decimals) {
    const f = Math.pow(10, decimals || 0);
    return Math.round(v * f) / f;
  }

  // +/- and pack chips; never below 0
  function step(buffer, delta, decimals) {
    const cur = bufferValue(buffer) || 0;
    const next = Math.max(0, round(cur + delta, decimals));
    return formatBuffer(next, decimals);
  }

  function formatBuffer(v, decimals) {
    if (v == null) return "";
    const s = round(v, decimals).toFixed(decimals || 0);
    return decimals ? s.replace(/\.?0+$/, "") : s;
  }

  function fmt(v, decimals) {
    if (v == null || v === "") return "—";
    return formatBuffer(Number(v), decimals == null ? 2 : decimals) || "0";
  }

  // ---- item helpers ------------------------------------------------------------
  // the unit + decimals the keypad uses for an item in a given kind
  function entryUnit(item, kind) {
    if (kind === "terima" && item.terima_unit) {
      return { unit: item.terima_unit, decimals: item.terima_decimals == null ? 2 : item.terima_decimals };
    }
    return { unit: item.unit, decimals: item.decimals || 0 };
  }

  // name in the chosen language, falling back to BM
  function itemName(item, lang) {
    if (lang === "ta" && item.name_ta) return item.name_ta;
    if (lang === "bn" && item.name_bn) return item.name_bn;
    return item.name_bm;
  }

  // ---- batches + offline queue -------------------------------------------------
  function uuid() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    const b = new Uint8Array(16);
    (typeof crypto !== "undefined" ? crypto : require("crypto").webcrypto).getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  // draft = {item_code: {qty, prefill_qty?}} -> the stok_submit_batch payload
  function buildBatch({ kind, draft, staffId, deviceId, supplier, receiptId, now }) {
    const items = Object.keys(draft)
      .filter((code) => draft[code] && draft[code].qty != null)
      .map((code) => {
        const row = { item_code: code, qty: draft[code].qty };
        if (draft[code].prefill_qty != null) row.prefill_qty = draft[code].prefill_qty;
        return row;
      });
    if (!items.length) return null;
    const b = {
      id: uuid(),
      kind,
      items,
      staff_id: staffId,
      device_id: deviceId,
      captured_at: (now || new Date()).toISOString(),
    };
    if (supplier) b.supplier = supplier;
    if (receiptId != null) b.receipt_id = receiptId;
    return b;
  }

  // What to do with a queued batch after one sync attempt.
  //   'done'  — saved (or was already saved): drop it from the queue
  //   'retry' — network / server / auth problem: keep it, try again later
  //   'stuck' — the server refused the content: keep it, show it to the office
  function syncOutcome(httpStatus, body) {
    if (httpStatus >= 200 && httpStatus < 300) {
      const s = body && body.status;
      return s === "ok" || s === "exists" ? "done" : "stuck";
    }
    if (httpStatus === 0 || httpStatus >= 500 || httpStatus === 401 || httpStatus === 408 || httpStatus === 429) {
      return "retry";
    }
    if (httpStatus === 403 && body && body.code === "42501" && /session/.test(body.message || "")) return "retry";
    return "stuck";
  }

  // ---- offline PIN check -------------------------------------------------------
  // After a successful ONLINE login the app keeps a PBKDF2 verifier of the PIN so
  // the same staff member can sign in on this device while offline.
  async function pinVerifier(pin, saltB64, subtle) {
    subtle = subtle || (typeof crypto !== "undefined" ? crypto.subtle : require("crypto").webcrypto.subtle);
    const enc = new TextEncoder();
    const salt = Uint8Array.from(atobSafe(saltB64), (c) => c.charCodeAt(0));
    const key = await subtle.importKey("raw", enc.encode(pin), "PBKDF2", false, ["deriveBits"]);
    const bits = await subtle.deriveBits({ name: "PBKDF2", salt, iterations: 120000, hash: "SHA-256" }, key, 256);
    return btoaSafe(String.fromCharCode(...new Uint8Array(bits)));
  }
  function atobSafe(s) { return typeof atob === "function" ? atob(s) : Buffer.from(s, "base64").toString("binary"); }
  function btoaSafe(s) { return typeof btoa === "function" ? btoa(s) : Buffer.from(s, "binary").toString("base64"); }

  return {
    businessDate, applyKey, bufferValue, step, formatBuffer, fmt, round,
    entryUnit, itemName, uuid, buildBatch, syncOutcome, pinVerifier,
  };
});
