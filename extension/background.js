// Criba · cookies — manda a Criba las cookies de Instagram / X cuando cambian.
// Funciona igual en Chrome/Edge/Brave (service worker) y en Firefox (página de fondo).
const B = globalThis.browser ?? globalThis.chrome;

// cookie que indica sesión iniciada: sin ella no se envía nada
const SITES = { "instagram.com": "sessionid", "x.com": "auth_token", "reddit.com": "reddit_session" };
const PERIODIC_MIN = 720; // por si algún cambio se escapara: revisión cada 12 h

async function config() {
  const { url = "", token = "", sites = { "instagram.com": true, "x.com": false, "reddit.com": false }, auto = true } =
    await B.storage.local.get(["url", "token", "sites", "auto"]);
  return { url: url.replace(/\/+$/, ""), token, sites, auto };
}

async function getCookies(site) {
  try { return await B.cookies.getAll({ domain: site, firstPartyDomain: null }); } // Firefox (aislamiento)
  catch (e) { return await B.cookies.getAll({ domain: site }); }                  // Chrome
}

async function hash(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function setState(site, state) {
  const { last = {} } = await B.storage.local.get("last");
  last[site] = { ...(last[site] || {}), ...state, at: Date.now() };
  await B.storage.local.set({ last });
  const bad = Object.values(last).some(s => s.ok === false);
  B.action.setBadgeText({ text: bad ? "!" : "" });
  if (bad) B.action.setBadgeBackgroundColor({ color: "#d93025" });
}

async function send(site, force = false) {
  const cfg = await config();
  if (!cfg.url || !cfg.token) return setState(site, { ok: false, msg: "Falta configurar la dirección y el token" });
  const cookies = await getCookies(site);
  if (!cookies.some(c => c.name === SITES[site] && c.value)) {
    return setState(site, { ok: false, msg: `No hay sesión iniciada en ${site} en este navegador` });
  }
  const h = await hash(cookies.map(c => `${c.name}=${c.value}`).sort().join("\n"));
  const { last = {} } = await B.storage.local.get("last");
  if (!force && last[site]?.hash === h && last[site]?.ok) return; // nada nuevo
  try {
    const r = await fetch(`${cfg.url}/api/ext/cookies`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Criba-Token": cfg.token },
      body: JSON.stringify({ site, user_agent: navigator.userAgent, cookies: cookies.map(c => ({
        domain: c.domain, hostOnly: c.hostOnly, path: c.path, secure: c.secure, httpOnly: c.httpOnly,
        expirationDate: c.expirationDate, name: c.name, value: c.value })) }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return setState(site, { ok: false, msg: data.detail || `Error ${r.status}` });
    await setState(site, { ok: true, msg: `Enviadas ${data.count} cookies`, hash: h });
  } catch (e) {
    await setState(site, { ok: false, msg: `No se pudo conectar con Criba: ${e.message}` });
  }
}

async function sendAll(force = false) {
  const cfg = await config();
  for (const site of Object.keys(SITES)) if (cfg.sites[site]) await send(site, force);
}

// Al cambiar una cookie de sesión (p. ej. al volver a iniciar sesión) se envía medio minuto después,
// cuando Instagram ha terminado de poner todas las demás.
B.cookies.onChanged.addListener(async ({ cookie, removed }) => {
  const site = Object.keys(SITES).find(s => cookie.domain.replace(/^\./, "").endsWith(s));
  if (!site) return;
  const cfg = await config();
  if (!cfg.auto || !cfg.sites[site]) return;
  if (removed && cookie.name === SITES[site]) {
    return setState(site, { ok: false, msg: `Se ha cerrado la sesión de ${site}: vuelve a iniciarla` });
  }
  if (!removed) B.alarms.create(`send:${site}`, { delayInMinutes: 0.5 });
});

B.alarms.onAlarm.addListener(async ({ name }) => {
  if (name.startsWith("send:")) await send(name.slice(5));
  if (name === "periodic" && (await config()).auto) await sendAll();
});

B.runtime.onInstalled.addListener(() => {
  B.alarms.create("periodic", { periodInMinutes: PERIODIC_MIN });
  B.runtime.openOptionsPage();
});
B.runtime.onStartup?.addListener(() => B.alarms.create("periodic", { periodInMinutes: PERIODIC_MIN }));

B.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.cmd === "send") { sendAll(true).then(() => reply({ ok: true })); return true; }
  return false;
});
