"""Descargas con gallery-dl / yt-dlp e ingesta en la cola de revisión."""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .config import ARCHIVES, COOKIES, DEVICE, DEVICE_HOURS, INBOX, PYUPD, TMP, kind_of
from .db import db, now
from .library import clean_name, unique_path

log = logging.getLogger("criba.dl")

# Al empezar a seguir: cuántos archivos baja sin descargar ya existentes como "vistos"
BASELINE_SCAN = 300
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


async def _run(args: list[str], timeout: int = 3600) -> tuple[int, str]:
    log.info("Ejecutando: %s", " ".join(args[2:]))
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "Tiempo de espera agotado"
    return proc.returncode, out.decode("utf-8", "replace")


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
    code, out = await _run(_gdl_args(url, dest) + [url])
    logs = [f"$ gallery-dl\n{out}"]
    if _media_files(dest):
        return code, "\n".join(logs)
    code, out = await _run(_ytdlp_args(url, dest))
    logs.append(f"$ yt-dlp\n{out}")
    if _media_files(dest):
        return code, "\n".join(logs)
    code, out = await _run(_gdl_args(url, dest) + ["-o", "extractor.generic.enabled=true", url])
    logs.append(f"$ gallery-dl (genérico)\n{out}")
    return code, "\n".join(logs)


def _archive_count(path: Path) -> int:
    try:
        with sqlite3.connect(path) as c:
            return c.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    except sqlite3.Error:
        return 0


async def download_subscription(sub: dict, dest: Path) -> tuple[int, str, str]:
    """Devuelve (código, log, nota). Usa un archivo de descargas por seguimiento para bajar solo lo nuevo."""
    archive = ARCHIVES / f"sub_{sub['id']}.sqlite"
    args = _gdl_args(sub["url"], dest) + ["--download-archive", str(archive)]
    note = ""
    if not sub["baseline_done"]:
        n = sub["initial_count"]
        if n == 0:  # solo lo nuevo: marcar lo existente como visto sin descargarlo
            code, out = await _run(args + ["--no-download", "--range", f"1-{BASELINE_SCAN}", sub["url"]])
            note = f"Marcados {_archive_count(archive)} archivos existentes como vistos"
            return code, out, note
        if n < 0:  # todo el histórico
            code, out = await _run(args + [sub["url"]], timeout=12 * 3600)
            return code, out, note
        # últimos N: se descargan y el resto se marca como visto para no bajarlo en la siguiente pasada
        code, out = await _run(args + ["--range", f"1-{n}", sub["url"]], timeout=6 * 3600)
        code2, out2 = await _run(args + ["--no-download", "--range", f"1-{BASELINE_SCAN}", sub["url"]])
        return code or code2, f"{out}\n$ marcar el resto como visto\n{out2}", note
    code, out = await _run(args + ["-o", f"skip=abort:{SKIP_ABORT}", sub["url"]])
    return code, out, note


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
           sub: dict | None = None, defaults: dict | None = None) -> tuple[list[int], int]:
    """Mueve los archivos descargados a la bandeja y los registra. Devuelve (ids nuevos, duplicados)."""
    new_ids, dups = [], 0
    site = site_of(source_url) if source_url else "telegram"
    for media in _media_files(folder):
        sha = _sha256(media)
        if db.one("SELECT id FROM items WHERE sha256=?", (sha,)):
            media.unlink(missing_ok=True)
            dups += 1
            continue
        m = _meta(_sidecar(media), site)
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
