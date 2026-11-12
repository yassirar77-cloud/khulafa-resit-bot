/* Khulafa Stok Dapur — office dashboard (live). */
(function () {
  "use strict";
  const C = window.StokCore;
  const CFG = window.STOK_CONFIG;
  const sb = window.supabase.createClient(CFG.url, CFG.anonKey, { auth: { persistSession: true, detectSessionInUrl: true } });
  const $main = document.getElementById("main");
  const S = { tab: "grid", date: C.businessDate(new Date()), outlet: "", outlets: [], items: [], cats: [], staff: [], channel: null };

  const STATUS = {
    high_wastage: ["🔴 HIGH WASTAGE", "s-red"],
    over_used: ["🟠 OVER-USED", "s-orange"],
    ok: ["🟢 OK", "s-green"],
    belum_kira: ["⏳ belum kira", "s-grey"],
    tiada_baki_semalam: ["— tiada baki semalam", "s-grey"],
    perlu_saiz_pek: ["📦 perlu saiz pek", "s-grey"],
  };
  const KIND = { terima: "🚚 Terima", guna: "🍳 Guna", baki: "🌙 Baki", buang: "🗑 Buang", minta: "🛒 Minta" };

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function toast(m) { const t = document.getElementById("toast"); t.textContent = m; t.className = "show"; clearTimeout(t._h); t._h = setTimeout(() => t.className = "", 2500); }
  function fmt(v, d) { return v == null ? "—" : C.fmt(v, d == null ? 2 : d); }
  function itemOf(code) { return S.items.find((i) => i.code === code) || { name_bm: code, unit: "", decimals: 2 }; }
  function outletLabel(code) { const o = S.outlets.find((x) => x.outlet_code === code); return o ? o.label : code; }
  function myt(ts) { return new Date(ts).toLocaleString("ms-MY", { timeZone: "Asia/Kuala_Lumpur", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }); }

  // ------------------------------------------------------------------ auth --
  async function boot() {
    const { data: { session } } = await sb.auth.getSession();
    if (!session) return loginScreen();
    const { data: isOffice, error } = await sb.rpc("stok_is_office");
    if (error || !isOffice) {
      $main.innerHTML = `<div class="card center"><h2>⛔ Akses ditolak</h2><p>${esc(session.user.email)} bukan pengguna pejabat.</p>
        <p class="dim">Minta admin tambah e-mel ini ke <code>stok_office_users</code>.</p><button id="lo" class="btn">Log keluar</button></div>`;
      document.getElementById("lo").onclick = async () => { await sb.auth.signOut(); location.reload(); };
      return;
    }
    document.getElementById("me").innerHTML = `<span class="dim">${esc(session.user.email)}</span> <button id="lo" class="btn sm">Keluar</button>`;
    document.getElementById("lo").onclick = async () => { await sb.auth.signOut(); location.reload(); };
    document.getElementById("tabs").hidden = false;
    document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => {
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
      S.tab = b.dataset.tab; render();
    });
    await loadRefs();
    subscribe();
    render();
  }

  function loginScreen() {
    $main.innerHTML = `<form class="card center" id="lf"><h2>🔐 Log masuk pejabat</h2>
      <p class="dim">Masukkan e-mel — kami hantar pautan log masuk.</p>
      <input type="email" id="em" required placeholder="nama@contoh.com" autocomplete="email">
      <button class="btn green">Hantar pautan ✉️</button><p id="lm" class="dim"></p></form>`;
    document.getElementById("lf").onsubmit = async (e) => {
      e.preventDefault();
      const email = document.getElementById("em").value.trim().toLowerCase();
      const { error } = await sb.auth.signInWithOtp({ email, options: { emailRedirectTo: location.origin + location.pathname } });
      document.getElementById("lm").textContent = error ? "❌ " + error.message : "✅ Semak e-mel anda dan tekan pautan.";
    };
  }

  async function loadRefs() {
    const [o, i, c, s] = await Promise.all([
      sb.from("stok_outlets").select("*").order("sort"),
      sb.from("stok_items").select("*").order("sort"),
      sb.from("stok_categories").select("*").order("sort"),
      sb.from("stok_staff").select("id,outlet_code,display_name,photo_url,pin_set_at,active,last_login_at,created_at").order("display_name"),
    ]);
    S.outlets = o.data || []; S.items = i.data || []; S.cats = c.data || []; S.staff = s.data || [];
    if (!S.outlet) { const live = S.outlets.find((x) => x.app_live); S.outlet = live ? live.outlet_code : ""; }
  }

  function subscribe() {
    S.channel = sb.channel("stok-live")
      .on("postgres_changes", { event: "INSERT", schema: "public", table: "stok_batches" }, (p) => {
        const b = p.new;
        const st = S.staff.find((x) => x.id === b.staff_id);
        toast(`${KIND[b.kind] || b.kind} · ${outletLabel(b.outlet_code)} · ${st ? st.display_name : ""} (${b.item_count})`);
        clearTimeout(S._rt); S._rt = setTimeout(() => { if (["grid", "rekod", "log"].includes(S.tab)) render(); }, 1500);
      })
      .subscribe();
  }

  // --------------------------------------------------------------- render ---
  function render() {
    ({ grid: tabGrid, rekod: tabRekod, log: tabLog, staff: tabStaff, items: tabItems, outlets: tabOutlets }[S.tab])();
  }

  function filters(withAll) {
    return `<div class="filters"><label>📅 <input type="date" id="fd" value="${S.date}"></label>
      <label>🏪 <select id="fo">${withAll ? `<option value="">Semua outlet</option>` : ""}${S.outlets.filter((o) => o.active).map((o) =>
        `<option value="${esc(o.outlet_code)}" ${o.outlet_code === S.outlet ? "selected" : ""}>${esc(o.label)}${o.app_live ? " ●" : ""}</option>`).join("")}</select></label>
      <span class="dim">Hari niaga: sebelum 12 tengah hari = hari semalam</span></div>`;
  }
  function bindFilters() {
    document.getElementById("fd").onchange = (e) => { S.date = e.target.value; render(); };
    document.getElementById("fo").onchange = (e) => { S.outlet = e.target.value; render(); };
  }

  async function tabGrid() {
    $main.innerHTML = filters(true) + `<div id="g"><div class="spin"></div></div>`;
    bindFilters();
    const { data, error } = await sb.rpc("stok_office_grid", { p_date: S.date, p_outlet: S.outlet || null });
    const g = document.getElementById("g");
    if (error) { g.innerHTML = `<div class="card">❌ ${esc(error.message)}</div>`; return; }
    if (!data.length) { g.innerHTML = `<div class="card center dim">Tiada rekod untuk ${esc(S.date)}.</div>`; return; }
    const byOutlet = {};
    data.forEach((r) => (byOutlet[r.outlet_code] = byOutlet[r.outlet_code] || []).push(r));
    g.innerHTML = Object.keys(byOutlet).map((oc) => {
      const rows = byOutlet[oc].sort((a, b) => (itemOf(a.item_code).sort || 0) - (itemOf(b.item_code).sort || 0));
      const red = rows.filter((r) => r.status === "high_wastage").length, org = rows.filter((r) => r.status === "over_used").length;
      return `<section class="card"><h3>🏪 ${esc(outletLabel(oc))} <span class="dim">${rows.length} barang</span>
        ${red ? `<span class="chip s-red">🔴 ${red}</span>` : ""}${org ? `<span class="chip s-orange">🟠 ${org}</span>` : ""}
        <button class="btn sm right" data-csv="${esc(oc)}">⬇ CSV</button></h3>
        <div class="scroll"><table><thead><tr><th>Barang</th><th>Terima</th><th>Guna</th><th>Baki kira</th><th>Baki semalam</th><th>Jangka</th><th>Varians</th><th>Status</th></tr></thead><tbody>
        ${rows.map((r) => { const it = itemOf(r.item_code); const d = it.decimals;
          const st = STATUS[r.status] || [r.status, ""];
          const terima = r.terima_raw == null ? "—" : (it.terima_unit && it.terima_unit !== it.unit
            ? `${fmt(r.terima_raw)} ${esc(it.terima_unit)}${r.terima != null ? ` <span class="dim">= ${fmt(r.terima, d)}</span>` : ""}` : fmt(r.terima_raw, d));
          return `<tr><td>${esc(it.icon || "")} ${esc(it.name_bm)} <span class="dim">${esc(it.unit)}</span></td>
            <td>${terima}</td><td>${fmt(r.guna, d)}</td><td><b>${fmt(r.baki, d)}</b></td>
            <td>${fmt(r.prev_baki, d)}${r.prev_baki_date && r.prev_baki_date !== prevDay(S.date) ? ` <span class="dim">(${esc(r.prev_baki_date.slice(5))})</span>` : ""}</td>
            <td>${fmt(r.expected, d)}</td><td>${r.variance_pct == null ? "—" : (r.variance_pct * 100).toFixed(1) + "%"}</td>
            <td><span class="chip ${st[1]}">${st[0]}</span></td></tr>`; }).join("")}
        </tbody></table></div></section>`;
    }).join("") + `<p class="dim legend">Jangka = baki semalam + terima − guna − buang. Varians = (guna sebenar − guna direkod) ÷ guna direkod, guna sebenar = baki semalam + terima − buang − baki kira.
      🔴 &gt; +15% HIGH WASTAGE · 🟢 −5% hingga +15% · 🟠 &lt; −5% OVER-USED.</p>`;
    g.querySelectorAll("[data-csv]").forEach((b) => b.onclick = () => csv(byOutlet[b.dataset.csv], b.dataset.csv));
  }
  function prevDay(d) { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() - 1); return x.toISOString().slice(0, 10); }
  function csv(rows, oc) {
    const head = ["outlet", "tarikh", "barang", "unit", "terima_bil", "terima", "guna", "baki", "baki_semalam", "jangka", "varians_pct", "status"];
    const lines = [head.join(",")].concat(rows.map((r) => { const it = itemOf(r.item_code);
      return [oc, S.date, `"${it.name_bm}"`, it.unit, r.terima_raw ?? "", r.terima ?? "", r.guna, r.baki ?? "", r.prev_baki ?? "", r.expected ?? "",
        r.variance_pct == null ? "" : (r.variance_pct * 100).toFixed(1), r.status].join(","); }));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    a.download = `stok_${oc}_${S.date}.csv`; a.click();
  }

  // tak rekod: did each live outlet record Terima / Guna / Baki for the day?
  async function tabRekod() {
    $main.innerHTML = filters(false) + `<div id="r"><div class="spin"></div></div>`;
    bindFilters();
    const { data } = await sb.from("stok_batches").select("outlet_code,kind,created_at").eq("business_date", S.date);
    const live = S.outlets.filter((o) => o.active && o.app_live);
    // the day closes at 02:00 MYT the next calendar day
    const past = Date.now() > Date.parse(nextDay(S.date) + "T02:00:00+08:00");
    document.getElementById("r").innerHTML = `<section class="card"><h3>🚦 Rekod ${esc(S.date)} <span class="dim">tutup 2:00 pagi ${esc(nextDay(S.date))}</span></h3>
      <table><thead><tr><th>Outlet</th><th>🚚 Terima</th><th>🍳 Guna</th><th>🌙 Baki</th></tr></thead><tbody>
      ${live.map((o) => `<tr><td>${esc(o.label)}</td>${["terima", "guna", "baki"].map((k) => {
        const n = (data || []).filter((b) => b.outlet_code === o.outlet_code && b.kind === k).length;
        return `<td>${n ? `<span class="chip s-green">✅ ${n}</span>` : past ? `<span class="chip s-red">TAK REKOD</span>` : `<span class="chip s-grey">belum</span>`}</td>`;
      }).join("")}</tr>`).join("") || `<tr><td colspan="4" class="dim">Tiada outlet aktif (tab Outlet).</td></tr>`}</tbody></table></section>`;
  }
  function nextDay(d) { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() + 1); return x.toISOString().slice(0, 10); }

  async function tabLog() {
    $main.innerHTML = `<div id="l"><div class="spin"></div></div>`;
    const { data, error } = await sb.from("stok_batches")
      .select("id,outlet_code,business_date,kind,supplier,item_count,captured_at,created_at,staff_id,device_id,stok_devices(label,user_agent),stok_entries(item_code,qty,unit,prefill_qty)")
      .order("created_at", { ascending: false }).limit(100);
    if (error) { document.getElementById("l").innerHTML = `<div class="card">❌ ${esc(error.message)}</div>`; return; }
    document.getElementById("l").innerHTML = `<section class="card"><h3>🕑 Aktiviti terkini <span class="dim">siapa · apa · bila · peranti</span></h3>
      <table><thead><tr><th>Masa</th><th>Outlet</th><th>Staf</th><th>Jenis</th><th>Barang</th><th>Peranti</th></tr></thead><tbody>
      ${(data || []).map((b) => { const st = S.staff.find((x) => x.id === b.staff_id);
        const late = new Date(b.created_at) - new Date(b.captured_at) > 120000;
        return `<tr><td>${esc(myt(b.captured_at))}${late ? `<br><span class="dim">📶 sampai ${esc(myt(b.created_at))}</span>` : ""}</td>
          <td>${esc(outletLabel(b.outlet_code))}</td><td>${esc(st ? st.display_name : "?")}</td>
          <td>${esc(KIND[b.kind] || b.kind)}${b.supplier ? `<br><span class="dim">${esc(b.supplier)}</span>` : ""}</td>
          <td><details><summary>${b.item_count} barang</summary>${(b.stok_entries || []).map((e) => { const it = itemOf(e.item_code);
            const changed = e.prefill_qty != null && Number(e.prefill_qty) !== Number(e.qty);
            return `<div>${esc(it.name_bm)}: <b>${fmt(e.qty)}</b> ${esc(e.unit)}${changed ? ` <span class="s-orange chip">resit ${fmt(e.prefill_qty)}</span>` : ""}</div>`; }).join("")}</details></td>
          <td class="dim">${esc(b.stok_devices ? b.stok_devices.label || "" : "")}<br><small>${esc((b.stok_devices && b.stok_devices.user_agent || "").slice(0, 40))}</small></td></tr>`; }).join("")}
      </tbody></table></section>`;
  }

  // ----------------------------------------------------------------- admin --
  async function upload(file, prefix) {
    const ext = (file.name.split(".").pop() || "jpg").toLowerCase();
    const path = `${prefix}/${C.uuid()}.${ext}`;
    const { error } = await sb.storage.from("stok-photos").upload(path, file, { upsert: false, contentType: file.type });
    if (error) { toast("❌ " + error.message); return null; }
    return sb.storage.from("stok-photos").getPublicUrl(path).data.publicUrl;
  }

  async function tabStaff() {
    await loadRefs();
    const rows = S.staff.filter((s) => !S.outlet || s.outlet_code === S.outlet);
    $main.innerHTML = `<div class="filters"><label>🏪 <select id="fo">${S.outlets.filter((o) => o.active).map((o) =>
      `<option value="${esc(o.outlet_code)}" ${o.outlet_code === S.outlet ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label></div>
      <section class="card"><h3>➕ Tambah staf</h3><form id="af" class="row">
        <input id="an" required placeholder="Nama (cth. Raju)" maxlength="40">
        <input id="ap" type="file" accept="image/*" capture="user">
        <input id="apin" required inputmode="numeric" pattern="\\d{4}" maxlength="4" placeholder="PIN 4 digit">
        <button class="btn green">Simpan</button></form>
        <p class="dim">PIN ringkas (1111, 1234…) ditolak. Staf log masuk: pilih gambar sendiri + PIN.</p></section>
      <section class="card"><h3>👥 Staf ${esc(outletLabel(S.outlet))}</h3><table><thead><tr><th></th><th>Nama</th><th>PIN</th><th>Log masuk akhir</th><th></th></tr></thead><tbody>
      ${rows.map((s) => `<tr class="${s.active ? "" : "off"}"><td>${s.photo_url ? `<img class="thumb" src="${esc(s.photo_url)}">` : "👤"}</td>
        <td>${esc(s.display_name)}</td><td>${s.pin_set_at ? "✅" : "❌"}</td><td class="dim">${s.last_login_at ? esc(myt(s.last_login_at)) : "—"}</td>
        <td><button class="btn sm" data-pin="${s.id}">🔑 PIN</button> <button class="btn sm" data-photo="${s.id}">📷</button>
        <button class="btn sm" data-act="${s.id}">${s.active ? "Nyahaktif" : "Aktifkan"}</button></td></tr>`).join("") || `<tr><td colspan="5" class="dim">Belum ada staf.</td></tr>`}
      </tbody></table></section>`;
    document.getElementById("fo").onchange = (e) => { S.outlet = e.target.value; tabStaff(); };
    document.getElementById("af").onsubmit = async (e) => {
      e.preventDefault();
      const pin = document.getElementById("apin").value;
      const f = document.getElementById("ap").files[0];
      const photo = f ? await upload(f, "staff") : null;
      const { data, error } = await sb.from("stok_staff").insert({ outlet_code: S.outlet, display_name: document.getElementById("an").value.trim(), photo_url: photo }).select("id").single();
      if (error) return toast("❌ " + error.message);
      const r = await sb.rpc("stok_set_pin", { p_staff: data.id, p_pin: pin });
      toast(r.data && r.data.ok ? "✅ Staf ditambah" : "⚠️ Staf ditambah, PIN ditolak (" + (r.data && r.data.reason) + ")");
      tabStaff();
    };
    $main.querySelectorAll("[data-pin]").forEach((b) => b.onclick = async () => {
      const pin = prompt("PIN baru (4 digit):");
      if (!pin) return;
      const r = await sb.rpc("stok_set_pin", { p_staff: b.dataset.pin, p_pin: pin });
      toast(r.data && r.data.ok ? "✅ PIN ditukar" : "❌ " + ((r.data && r.data.reason) || (r.error && r.error.message)));
      tabStaff();
    });
    $main.querySelectorAll("[data-photo]").forEach((b) => b.onclick = () => {
      const inp = document.createElement("input"); inp.type = "file"; inp.accept = "image/*";
      inp.onchange = async () => { const url = await upload(inp.files[0], "staff"); if (url) { await sb.from("stok_staff").update({ photo_url: url }).eq("id", b.dataset.photo); tabStaff(); } };
      inp.click();
    });
    $main.querySelectorAll("[data-act]").forEach((b) => b.onclick = async () => {
      const s = S.staff.find((x) => x.id === b.dataset.act);
      await sb.from("stok_staff").update({ active: !s.active }).eq("id", s.id); tabStaff();
    });
  }

  async function tabItems() {
    await loadRefs();
    const units = ["kg", "pcs", "beg", "karung", "pek", "tin", "botol", "kotak", "ikat", "biji"];
    $main.innerHTML = S.cats.map((c) => `<section class="card"><h3>${esc(c.icon)} ${esc(c.name_bm)}</h3><div class="scroll"><table>
      <thead><tr><th></th><th>Nama BM</th><th>Tamil</th><th>Bengali</th><th>Unit kira</th><th>Unit bil</th><th>Bil → unit kira</th><th>Kata kunci resit</th><th>Aktif</th><th></th></tr></thead><tbody>
      ${S.items.filter((i) => i.category === c.code).map((i) => `<tr data-code="${esc(i.code)}" class="${i.active ? "" : "off"}">
        <td>${i.photo_url ? `<img class="thumb" src="${esc(i.photo_url)}">` : esc(i.icon || "")}</td>
        <td><input data-f="name_bm" value="${esc(i.name_bm)}"></td><td><input data-f="name_ta" value="${esc(i.name_ta || "")}"></td><td><input data-f="name_bn" value="${esc(i.name_bn || "")}"></td>
        <td><select data-f="unit">${units.map((u) => `<option ${u === i.unit ? "selected" : ""}>${u}</option>`).join("")}</select></td>
        <td><select data-f="terima_unit"><option value="">=</option>${units.map((u) => `<option ${u === i.terima_unit ? "selected" : ""}>${u}</option>`).join("")}</select></td>
        <td><input data-f="terima_to_unit" type="number" step="any" min="0" value="${i.terima_to_unit ?? ""}" placeholder="${i.terima_unit ? "cth. 4" : ""}" style="width:70px"></td>
        <td><input data-f="match_keywords" value="${esc((i.match_keywords || []).join(", "))}"></td>
        <td><input data-f="active" type="checkbox" ${i.active ? "checked" : ""}></td>
        <td><button class="btn sm" data-save="${esc(i.code)}">💾</button> <button class="btn sm" data-iphoto="${esc(i.code)}">📷</button></td></tr>`).join("")}
      </tbody></table></div>
      <form class="row add" data-cat="${esc(c.code)}"><input name="name_bm" required placeholder="Barang baru (BM)"><select name="unit">${units.map((u) => `<option>${u}</option>`).join("")}</select>
        <input name="kw" placeholder="Kata kunci resit (koma)"><button class="btn green sm">➕ Tambah</button></form></section>`).join("");
    $main.querySelectorAll("[data-save]").forEach((b) => b.onclick = async () => {
      const tr = b.closest("tr"); const v = (f) => tr.querySelector(`[data-f="${f}"]`);
      const patch = {
        name_bm: v("name_bm").value.trim(), name_ta: v("name_ta").value.trim() || null, name_bn: v("name_bn").value.trim() || null,
        unit: v("unit").value, terima_unit: v("terima_unit").value || null,
        terima_to_unit: v("terima_to_unit").value ? Number(v("terima_to_unit").value) : null,
        match_keywords: v("match_keywords").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
        active: v("active").checked, updated_at: new Date().toISOString(),
      };
      patch.decimals = ["kg"].includes(patch.unit) ? 2 : 0;
      const { error } = await sb.from("stok_items").update(patch).eq("code", tr.dataset.code);
      toast(error ? "❌ " + error.message : "✅ Disimpan");
    });
    $main.querySelectorAll("[data-iphoto]").forEach((b) => b.onclick = () => {
      const inp = document.createElement("input"); inp.type = "file"; inp.accept = "image/*";
      inp.onchange = async () => { const url = await upload(inp.files[0], "items"); if (url) { await sb.from("stok_items").update({ photo_url: url }).eq("code", b.dataset.iphoto); tabItems(); } };
      inp.click();
    });
    $main.querySelectorAll("form.add").forEach((f) => f.onsubmit = async (e) => {
      e.preventDefault();
      const name = f.name_bm.value.trim();
      const code = (f.dataset.cat + "_" + name.toLowerCase().replace(/[^a-z0-9]+/g, "_")).replace(/_+$/, "").slice(0, 40);
      const { error } = await sb.from("stok_items").insert({ code, category: f.dataset.cat, name_bm: name, unit: f.unit.value,
        decimals: f.unit.value === "kg" ? 2 : 0, match_keywords: f.kw.value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean), sort: 900 });
      toast(error ? "❌ " + error.message : "✅ Ditambah"); if (!error) tabItems();
    });
  }

  async function tabOutlets() {
    await loadRefs();
    $main.innerHTML = `<section class="card"><h3>🏪 Outlet</h3><p class="dim">● App hidup = dapur outlet ini boleh guna app (Fasa 1: SEK-20).</p><table>
      <thead><tr><th>Outlet</th><th>Kod</th><th>App hidup</th></tr></thead><tbody>
      ${S.outlets.map((o) => `<tr><td>${esc(o.label)}</td><td class="dim">${esc(o.outlet_code)}</td>
        <td><input type="checkbox" data-live="${esc(o.outlet_code)}" ${o.app_live ? "checked" : ""}></td></tr>`).join("")}</tbody></table></section>`;
    $main.querySelectorAll("[data-live]").forEach((c) => c.onchange = async () => {
      const { error } = await sb.from("stok_outlets").update({ app_live: c.checked }).eq("outlet_code", c.dataset.live);
      toast(error ? "❌ " + error.message : "✅ Disimpan");
    });
  }

  boot();
})();
