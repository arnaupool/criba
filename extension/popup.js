const B = globalThis.browser ?? globalThis.chrome;
const $ = id => document.getElementById(id);

function ago(ms) {
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return "ahora";
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} d`;
}

async function render() {
  const { url = "", last = {} } = await B.storage.local.get(["url", "last"]);
  const el = $("status");
  el.replaceChildren();
  if (!url) {
    el.textContent = "Sin configurar: pulsa Ajustes.";
    return;
  }
  const entries = Object.entries(last);
  if (!entries.length) el.textContent = "Todavía no se ha enviado nada.";
  for (const [site, v] of entries) {
    const p = document.createElement("p");
    p.className = v.ok ? "ok" : "bad";
    p.textContent = `${v.ok ? "✓" : "⚠️"} ${site}: ${v.msg} (${ago(v.at)})`;
    el.append(p);
  }
}

$("now").onclick = async () => {
  $("now").disabled = true;
  $("now").textContent = "Enviando…";
  await B.runtime.sendMessage({ cmd: "send" });
  $("now").disabled = false;
  $("now").textContent = "Enviar ahora";
  render();
};
$("opts").onclick = () => B.runtime.openOptionsPage();
render();
