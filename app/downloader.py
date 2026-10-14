"""Descargas con gallery-dl / yt-dlp e ingesta en la cola de revisión."""
import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import ARCHIVES, COOKIES, DEVICE, DEVICE_HOURS, INBOX, PYUPD, TMP, kind_of
from .db import db, now
from .library import clean_name, unique_path

log = logging.getLogger("criba.dl")

# Al empezar a seguir: cuántos archivos ya existentes se marcan como "vistos" sin descargarlos.
# Basta con cubrir de sobra los SKIP_ABORT primeros (y las fijadas): cada comprobación se detiene
# en cuanto encuentra SKIP_ABORT ya vistos seguidos, así que nunca llega más abajo.
BASELINE_SCAN = 15  # > SKIP_ABORT + las 3 publicaciones fijadas que permite Instagram
# Archivos ya vistos seguidos tras los que se deja de recorrer el perfil
SKIP_ABORT = 6

SITES = {
    "instagram.com": "instagram",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "reddit.com": "reddit",
    "redd.it": "reddit",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "bsky.app": "bluesky",
    "facebook.com": "facebook",
    "threads.net": "threads",
    "threads.com": "threads",
    "pinterest.com": "pinterest",
    "tumblr.com": "tumblr",
}
# Fichero de cookies que se usa para cada sitio
COOKIE_FILE = {
    "instagram": "instagram.com", "twitter": "x.com", "reddit": "reddit.com", "tiktok": "tiktok.com",
    "youtube": "youtube.com", "facebook": "facebook.com", "threads": "threads.net",
    "pinterest": "pinterest.com", "tumblr": "tumblr.com", "bluesky": "bsky.app",
}


def site_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for dom, site in SITES.items():
        if host == dom or host.endswith("." + dom):
            return site
    return host.removeprefix("www.") or "web"


def cookies_for(url: str) -> Path | None:
    site = site_of(url)
    name = COOKIE_FILE.get(site, site)
    p = COOKIES / f"{name}.txt"
    return p if p.exists() and p.stat().st_size else None


def _tool(module: str) -> list[str]:
    return [sys.executable, "-m", module]


def _env() -> dict:
    env = dict(os.environ)
    if PYUPD.exists():
        env["PYTHONPATH"] = str(PYUPD) + os.pathsep + env.get("PYTHONPATH", "")
    return env


# Trabajo en curso: lo fija el worker para que _run vaya guardando el registro en directo
current_job: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_job", default=None)
LIVE_EVERY = 3  # segundos entre volcados del registro en directo
# Tiempo máximo de cada paso de una comprobación (salvo «todo el histórico»)
STEP_TIMEOUT = 30 * 60
LINK_TIMEOUT = 2 * 3600  # un enlace puede ser un vídeo largo

# Procesos en marcha por trabajo y trabajos que se han pedido cancelar
_procs: dict[int, asyncio.subprocess.Process] = {}
_cancelled: set[int] = set()


class JobCancelled(Exception):
    pass


def cancel_job(job_id: int) -> bool:
    """Marca el trabajo como cancelado y mata lo que esté ejecutando. Devuelve si había proceso."""
    _cancelled.add(job_id)
    proc = _procs.get(job_id)
    if proc and proc.returncode is None:
        proc.kill()
        return True
    return False


def forget_cancel(job_id: int):
    _cancelled.discard(job_id)


async def _run(args: list[str], timeout: int = STEP_TIMEOUT) -> tuple[int, str]:
    job_id = current_job.get()
    if job_id in _cancelled:
        raise JobCancelled
    log.info("Ejecutando: %s", " ".join(args[2:]))
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env())
    if job_id:
        _procs[job_id] = proc
    try:
        return await _collect(proc, job_id, timeout)
    finally:
        _procs.pop(job_id, None)


async def _collect(proc, job_id: int | None, timeout: int) -> tuple[int, str]:
    row = db.one("SELECT log FROM jobs WHERE id=?", (job_id,)) if job_id else None
    prefix = (row or {}).get("log") or ""
    lines: list[str] = []
    loop = asyncio.get_running_loop()
    deadline, last_dump = loop.time() + timeout, 0.0

    async def read():
        nonlocal last_dump
        while raw := await proc.stdout.readline():
            lines.append(raw.decode("utf-8", "replace").rstrip("\n"))
            if job_id and loop.time() - last_dump > LIVE_EVERY:
                last_dump = loop.time()
                db.x("UPDATE jobs SET log=? WHERE id=?", (tail(prefix + "\n".join(lines)), job_id))
        await proc.wait()

    try:
        await asyncio.wait_for(read(), max(1, deadline - loop.time()))
    except asyncio.TimeoutError:
        proc.kill()
        lines.append(f"Tiempo de espera agotado ({timeout // 60} min)")
        return -1, "\n".join(lines)
    if job_id in _cancelled:
        raise JobCancelled
    out = "\n".join(lines)
    if job_id:  # lo siguiente que se ejecute en este trabajo se añade detrás
        db.x("UPDATE jobs SET log=? WHERE id=?", (tail(prefix + out + "\n"), job_id))
    return proc.returncode, out


def _gdl_args(url: str, dest: Path) -> list[str]:
    args = _tool("gallery_dl") + ["-D", str(dest), "--write-metadata"]
    if c := cookies_for(url):
        args += ["--cookies", str(c)]
    return args


def _ytdlp_args(url: str, dest: Path) -> list[str]:
    args = _tool("yt_dlp") + [
        "--no-progress", "--write-info-json", "--no-mtime", "--merge-output-format", "mp4",
        "--playlist-end", "50", "-o", str(dest / "%(title).80B [%(id)s].%(ext)s"),
    ]
    if c := cookies_for(url):
        args += ["--cookies", str(c)]
    return args + [url]


def _media_files(folder: Path) -> list[Path]:
    files = [p for p in folder.rglob("*") if p.is_file() and kind_of(p.name) in ("image", "video")]
    return sorted(files)


async def download_link(url: str, dest: Path) -> tuple[int, str]:
    """gallery-dl primero (redes sociales, galerías, enlaces directos), luego yt-dlp, luego extractor genérico."""
    code, out = await _run(_gdl_args(url, dest) + [url], LINK_TIMEOUT)
    logs = [f"$ gallery-dl\n{out}"]
    if _media_files(dest):
        return code, "\n".join(logs)
    code, out = await _run(_ytdlp_args(url, dest), LINK_TIMEOUT)
    logs.append(f"$ yt-dlp\n{out}")
    if _media_files(dest):
        return code, "\n".join(logs)
    code, out = await _run(_gdl_args(url, dest) + ["-o", "extractor.generic.enabled=true", url], LINK_TIMEOUT)
    logs.append(f"$ gallery-dl (genérico)\n{out}")
    return code, "\n".join(logs)


def _archive_count(path: Path) -> int:
    try:
        with sqlite3.connect(path) as c:
            return c.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    except sqlite3.Error:
        return 0


# Secciones de un perfil de Instagram (opción «include» de gallery-dl)
IG_PARTS = {"posts": "Publicaciones", "reels": "Reels", "stories": "Historias", "highlights": "Destacadas",
            "tagged": "Etiquetadas"}
IG_DEFAULT = ["posts", "reels", "stories"]
# Sin sesión iniciada Instagram solo enseña las publicaciones (comprobado con gallery-dl 1.32.16:
# reels da KeyError, etiquetadas redirige al login, historias y destacadas no devuelven nada)
IG_LOGIN_PARTS = {"reels", "stories", "highlights", "tagged"}


def sub_parts(sub: dict) -> list[str]:
    """Secciones que se miran de un seguimiento (solo Instagram; vacío en el resto de sitios)."""
    if sub["site"] != "instagram":
        return []
    parts = [p for p in (sub.get("parts") or "").split(",") if p in IG_PARTS]
    return parts or list(IG_DEFAULT)


def _labels(parts) -> str:
    return ", ".join(IG_PARTS[p] for p in IG_PARTS if p in parts)


async def download_subscription(sub: dict, dest: Path) -> tuple[int, str, str, str | None]:
    """Devuelve (código, log, nota, secciones ya marcadas). Un archivo de descargas por seguimiento
    hace que solo se baje lo nuevo."""
    archive = ARCHIVES / f"sub_{sub['id']}.sqlite"
    args = _gdl_args(sub["url"], dest) + ["--download-archive", str(archive)]
    notes, logs = [], []
    parts = sub_parts(sub)
    if parts and not cookies_for(sub["url"]) and IG_LOGIN_PARTS & set(parts):
        skipped = IG_LOGIN_PARTS & set(parts)
        parts = [p for p in parts if p not in skipped]
        notes.append(f"Sin cookies de Instagram: se saltan {_labels(skipped)}")
    if sub_parts(sub) and not parts:
        return 1, "No hay nada que mirar sin cookies", "; ".join(notes), sub.get("parts_done")
    include = ["-o", f"include={','.join(parts)}"] if parts else []

    async def mark_seen(which: list[str] | None, upto: int = BASELINE_SCAN) -> int:
        """Marca como visto lo ya publicado sin descargarlo. Las historias nunca se marcan: duran
        24 h, así que siempre son recientes y se descargan."""
        if which:
            which = [p for p in which if p != "stories"]
            if not which:
                return 0
        inc = ["-o", f"include={','.join(which)}"] if which else []
        what = f" de {_labels(which)}" if which else ""
        before = _archive_count(archive)
        c, out = await _run(args + inc + ["--no-download", "--range", f"1-{upto}", sub["url"]])
        notes.append(f"Marcados {_archive_count(archive) - before} archivos anteriores{what} como vistos")
        logs.append(f"$ marcar como visto{what}\n{out}")
        return c

    async def catch_up(which: list[str] | None) -> int:
        """Secciones recién activadas o «Recuperar desde que lo sigo»: se descargan los BASELINE_SCAN
        primeros y el worker descarta al ingerir lo anterior a seguir el perfil. Sin --filter de fecha:
        gallery-dl lo aplica antes que --range y recorrería el perfil entero buscando coincidencias."""
        inc = ["-o", f"include={','.join(which)}"] if which else []
        what = f" de {_labels(which)}" if which else ""
        c, out = await _run(args + inc + ["--range", f"1-{BASELINE_SCAN}", sub["url"]])
        logs.append(f"$ revisar{what} desde que lo sigues\n{out}")
        return c

    async def stories_now() -> int:
        """Al activar las historias, bajar ya las que haya (si no, pueden caducar antes de la siguiente)."""
        if "stories" not in parts:
            return 0
        c, out = await _run(args + ["-o", "include=stories", sub["url"]])
        logs.append(f"$ historias actuales\n{out}")
        return c

    if not sub["baseline_done"]:
        n = sub["initial_count"]
        if n == 0:  # solo lo nuevo: marcar lo existente como visto sin descargarlo
            code = await mark_seen(parts or None)
            code = await stories_now() or code
        elif n < 0:  # todo el histórico
            code, out = await _run(args + include + [sub["url"]], timeout=12 * 3600)
            logs.append(out)
        else:  # últimos N: se descargan y el resto se marca como visto para no bajarlo en la siguiente pasada
            code, out = await _run(args + include + ["--range", f"1-{n}", sub["url"]], timeout=6 * 3600)
            logs.append(out)
            code = await mark_seen(parts or None, n + BASELINE_SCAN) or code
        return code, "\n".join(logs), "; ".join(notes), ",".join(parts) or None

    # secciones activadas después (p. ej. reels o historias) o «Recuperar desde que lo sigo»
    done = [p for p in (sub.get("parts_done") or "").split(",") if p]
    if not archive.exists():  # recuperar (o archivo perdido): empezar de nuevo
        done = []
        if not parts and await catch_up(None):
            return 1, "\n".join(logs), "; ".join(notes), None
    new_parts = [p for p in parts if p not in done]
    if parts and new_parts:
        if await catch_up(new_parts) == 0:
            done += new_parts
    # Sin --filter de fecha: lo filtrado no cuenta para skip=abort y recorrería el perfil entero.
    # Lo anterior a seguir el perfil (p. ej. una publicación antigua recién fijada) se descarta al ingerir.
    code, out = await _run(args + include + ["-o", f"skip=abort:{SKIP_ABORT}", sub["url"]])
    logs.append(out)
    return code, "\n".join(logs), "; ".join(notes), ",".join(done) or None


def _utc(iso: str) -> datetime:
    """Fecha ISO, con o sin zona (las de gallery-dl vienen sin zona y son UTC), en UTC."""
    d = datetime.fromisoformat(iso)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


# ---------------------------------------------------------------- metadatos

def _sidecar(media: Path) -> dict:
    """gallery-dl escribe archivo.ext.json; yt-dlp escribe archivo.info.json."""
    for cand in (media.with_name(media.name + ".json"), media.with_suffix(".info.json")):
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
    return {}


def _first(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
    return None


def _meta(d: dict, site: str) -> dict:
    author_d = d.get("author") if isinstance(d.get("author"), dict) else {}
    user_d = d.get("user") if isinstance(d.get("user"), dict) else {}
    owner_d = d.get("owner") if isinstance(d.get("owner"), dict) else {}
    author = _first(
        d.get("username"), author_d.get("name"), author_d.get("handle"), author_d.get("nick"),
        d.get("author") if isinstance(d.get("author"), str) else None,
        d.get("uploader_id"), d.get("uploader"), d.get("channel"),
        user_d.get("name"), user_d.get("username"), owner_d.get("username"), d.get("blog_name"),
    )
    if author:
        author = author.lstrip("@")
    post_id = _first(d.get("post_shortcode"), d.get("shortcode"), d.get("tweet_id"), d.get("post_id"),
                     d.get("id"), d.get("display_id"))
    caption = _first(d.get("content"), d.get("text"), d.get("title"), d.get("description"),
                     d.get("selftext"), d.get("fulltitle"))
    post_url = _first(d.get("post_url"), d.get("webpage_url"))
    if not post_url and site == "bluesky" and d.get("post_id") and author:
        post_url = f"https://bsky.app/profile/{author}/post/{d['post_id']}"
    if not post_url and d.get("permalink"):
        post_url = "https://www.reddit.com" + d["permalink"] if d["permalink"].startswith("/") else d["permalink"]
    if not post_url and site == "twitter" and d.get("tweet_id") and author:
        post_url = f"https://x.com/{author}/status/{d['tweet_id']}"
    date = None
    if isinstance(d.get("date"), str):
        date = d["date"].replace(" ", "T")
    elif d.get("timestamp"):
        try:
            date = datetime.fromtimestamp(float(d["timestamp"])).isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            pass
    elif isinstance(d.get("upload_date"), str) and len(d["upload_date"]) == 8:
        u = d["upload_date"]
        date = f"{u[:4]}-{u[4:6]}-{u[6:]}"
    num = d.get("num") if isinstance(d.get("num"), int) else None
    count = d.get("count") if isinstance(d.get("count"), int) else None
    return {"author": author, "post_id": post_id, "caption": (caption or "")[:3000] or None,
            "post_url": post_url, "date": date, "num": num, "count": count}


def _save_name(m: dict, media: Path, sha: str) -> str:
    date = (m["date"] or now())[:10]
    author = re.sub(r"[^\w.-]+", "", m["author"] or "") or "desconocido"
    pid = re.sub(r"[^\w-]+", "", m["post_id"] or "")[:40] or sha[:10]
    parts = [date, author, pid]
    if m["num"] and (m["num"] > 1 or (m["count"] or 0) > 1):
        parts.append(str(m["num"]))
    return clean_name("_".join(parts)) + media.suffix.lower()


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest(folder: Path, source: str, source_url: str | None, job_id: int | None = None,
           sub: dict | None = None, defaults: dict | None = None, min_date: str | None = None,
           stats: dict | None = None) -> tuple[list[int], int]:
    """Mueve los archivos descargados a la bandeja y los registra. Devuelve (ids nuevos, duplicados).
    Con min_date se descarta lo publicado antes (y se cuenta en stats["old"])."""
    new_ids, dups = [], 0
    site = site_of(source_url) if source_url else "telegram"
    limit = _utc(min_date) if min_date else None
    for media in _media_files(folder):
        sha = _sha256(media)
        if db.one("SELECT id FROM items WHERE sha256=?", (sha,)):
            media.unlink(missing_ok=True)
            dups += 1
            continue
        m = _meta(_sidecar(media), site)
        if limit and m["date"] and _utc(m["date"]) < limit:
            media.unlink(missing_ok=True)
            if stats is not None:
                stats["old"] = stats.get("old", 0) + 1
            continue
        for k, v in (defaults or {}).items():
            m[k] = m.get(k) or v
        iid = db.x(
            """INSERT INTO items(file, orig_name, save_name, kind, size, sha256, source, source_url, post_url,
                                 author, caption, posted_at, subscription_id, job_id, created_at)
               VALUES('', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (media.name, _save_name(m, media, sha), kind_of(media.name), media.stat().st_size, sha, source,
             source_url, m["post_url"], m["author"], m["caption"], m["date"],
             sub["id"] if sub else None, job_id, now()))
        dst = unique_path(INBOX, f"{iid}_{clean_name(media.name)}")
        shutil.move(str(media), str(dst))
        db.x("UPDATE items SET file=? WHERE id=?", (dst.name, iid))
        new_ids.append(iid)
    return new_ids, dups


def device_dir(job_id: int) -> Path:
    return DEVICE / f"job_{job_id}"


def prepare_device_files(folder: Path, source_url: str, dest: Path) -> list[Path]:
    """Deja en dest los archivos descargados con nombre fecha_autor_id.ext, listos para el navegador."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    site = site_of(source_url)
    out = []
    for media in _media_files(folder):
        m = _meta(_sidecar(media), site)
        name = _save_name(m, media, hashlib.sha1(media.name.encode()).hexdigest())
        dst = unique_path(dest, name)
        shutil.move(str(media), str(dst))
        out.append(dst)
    if not out:
        dest.rmdir()
    return out


def device_files(job_id: int) -> list[Path]:
    d = device_dir(job_id)
    return sorted(p for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def purge_device():
    limit = time.time() - DEVICE_HOURS * 3600
    for d in DEVICE.iterdir():
        if d.is_dir() and d.stat().st_mtime < limit:
            shutil.rmtree(d, ignore_errors=True)


def job_tmp(job_id: int) -> Path:
    p = TMP / f"job_{job_id}"
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    return p


def cleanup_tmp(p: Path):
    shutil.rmtree(p, ignore_errors=True)


def tail(text: str, n: int = 6000) -> str:
    return text if len(text) <= n else "…\n" + text[-n:]


def last_error_line(text: str) -> str:
    lines = [l for l in text.splitlines() if "error" in l.lower()]
    return (lines[-1] if lines else text.strip().splitlines()[-1] if text.strip() else "Error desconocido")[:300]


async def update_tools() -> str:
    """Instala la última versión de gallery-dl y yt-dlp en /data/pyupd (tiene prioridad sobre la de la imagen)."""
    code, out = await _run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                            "-q", "-U", "--target", str(PYUPD), "gallery-dl", "yt-dlp[default]"], timeout=900)
    versions = await tool_versions()
    return f"{'OK' if code == 0 else 'Error'}\n{out.strip()}\n{versions}".strip()


async def tool_versions() -> str:
    _, g = await _run(_tool("gallery_dl") + ["--version"], timeout=60)
    _, y = await _run(_tool("yt_dlp") + ["--version"], timeout=60)
    return f"gallery-dl {g.strip()} · yt-dlp {y.strip()}"
