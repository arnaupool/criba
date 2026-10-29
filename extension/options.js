const B = globalThis.browser ?? globalThis.chrome;
const $ = id => document.getElementById(id);
const say = (text, ok = true) => { $("msg").textContent = text; $("msg").className = ok ? "ok" : "bad"; };

async function load() {
  const { url = "", token = "", sites = { "instagram.com": true, "x.com": false, "reddit.com": false }, auto = true } =
    await B.storage.local.get(["url", "token", "sites", "auto"]);
  $("url").value = url; $("token").value = token;
  $("site-instagram").checked = !!sites["instagram.com"]; $("site-x").checked = !!sites["x.com"];
  $("site-reddit").checked = !!sites["reddit.com"];
  $("auto").checked = auto;
}

function origin() {
  try { return new URL($("url").value.trim()).origin; } catch (e) { return null; }
}

$("save").onclick = async () => {
  const o = origin();
  if (!o) return say("La dirección no es válida (ej. https://criba.tudominio.com)", false);
  // permiso para hablar con tu Criba (lo pide el navegador una vez)
  const granted = await B.permissions.request({ origins: [o + "/*"] }).catch(() => false);
  if (!granted) return say("Sin permiso para conectar con esa dirección no se pueden enviar las cookies", false);
  await B.storage.local.set({
    url: o, token: $("token").value.trim(), auto: $("auto").checked,
    sites: { "instagram.com": $("site-instagram").checked, "x.com": $("site-x").checked,
             "reddit.com": $("site-reddit").checked },
  });
  say("✓ Guardado");
};

$("test").onclick = async () => {
  const o = origin();
  if (!o) return say("Pon primero la dirección", false);
  try {
    const r = await fetch(`${o}/api/ext/ping`, { headers: { "X-Criba-Token": $("token").value.trim() } });
    const d = await r.json().catch(() => ({}));
    say(r.ok ? `✓ Conectado con Criba ${d.version || ""}` : (d.detail || `Error ${r.status}`), r.ok);
  } catch (e) { say(`No se pudo conectar: ${e.message}. ¿Has pulsado Guardar y aceptado el permiso?`, false); }
};

$("now").onclick = async () => {
  say("Enviando…");
  await B.runtime.sendMessage({ cmd: "send" });
  const { last = {} } = await B.storage.local.get("last");
  const lines = Object.entries(last).map(([s, v]) => `${s}: ${v.msg}`);
  say(lines.join(" · ") || "No hay nada marcado para enviar", Object.values(last).every(v => v.ok));
};

load();
