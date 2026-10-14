"""Criba: API web + arranque del bot, la cola de descargas y el planificador."""
import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import bot, config, library, tgreview, worker
from .config import COOKIES, DATA, LIBRARY, UI_PASSWORD
from .db import db, now
from .downloader import COOKIE_FILE, tool_versions, update_tools
from .subscriptions import MIN_INTERVAL, add_subscription, parse_initial
from .thumbs import make_thumb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("criba")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker.recover()
    tasks = [asyncio.create_task(worker.worker("link")), asyncio.create_task(worker.worker("sub")),
             asyncio.create_task(worker.scheduler())]
    await bot.start()
    yield
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
    if not UI_PASSWORD or request.url.path in PUBLIC_PATHS or _valid_session(request.cookies.get(COOKIE)):
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

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


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
                               "author", "caption", "posted_at", "subscription_id", "created_at", "status", "dest")}


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


@app.get("/api/jobs")
async def jobs(limit: int = 50):
    rows = db.q("SELECT j.*, s.name sub_name FROM jobs j LEFT JOIN subscriptions s ON s.id=j.subscription_id "
                "ORDER BY j.id DESC LIMIT ?", (min(limit, 300),))
    return {"jobs": rows}


@app.delete("/api/jobs/{jid}")
async def delete_job(jid: int):
    """Quita una descarga terminada o fallida de la lista (no toca los archivos ya en la cola)."""
    return {"deleted": db.x("DELETE FROM jobs WHERE id=? AND status IN ('done','error')", (jid,))}


@app.post("/api/jobs/clear")
async def clear_jobs(status: str = Body("error", embed=True)):
    if status not in ("error", "done", "finished"):
        raise HTTPException(400, "Estado no válido")
    states = ("done", "error") if status == "finished" else (status, status)
    return {"deleted": db.x("DELETE FROM jobs WHERE status IN (?, ?)", states)}


@app.post("/api/jobs/{jid}/retry")
async def retry(jid: int):
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise HTTPException(404, "No existe")
    if j["kind"] == "sub":
        return {"job": worker.enqueue_sub(j["subscription_id"], "web")}
    return {"job": worker.enqueue_link(j["url"], "web")}


# ------------------------------------------------------------------ API: seguimientos

@app.get("/api/subs")
async def subs():
    return {"subs": db.q("SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.subscription_id=s.id AND "
                         "i.status='pending') pending FROM subscriptions s ORDER BY s.name COLLATE NOCASE")}


@app.post("/api/subs")
async def create_sub(url: str = Body(...), name: str | None = Body(None), interval_min: int | None = Body(None),
                     initial: str = Body("nuevo"), folder: str | None = Body(None)):
    try:
        return add_subscription(url, name, interval_min, parse_initial(initial), folder)
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
    if "folder" in data:
        fields["folder"] = (data["folder"] or "").strip() or None
    if fields:
        db.x(f"UPDATE subscriptions SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), sid))
    return db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))


@app.post("/api/subs/{sid}/check")
async def check_sub(sid: int):
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
                        if p.exists() else None})
    return {
        "cookies": cookies,
        "telegram": {"configured": bool(config.TELEGRAM_TOKEN), **bot.info,
                     "allowed": sorted(config.TELEGRAM_ALLOWED_IDS)},
        "library": str(LIBRARY), "data": str(DATA), "trash_days": config.TRASH_DAYS,
        "public_url": config.PUBLIC_URL, "password": bool(UI_PASSWORD),
        "versions": db.get_setting("versions", ""),
    }


@app.post("/api/cookies/{site}")
async def upload_cookies(site: str, file: UploadFile):
    site = site.strip().lower().removeprefix("www.")
    if not site or "/" in site or "\\" in site or ".." in site:
        raise HTTPException(400, "Sitio no válido")
    content = await file.read()
    text = content.decode("utf-8", "replace")
    if "\t" not in text:
        raise HTTPException(400, "No parece un cookies.txt en formato Netscape")
    (COOKIES / f"{site}.txt").write_text(text, encoding="utf-8")
    return {"ok": True}


@app.delete("/api/cookies/{site}")
async def delete_cookies(site: str):
    p = COOKIES / f"{site}.txt"
    if p.parent != COOKIES:
        raise HTTPException(400, "Sitio no válido")
    p.unlink(missing_ok=True)
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
