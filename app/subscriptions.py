"""Alta y normalización de seguimientos de perfiles."""
import re
from urllib.parse import urlparse

from .config import DEFAULT_INTERVAL
from .db import db, now
from .downloader import IG_PARTS, site_of


def clean_parts(parts) -> str | None:
    """Lista de secciones de Instagram válidas, en orden canónico, como texto separado por comas."""
    if parts is None:
        return None
    if isinstance(parts, str):
        parts = parts.split(",")
    return ",".join(p for p in IG_PARTS if p in set(parts)) or None


# Instagram y X vigilan más las consultas automáticas: intervalos más largos por defecto
SITE_INTERVAL = {"instagram": 360, "twitter": 180, "tiktok": 240}
MIN_INTERVAL = 15


def parse_initial(value) -> int:
    """'nuevo' → 0 (solo lo nuevo), 'todo' → -1, número → últimos N archivos."""
    v = str(value).strip().lower()
    if v in ("", "nuevo", "nuevos", "new", "0"):
        return 0
    if v in ("todo", "todos", "all", "-1"):
        return -1
    if v.isdigit():
        return int(v)
    raise ValueError("El modo inicial debe ser «nuevo», un número o «todo»")


def normalize(url: str) -> tuple[str, str, str]:
    """Devuelve (url, sitio, nombre)."""
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    p = urlparse(url)
    if not p.hostname:
        raise ValueError("URL no válida")
    site = site_of(url)
    parts = [x for x in p.path.split("/") if x]
    name = parts[-1] if parts else p.hostname
    if site == "twitter" and parts:
        name = "@" + parts[0]
        if len(parts) == 1:  # perfil → solo su pestaña de multimedia
            url = f"https://x.com/{parts[0]}/media"
    elif site == "instagram" and parts:
        name = "@" + parts[0]
    elif site == "reddit" and len(parts) >= 2 and parts[0] in ("u", "user", "r"):
        name = ("u/" if parts[0] != "r" else "r/") + parts[1]
    elif site == "tiktok" and parts and parts[0].startswith("@"):
        name = parts[0]
    elif site == "bluesky" and len(parts) >= 2 and parts[0] == "profile":
        name = "@" + parts[1]
    return url, site, name


def add_subscription(url: str, name: str | None = None, interval: int | None = None, initial: int = 0,
                     folder: str | None = None, parts=None) -> dict:
    url, site, auto_name = normalize(url)
    if db.one("SELECT id FROM subscriptions WHERE url=?", (url,)):
        raise ValueError("Ya sigues ese perfil")
    interval = max(MIN_INTERVAL, int(interval or SITE_INTERVAL.get(site, DEFAULT_INTERVAL)))
    sid = db.x("""INSERT INTO subscriptions(url,name,site,interval_min,initial_count,folder,parts,created_at)
                  VALUES(?,?,?,?,?,?,?,?)""",
               (url, (name or "").strip() or auto_name, site, interval, initial, folder or None,
                clean_parts(parts) if site == "instagram" else None, now()))
    from .worker import enqueue_sub
    enqueue_sub(sid, "alta")
    return db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
