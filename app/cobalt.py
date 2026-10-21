"""cobalt (https://github.com/imputnet/cobalt): descargador de reserva para enlaces sueltos.

Va dentro de la imagen y Criba lo arranca en 127.0.0.1:9000. Con COBALT_URL se usa otra instancia.
Descarga por caminos distintos a gallery-dl (p. ej. las versiones incrustables de Instagram), así que
a menudo funciona cuando gallery-dl o yt-dlp fallan. No recorre perfiles: solo enlaces.
"""
import asyncio
import json
import logging
import mimetypes
import os
import re
from pathlib import Path

import httpx

from .config import COOKIES, DATA

log = logging.getLogger("criba.cobalt")

HOME = Path("/cobalt")
PORT = 9000
EXTERNAL = os.environ.get("COBALT_URL", "").strip().rstrip("/")
API_KEY = os.environ.get("COBALT_API_KEY", "").strip()
COOKIE_JSON = DATA / "cobalt-cookies.json"

# cookies.txt de Criba → servicio de cobalt y cookies que necesita
COOKIE_MAP = {"instagram": ("instagram.com", None), "twitter": ("x.com", {"auth_token", "ct0"})}

_proc: asyncio.subprocess.Process | None = None
_runner: asyncio.Task | None = None
_restart_now = False


def base_url() -> str | None:
    if EXTERNAL:
        return EXTERNAL
    return f"http://127.0.0.1:{PORT}" if (HOME / "src" / "cobalt.js").exists() else None


def _netscape_pairs(path: Path, domain: str, only: set | None) -> list[str]:
    pairs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#HttpOnly_"):  # cookie HttpOnly, no comentario
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) < 7 or not f[0].lstrip(".").endswith(domain):
            continue
        name, value = f[5], f[6].strip()
        if only is None or name in only:
            pairs.append(f"{name}={value}")
    return pairs


def write_cookies():
    """Convierte las cookies de Criba al formato de cobalt (una cadena «a=b; c=d» por servicio)."""
    out = {}
    for service, (name, only) in COOKIE_MAP.items():
        p = COOKIES / f"{name}.txt"
        if p.exists() and (pairs := _netscape_pairs(p, name, only)):
            out[service] = ["; ".join(pairs)]
    COOKIE_JSON.write_text(json.dumps(out), encoding="utf-8")
    try:
        COOKIE_JSON.chmod(0o600)
    except OSError:
        pass


async def _run_forever():
    global _proc, _restart_now
    env = {**os.environ, "API_URL": f"http://127.0.0.1:{PORT}/", "API_PORT": str(PORT),
           "API_LISTEN_ADDRESS": "127.0.0.1", "COOKIE_PATH": str(COOKIE_JSON), "DURATION_LIMIT": "10800"}
    while True:
        write_cookies()
        _proc = await asyncio.create_subprocess_exec(
            "node", "src/cobalt", cwd=HOME, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        log.info("cobalt arrancado (pid %s)", _proc.pid)
        while raw := await _proc.stdout.readline():
            line = re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8", "replace")).rstrip()
            if line and not line.startswith("~"):
                log.debug("cobalt: %s", line)
        code = await _proc.wait()
        if _restart_now:  # reinicio pedido (cookies nuevas): sin espera
            _restart_now = False
            continue
        log.warning("cobalt se ha parado (código %s); se reinicia en 10 s", code)
        await asyncio.sleep(10)


async def start():
    global _runner
    if EXTERNAL or not base_url():
        return
    _runner = asyncio.create_task(_run_forever())


async def stop():
    if _runner:
        _runner.cancel()
    if _proc and _proc.returncode is None:
        _proc.terminate()


def restart():
    """Tras cambiar las cookies: cobalt las lee al arrancar."""
    global _restart_now
    if _proc and _proc.returncode is None:
        _restart_now = True
        _proc.terminate()  # _run_forever lo vuelve a lanzar al momento con las cookies nuevas


async def status() -> dict:
    url = base_url()
    if not url:
        return {"available": False}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            data = (await c.get(url + "/")).json().get("cobalt", {})
        return {"available": True, "ok": True, "version": data.get("version"), "external": bool(EXTERNAL)}
    except Exception as e:  # noqa: BLE001
        return {"available": True, "ok": False, "error": str(e)[:200], "external": bool(EXTERNAL)}


def _ext(name: str | None, content_type: str | None, kind: str | None) -> str:
    if name and Path(name).suffix:
        return Path(name).suffix
    guess = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) if content_type else None
    if guess in (".jpe", ".jpeg"):
        guess = ".jpg"
    return guess or (".mp4" if kind in ("video", "gif") else ".jpg")


async def _save(client: httpx.AsyncClient, url: str, dest: Path, stem: str, name: str | None,
                kind: str | None) -> Path:
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        path = dest / f"{stem}{_ext(name, r.headers.get('content-type'), kind)}"
        with open(path, "wb") as f:
            async for chunk in r.aiter_bytes(1 << 16):
                f.write(chunk)
    return path


async def download(url: str, dest: Path) -> tuple[bool, str]:
    """Descarga el enlace con cobalt en dest. Devuelve (si ha bajado algo, registro)."""
    base = base_url()
    if not base:
        return False, "cobalt no está disponible"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Api-Key {API_KEY}"
    body = {"url": url, "videoQuality": "max", "filenameStyle": "basic", "alwaysProxy": True}
    lines = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=600), follow_redirects=True) as c:
            r = await c.post(base + "/", headers=headers, json=body)
            data = r.json()
            status = data.get("status")
            lines.append(f"cobalt: {status}")
            saved = []
            if status in ("tunnel", "redirect"):
                saved.append(await _save(c, data["url"], dest, "cobalt", data.get("filename"), None))
            elif status == "picker":
                for k, item in enumerate(data.get("picker") or [], 1):
                    saved.append(await _save(c, item["url"], dest, f"cobalt_{k}", None, item.get("type")))
            elif status == "local-processing" and len(data.get("tunnel") or []) == 1:
                out = data.get("output") or {}
                saved.append(await _save(c, data["tunnel"][0], dest, "cobalt", out.get("filename"), None))
            elif status == "error":
                err = data.get("error") or {}
                lines.append(f"[error] cobalt: {err.get('code', 'error')} {err.get('context') or ''}".strip())
            else:
                lines.append(f"[error] cobalt: respuesta no soportada ({status})")
            for p in saved:  # para que la publicación original quede enlazada en la cola
                p.with_name(p.name + ".json").write_text(json.dumps({"webpage_url": url}), encoding="utf-8")
                lines.append(f"{p.name} ({p.stat().st_size} bytes)")
            return bool(saved), "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        lines.append(f"[error] cobalt: {e!r}"[:300])
        return False, "\n".join(lines)
