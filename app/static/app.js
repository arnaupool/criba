"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const isMobile = () => matchMedia("(max-width: 760px)").matches;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const norm = s => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search + location.hash); throw new Error("401"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

function ago(iso) {
  if (!iso) return "nunca";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const s = (Date.now() - d) / 1000;
  if (s < -60) { const m = Math.round(-s / 60); return m < 60 ? `en ${m} min` : `en ${Math.round(m / 60)} h`; }
  if (s < 60) return "ahora";
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} d`;
}
const fmtDate = iso => iso ? new Date(iso.length === 10 ? iso + "T12:00" : iso).toLocaleDateString("es-ES", { day: "numeric", month: "short", year: "numeric" }) : "";
const fmtSize = b => !b ? "" : b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
const fmtInterval = m => m % 1440 === 0 ? `${m / 1440} d` : m % 60 === 0 ? `${m / 60} h` : `${m} min`;
const SOURCE = { telegram: "Telegram", link: "Enlace", sub: "Seguimiento", web: "Web" };

// ------------------------------------------------------------ aviso con deshacer
let toastTimer;
function toast(text, undo) {
  const t = $("#toast");
  $("#toast-text").textContent = text;
  $("#toast-undo").classList.toggle("hidden", !undo);
  $("#toast-undo").onclick = async () => { t.classList.add("hidden"); if (undo) await undo(); };
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), undo ? 7000 : 3500);
}
const fail = e => { if (e.message !== "401") toast("❌ " + e.message); };

// ------------------------------------------------------------ estado
const S = {
  view: "revisar", status: "pending", sub: null, source: null, kind: "",
  items: [], total: 0, selected: new Set(), anchor: null, idx: -1,
  subs: [], counts: null, loadedPending: null,
  mode: (() => { try { return localStorage.getItem("criba.mode"); } catch (e) { return null; } })() || (isMobile() ? "focus" : "grid"),
};
const PAGE = 120;

// ------------------------------------------------------------ navegación
function route() {
  const v = (location.hash.slice(1) || "revisar").split("?")[0];
  S.view = ["revisar", "seguimientos", "descargas", "ajustes"].includes(v) ? v : "revisar";
  if (S.view !== "revisar" && !V.el.classList.contains("hidden")) closeViewer();
  $$(".view").forEach(el => el.classList.toggle("hidden", el.id !== "view-" + S.view));
  $$("#tabs a").forEach(a => a.classList.toggle("on", a.dataset.view === S.view));
  if (S.view === "revisar") loadItems(true);
  if (S.view === "seguimientos") loadSubs();
  if (S.view === "descargas") loadJobs();
  if (S.view === "ajustes") loadSettings();
}
addEventListener("hashchange", route);

// ------------------------------------------------------------ contadores
async function refreshCounts() {
  try {
    const c = await api("/api/counts");
    S.counts = c;
    $("#badge-pending").textContent = c.pending || "";
    $("#badge-jobs").textContent = c.jobs || "";
    document.title = c.pending ? `(${c.pending}) Criba` : "Criba";
    if (S.view === "revisar") renderChips();
    if (S.view === "revisar" && S.status === "pending" && S.loadedPending !== null && c.pending > S.loadedPending)
      $("#newpill").classList.remove("hidden");
  } catch (e) { /* sin conexión: se reintenta */ }
}

// ------------------------------------------------------------ REVISAR
function filterQuery() {
  const p = new URLSearchParams({ status: S.status, limit: PAGE });
  if (S.sub) p.set("sub", S.sub);
  if (S.source) p.set("source", S.source);
  if (S.kind) p.set("kind", S.kind);
  if (S.status === "pending") p.set("order", "asc");
  return p;
}

async function loadItems(reset) {
  const p = filterQuery();
  p.set("offset", reset ? 0 : S.items.length);
  $$("#status-seg button").forEach(x => x.classList.toggle("on", x.dataset.status === S.status));
  try {
    const [data] = await Promise.all([api("/api/items?" + p), S.subs.length ? null : loadSubsData()]);
    if (reset) { S.items = []; S.selected.clear(); }
    S.items.push(...data.items);
    S.total = data.total;
    if (S.status === "pending" && reset) { S.loadedPending = S.counts ? S.counts.pending : null; $("#newpill").classList.add("hidden"); }
    renderGrid();
    renderChips();
  } catch (e) { fail(e); }
}

async function loadSubsData() {
  const d = await api("/api/subs");
  S.subs = d.subs;
}

function renderChips() {
  const c = S.counts || { by_sub: [], by_source: [], pending: 0 };
  const pend = S.status === "pending";
  const n = (arr, key, val) => pend ? (arr.find(x => x[key] === val)?.n || 0) : null;
  const chips = [{ label: "Todo", count: pend ? c.pending : null, on: !S.sub && !S.source, set: {} }];
  for (const [src, label] of [["telegram", "Telegram"], ["link", "Enlaces"]]) {
    const cnt = n(c.by_source, "source", src);
    if (!pend || cnt) chips.push({ label, count: cnt, on: S.source === src, set: { source: src } });
  }
  for (const s of S.subs) {
    const cnt = n(c.by_sub, "id", s.id);
    if (!pend || cnt) chips.push({ label: s.name, count: cnt, on: S.sub == s.id, set: { sub: s.id } });
  }
  $("#chips").innerHTML = chips.map((ch, i) =>
    `<button class="chip ${ch.on ? "on" : ""}" data-i="${i}">${esc(ch.label)}${ch.count !== null ? `<b>${ch.count}</b>` : ""}</button>`).join("");
  $$("#chips .chip").forEach(b => b.onclick = () => {
    const ch = chips[b.dataset.i];
    S.sub = ch.set.sub || null; S.source = ch.set.source || null;
    loadItems(true);
  });
}

function tileHTML(it, i) {
  const sel = S.selected.has(it.id) ? "sel" : "";
  const who = it.author ? "@" + it.author.replace(/^@/, "") : (it.sub_name || SOURCE[it.source] || "");
  return `<div class="tile ${sel}" data-i="${i}">
    <img loading="lazy" src="/api/thumb/${it.id}" alt="">
    <span class="chk">✓</span>
    ${it.kind === "video" ? '<span class="vid">▶ vídeo</span>' : ""}
    ${S.status === "kept" ? `<span class="dest">📁 ${esc(it.dest || "Biblioteca")}</span>` : ""}
    <span class="who">${esc(who)}</span></div>`;
}

function renderGrid() {
  const g = $("#grid");
  g.innerHTML = S.items.map(tileHTML).join("");
  g.classList.toggle("selecting", S.selected.size > 0);
  $("#empty-review").classList.toggle("hidden", S.items.length > 0);
  $("#empty-review p:nth-child(2)").textContent =
    S.status === "pending" ? "No hay nada pendiente de revisar." : S.status === "kept" ? "Aún no has guardado nada." : "La papelera está vacía.";
  $("#more").classList.toggle("hidden", S.items.length >= S.total);
  renderSelbar();
}

function renderSelbar() {
  const n = S.selected.size;
  $("#selbar").classList.toggle("hidden", n === 0);
  $("#grid").classList.toggle("selecting", n > 0);
  $("#sel-count").textContent = `${n} seleccionado${n !== 1 ? "s" : ""}`;
  const pend = S.status === "pending";
  $("#sel-move").classList.toggle("hidden", !pend);
  $("#sel-trash").classList.toggle("hidden", !pend);
  $("#sel-restore").classList.toggle("hidden", pend);
}

function toggleSel(i, range) {
  if (range && S.anchor !== null) {
    const [a, b] = [Math.min(S.anchor, i), Math.max(S.anchor, i)];
    for (let k = a; k <= b; k++) S.selected.add(S.items[k].id);
  } else {
    const id = S.items[i].id;
    S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
    S.anchor = i;
  }
  $$("#grid .tile").forEach(t => t.classList.toggle("sel", S.selected.has(S.items[t.dataset.i].id)));
  renderSelbar();
}

// clic / pulsación larga en la cuadrícula
let pressTimer, pressed = false;
$("#grid").addEventListener("pointerdown", e => {
  const t = e.target.closest(".tile");
  if (!t || e.pointerType === "mouse") return;
  pressed = false;
  pressTimer = setTimeout(() => { pressed = true; toggleSel(+t.dataset.i); navigator.vibrate?.(20); }, 450);
});
["pointerup", "pointercancel", "pointermove"].forEach(ev => $("#grid").addEventListener(ev, e => {
  if (ev !== "pointermove" || Math.abs(e.movementX) + Math.abs(e.movementY) > 6) clearTimeout(pressTimer);
}));
$("#grid").addEventListener("contextmenu", e => { if (e.target.closest(".tile") && e.pointerType !== "mouse") e.preventDefault(); });
$("#grid").addEventListener("click", e => {
  const t = e.target.closest(".tile");
  if (!t) return;
  if (pressed) { pressed = false; return; }
  const i = +t.dataset.i;
  if (e.target.closest(".chk") || e.shiftKey || e.ctrlKey || e.metaKey || S.selected.size > 0) toggleSel(i, e.shiftKey);
  else openViewer(i);
});

$("#more").onclick = () => loadItems(false);
$("#newpill").onclick = () => loadItems(true);
$("#kind-filter").onchange = e => { S.kind = e.target.value; loadItems(true); };
$$("#status-seg button").forEach(b => b.onclick = () => {
  S.status = b.dataset.status;
  $$("#status-seg button").forEach(x => x.classList.toggle("on", x === b));
  loadItems(true);
});
function setMode(m) {
  S.mode = m;
  try { localStorage.setItem("criba.mode", m); } catch (e) { /* nada */ }
  $$("#mode-seg button").forEach(x => x.classList.toggle("on", x.dataset.mode === m));
}
$$("#mode-seg button").forEach(b => b.onclick = () => {
  setMode(b.dataset.mode);
  if (b.dataset.mode === "focus" && S.items.length) openViewer(0);
});

$("#sel-all").onclick = () => { S.items.forEach(it => S.selected.add(it.id)); renderGrid(); };
$("#sel-cancel").onclick = () => { S.selected.clear(); renderGrid(); };
$("#sel-trash").onclick = () => trashIds([...S.selected]);
$("#sel-restore").onclick = () => restoreIds([...S.selected]);
$("#sel-move").onclick = async () => {
  const ids = [...S.selected];
  const first = S.items.find(it => it.id === ids[0]);
  const choice = await pickFolder({ title: `Guardar ${ids.length} en…`, suggested: first?.sub_folder });
  if (choice) await moveIds(ids, choice);
};

function dropItems(ids) {
  const set = new Set(ids);
  S.items = S.items.filter(it => !set.has(it.id));
  S.total -= ids.length;
  ids.forEach(id => S.selected.delete(id));
  if (S.loadedPending !== null && S.status === "pending") S.loadedPending -= ids.length;
}

async function trashIds(ids, inViewer) {
  try {
    await api("/api/items/trash", { method: "POST", json: { ids } });
    dropItems(ids);
    afterChange(inViewer);
    toast(ids.length === 1 ? "🗑 Movido a la papelera" : `🗑 ${ids.length} movidos a la papelera`, () => restoreIds(ids, inViewer, true));
  } catch (e) { fail(e); }
}

async function moveIds(ids, choice, inViewer) {
  try {
    const r = await api("/api/items/move", { method: "POST", json: { ids, folder: choice.folder, new_folder: choice.newName || null } });
    dropItems(ids);
    afterChange(inViewer);
    toast(`📁 ${ids.length === 1 ? "Guardado" : ids.length + " guardados"} en ${r.folder || "Biblioteca"}`, () => restoreIds(ids, inViewer, true));
  } catch (e) { fail(e); }
}

async function restoreIds(ids, inViewer, isUndo) {
  try {
    await api("/api/items/restore", { method: "POST", json: { ids } });
    if (isUndo) {
      await loadItems(true);
      if (inViewer) { const i = S.items.findIndex(it => it.id === ids[0]); if (i >= 0) openViewer(i); }
      toast("↩ Deshecho");
    } else {
      dropItems(ids);
      afterChange(inViewer);
      toast(`↩ ${ids.length === 1 ? "Devuelto" : ids.length + " devueltos"} a la cola`);
    }
  } catch (e) { fail(e); }
}

function afterChange(inViewer) {
  renderGrid();
  refreshCounts();
  if (inViewer) {
    if (!S.items.length) {
      if (S.items.length < S.total) loadItems(false).then(() => S.items.length ? showItem(0) : closeViewer());
      else closeViewer();
    } else showItem(Math.min(S.idx, S.items.length - 1));
  }
}

// ------------------------------------------------------------ VISOR
const V = { el: $("#viewer"), media: $("#v-media") };

function openViewer(i) {
  V.el.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  showItem(i);
  V.el.focus();
  let seen = false;
  try { seen = !!localStorage.getItem("criba.hint"); localStorage.setItem("criba.hint", "1"); } catch (e) { /* nada */ }
  if (isMobile() && !seen) {
    $("#v-hint").classList.remove("hidden");
    setTimeout(() => $("#v-hint").classList.add("hidden"), 5000);
  }
}
function closeViewer() {
  V.el.classList.add("hidden");
  V.media.innerHTML = "";
  document.body.style.overflow = "";
  if (S.mode === "focus") setMode("grid");
}

function showItem(i) {
  if (i < 0 || i >= S.items.length) return;
  if (i >= S.items.length - 5 && S.items.length < S.total) loadItems(false);
  S.idx = i;
  const it = S.items[i];
  V.media.style.transform = ""; V.media.style.opacity = "";
  $$(".v-stamp").forEach(s => s.style.opacity = 0);
  V.media.innerHTML = it.kind === "video"
    ? `<video src="/api/media/${it.id}" controls loop playsinline preload="auto" poster="/api/thumb/${it.id}"></video>`
    : `<img src="/api/media/${it.id}" alt="" draggable="false">`;
  const v = $("video", V.media);
  if (v) v.play().catch(() => { v.muted = true; v.play().catch(() => {}); });
  // precarga de la siguiente imagen
  const nx = S.items[i + 1];
  if (nx && nx.kind === "image") new Image().src = `/api/media/${nx.id}`;

  $("#v-pos").textContent = `${i + 1} / ${S.total}`;
  const open = $("#v-open");
  const link = it.post_url || it.source_url;
  open.classList.toggle("hidden", !link);
  if (link) open.href = link;
  const meta = [];
  if (it.author) meta.push(`<b>@${esc(it.author.replace(/^@/, ""))}</b>`);
  meta.push(esc(it.sub_name ? "👀 " + it.sub_name : SOURCE[it.source] || it.source));
  if (it.posted_at) meta.push("📅 " + fmtDate(it.posted_at));
  meta.push("⬇️ " + ago(it.created_at));
  if (it.size) meta.push(fmtSize(it.size));
  if (S.status === "kept") meta.push(`📁 ${esc(it.dest || "Biblioteca")}`);
  $("#v-meta").innerHTML = meta.join(" · ");
  const cap = $("#v-caption");
  cap.textContent = it.caption || "";
  cap.classList.add("clamp");
  cap.onclick = () => cap.classList.toggle("clamp");

  const pend = S.status === "pending";
  $("#v-trash").classList.toggle("hidden", !pend);
  $("#v-move").classList.toggle("hidden", !pend);
  $("#v-restore").classList.toggle("hidden", pend);
  renderQuick(it, pend);
  $("#v-prev").disabled = i === 0;
  $("#v-next").disabled = i >= S.items.length - 1;
}

let recentCache = [];
function quickFolders(it) {
  const out = [];
  if (it.sub_folder) out.push({ folder: it.sub_folder, tag: "sugerida" });
  for (const f of recentCache) if (!out.some(o => o.folder === f) && out.length < 4) out.push({ folder: f });
  return out;
}
function renderQuick(it, pend) {
  const q = $("#v-quick");
  if (!pend) { q.innerHTML = ""; return; }
  const list = quickFolders(it);
  q.innerHTML = list.map((f, k) => {
    const short = f.folder.split("/").pop() || "Biblioteca";
    return `<button class="btn" data-k="${k}" title="${esc(f.folder)} (tecla ${k + 1})">📁 ${esc(short)}${f.tag ? ` <small>${f.tag}</small>` : ""}</button>`;
  }).join("");
  $$("button", q).forEach(b => b.onclick = () => moveIds([it.id], { folder: list[b.dataset.k].folder }, true).then(loadRecent));
}
async function loadRecent() {
  try { recentCache = (await api("/api/folders")).recent; } catch (e) { /* nada */ }
  if (!V.el.classList.contains("hidden") && S.items[S.idx]) renderQuick(S.items[S.idx], S.status === "pending");
}

async function viewerMove() {
  const it = S.items[S.idx];
  if (!it) return;
  const choice = await pickFolder({ title: "Guardar en…", suggested: it.sub_folder });
  if (choice) { await moveIds([it.id], choice, true); loadRecent(); }
  V.el.focus();
}
const viewerTrash = () => { const it = S.items[S.idx]; if (it) trashIds([it.id], true); };

$("#v-close").onclick = closeViewer;
$("#v-prev").onclick = () => showItem(S.idx - 1);
$("#v-next").onclick = () => showItem(S.idx + 1);
$("#v-trash").onclick = viewerTrash;
$("#v-move").onclick = viewerMove;
$("#v-restore").onclick = () => { const it = S.items[S.idx]; if (it) restoreIds([it.id], true); };

document.addEventListener("keydown", e => {
  if (e.target.matches("input, textarea, select")) return;
  if (["#picker", "#adddlg", "#devdlg"].some(s => !$(s).classList.contains("hidden"))) {
    if (e.key === "Escape") { $("#picker-close").click(); $("#adddlg").classList.add("hidden"); $("#devdlg").classList.add("hidden"); }
    return;
  }
  if (!V.el.classList.contains("hidden")) {
    const pend = S.status === "pending";
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowRight" || e.key === "j") showItem(S.idx + 1);
    else if (e.key === "ArrowLeft" || e.key === "k") showItem(S.idx - 1);
    else if (pend && ["Delete", "Backspace", "x", "X"].includes(e.key)) viewerTrash();
    else if (pend && (e.key === "g" || e.key === "G" || e.key === "Enter")) viewerMove();
    else if (pend && /^[1-4]$/.test(e.key)) $$("#v-quick button")[+e.key - 1]?.click();
    else if (e.key === " ") { const v = $("video", V.media); if (v) { v.paused ? v.play() : v.pause(); } }
    else return;
    e.preventDefault();
    return;
  }
  if (e.key === "n" || e.key === "N") { openAdd(); e.preventDefault(); }
  if (S.view === "revisar" && (e.ctrlKey || e.metaKey) && e.key === "a") { e.preventDefault(); $("#sel-all").click(); }
  if (S.view === "revisar" && e.key === "Escape") $("#sel-cancel").click();
  if (S.view === "revisar" && S.selected.size && e.key === "Delete") $("#sel-trash").click();
});

// gestos: derecha = guardar, izquierda = borrar, arriba/abajo = siguiente/anterior
(() => {
  const stage = $("#v-stage");
  let x0, y0, dx = 0, dy = 0, active = false, axis = null;
  stage.addEventListener("pointerdown", e => {
    if (e.pointerType !== "touch") return;
    active = true; axis = null; x0 = e.clientX; y0 = e.clientY; dx = dy = 0;
  });
  stage.addEventListener("pointermove", e => {
    if (!active) return;
    dx = e.clientX - x0; dy = e.clientY - y0;
    if (!axis && Math.hypot(dx, dy) > 12) axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    if (axis === "x" && S.status === "pending") {
      V.media.classList.add("dragging");
      V.media.style.transform = `translateX(${dx}px) rotate(${dx / 25}deg)`;
      $(".v-stamp.keep").style.opacity = Math.max(0, Math.min(1, dx / 110));
      $(".v-stamp.del").style.opacity = Math.max(0, Math.min(1, -dx / 110));
    } else if (axis === "y") {
      V.media.classList.add("dragging");
      V.media.style.transform = `translateY(${dy * .5}px)`;
    }
  });
  const end = async () => {
    if (!active) return;
    active = false;
    V.media.classList.remove("dragging");
    const reset = () => { V.media.style.transform = ""; $$(".v-stamp").forEach(s => s.style.opacity = 0); };
    if (axis === "x" && S.status === "pending" && Math.abs(dx) > 100) {
      if (dx < 0) {
        V.media.style.transform = "translateX(-120%) rotate(-12deg)"; V.media.style.opacity = 0;
        setTimeout(viewerTrash, 150);
      } else { reset(); viewerMove(); }
    } else if (axis === "y" && Math.abs(dy) > 70) {
      reset(); showItem(S.idx + (dy < 0 ? 1 : -1));
    } else reset();
  };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
})();

// ------------------------------------------------------------ SELECTOR DE CARPETA
const P = { resolve: null, folders: [], recent: [], suggested: null, sel: "" };

async function pickFolder({ title, suggested, allowNone }) {
  $("#picker-title").textContent = title;
  $("#picker-search").value = "";
  $("#picker-newname").value = "";
  $("#picker-ok").textContent = allowNone ? "Elegir" : "Guardar aquí";
  P.suggested = suggested || null;
  P.allowNone = !!allowNone;
  try {
    const d = await api("/api/folders");
    P.folders = d.folders; P.recent = d.recent; recentCache = d.recent;
  } catch (e) { fail(e); return null; }
  P.sel = P.suggested ?? P.recent[0] ?? "";
  renderPicker();
  $("#picker").classList.remove("hidden");
  if (!isMobile()) $("#picker-search").focus();
  return new Promise(res => { P.resolve = res; });
}

function folderRow(f, extra = "") {
  const parts = f.split("/");
  const name = f === "" ? "Biblioteca (raíz)" : parts.pop();
  const path = parts.join(" / ");
  return `<div class="f ${P.sel === f ? "on" : ""}" data-f="${esc(f)}">📁 <span>${esc(name)}</span>
    ${path ? `<span class="path">${esc(path)}</span>` : ""}${extra}</div>`;
}

function filteredFolders() {
  const q = norm($("#picker-search").value.trim());
  if (!q) return P.folders;
  const words = q.split(/\s+/);
  return P.folders.filter(f => { const n = norm(f); return words.every(w => n.includes(w)); });
}

function renderPicker() {
  const q = $("#picker-search").value.trim();
  let html = "";
  if (!q) {
    if (P.suggested !== null) html += `<div class="grp">Sugerida</div>` + folderRow(P.suggested);
    const rec = P.recent.filter(f => f !== P.suggested && P.folders.includes(f));
    if (rec.length) html += `<div class="grp">Recientes</div>` + rec.map(f => folderRow(f)).join("");
    html += `<div class="grp">Todas</div>` + folderRow("");
  }
  const list = filteredFolders();
  html += list.slice(0, 400).map(f => folderRow(f)).join("");
  if (q && !list.length) html += `<div class="grp">Ninguna carpeta coincide · escribe el nombre abajo para crearla</div>`;
  if (list.length > 400) html += `<div class="grp">… ${list.length - 400} más, afina la búsqueda</div>`;
  $("#picker-list").innerHTML = html;
  $("#picker-sel").textContent = "📁 " + (P.sel || "Biblioteca");
  $("#picker-parent").textContent = `La nueva carpeta se creará dentro de: ${P.sel || "Biblioteca"}`;
}

function closePicker(val) {
  $("#picker").classList.add("hidden");
  if (P.resolve) { P.resolve(val); P.resolve = null; }
}
$("#picker-list").addEventListener("click", e => {
  const r = e.target.closest(".f");
  if (!r) return;
  P.sel = r.dataset.f;
  renderPicker();
});
$("#picker-list").addEventListener("dblclick", e => { if (e.target.closest(".f")) $("#picker-ok").click(); });
$("#picker-search").addEventListener("input", () => {
  const list = filteredFolders();
  if (list.length && $("#picker-search").value.trim()) P.sel = list[0];
  renderPicker();
});
$("#picker-search").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); $("#picker-ok").click(); }
  if (e.key === "Escape") closePicker(null);
});
$("#picker-newname").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); $("#picker-ok").click(); }
  if (e.key === "Escape") closePicker(null);
});
$("#picker-close").onclick = () => closePicker(null);
$("#picker").addEventListener("click", e => { if (e.target.id === "picker") closePicker(null); });
$("#picker-ok").onclick = () => closePicker({ folder: P.sel, newName: $("#picker-newname").value.trim() || null });

// ------------------------------------------------------------ AÑADIR ENLACE
function openAdd() {
  $("#adddlg").classList.remove("hidden");
  const ta = $("#add-form textarea");
  ta.value = "";
  ta.focus();
  navigator.clipboard?.readText?.().then(t => { if (/^https?:\/\//.test(t.trim()) && !ta.value) ta.value = t.trim(); }).catch(() => {});
}
$("#btn-add").onclick = openAdd;
$$("#adddlg [data-close]").forEach(b => b.onclick = () => $("#adddlg").classList.add("hidden"));
$("#adddlg").addEventListener("click", e => { if (e.target.id === "adddlg") $("#adddlg").classList.add("hidden"); });
async function submitUrls(text) {
  const r = await api("/api/download", { method: "POST", json: { url: text } });
  toast(`⬇️ ${r.jobs.length === 1 ? "Descarga en cola" : r.jobs.length + " descargas en cola"}`);
  refreshCounts();
  if (S.view === "descargas") loadJobs();
}
$("#add-form").onsubmit = async e => {
  e.preventDefault();
  try { await submitUrls(e.target.url.value); $("#adddlg").classList.add("hidden"); } catch (err) { fail(err); }
};
$("#dl-form").onsubmit = async e => {
  e.preventDefault();
  try { await submitUrls(e.target.url.value); e.target.reset(); } catch (err) { fail(err); }
};

// ------------------------------------------------------------ A ESTE DISPOSITIVO
// En el móvil se guarda con el menú de compartir (iOS: «Guardar imagen/vídeo» → Fotos);
// en el PC se descarga directamente.
const DEV = { jobs: [], timer: null };
const shareMode = () => {
  try {
    return matchMedia("(pointer: coarse)").matches && !!navigator.canShare &&
      navigator.canShare({ files: [new File(["x"], "x.jpg", { type: "image/jpeg" })] });
  } catch (e) { return false; }
};
const devUrl = (j, f) => `/api/device/${j.id}/file/${f.i}`;

$$("[data-device]").forEach(b => b.onclick = async () => {
  const form = b.closest("form");
  const text = form.url.value.trim();
  if (!text) { form.url.focus(); return; }
  try {
    const r = await api("/api/device", { method: "POST", json: { url: text } });
    r.jobs.forEach(id => DEV.jobs.unshift({ id, url: "", status: "queued", files: [], ready: {}, downloaded: false }));
    form.reset();
    $("#adddlg").classList.add("hidden");
    $("#devdlg").classList.remove("hidden");
    renderDev();
    pollDev();
  } catch (err) { fail(err); }
});
$("#dev-close").onclick = () => $("#devdlg").classList.add("hidden");
$("#devdlg").addEventListener("click", e => { if (e.target.id === "devdlg") $("#devdlg").classList.add("hidden"); });

async function pollDev() {
  clearTimeout(DEV.timer);
  for (const j of DEV.jobs.filter(j => j.status === "queued" || j.status === "running")) {
    try {
      Object.assign(j, await api(`/api/device/${j.id}`));
      if (j.status === "done") onDevReady(j);
    } catch (e) { if (e.message !== "401") { j.status = "error"; j.error = e.message; } }
  }
  renderDev();
  if (DEV.jobs.some(j => j.status === "queued" || j.status === "running")) DEV.timer = setTimeout(pollDev, 1500);
}

async function onDevReady(j) {
  if (!shareMode()) {
    // PC: se descargan solos, uno detrás de otro para que el navegador no los bloquee
    if (j.downloaded) return;
    j.downloaded = true;
    for (const f of j.files) { downloadFile(j, f); await new Promise(r => setTimeout(r, 500)); }
    return;
  }
  // móvil: se preparan antes de que pulses «Guardar» (iOS exige que compartir siga al toque)
  for (const f of j.files) {
    try {
      const blob = await (await fetch(devUrl(j, f))).blob();
      j.ready[f.i] = new File([blob], f.name, { type: blob.type || "application/octet-stream" });
    } catch (e) { j.ready[f.i] = null; }
    renderDev();
  }
}

function downloadFile(j, f) {
  const a = document.createElement("a");
  a.href = devUrl(j, f);
  a.download = f.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function shareFiles(files) {
  try { await navigator.share({ files }); }
  catch (e) { if (e.name !== "AbortError") toast("❌ No se pudo abrir el menú de compartir: " + e.message); }
}

function renderDev() {
  const share = shareMode();
  $("#dev-list").innerHTML = DEV.jobs.length ? DEV.jobs.map(j => {
    let body = "";
    if (j.status === "queued" || j.status === "running") body = `<div class="st">⏳ Descargando en el servidor…</div>`;
    else if (j.status === "error") body = `<div class="st">❌ No se ha podido descargar</div><div class="err">${esc(j.error || "")}</div>`;
    else if (j.expired) body = `<div class="st">⌛ Ya no está en el servidor (se borran a la hora)</div>`;
    else body = `<div class="st">✅ ${j.files.length} archivo${j.files.length !== 1 ? "s" : ""}</div>` + j.files.map(f => {
      const ico = f.kind === "video" ? "🎬" : "🖼";
      let btn;
      if (!share) btn = `<a class="btn small" href="${devUrl(j, f)}" download="${esc(f.name)}">Descargar</a>`;
      else if (j.ready[f.i] === undefined) btn = `<button class="btn small" disabled>Preparando…</button>`;
      else if (j.ready[f.i] === null) btn = `<a class="btn small" href="${devUrl(j, f)}" download="${esc(f.name)}">Abrir</a>`;
      else btn = `<button class="btn small primary" data-share="${j.id}:${f.i}">Guardar</button>`;
      return `<div class="dev-file"><span>${ico}</span><span class="nm">${esc(f.name)}</span><span class="sz">${fmtSize(f.size)}</span>${btn}</div>`;
    }).join("");
    return `<div class="dev-job">${body}<div class="url">${esc(j.url)}</div></div>`;
  }).join("") : `<p class="muted">Nada por ahora.</p>`;
  const ready = share ? DEV.jobs.flatMap(j => Object.values(j.ready).filter(Boolean)) : [];
  $("#dev-all").classList.toggle("hidden", ready.length < 2);
  $("#dev-all").textContent = `Guardar todo (${ready.length})`;
  $("#dev-all").onclick = () => shareFiles(ready);
}
$("#dev-list").addEventListener("click", e => {
  const b = e.target.closest("[data-share]");
  if (!b) return;
  const [jid, i] = b.dataset.share.split(":").map(Number);
  const f = DEV.jobs.find(j => j.id === jid)?.ready[i];
  if (f) shareFiles([f]);
});

// ------------------------------------------------------------ SEGUIMIENTOS
let newSubFolder = null;
$("#sub-folder-btn").onclick = async () => {
  const c = await pickFolder({ title: "Carpeta sugerida para este perfil", suggested: newSubFolder, allowNone: true });
  if (!c) return;
  newSubFolder = c.newName ? await createFolder(c.folder, c.newName) : c.folder;
  $("#sub-folder-btn span").textContent = newSubFolder || "Biblioteca";
};
async function createFolder(parent, name) {
  try { return (await api("/api/folders", { method: "POST", json: { parent, name } })).folder; } catch (e) { fail(e); return parent; }
}

$("#sub-form").onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const mode = f.initial_mode.value;
  const body = {
    url: f.url.value.trim(), name: f.name.value.trim() || null,
    interval_min: f.interval_min.value ? +f.interval_min.value : null,
    initial: mode === "n" ? String(f.initial_n.value || 20) : mode, folder: newSubFolder,
  };
  try {
    const s = await api("/api/subs", { method: "POST", json: body });
    toast(`👀 Siguiendo a ${s.name}`);
    f.reset(); newSubFolder = null; $("#sub-folder-btn span").textContent = "ninguna";
    loadSubs();
  } catch (err) { fail(err); }
};

async function loadSubs() {
  try { await loadSubsData(); } catch (e) { return fail(e); }
  const el = $("#subs");
  if (!S.subs.length) { el.innerHTML = `<p class="muted">Todavía no sigues a nadie.</p>`; return; }
  el.innerHTML = `<div class="list">${S.subs.map(s => {
    const initial = s.initial_count === 0 ? "solo lo nuevo" : s.initial_count < 0 ? "todo el histórico" : `últimos ${s.initial_count}`;
    let stat;
    if (!s.baseline_done && !s.last_check) stat = `Preparando (${initial})…`;
    else if (s.last_error) stat = `<span class="err">⚠️ ${esc(s.last_error)}</span>`;
    else stat = `Comprobado ${ago(s.last_check)} · ${s.last_new} nuevos la última vez · ${s.total} en total`;
    return `<div class="card sub ${s.enabled ? "" : "off"}" data-id="${s.id}">
      <div>
        <div class="name">${esc(s.name)} <span class="site">${esc(s.site)}</span>
          ${s.pending ? `<a class="pending-link" href="#revisar" data-review="${s.id}">${s.pending} por revisar</a>` : ""}
          ${s.enabled ? "" : '<span class="site">en pausa</span>'}</div>
        <div class="url"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a></div>
        <div class="stat">${stat}</div>
        <div class="stat">Cada ${fmtInterval(s.interval_min)} · próxima ${s.enabled ? ago(s.next_check) : "—"} · 📁 ${esc(s.folder || "sin carpeta sugerida")}</div>
      </div>
      <div></div>
      <div class="acts">
        <button class="btn small" data-act="check">🔄 Comprobar ahora</button>
        <button class="btn small" data-act="toggle">${s.enabled ? "⏸ Pausar" : "▶ Reanudar"}</button>
        <select class="btn small" data-act="interval" aria-label="Intervalo">
          ${[15, 30, 60, 120, 180, 360, 720, 1440].concat([s.interval_min]).filter((v, i, a) => a.indexOf(v) === i).sort((a, b) => a - b)
            .map(v => `<option value="${v}" ${v === s.interval_min ? "selected" : ""}>cada ${fmtInterval(v)}</option>`).join("")}
        </select>
        <button class="btn small" data-act="folder">📁 Carpeta</button>
        <button class="btn small" data-act="rename">✏️ Nombre</button>
        <button class="btn small danger" data-act="delete">Dejar de seguir</button>
      </div></div>`;
  }).join("")}</div>`;
}

$("#subs").addEventListener("click", async e => {
  const rv = e.target.closest("[data-review]");
  if (rv) { S.sub = +rv.dataset.review; S.source = null; S.status = "pending"; return; }
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const id = +b.closest(".sub").dataset.id;
  const s = S.subs.find(x => x.id === id);
  try {
    if (b.dataset.act === "check") { await api(`/api/subs/${id}/check`, { method: "POST" }); toast("🔄 Comprobación en cola"); }
    if (b.dataset.act === "toggle") await api(`/api/subs/${id}`, { method: "PATCH", json: { enabled: !s.enabled } });
    if (b.dataset.act === "rename") {
      const n = prompt("Nombre", s.name);
      if (n) await api(`/api/subs/${id}`, { method: "PATCH", json: { name: n } });
    }
    if (b.dataset.act === "folder") {
      const c = await pickFolder({ title: `Carpeta sugerida para ${s.name}`, suggested: s.folder, allowNone: true });
      if (c) {
        const folder = c.newName ? await createFolder(c.folder, c.newName) : c.folder;
        await api(`/api/subs/${id}`, { method: "PATCH", json: { folder } });
      }
    }
    if (b.dataset.act === "delete") {
      if (!confirm(`¿Dejar de seguir a ${s.name}? Lo que ya está en la cola se queda.`)) return;
      await api(`/api/subs/${id}`, { method: "DELETE" });
    }
    loadSubs();
  } catch (err) { fail(err); }
});
$("#subs").addEventListener("change", async e => {
  if (e.target.dataset.act !== "interval") return;
  const id = +e.target.closest(".sub").dataset.id;
  try { await api(`/api/subs/${id}`, { method: "PATCH", json: { interval_min: +e.target.value } }); loadSubs(); } catch (err) { fail(err); }
});

// ------------------------------------------------------------ DESCARGAS
const JOB_ICON = { queued: "🕓", running: "⏳", done: "✅", error: "❌" };
let jobsTimer;
async function loadJobs() {
  clearTimeout(jobsTimer);
  let d;
  try { d = await api("/api/jobs?limit=80"); } catch (e) { return fail(e); }
  const open = new Set($$("#jobs pre").map(p => p.dataset.id));
  $("#jobs").innerHTML = d.jobs.length ? `<div class="list">${d.jobs.map(j => {
    const what = j.kind === "sub" ? `👀 ${esc(j.sub_name || "seguimiento borrado")}`
      : `${j.kind === "device" ? "📱 " : ""}<a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.url)}</a>`;
    let res = "";
    if (j.status === "done") res = j.note && !j.new_items ? esc(j.note)
      : `${j.new_items} nuevo${j.new_items !== 1 ? "s" : ""}${j.dup_items ? ` · ${j.dup_items} repetidos` : ""}`;
    if (j.status === "running") res = "descargando…";
    if (j.status === "queued") res = "en espera";
    if (j.status === "error") res = "falló";
    return `<div class="card job" data-id="${j.id}">
      <div class="st">${JOB_ICON[j.status] || ""}</div>
      <div><div class="what">${what}</div><div class="when">${ago(j.created_at)} · ${j.origin || ""} · ${res}</div></div>
      <div>${j.log ? `<button class="btn small ghost" data-log="${j.id}">Registro</button>` : ""}
        ${j.status === "error" ? `<button class="btn small" data-retry="${j.id}">Reintentar</button>` : ""}
        ${j.status === "done" || j.status === "error" ? `<button class="btn small ghost" data-deljob="${j.id}" title="Borrar de la lista" aria-label="Borrar de la lista">✕</button>` : ""}</div>
      ${j.log ? `<pre data-id="${j.id}" class="${open.has(String(j.id)) ? "" : "hidden"}">${esc(j.log)}</pre>` : ""}
    </div>`;
  }).join("")}</div>` : `<p class="muted">Aún no hay descargas.</p>`;
  if (S.view === "descargas") jobsTimer = setTimeout(loadJobs, d.jobs.some(j => j.status === "running" || j.status === "queued") ? 3000 : 15000);
}
$("#jobs").addEventListener("click", async e => {
  const l = e.target.closest("[data-log]");
  if (l) $(`#jobs pre[data-id="${l.dataset.log}"]`).classList.toggle("hidden");
  const r = e.target.closest("[data-retry]");
  if (r) { try { await api(`/api/jobs/${r.dataset.retry}/retry`, { method: "POST" }); loadJobs(); } catch (err) { fail(err); } }
  const d = e.target.closest("[data-deljob]");
  if (d) {
    try { await api(`/api/jobs/${d.dataset.deljob}`, { method: "DELETE" }); d.closest(".job").remove(); } catch (err) { fail(err); }
  }
});
async function clearJobs(status, label) {
  try {
    const r = await api("/api/jobs/clear", { method: "POST", json: { status } });
    toast(`🧹 ${r.deleted} ${label} borrada${r.deleted !== 1 ? "s" : ""}`);
    loadJobs();
  } catch (err) { fail(err); }
}
$("#jobs-clear-error").onclick = () => clearJobs("error", "fallida");
$("#jobs-clear-all").onclick = () => clearJobs("finished", "descarga");

// ------------------------------------------------------------ AJUSTES
async function loadSettings() {
  let s;
  try { s = await api("/api/settings"); } catch (e) { return fail(e); }
  const tg = s.telegram;
  const tgState = !tg.configured ? `<span class="bad">Sin configurar</span> (falta TELEGRAM_TOKEN)`
    : tg.ok ? `<span class="ok">Conectado</span> como <a href="https://t.me/${esc(tg.username)}" target="_blank">@${esc(tg.username)}</a>`
    : `<span class="bad">Error:</span> ${esc(tg.error || "no arrancó")}`;
  const bm = `javascript:(()=>{window.open(${JSON.stringify(location.origin + "/add?url=")}+encodeURIComponent(location.href),"_blank","width=420,height=320")})()`;
  $("#settings").innerHTML = `<div class="settings">
    <div class="card"><h2>Telegram</h2><dl class="kv">
      <dt>Bot</dt><dd>${tgState}</dd>
      <dt>Usuarios autorizados</dt><dd>${tg.allowed.length ? tg.allowed.join(", ") : '<span class="bad">ninguno</span> — escribe /id al bot y pon tu ID en TELEGRAM_ALLOWED_IDS'}</dd>
      <dt>Enlaces en los avisos</dt><dd>${s.public_url ? esc(s.public_url) : '<span class="bad">sin CRIBA_URL</span> (los mensajes no llevarán enlace)'}</dd>
    </dl></div>

    <div class="card"><h2>Cookies de sesión</h2>
      <p class="hint" style="margin-top:0">Instagram y X (y a veces Reddit o TikTok) solo dejan descargar con sesión iniciada. Entra con tu cuenta
      secundaria en el navegador, exporta las cookies con la extensión <b>Get cookies.txt LOCALLY</b> (Chrome/Firefox) estando en esa web
      y súbelas aquí. Si un seguimiento empieza a fallar, normalmente hay que volver a subirlas.</p>
      <div id="cookie-rows">${s.cookies.map(c => `<div class="cookie-row" data-site="${esc(c.site)}">
        <span class="site-name">${esc(c.site)}</span>
        <span class="${c.present ? "ok" : "muted"}">${c.present ? "✓ subidas " + esc(c.updated) : "no hay"}</span>
        <span class="sp"></span>
        <label class="btn small">Subir cookies.txt<input type="file" accept=".txt,text/plain" hidden data-upload></label>
        ${c.present ? '<button class="btn small ghost" data-delcookie>Quitar</button>' : ""}
      </div>`).join("")}
      <div class="cookie-row"><input id="other-site" placeholder="otro dominio (ej. tumblr.com)" style="max-width:240px">
        <label class="btn small">Subir<input type="file" accept=".txt,text/plain" hidden id="other-upload"></label></div>
      </div>
    </div>

    <div class="card"><h2>Marcador «Enviar a Criba»</h2>
      <p class="hint" style="margin-top:0">Arrástralo a la barra de marcadores. Al pulsarlo en cualquier página, envía esa dirección a la cola de descarga.</p>
      <a class="bookmarklet" href="${esc(bm)}" onclick="event.preventDefault()">➕ Enviar a Criba</a>
    </div>

    <div class="card"><h2>Descargadores</h2>
      <p class="hint" style="margin-top:0">gallery-dl y yt-dlp se actualizan solos al arrancar el contenedor. Si un sitio deja de funcionar, actualiza a mano.</p>
      <p id="versions" class="muted">${esc(s.versions || "")}</p>
      <button class="btn" id="btn-update">⬆️ Actualizar ahora</button> <button class="btn ghost" id="btn-versions">Ver versiones</button>
      <pre class="out hidden" id="update-out"></pre>
    </div>

    <div class="card"><h2>Almacenamiento</h2><dl class="kv">
      <dt>Biblioteca</dt><dd>${esc(s.library)}</dd>
      <dt>Datos y cola</dt><dd>${esc(s.data)}</dd>
      <dt>Papelera</dt><dd>${S.counts?.trashed ?? 0} archivos · se vacía sola a los ${s.trash_days} días
        <button class="btn small danger" id="btn-empty-trash">Vaciar ya</button></dd>
    </dl></div>

    ${s.password ? `<form method="post" action="/logout"><button class="btn ghost">Cerrar sesión</button></form>` : ""}
  </div>`;

  $$("#cookie-rows [data-upload]").forEach(inp => inp.onchange = () => uploadCookies(inp.closest(".cookie-row").dataset.site, inp.files[0]));
  $$("#cookie-rows [data-delcookie]").forEach(b => b.onclick = async () => {
    try { await api(`/api/cookies/${b.closest(".cookie-row").dataset.site}`, { method: "DELETE" }); loadSettings(); } catch (e) { fail(e); }
  });
  $("#other-upload").onchange = e => {
    const site = $("#other-site").value.trim();
    if (!site) { toast("Escribe primero el dominio"); return; }
    uploadCookies(site, e.target.files[0]);
  };
  $("#btn-update").onclick = async e => {
    e.target.disabled = true; e.target.textContent = "Actualizando… (puede tardar un minuto)";
    try {
      const r = await api("/api/tools/update", { method: "POST" });
      $("#update-out").textContent = r.output; $("#update-out").classList.remove("hidden");
      $("#versions").textContent = r.output.trim().split("\n").pop();
    } catch (err) { fail(err); }
    e.target.disabled = false; e.target.textContent = "⬆️ Actualizar ahora";
  };
  $("#btn-versions").onclick = async () => { try { $("#versions").textContent = (await api("/api/tools/versions")).versions; } catch (e) { fail(e); } };
  $("#btn-empty-trash").onclick = async () => {
    if (!confirm("¿Borrar definitivamente todo lo que hay en la papelera?")) return;
    try { const r = await api("/api/trash/empty", { method: "POST" }); toast(`🗑 ${r.deleted} borrados definitivamente`); refreshCounts().then(loadSettings); } catch (e) { fail(e); }
  };
}

async function uploadCookies(site, file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try { await api(`/api/cookies/${encodeURIComponent(site)}`, { method: "POST", body: fd }); toast(`🍪 Cookies de ${site} guardadas`); loadSettings(); }
  catch (e) { fail(e); }
}

// ------------------------------------------------------------ arranque
(function init() {
  const q = new URLSearchParams(location.search);
  if (q.get("sub")) { S.sub = +q.get("sub"); history.replaceState(null, "", "/#revisar"); }
  setMode(S.mode);
  refreshCounts().then(route);
  loadRecent();
  setInterval(refreshCounts, 10000);
  // en modo «una a una» se abre el visor en cuanto hay elementos
  const origLoad = loadItems;
  loadItems = async function (reset) {
    await origLoad(reset);
    if (reset && S.mode === "focus" && S.view === "revisar" && S.items.length && V.el.classList.contains("hidden") && !S._focusOpened) {
      S._focusOpened = true; openViewer(0);
    }
  };
})();
