// stok-login — kitchen PIN login for Khulafa Stok Dapur.
//
// POST {staff_id, pin, device_id, outlet_code?, device_label?}
//  1. stok_check_pin (service_role only; bcrypt; 5 wrong = 15 min lock)
//  2. ensure the staff member has a Supabase Auth user whose app_metadata is
//     {stok_role:'kitchen', stok_outlet, stok_staff} — RLS keys on these
//  3. mint a normal session for that user (admin magic-link token, verified
//     server-side; no e-mail is sent) and return it
//  4. register / touch the device in stok_devices (activity log)
// The service-role key never leaves this function.
import { createClient } from "jsr:@supabase/supabase-js@2";

const URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ANON = Deno.env.get("SUPABASE_ANON_KEY")!;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

function randomPassword() {
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  return btoa(String.fromCharCode(...b));
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ ok: false, reason: "method" }, 405);

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return json({ ok: false, reason: "bad_request" }, 400);
  }
  const staffId = String(body.staff_id ?? "");
  const pin = String(body.pin ?? "");
  const deviceId = String(body.device_id ?? "");
  const outlet = body.outlet_code ? String(body.outlet_code) : null;
  if (!UUID.test(staffId) || !/^\d{4}$/.test(pin) || !UUID.test(deviceId)) {
    return json({ ok: false, reason: "bad_request" }, 400);
  }

  const admin = createClient(URL, SERVICE, { auth: { persistSession: false, autoRefreshToken: false } });

  const { data: chk, error: chkErr } = await admin.rpc("stok_check_pin", { p_staff: staffId, p_pin: pin });
  if (chkErr) return json({ ok: false, reason: "server" }, 500);
  if (!chk?.ok) return json({ ok: false, reason: chk?.reason ?? "wrong", left: chk?.left, until: chk?.until }, 401);

  const staff = chk.staff as { id: string; outlet_code: string; name: string; photo_url: string | null; auth_user_id: string | null };
  if (outlet && outlet !== staff.outlet_code) return json({ ok: false, reason: "wrong_outlet" }, 403);

  const email = `k-${staff.id}@kitchen.stok.khulafa.app`;
  const appMeta = { stok_role: "kitchen", stok_outlet: staff.outlet_code, stok_staff: staff.id };

  let userId = staff.auth_user_id;
  if (userId) {
    const { error } = await admin.auth.admin.updateUserById(userId, { app_metadata: appMeta, ban_duration: "none" });
    if (error) userId = null; // user deleted in the dashboard — recreate below
  }
  if (!userId) {
    const { data: created, error } = await admin.auth.admin.createUser({
      email, email_confirm: true, password: randomPassword(), app_metadata: appMeta,
    });
    if (error || !created?.user) {
      // already exists (e.g. auth_user_id was cleared): find it by e-mail
      const { data: list } = await admin.auth.admin.listUsers({ page: 1, perPage: 1000 });
      const found = list?.users?.find((u) => u.email === email);
      if (!found) return json({ ok: false, reason: "server" }, 500);
      userId = found.id;
      await admin.auth.admin.updateUserById(userId, { app_metadata: appMeta });
    } else {
      userId = created.user.id;
    }
    await admin.from("stok_staff").update({ auth_user_id: userId }).eq("id", staff.id);
  }

  const { data: link, error: linkErr } = await admin.auth.admin.generateLink({ type: "magiclink", email });
  const tokenHash = link?.properties?.hashed_token;
  if (linkErr || !tokenHash) return json({ ok: false, reason: "server" }, 500);
  const anon = createClient(URL, ANON, { auth: { persistSession: false, autoRefreshToken: false } });
  let { data: ver, error: verErr } = await anon.auth.verifyOtp({ type: "email", token_hash: tokenHash });
  if (verErr || !ver?.session) {
    ({ data: ver, error: verErr } = await anon.auth.verifyOtp({ type: "magiclink", token_hash: tokenHash }));
  }
  const s = ver?.session;
  if (verErr || !s) return json({ ok: false, reason: "server" }, 500);

  await admin.from("stok_devices").upsert({
    device_id: deviceId,
    outlet_code: staff.outlet_code,
    label: body.device_label ? String(body.device_label).slice(0, 60) : null,
    user_agent: (req.headers.get("user-agent") ?? "").slice(0, 200),
    last_staff_id: staff.id,
    last_seen: new Date().toISOString(),
  }, { onConflict: "device_id", ignoreDuplicates: false });

  return json({
    ok: true,
    access_token: s.access_token,
    refresh_token: s.refresh_token,
    expires_at: s.expires_at,
    staff: { id: staff.id, name: staff.name, outlet_code: staff.outlet_code, photo_url: staff.photo_url },
  });
});
