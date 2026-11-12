/* Khulafa Stok Dapur — kitchen app (vanilla JS, offline-first, zero typing). */
(function () {
  "use strict";
  const C = window.StokCore;
  const CFG = window.STOK_CONFIG;
  const $app = document.getElementById("app");

  // ------------------------------------------------------------------ text --
  const T = {
    pick_outlet: ["Pilih kedai", "கடையைத் தேர்ந்தெடு", "দোকান বাছুন"],
    terima: ["Terima Barang", "பொருள் வரவு", "মাল গ্রহণ"],
    guna: ["Guna", "பயன்பாடு", "ব্যবহার"],
    baki: ["Baki (Kira Stok)", "மீதி எண்ணிக்கை", "অবশিষ্ট গণনা"],
    hantar: ["HANTAR", "அனுப்பு", "পাঠান"],
    saved: ["Disimpan", "சேமிக்கப்பட்டது", "সংরক্ষিত"],
    unsent: ["belum hantar", "அனுப்பவில்லை", "পাঠানো হয়নি"],
    other: ["Lain-lain", "மற்றவை", "অন্যান্য"],
    add: ["Tambah", "சேர்", "যোগ"],
    from_receipt: ["resit", "ரசீது", "রসিদ"],
    need_net: ["Perlu internet sekali", "ஒருமுறை இணையம் தேவை", "একবার ইন্টারনেট লাগবে"],
    wrong_pin: ["PIN salah", "தவறான PIN", "ভুল PIN"],
    locked: ["Dikunci 15 minit", "15 நிமிடம் பூட்டு", "১৫ মিনিট লক"],
    no_pin: ["Minta pejabat set PIN", "அலுவலகத்திடம் PIN கேள்", "অফিস থেকে PIN নিন"],
    empty_receipts: ["Tiada resit hari ini", "இன்று ரசீது இல்லை", "আজ কোনো রসিদ নেই"],
    stuck: ["Gagal — tunjuk pejabat", "தோல்வி — அலுவலகம்", "ব্যর্থ — অফিসকে দেখান"],
    no_staff: ["Tiada staf — minta pejabat tambah", "பணியாளர் இல்லை — அலுவலகத்திடம் கேள்", "কোনো কর্মী নেই — অফিসকে বলুন"],
    no_outlet: ["Tiada kedai aktif — minta pejabat hidupkan", "கடை இல்லை — அலுவலகத்திடம் கேள்", "কোনো দোকান নেই — অফিসকে বলুন"],
  };
  const LANGS = [["bm", "BM"], ["ta", "தமிழ்"], ["bn", "বাংলা"]];
  const LIDX = { bm: 0, ta: 1, bn: 2 };
  function t(key) { return T[key][LIDX[S.cfg.lang || "bm"]] || T[key][0]; }
  function t3(key) { return T[key]; }
  const KIND_ICON = { terima: "🚚", guna: "🍳", baki: "🌙" };
  const KIND_COLOR = { terima: "k-terima", guna: "k-guna", baki: "k-baki" };

  // --------------------------------------------------------------- storage --
  const store = {
    get(k, d) { try { const v = localStorage.getItem("sd." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("sd." + k, JSON.stringify(v)); } catch (e) { /* full / private mode */ } },
    del(k) { try { localStorage.removeItem("sd." + k); } catch (e) { /* ignore */ } },
  };

  const S = {
    cfg: store.get("cfg", { lang: "bm" }),
    roster: store.get("roster", []),
    catalog: store.get("catalog", null),
    staff: null,
    screen: "boot",
    kind: null,
    cat: null,
    draft: {},
    supplier: null,
    today: {},
    prefill: store.get("prefill", []),
    syncing: false,
  };
  if (!S.cfg.device_id) { S.cfg.device_id = C.uuid(); store.set("cfg", S.cfg); }

  // ------------------------------------------------------------------- api --
  async function call(path, { method = "GET", body, token, headers = {} } = {}) {
    try {
      const r = await fetch(CFG.url + path, {
        method,
        headers: Object.assign({
          apikey: CFG.anonKey,
          Authorization: "Bearer " + (token || CFG.anonKey),
          "Content-Type": "application/json",
        }, headers),
        body: body == null ? undefined : JSON.stringify(body),
      });
      let data = null;
      try { data = await r.json(); } catch (e) { /* empty */ }
      return { status: r.status, data };
    } catch (e) {
      return { status: 0, data: null };
    }
  }
  const rpc = (name, args, token) => call("/rest/v1/rpc/" + name, { method: "POST", body: args || {}, token });

  async function tokenFor(staffId) {
    const s = store.get("sess." + staffId, null);
    if (!s) return null;
    if (s.expires_at && s.expires_at - 90 > Date.now() / 1000) return s.access_token;
    const r = await call("/auth/v1/token?grant_type=refresh_token", { method: "POST", body: { refresh_token: s.refresh_token } });
    if (r.status === 200 && r.data && r.data.access_token) {
      store.set("sess." + staffId, { access_token: r.data.access_token, refresh_token: r.data.refresh_token, expires_at: r.data.expires_at });
      return r.data.access_token;
    }
    if (r.status === 400 || r.status === 401) store.del("sess." + staffId); // revoked: needs a fresh PIN login
    return null;
  }

  // --------------------------------------------------------------- queue ----
  function queue() { return store.get("queue", []); }
  function stuck() { return store.get("stuck", []); }

  async function syncQueue() {
    if (S.syncing || !navigator.onLine) return;
    S.syncing = true;
    try {
      let q = queue();
      for (const b of q.slice()) {
        const token = await tokenFor(b.staff_id);
        if (!token) continue; // that staff member must sign in once to release it
        const payload = Object.assign({}, b);
        delete payload.staff_id;
        const r = await rpc("stok_submit_batch", { p: payload }, token);
        const out = C.syncOutcome(r.status, r.data);
        if (out === "retry") { if (r.status === 0) break; else continue; }
        q = queue().filter((x) => x.id !== b.id);
        store.set("queue", q);
        if (out === "stuck") store.set("stuck", stuck().concat([Object.assign({}, b, { error: r.data })]));
      }
    } finally {
      S.syncing = false;
      renderBadge();
    }
  }

  // ------------------------------------------------------------------ util --
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function items() { return (S.catalog && S.catalog.items) || []; }
  function itemByCode(code) { return items().find((i) => i.code === code); }
  function names3(o) {
    const main = C.itemName(o, S.cfg.lang);
    const others = [o.name_bm, o.name_ta, o.name_bn].filter((n) => n && n !== main);
    return `<div class="n1">${esc(main)}</div><div class="n2">${others.map(esc).join(" · ")}</div>`;
  }
  function icon(o) {
    return o.photo_url ? `<img class="ph" src="${esc(o.photo_url)}" alt="">` : `<div class="em">${esc(o.icon || "📦")}</div>`;
  }
  function avatar(st) {
    if (st.photo_url) return `<img class="av" src="${esc(st.photo_url)}" alt="">`;
    const hue = [...st.id].reduce((a, c) => a + c.charCodeAt(0), 0) % 360;
    return `<div class="av" style="background:hsl(${hue} 55% 45%)">${esc((st.name || "?").trim().charAt(0).toUpperCase())}</div>`;
  }
  function draftCount() { return Object.values(S.draft).filter((d) => d && d.qty != null).length; }

  // idle: back to the staff picker after 3 minutes (shared kitchen phone)
  let idleTimer = null;
  function poke() {
    clearTimeout(idleTimer);
    if (S.staff) idleTimer = setTimeout(() => { S.staff = null; S.draft = {}; go("staff"); }, 180000);
  }
  ["pointerdown", "keydown"].forEach((e) => document.addEventListener(e, poke, { passive: true }));

  // ------------------------------------------------------------------ feedback
  function beep(ok) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const notes = ok ? [660, 880, 1320] : [300, 200];
      notes.forEach((f, i) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f; o.type = "sine";
        g.gain.setValueAtTime(0.25, ctx.currentTime + i * 0.13);
        g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.13 + 0.12);
        o.connect(g); g.connect(ctx.destination);
        o.start(ctx.currentTime + i * 0.13); o.stop(ctx.currentTime + i * 0.13 + 0.13);
      });
    } catch (e) { /* no audio */ }
    try { navigator.vibrate && navigator.vibrate(ok ? [120, 60, 120] : [400]); } catch (e) { /* ignore */ }
  }
  function flash(ok, queued) {
    beep(ok);
    const el = document.createElement("div");
    el.className = "flash " + (ok ? "ok" : "bad");
    el.innerHTML = ok
      ? `<div class="big">✓</div><div class="msg">${t3("saved").map(esc).join("<br>")}</div>${queued ? `<div class="cloud">⏳ ${esc(t("unsent"))}</div>` : ""}`
      : `<div class="big">✕</div>`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), ok ? 1800 : 900);
  }

  // ---------------------------------------------------------------- chrome --
  function topbar(extra) {
    return `<header class="top">
      <div class="langs">${LANGS.map(([k, f]) => `<button class="flag ${S.cfg.lang === k ? "on" : ""}" data-lang="${k}">${f}</button>`).join("")}</div>
      <div id="badge" class="badge"></div>
      ${extra || ""}
    </header>`;
  }
  function renderBadge() {
    const el = document.getElementById("badge");
    if (!el) return;
    const n = queue().length, s = stuck().length, off = !navigator.onLine;
    el.innerHTML = (off ? `<span class="pill off">📵</span>` : "") +
      (n ? `<span class="pill wait">⏳ ${n} ${esc(t("unsent"))}</span>` : "") +
      (s ? `<span class="pill err">⚠️ ${s}</span>` : "") +
      (!off && !n && !s ? `<span class="pill okp">☁️✓</span>` : "");
  }
  function bind() {
    $app.querySelectorAll("[data-lang]").forEach((b) => b.onclick = () => { S.cfg.lang = b.dataset.lang; store.set("cfg", S.cfg); render(); });
    renderBadge();
  }
  function go(screen) { S.screen = screen; render(); window.scrollTo(0, 0); }

  // --------------------------------------------------------------- screens --
  async function screenOutlet() {
    $app.innerHTML = topbar() + `<h1 class="title">🏪 ${t3("pick_outlet").map(esc).join(" · ")}</h1><div id="list" class="grid g2"><div class="spin"></div></div>`;
    bind();
    const r = await rpc("stok_live_outlets", {});
    const list = document.getElementById("list");
    if (r.status !== 200) { list.innerHTML = `<div class="note">📶❌ ${esc(t("need_net"))}</div>`; return; }
    if (!r.data.length) { list.innerHTML = `<div class="note">🏪✕ ${t3("no_outlet").map(esc).join("<br>")}</div>`; return; }
    list.innerHTML = r.data.map((o) => `<button class="tile big" data-o="${esc(o.code)}" data-l="${esc(o.label)}">🏪<div class="n1">${esc(o.label)}</div></button>`).join("");
    list.querySelectorAll("[data-o]").forEach((b) => b.onclick = () => {
      S.cfg.outlet_code = b.dataset.o; S.cfg.outlet_label = b.dataset.l; store.set("cfg", S.cfg);
      store.set("roster", []); S.roster = []; go("staff");
    });
  }

  async function screenStaff() {
    let loaded = false;
    const draw = () => {
      $app.innerHTML = topbar(`<button class="ghost" id="chg">🏪 ${esc(S.cfg.outlet_label || S.cfg.outlet_code)}</button>`) +
        `<div class="grid g3">${S.roster.map((st) => `<button class="tile staff" data-s="${esc(st.id)}">${avatar(st)}<div class="n1">${esc(st.name)}</div></button>`).join("") ||
          (loaded ? `<div class="note">👤✕ ${t3("no_staff").map(esc).join("<br>")}<br><br>🏪 ⬆️</div>`
                  : navigator.onLine ? `<div class="spin"></div>` : `<div class="note">📶❌ ${esc(t("need_net"))}</div>`)}</div>`;
      bind();
      document.getElementById("chg").onclick = () => { if (confirm("Tukar kedai?")) { delete S.cfg.outlet_code; store.set("cfg", S.cfg); go("outlet"); } };
      $app.querySelectorAll("[data-s]").forEach((b) => b.onclick = () => { S.pinFor = S.roster.find((x) => x.id === b.dataset.s); go("pin"); });
    };
    draw();
    const r = await rpc("stok_roster", { p_outlet: S.cfg.outlet_code });
    if (r.status === 200 && Array.isArray(r.data)) { S.roster = r.data; store.set("roster", r.data); }
    loaded = r.status === 200;
    if (S.screen === "staff") draw();
    syncQueue();
  }

  function screenPin() {
    const st = S.pinFor;
    let buf = "";
    $app.innerHTML = topbar() + `<div class="pinhead">${avatar(st)}<div class="n1">${esc(st.name)}</div></div>
      <div class="dots" id="dots"></div><div id="pinmsg" class="pinmsg"></div>
      <div class="pad">${["1", "2", "3", "4", "5", "6", "7", "8", "9", "x", "0", "back"].map((k) =>
        `<button class="key ${k === "x" ? "red" : ""}" data-k="${k}">${k === "back" ? "⌫" : k === "x" ? "❌" : k}</button>`).join("")}</div>`;
    bind();
    const dots = () => { document.getElementById("dots").innerHTML = [0, 1, 2, 3].map((i) => `<span class="${i < buf.length ? "on" : ""}"></span>`).join(""); };
    dots();
    const msg = (m, bad) => { const el = document.getElementById("pinmsg"); el.textContent = m; el.className = "pinmsg " + (bad ? "bad" : ""); };
    if (!st.has_pin) msg("🔒 " + t("no_pin"), true);
    $app.querySelectorAll("[data-k]").forEach((b) => b.onclick = async () => {
      const k = b.dataset.k;
      if (k === "x") return go("staff");
      if (k === "back") { buf = buf.slice(0, -1); return dots(); }
      if (buf.length >= 4) return;
      buf += k; dots();
      if (buf.length < 4) return;
      const pin = buf; buf = "";
      msg("…");
      const ok = await login(st, pin, msg);
      if (ok) { flash(true); S.staff = st; poke(); go("home"); } else { dots(); flash(false); }
    });
  }

  async function login(st, pin, msg) {
    if (navigator.onLine) {
      const r = await call("/functions/v1/stok-login", { method: "POST", body: {
        staff_id: st.id, pin, device_id: S.cfg.device_id, outlet_code: S.cfg.outlet_code,
        device_label: (S.cfg.outlet_code || "") + " " + S.cfg.device_id.slice(0, 4) } });
      if (r.status === 200 && r.data && r.data.ok) {
        store.set("sess." + st.id, { access_token: r.data.access_token, refresh_token: r.data.refresh_token, expires_at: r.data.expires_at });
        try {
          const salt = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
          store.set("pin." + st.id, { salt, v: await C.pinVerifier(pin, salt) });
        } catch (e) { /* no WebCrypto: offline login just won't be available */ }
        loadCatalog(r.data.access_token);
        return true;
      }
      if (r.status !== 0) {
        const reason = r.data && r.data.reason;
        msg(reason === "locked" ? "🔒 " + t("locked") : "✕ " + t("wrong_pin") + (r.data && r.data.left != null ? ` (${r.data.left})` : ""), true);
        return false;
      }
    }
    // offline: the verifier saved at this staff member's last online login
    const pv = store.get("pin." + st.id, null);
    if (!pv) { msg("📶❌ " + t("need_net"), true); return false; }
    try {
      if ((await C.pinVerifier(pin, pv.salt)) === pv.v) return true;
    } catch (e) { /* ignore */ }
    msg("✕ " + t("wrong_pin"), true);
    return false;
  }

  async function loadCatalog(token) {
    const [c, i, u] = await Promise.all([
      call("/rest/v1/stok_categories?select=*&order=sort", { token }),
      call("/rest/v1/stok_items?select=code,category,name_bm,name_ta,name_bn,icon,photo_url,unit,decimals,terima_unit,terima_decimals,actions,outlets,sort&active=eq.true&order=sort", { token }),
      call("/rest/v1/stok_uom?select=item_code,pack_unit,units_per_pack", { token }),
    ]);
    if (c.status === 200 && i.status === 200) {
      const its = i.data.filter((x) => !x.outlets || x.outlets.includes(S.cfg.outlet_code));
      S.catalog = { categories: c.data, items: its, uom: u.status === 200 ? u.data : [] };
      store.set("catalog", S.catalog);
      if (S.screen === "home" || S.screen === "boot") render();
    }
  }

  async function screenHome() {
    const kinds = ["terima", "guna", "baki"];
    const draw = () => {
      $app.innerHTML = topbar(`<button class="ghost who" id="out">${avatar(S.staff)}<span>${esc(S.staff.name)}</span> 🚪</button>`) +
        (S.catalog ? "" : `<div class="note">📶❌ ${esc(t("need_net"))}</div>`) +
        `<div class="grid g1">${kinds.map((k) => `<button class="tile kind ${KIND_COLOR[k]}" data-k="${k}">
          <div class="kicon">${KIND_ICON[k]}</div><div>${t3(k).map((x, i) => `<div class="${i ? "n2" : "n1"}">${esc(x)}</div>`).join("")}</div>
          ${S.today[k] ? `<div class="done">✓${S.today[k] > 1 ? S.today[k] : ""}</div>` : ""}</button>`).join("")}</div>` +
        (stuck().length ? `<div class="note err">⚠️ ${stuck().length} — ${esc(t("stuck"))}</div>` : "");
      bind();
      document.getElementById("out").onclick = () => { S.staff = null; S.draft = {}; go("staff"); };
      $app.querySelectorAll("[data-k]").forEach((b) => b.onclick = () => {
        if (!S.catalog) return;
        S.kind = b.dataset.k; S.draft = {}; S.supplier = null; S.cat = null;
        go(S.kind === "terima" ? "suppliers" : "cats");
      });
    };
    draw();
    syncQueue();
    const token = await tokenFor(S.staff.id);
    if (token) {
      if (!S.catalog) await loadCatalog(token);
      const r = await rpc("stok_today", {}, token);
      if (r.status === 200 && r.data && r.data.kinds) { S.today = r.data.kinds; if (S.screen === "home") draw(); }
    }
  }

  async function screenSuppliers() {
    const draw = (loading) => {
      $app.innerHTML = topbar(`<button class="ghost" id="back">⬅️</button>`) +
        `<h1 class="title k-terima">🚚 ${t3("terima").map(esc).join(" · ")}</h1>
        <div class="grid g1">${S.prefill.map((r, idx) => `<button class="tile sup" data-i="${idx}">
          <div class="kicon">📄</div><div><div class="n1">${esc(r.supplier)}</div>
          <div class="n2">${r.lines.length} ✕ · ${new Date(r.at).toLocaleTimeString("ms-MY", { hour: "2-digit", minute: "2-digit" })}</div></div></button>`).join("")}
          ${loading ? `<div class="spin"></div>` : (!S.prefill.length ? `<div class="note">📄✕ ${esc(t("empty_receipts"))}</div>` : "")}
          <button class="tile sup other" id="other"><div class="kicon">➕</div><div>${t3("other").map((x, i) => `<div class="${i ? "n2" : "n1"}">${esc(x)}</div>`).join("")}</div></button>
        </div>`;
      bind();
      document.getElementById("back").onclick = () => go("home");
      document.getElementById("other").onclick = () => { S.supplier = null; go("cats"); };
      $app.querySelectorAll("[data-i]").forEach((b) => b.onclick = () => {
        const r = S.prefill[+b.dataset.i];
        S.supplier = { name: r.supplier, receipt_id: r.receipt_id };
        S.draft = {};
        r.lines.forEach((l) => {
          if (!itemByCode(l.item_code)) return;
          const prev = S.draft[l.item_code];
          const q = l.qty == null ? null : Number(l.qty) + (prev && prev.qty ? prev.qty : 0);
          S.draft[l.item_code] = { qty: q, prefill_qty: q, fromReceipt: true };
        });
        S.cat = "__draft";
        go("items");
      });
    };
    draw(true);
    const token = await tokenFor(S.staff.id);
    if (token) {
      const r = await rpc("stok_terima_prefill", { p_hours: 20 }, token);
      if (r.status === 200 && Array.isArray(r.data)) {
        const used = new Set(queue().filter((b) => b.receipt_id).map((b) => b.receipt_id));
        S.prefill = r.data.filter((x) => !used.has(x.receipt_id));
        store.set("prefill", S.prefill);
      }
    }
    if (S.screen === "suppliers") draw(false);
  }

  function screenCats() {
    const cats = (S.catalog.categories || []);
    const filledIn = (cat) => items().filter((i) => i.category === cat && S.draft[i.code] && S.draft[i.code].qty != null).length;
    $app.innerHTML = topbar(`<button class="ghost" id="back">⬅️</button>`) +
      `<h1 class="title ${KIND_COLOR[S.kind]}">${KIND_ICON[S.kind]} ${t3(S.kind).map(esc).join(" · ")}${S.supplier ? ` — ${esc(S.supplier.name)}` : ""}</h1>
      <div class="grid g2">${cats.map((c) => `<button class="tile cat" data-c="${esc(c.code)}">${icon(c)}${names3(c)}
        ${filledIn(c.code) ? `<div class="done">✓${filledIn(c.code)}</div>` : ""}</button>`).join("")}</div>` + sendBar();
    bind();
    document.getElementById("back").onclick = () => {
      if (S.supplier || S.kind === "terima") { S.cat = "__draft"; return go(S.supplier ? "items" : "suppliers"); }
      if (draftCount() && !confirm("❌ ?")) return;
      S.draft = {}; go("home");
    };
    $app.querySelectorAll("[data-c]").forEach((b) => b.onclick = () => { S.cat = b.dataset.c; go("items"); });
    bindSend();
  }

  function screenItems() {
    const list = S.cat === "__draft"
      ? Object.keys(S.draft).map(itemByCode).filter(Boolean)
      : items().filter((i) => i.category === S.cat && i.actions.includes(S.kind));
    const cat = (S.catalog.categories || []).find((c) => c.code === S.cat);
    $app.innerHTML = topbar(`<button class="ghost" id="back">⬅️</button>`) +
      `<h1 class="title ${KIND_COLOR[S.kind]}">${KIND_ICON[S.kind]} ${cat ? names3(cat) : (S.supplier ? "📄 " + esc(S.supplier.name) : "")}</h1>
      <div class="grid g3">${list.map((it) => {
        const d = S.draft[it.code];
        const { unit, decimals } = C.entryUnit(it, S.kind);
        const has = d && d.qty != null;
        const cls = has ? (d.fromReceipt && d.qty === d.prefill_qty ? "pre" : "set") : "";
        return `<button class="tile item ${cls}" data-i="${esc(it.code)}">${icon(it)}${names3(it)}
          <div class="val">${has ? esc(C.fmt(d.qty, decimals)) + " " + esc(unit) : "?"}</div>
          ${d && d.fromReceipt ? `<div class="tag">📄 ${esc(t("from_receipt"))}</div>` : ""}</button>`;
      }).join("")}
      ${S.cat === "__draft" ? `<button class="tile item add" id="add"><div class="em">➕</div><div class="n1">${esc(t("add"))}</div></button>` : ""}
      </div>` + sendBar();
    bind();
    document.getElementById("back").onclick = () => {
      if (S.cat === "__draft") { if (draftCount() && !confirm("❌ ?")) return; S.draft = {}; S.supplier = null; return go("suppliers"); }
      go("cats");
    };
    const add = document.getElementById("add");
    if (add) add.onclick = () => go("cats");
    $app.querySelectorAll("[data-i]").forEach((b) => b.onclick = () => openKeypad(itemByCode(b.dataset.i)));
    bindSend();
  }

  function sendBar() {
    const n = draftCount();
    return `<footer class="send"><button class="btn red" id="cancel">❌</button>
      <button class="btn green" id="send" ${n ? "" : "disabled"}>✅ ${esc(t("hantar"))} ${n ? `(${n})` : ""}</button></footer>`;
  }
  function bindSend() {
    document.getElementById("cancel").onclick = () => {
      if (draftCount() && !confirm("❌ ?")) return;
      S.draft = {}; S.supplier = null; go("home");
    };
    document.getElementById("send").onclick = submit;
  }

  async function submit() {
    const draft = {};
    Object.keys(S.draft).forEach((k) => { if (S.draft[k] && S.draft[k].qty != null) draft[k] = S.draft[k]; });
    const b = C.buildBatch({ kind: S.kind, draft, staffId: S.staff.id, deviceId: S.cfg.device_id,
      supplier: S.supplier && S.supplier.name, receiptId: S.supplier && S.supplier.receipt_id });
    if (!b) return;
    store.set("queue", queue().concat([b]));
    if (S.supplier) S.prefill = S.prefill.filter((r) => r.receipt_id !== S.supplier.receipt_id);
    store.set("prefill", S.prefill);
    S.today[S.kind] = (S.today[S.kind] || 0) + 1;
    S.draft = {}; S.supplier = null;
    await syncQueue();
    flash(true, queue().some((x) => x.id === b.id));
    go("home");
  }

  // ---------------------------------------------------------------- keypad --
  function openKeypad(it) {
    const { unit, decimals } = C.entryUnit(it, S.kind);
    const d = S.draft[it.code];
    let buf = d && d.qty != null ? C.formatBuffer(d.qty, decimals) : "";
    let fresh = true; // first digit replaces the shown value
    const packs = ((S.catalog && S.catalog.uom) || []).filter((u) => u.item_code === it.code && S.kind !== "terima");
    const el = document.createElement("div");
    el.className = "modal";
    const draw = () => {
      el.innerHTML = `<div class="sheet">
        <div class="khead">${icon(it)}${names3(it)}</div>
        <div class="display"><span id="disp">${esc(buf === "" ? "0" : buf)}</span> <small>${esc(unit)}</small></div>
        ${d && d.prefill_qty != null ? `<div class="hint">📄 ${esc(C.fmt(d.prefill_qty, decimals))} ${esc(unit)}</div>` : ""}
        <div class="steps"><button class="key step" data-s="-1">−1</button><button class="key step" data-s="1">+1</button>
          ${packs.map((p) => `<button class="key step pack" data-s="${p.units_per_pack}">+1 ${esc(p.pack_unit)}<small> (${esc(p.units_per_pack)})</small></button>`).join("")}</div>
        <div class="pad">${["1", "2", "3", "4", "5", "6", "7", "8", "9", decimals ? "." : "", "0", "back"].map((k) =>
          k ? `<button class="key" data-k="${k}">${k === "back" ? "⌫" : k}</button>` : `<span></span>`).join("")}</div>
        <div class="row2"><button class="btn red" id="kx">❌</button><button class="btn grey" id="kc">🗑</button><button class="btn green" id="kok">✅</button></div>
      </div>`;
      el.querySelectorAll("[data-k]").forEach((b) => b.onclick = () => {
        const k = b.dataset.k;
        if (fresh && k !== "back") buf = "";
        fresh = false;
        buf = C.applyKey(buf, k, decimals);
        el.querySelector("#disp").textContent = buf === "" ? "0" : buf;
      });
      el.querySelectorAll("[data-s]").forEach((b) => b.onclick = () => {
        fresh = false;
        buf = C.step(buf, Number(b.dataset.s), decimals);
        el.querySelector("#disp").textContent = buf === "" ? "0" : buf;
      });
      el.querySelector("#kx").onclick = () => el.remove();
      el.querySelector("#kc").onclick = () => {
        if (d && d.fromReceipt) S.draft[it.code] = Object.assign({}, d, { qty: null }); else delete S.draft[it.code];
        el.remove(); render();
      };
      el.querySelector("#kok").onclick = () => {
        const v = C.bufferValue(buf);
        const q = v == null ? 0 : C.round(v, decimals);
        S.draft[it.code] = Object.assign({}, d || {}, { qty: q });
        try { navigator.vibrate && navigator.vibrate(40); } catch (e) { /* ignore */ }
        el.remove(); render();
      };
    };
    draw();
    document.body.appendChild(el);
  }

  // ---------------------------------------------------------------- router --
  function render() {
    if (!S.cfg.outlet_code) return screenOutlet();
    if (S.screen === "boot" || (!S.staff && S.screen !== "pin")) S.screen = S.screen === "pin" ? "pin" : "staff";
    const map = { staff: screenStaff, pin: screenPin, home: screenHome, suppliers: screenSuppliers, cats: screenCats, items: screenItems };
    (map[S.screen] || screenStaff)();
  }

  window.addEventListener("online", () => { renderBadge(); syncQueue(); });
  window.addEventListener("offline", renderBadge);
  setInterval(syncQueue, 30000);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  render();
})();
