"""Criba: API web + arranque del bot, la cola de descargas y el planificador."""
import asyncio
import hashlib
import hmac
import logging
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import bot, cobalt, config, frames, library, tgreview, worker
from .config import COOKIES, DATA, LIBRARY, UI_PASSWORD
from .db import db, now
from .downloader import (COOKIE_FILE, IG_DEFAULT, IG_PARTS, VERBOSE_ORIGIN, clear_cookies_bad, clear_ratelimit,
                         device_dir, device_files, sub_parts, tool_versions, update_tools)
from .subscriptions import MIN_INTERVAL, add_subscription, clean_parts, parse_initial
from .thumbs import make_thumb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("criba")
# httpx registra cada URL de la API de Telegram, que lleva el token del bot dentro
logging.getLogger("httpx").setLevel(logging.WARNING)
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Criba %s", config.VERSION)
    worker.recover()
    tasks = [asyncio.create_task(worker.worker("link")), asyncio.create_task(worker.worker("sub")),
             asyncio.create_task(worker.worker("device")),
             asyncio.create_task(worker.scheduler())]
    await bot.start()
    await cobalt.start()
    yield
    await cobalt.stop()
    await bot.stop()
    for t in tasks:
        t.cancel()


app = FastAPI(title="Criba", lifespan=lifespan, docs_url=None, redoc_url=None)

# ------------------------------------------------------------------ sesión

SECRET = db.get_setting("secret")
if not SECRET:
    SECRET = secrets.token_hex(32)
    db.set_setting("secret", SECRET)
COOKIE = "criba_session"
SESSION_DAYS = 90
PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/static/icon.svg", "/static/login.css"}


def _sign(ts: str) -> str:
    key = (SECRET + UI_PASSWORD).encode()  # cambiar la contraseña invalida las sesiones
    return hmac.new(key, ts.encode(), hashlib.sha256).hexdigest()


def _valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    ts, sig = value.split(".", 1)
    if not ts.isdigit() or time.time() - int(ts) > SESSION_DAYS * 86400:
        return False
    return hmac.compare_digest(sig, _sign(ts))


@app.middleware("http")
async def auth(request: Request, call_next):
    # /api/ext/ (salvo /token) usa el token de la extensión, que comprueba cada ruta
    ext = request.url.path.startswith("/api/ext/") and request.url.path != "/api/ext/token"
    if (not UI_PASSWORD or ext or request.url.path in PUBLIC_PATHS
            or _valid_session(request.cookies.get(COOKIE))):
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            # revalidar siempre: tras actualizar el contenedor no debe quedarse el JS/CSS viejo
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Sesión caducada"}, status_code=401)
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


LOGIN_HTML = (STATIC / "login.html").read_text(encoding="utf-8")


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    return LOGIN_HTML.replace("{{error}}", "Contraseña incorrecta" if error else "")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    nxt = str(form.get("next") or "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if not UI_PASSWORD or not hmac.compare_digest(str(form.get("password", "")).encode(), UI_PASSWORD.encode()):
        await asyncio.sleep(1.5)  # frena intentos por fuerza bruta
        return RedirectResponse(f"/login?error=1&next={nxt}", status_code=303)
    ts = str(int(time.time()))
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, f"{ts}.{_sign(ts)}", max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ------------------------------------------------------------------ páginas

# ?v=… en el JS y el CSS: tras actualizar, el navegador no puede seguir usando la copia vieja
ASSET_V = config.VERSION if config.VERSION != "dev" else str(int(time.time()))
INDEX_HTML = ((STATIC / "index.html").read_text(encoding="utf-8")
              .replace('"/static/app.js"', f'"/static/app.js?v={ASSET_V}"')
              .replace('"/static/style.css"', f'"/static/style.css?v={ASSET_V}"'))


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "Criba", "short_name": "Criba", "start_url": "/", "display": "standalone",
        "background_color": "#111214", "theme_color": "#111214",
        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    })


@app.get("/add", response_class=HTMLResponse)
async def add_from_bookmarklet(url: str):
    """Destino del marcador «Enviar a Criba»."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL no válida")
    worker.enqueue_link(url, "web")
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<body style='font:16px system-ui;background:#111214;color:#eee;display:grid;place-items:center;height:90vh'>"
        "<div style='text-align:center'><p style='font-size:40px;margin:0'>✅</p><p>Enviado a la cola de descarga</p>"
        "<p><a style='color:#8ab4ff' href='/'>Abrir Criba</a></p></div>"
        "<script>setTimeout(()=>{history.length>1?history.back():window.close()},1500)</script>")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ------------------------------------------------------------------ API: cola

def _item_out(it: dict) -> dict:
    return {k: it[k] for k in ("id", "kind", "orig_name", "save_name", "size", "source", "source_url", "post_url",
                               "author", "caption", "posted_at", "subscription_id", "created_at", "status", "dest",
                               "from_item")}


@app.get("/api/items")
async def items(status: str = "pending", sub: int | None = None, source: str | None = None, kind: str | None = None,
                limit: int = 200, offset: int = 0, order: str = "desc"):
    where, args = ["status=?"], [status]
    if sub:
        where.append("subscription_id=?")
        args.append(sub)
    if source:
        where.append("source=?")
        args.append(source)
    if kind:
        where.append("kind=?")
        args.append(kind)
    w = " AND ".join(where)
    direction = "ASC" if order == "asc" else "DESC"
    rows = db.q(f"SELECT i.*, s.name sub_name, s.folder sub_folder FROM items i "
                f"LEFT JOIN subscriptions s ON s.id=i.subscription_id WHERE {w} "
                f"ORDER BY i.id {direction} LIMIT ? OFFSET ?", (*args, min(limit, 1000), offset))
    total = db.one(f"SELECT COUNT(*) n FROM items WHERE {w}", args)["n"]
    out = []
    for r in rows:
        o = _item_out(r)
        o["sub_name"], o["sub_folder"] = r["sub_name"], r["sub_folder"]
        out.append(o)
    return {"items": out, "total": total}


@app.get("/api/counts")
async def counts():
    pending = db.one("SELECT COUNT(*) n FROM items WHERE status='pending'")["n"]
    by_sub = db.q("SELECT subscription_id id, COUNT(*) n FROM items WHERE status='pending' AND subscription_id "
                  "IS NOT NULL GROUP BY subscription_id")
    by_source = db.q("SELECT source, COUNT(*) n FROM items WHERE status='pending' GROUP BY source")
    jobs = db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")["n"]
    trashed = db.one("SELECT COUNT(*) n FROM items WHERE status='trashed'")["n"]
    return {"pending": pending, "by_sub": by_sub, "by_source": by_source, "jobs": jobs, "trashed": trashed}


def _get_item(iid: int) -> dict:
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        raise HTTPException(404, "No existe")
    return it


@app.get("/api/media/{iid}")
async def media(iid: int):
    it = _get_item(iid)
    try:
        p = library.item_path(it)
    except library.PathError:
        raise HTTPException(404, "Sin fichero")
    if not p.exists():
        raise HTTPException(404, "Sin fichero")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


PLACEHOLDER = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 3"><rect width="4" height="3" fill="#2a2c31"/>'
               '<path d="M1.6 1v1l.9-.5z" fill="#777"/></svg>')


@app.get("/api/thumb/{iid}")
async def thumb(iid: int):
    it = _get_item(iid)
    try:
        p = library.item_path(it)
    except library.PathError:
        p = None
    t = await asyncio.to_thread(make_thumb, iid, p, it["kind"]) if p and p.exists() else None
    if not t:
        return Response(PLACEHOLDER, media_type="image/svg+xml")
    return FileResponse(t, headers={"Cache-Control": "private, max-age=604800"})


@app.post("/api/items/move")
async def move(ids: list[int] = Body(...), folder: str = Body(...), new_folder: str | None = Body(None)):
    try:
        if new_folder and new_folder.strip():
            folder = library.create_folder(folder, new_folder.strip())
        moved = await asyncio.to_thread(library.move_items, ids, folder)
    except library.PathError as e:
        raise HTTPException(400, str(e))
    await tgreview.refresh_items(ids)  # los mensajes del bot reflejan la decisión
    return {"moved": moved, "folder": folder}


def _items_full(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = db.q(f"SELECT i.*, s.name sub_name, s.folder sub_folder FROM items i "
                f"LEFT JOIN subscriptions s ON s.id=i.subscription_id WHERE i.id IN ({marks})", ids)
    by_id = {r["id"]: {**_item_out(r), "sub_name": r["sub_name"], "sub_folder": r["sub_folder"]} for r in rows}
    return [by_id[i] for i in ids if i in by_id]


@app.post("/api/items/frame")
async def to_frame(ids: list[int] = Body(...), t: float = Body(0.0)):
    """«Solo imagen»: cambia cada vídeo por su fotograma del segundo t (el vídeo va a la papelera)."""
    made, errors = {}, []
    for iid in ids:
        try:
            made[iid] = await asyncio.to_thread(frames.video_to_frame, iid, t)
        except frames.FrameError as e:
            errors.append(str(e))
    await tgreview.refresh_items(list(made.values()))
    if not made and errors:
        raise HTTPException(400, errors[0])
    return {"map": made, "items": _items_full(list(made.values())), "errors": errors}


@app.post("/api/items/unframe")
async def undo_frame(ids: list[int] = Body(..., embed=True)):
    vids = []
    for fid in ids:
        try:
            vids.append(await asyncio.to_thread(frames.frame_undo, fid))
        except frames.FrameError as e:
            raise HTTPException(400, str(e))
    await tgreview.refresh_items(vids)
    return {"items": _items_full(vids)}


@app.post("/api/items/trash")
async def trash(ids: list[int] = Body(..., embed=True)):
    n = await asyncio.to_thread(library.trash_items, ids)
    await tgreview.refresh_items(ids)
    return {"trashed": n}


@app.post("/api/items/restore")
async def restore(ids: list[int] = Body(..., embed=True)):
    n = await asyncio.to_thread(library.restore_items, ids)
    await tgreview.refresh_items(ids)
    return {"restored": n}


@app.post("/api/trash/empty")
async def empty_trash():
    ids = [r["id"] for r in db.q("SELECT id FROM items WHERE status='trashed'")]
    for iid in ids:
        it = _get_item(iid)
        (config.TRASH / it["file"]).unlink(missing_ok=True)
        (config.THUMBS / f"{iid}.jpg").unlink(missing_ok=True)
        db.x("UPDATE items SET status='deleted', file='' WHERE id=?", (iid,))
    return {"deleted": len(ids)}


# ------------------------------------------------------------------ API: biblioteca

@app.get("/api/folders")
async def folders():
    return {"folders": await asyncio.to_thread(library.list_folders), "recent": library.recent_folders(),
            "root": str(LIBRARY)}


@app.post("/api/folders")
async def new_folder(parent: str = Body(""), name: str = Body(...)):
    try:
        return {"folder": library.create_folder(parent, name)}
    except library.PathError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ API: descargas

@app.post("/api/download")
async def download(url: str = Body(..., embed=True)):
    urls = [u for u in url.split() if u.startswith(("http://", "https://"))]
    if not urls:
        raise HTTPException(400, "Pon un enlace que empiece por http(s)://")
    return {"jobs": [worker.enqueue_link(u, "web") for u in urls]}


# ------------------------------------------------------------------ API: a este dispositivo

@app.post("/api/device")
async def device_download(url: str = Body(..., embed=True)):
    urls = [u for u in url.split() if u.startswith(("http://", "https://"))]
    if not urls:
        raise HTTPException(400, "Pon un enlace que empiece por http(s)://")
    return {"jobs": [worker.enqueue_device(u) for u in urls]}


@app.get("/api/device/{jid}")
async def device_status(jid: int):
    j = db.one("SELECT * FROM jobs WHERE id=? AND kind='device'", (jid,))
    if not j:
        raise HTTPException(404, "No existe")
    files = [{"i": i, "name": p.name, "size": p.stat().st_size, "kind": config.kind_of(p.name)}
             for i, p in enumerate(device_files(jid))]
    expired = j["status"] == "done" and not files
    return {"id": jid, "url": j["url"], "status": j["status"], "files": files, "expired": expired,
            "error": downloader_error(j) if j["status"] == "error" else None}


def downloader_error(job: dict) -> str:
    from .downloader import last_error_line
    return last_error_line(job["log"] or "")


@app.get("/api/device/{jid}/file/{i}")
async def device_file(jid: int, i: int):
    files = device_files(jid)
    if i < 0 or i >= len(files):
        raise HTTPException(404, "El archivo ya no está (se borran a la hora)")
    return FileResponse(files[i], filename=files[i].name, headers={"Cache-Control": "no-store"})


@app.delete("/api/device/{jid}")
async def device_forget(jid: int):
    import shutil
    shutil.rmtree(device_dir(jid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/jobs")
async def jobs(limit: int = 50):
    rows = db.q("SELECT j.*, s.name sub_name FROM jobs j LEFT JOIN subscriptions s ON s.id=j.subscription_id "
                "ORDER BY j.id DESC LIMIT ?", (min(limit, 300),))
    return {"jobs": rows}


@app.delete("/api/jobs/{jid}")
async def delete_job(jid: int):
    """Quita una descarga terminada o fallida de la lista (no toca los archivos ya en la cola)."""
    return {"deleted": db.x("DELETE FROM jobs WHERE id=? AND status IN ('done','error','cancelled')", (jid,))}


@app.post("/api/jobs/{jid}/cancel")
async def cancel_job(jid: int):
    state = worker.cancel(jid)
    if state is None:
        raise HTTPException(400, "Esa descarga ya había terminado")
    return {"state": state}


@app.post("/api/jobs/clear")
async def clear_jobs(status: str = Body("error", embed=True)):
    if status not in ("error", "done", "finished"):
        raise HTTPException(400, "Estado no válido")
    states = {"error": ("error", "cancelled"), "done": ("done",), "finished": ("done", "error", "cancelled")}[status]
    marks = ",".join("?" * len(states))
    return {"deleted": db.x(f"DELETE FROM jobs WHERE status IN ({marks})", states)}


@app.post("/api/jobs/{jid}/retry")
async def retry(jid: int, verbose: bool = False):
    """Reintentar; con verbose=1, gallery-dl y yt-dlp en modo detallado para ver por qué falla."""
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise HTTPException(404, "No existe")
    origin = VERBOSE_ORIGIN if verbose else "web"
    if j["kind"] == "sub":
        return {"job": worker.enqueue_sub(j["subscription_id"], origin)}
    if j["kind"] == "device":
        return {"job": worker.enqueue_device(j["url"])}
    if j["kind"] == "tray":
        return {"job": worker.enqueue_tray(origin)}
    return {"job": worker.enqueue_link(j["url"], origin)}


# ------------------------------------------------------------------ API: seguimientos

@app.get("/api/subs")
async def subs():
    rows = db.q("SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.subscription_id=s.id AND "
                "i.status='pending') pending FROM subscriptions s ORDER BY s.name COLLATE NOCASE")
    for r in rows:
        r["parts_effective"] = sub_parts(r)
    return {"subs": rows, "ig_parts": IG_PARTS, "ig_default": IG_DEFAULT,
            "hours": dict(zip(("enabled", "start", "end"), worker.active_hours())),
            "tray": {"count": len(worker.tray_subs()), "last": db.get_setting("tray_last"),
                     "next": db.get_setting("tray_next")}}


@app.post("/api/tray/check")
async def tray_check():
    """Revisar ya la bandeja de historias de Instagram."""
    return {"job": worker.enqueue_tray("web")}


@app.post("/api/subs")
async def create_sub(url: str = Body(...), name: str | None = Body(None), interval_min: int | None = Body(None),
                     initial: str = Body("nuevo"), folder: str | None = Body(None),
                     parts: list[str] | None = Body(None)):
    try:
        return add_subscription(url, name, interval_min, parse_initial(initial), folder, parts)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/subs/{sid}")
async def update_sub(sid: int, data: dict = Body(...)):
    if not db.one("SELECT id FROM subscriptions WHERE id=?", (sid,)):
        raise HTTPException(404, "No existe")
    fields = {}
    if "name" in data and str(data["name"]).strip():
        fields["name"] = str(data["name"]).strip()
    if "interval_min" in data:
        fields["interval_min"] = max(MIN_INTERVAL, int(data["interval_min"]))
    if "enabled" in data:
        fields["enabled"] = 1 if data["enabled"] else 0
    if "parts" in data:
        fields["parts"] = clean_parts(data["parts"])
        if not fields["parts"]:
            raise HTTPException(400, "Elige al menos una sección")
    if "folder" in data:
        fields["folder"] = (data["folder"] or "").strip() or None
    if fields:
        db.x(f"UPDATE subscriptions SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), sid))
    return db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))


@app.post("/api/subs/{sid}/check")
async def check_sub(sid: int):
    return {"job": worker.enqueue_sub(sid, "web")}


@app.post("/api/subs/{sid}/resync")
async def resync_sub(sid: int):
    """Olvida lo marcado como visto y vuelve a bajar lo publicado desde que se sigue el perfil.
    Lo que ya pasó por la cola no se repite: los duplicados se descartan por contenido."""
    if not db.one("SELECT id FROM subscriptions WHERE id=?", (sid,)):
        raise HTTPException(404, "No existe")
    if db.one("SELECT id FROM jobs WHERE subscription_id=? AND status IN ('queued','running')", (sid,)):
        raise HTTPException(400, "Hay una comprobación en marcha: espera a que acabe o cancélala")
    (config.ARCHIVES / f"sub_{sid}.sqlite").unlink(missing_ok=True)
    db.x("UPDATE subscriptions SET parts_done=NULL WHERE id=?", (sid,))
    return {"job": worker.enqueue_sub(sid, "web")}


@app.delete("/api/subs/{sid}")
async def delete_sub(sid: int):
    db.x("UPDATE items SET subscription_id=NULL WHERE subscription_id=?", (sid,))
    db.x("DELETE FROM subscriptions WHERE id=?", (sid,))
    (config.ARCHIVES / f"sub_{sid}.sqlite").unlink(missing_ok=True)
    return {"ok": True}


# ------------------------------------------------------------------ API: ajustes

COOKIE_SITES = sorted(set(COOKIE_FILE.values()))


@app.get("/api/settings")
async def settings():
    cookies = []
    for name in sorted(set(COOKIE_SITES) | {p.stem for p in COOKIES.glob("*.txt")}):
        p = COOKIES / f"{name}.txt"
        cookies.append({"site": name, "present": p.exists(),
                        "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
                        if p.exists() else None,
                        "bad": db.get_setting(f"cookies_bad:{name}") if p.exists() else None})
    return {
        "cookies": cookies,
        "telegram": {"configured": bool(config.TELEGRAM_TOKEN), **bot.info,
                     "allowed": sorted(config.TELEGRAM_ALLOWED_IDS)},
        "library": str(LIBRARY), "data": str(DATA), "trash_days": config.TRASH_DAYS,
        "public_url": config.PUBLIC_URL, "password": bool(UI_PASSWORD),
        "versions": db.get_setting("versions", ""),
        "version": config.VERSION, "commit": config.COMMIT,
        "browser_ua": db.get_setting("browser_ua", "") or "",
        "gdl_dev": db.get_setting("gdl_channel") == "dev",
        "ext_token": ext_token(),
        "hours": dict(zip(("enabled", "start", "end"), worker.active_hours())),
        "cobalt": await cobalt.status(),
        "pauses": [{"site": r["key"].split(":", 1)[1], "until": r["value"]}
                   for r in db.q("SELECT key, value FROM settings WHERE key LIKE 'ratelimit:%' AND value > ?",
                                 (now(),))],
    }


@app.post("/api/cookies/{site}")
async def upload_cookies(site: str, file: UploadFile):
    site = site.strip().lower().removeprefix("www.")
    if not site or "/" in site or "\\" in site or ".." in site:
        raise HTTPException(400, "Sitio no válido")
    content = await file.read()
    _save_cookies(site, content.decode("utf-8", "replace"))
    return {"ok": True}


def _save_cookies(site: str, text: str):
    """Guarda un cookies.txt (de la web o de la extensión) y deja todo listo para usarlo."""
    if "\t" not in text:
        raise HTTPException(400, "No parece un cookies.txt en formato Netscape")
    if (need := SESSION_COOKIE.get(site)) and f"\t{need}\t" not in text:
        raise HTTPException(400, f"Falta la cookie de sesión «{need}»: exporta las cookies con la sesión iniciada "
                                 f"en {site}")
    (COOKIES / f"{site}.txt").write_text(text, encoding="utf-8")
    db.set_setting(f"cookies_at:{site}", now())
    clear_cookies_bad(site)
    # gallery-dl guarda en caché la sesión anterior: con cookies nuevas puede seguir usándola y acabar
    # en «redirect to login page» (lo explica su autor; equivale a --clear-cache)
    shutil.rmtree(DATA / "cache" / "gallery-dl", ignore_errors=True)
    cobalt.restart()  # cobalt lee las cookies al arrancar


# ------------------------------------------------------------------ extensión del navegador
# La extensión «Criba · cookies» manda las cookies de Instagram / X cuando cambian. Se identifica con un
# token (Ajustes), no con la sesión de la web, así que estas rutas quedan fuera del inicio de sesión.

EXT_SITES = {"instagram.com", "x.com"}
EXT_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type, X-Criba-Token",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}


def ext_token(create: bool = False) -> str:
    tok = db.get_setting("ext_token", "") or ""
    if not tok and create:
        tok = secrets.token_urlsafe(24)
        db.set_setting("ext_token", tok)
    return tok


def _ext_check(request: Request):
    tok = ext_token()
    sent = request.headers.get("x-criba-token", "")
    if not tok or not hmac.compare_digest(sent.encode(), tok.encode()):
        raise HTTPException(401, "Token no válido (cópialo de Criba → Ajustes → Extensión)", headers=EXT_CORS)


def _netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Enviado por la extensión «Criba · cookies»"]
    for c in cookies:
        name, value = str(c.get("name", "")), str(c.get("value", ""))
        if not name or "\t" in name + value or "\n" in name + value:
            continue
        host_only = bool(c.get("hostOnly"))
        domain = str(c.get("domain", "")).strip()
        if not host_only and not domain.startswith("."):
            domain = "." + domain
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([prefix + domain, "FALSE" if host_only else "TRUE", str(c.get("path") or "/"),
                                "TRUE" if c.get("secure") else "FALSE", str(int(c.get("expirationDate") or 0)),
                                name, value]))
    return "\n".join(lines) + "\n"


@app.options("/api/ext/{rest:path}")
async def ext_preflight(rest: str):
    return Response(status_code=204, headers=EXT_CORS)


@app.get("/api/ext/ping")
async def ext_ping(request: Request):
    _ext_check(request)
    return JSONResponse({"ok": True, "version": config.VERSION}, headers=EXT_CORS)


@app.post("/api/ext/cookies")
async def ext_cookies(request: Request):
    _ext_check(request)
    data = await request.json()
    site = str(data.get("site", "")).lower()
    if site not in EXT_SITES:
        raise HTTPException(400, "Sitio no admitido", headers=EXT_CORS)
    cookies = [c for c in data.get("cookies") or [] if str(c.get("domain", "")).lstrip(".").endswith(site)]
    try:
        _save_cookies(site, _netscape(cookies))
    except HTTPException as e:
        raise HTTPException(e.status_code, e.detail, headers=EXT_CORS)
    if ua := " ".join(str(data.get("user_agent") or "").split())[:400]:
        db.set_setting("browser_ua", ua)  # mismo navegador que las cookies
    return JSONResponse({"ok": True, "count": len(cookies)}, headers=EXT_CORS)


@app.post("/api/ext/token")
async def ext_new_token():
    """Genera (o regenera) el token de la extensión. Este sí exige la sesión de la web."""
    db.set_setting("ext_token", "")
    return {"token": ext_token(create=True)}


@app.get("/extension.zip")
async def extension_zip():
    """La extensión empaquetada, lista para cargar en Chrome/Edge/Brave o firmar para Firefox."""
    import io
    import zipfile
    buf = io.BytesIO()
    src = Path(__file__).parent.parent / "extension"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src).as_posix())
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="criba-cookies.zip"'})


@app.post("/api/settings/hours")
async def set_hours(enabled: bool = Body(True), start: int = Body(9), end: int = Body(23)):
    if not (0 <= start <= 23 and 0 <= end <= 24):
        raise HTTPException(400, "Horas no válidas")
    db.set_setting("hours_enabled", "1" if enabled else "0")
    db.set_setting("hours_from", str(start))
    db.set_setting("hours_to", str(end))
    return {"enabled": enabled, "start": start, "end": end}


@app.post("/api/settings/gdl_channel")
async def set_gdl_channel(dev: bool = Body(False, embed=True)):
    db.set_setting("gdl_channel", "dev" if dev else "stable")
    return {"dev": dev}


@app.post("/api/settings/ua")
async def set_browser_ua(ua: str = Body("", embed=True)):
    ua = " ".join(ua.split())[:400]
    db.set_setting("browser_ua", ua)
    return {"ua": ua}


@app.delete("/api/ratelimit/{site}")
async def resume_site(site: str):
    clear_ratelimit(site)
    return {"ok": True}


# Cookie que indica sesión iniciada en cada sitio (sin ella las cookies no sirven)
SESSION_COOKIE = {"instagram.com": "sessionid", "x.com": "auth_token"}


@app.delete("/api/cookies/{site}")
async def delete_cookies(site: str):
    p = COOKIES / f"{site}.txt"
    if p.parent != COOKIES:
        raise HTTPException(400, "Sitio no válido")
    p.unlink(missing_ok=True)
    clear_cookies_bad(site)
    cobalt.restart()
    return {"ok": True}


@app.post("/api/tools/update")
async def tools_update():
    out = await update_tools()
    db.set_setting("versions", out.splitlines()[-1])
    return {"output": out}


@app.get("/api/tools/versions")
async def versions():
    v = await tool_versions()
    db.set_setting("versions", v)
    return {"versions": v, "checked": now()}
