"""Operaciones de ficheros: carpetas de la biblioteca, mover, papelera y restaurar."""
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import INBOX, LIBRARY, THUMBS, TRASH, TRASH_DAYS
from .db import db, now

_folders_cache: tuple[float, list[str]] | None = None


class PathError(ValueError):
    pass


def safe_join(root: Path, rel: str) -> Path:
    rel = (rel or "").replace("\\", "/").strip("/")
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise PathError("Ruta fuera de la biblioteca")
    return p


def clean_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "archivo"


def list_folders() -> list[str]:
    """Todas las carpetas de la biblioteca (rutas relativas), cacheado 60 s."""
    global _folders_cache
    if _folders_cache and time.time() - _folders_cache[0] < 60:
        return _folders_cache[1]
    out = []
    for dirpath, dirnames, _ in os.walk(LIBRARY):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "@")))
        rel = Path(dirpath).relative_to(LIBRARY).as_posix()
        if rel != ".":
            out.append(rel)
    _folders_cache = (time.time(), out)
    return out


def invalidate_folders():
    global _folders_cache
    _folders_cache = None


def create_folder(parent: str, name: str) -> str:
    name = clean_name(name)
    target = safe_join(LIBRARY, f"{parent}/{name}" if parent else name)
    target.mkdir(parents=True, exist_ok=True)
    invalidate_folders()
    return target.relative_to(LIBRARY).as_posix()


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    if not p.exists():
        return p
    stem, ext = os.path.splitext(name)
    i = 2
    while (folder / f"{stem} ({i}){ext}").exists():
        i += 1
    return folder / f"{stem} ({i}){ext}"


def item_path(item: dict) -> Path:
    base = {"pending": INBOX, "trashed": TRASH, "kept": LIBRARY}.get(item["status"])
    if base is None:
        raise PathError("El elemento ya no tiene fichero")
    return base / item["file"]


def move_items(ids: list[int], folder: str) -> list[dict]:
    dest_dir = safe_join(LIBRARY, folder)
    if not dest_dir.is_dir():
        raise PathError(f"No existe la carpeta {folder}")
    done = []
    for iid in ids:
        it = db.one("SELECT * FROM items WHERE id=? AND status='pending'", (iid,))
        if not it:
            continue
        src = item_path(it)
        dst = unique_path(dest_dir, clean_name(it["save_name"] or it["orig_name"]))
        shutil.move(str(src), str(dst))
        rel = dst.relative_to(LIBRARY).as_posix()
        db.x("UPDATE items SET status='kept', file=?, dest=?, decided_at=? WHERE id=?",
             (rel, dest_dir.relative_to(LIBRARY).as_posix() if dest_dir != LIBRARY else "", now(), iid))
        done.append({"id": iid, "path": rel})
    _remember_folder(folder)
    return done


def trash_items(ids: list[int]) -> int:
    n = 0
    for iid in ids:
        it = db.one("SELECT * FROM items WHERE id=? AND status='pending'", (iid,))
        if not it:
            continue
        src = item_path(it)
        if src.exists():
            dst = unique_path(TRASH, Path(it["file"]).name)
            shutil.move(str(src), str(dst))
            rel = dst.name
        else:
            rel = it["file"]
        db.x("UPDATE items SET status='trashed', file=?, decided_at=? WHERE id=?", (rel, now(), iid))
        n += 1
    return n


def restore_items(ids: list[int]) -> int:
    """Devuelve a la cola elementos borrados (papelera) o archivados por error."""
    n = 0
    for iid in ids:
        it = db.one("SELECT * FROM items WHERE id=? AND status IN ('trashed','kept')", (iid,))
        if not it:
            continue
        src = item_path(it)
        if not src.exists():
            continue
        dst = unique_path(INBOX, f"{iid}_{clean_name(it['orig_name'])}")
        shutil.move(str(src), str(dst))
        db.x("UPDATE items SET status='pending', file=?, dest=NULL, decided_at=NULL WHERE id=?", (dst.name, iid))
        n += 1
    return n


def purge_trash() -> int:
    """Borra definitivamente lo que lleva más de TRASH_DAYS en la papelera."""
    limit = (datetime.now(timezone.utc) - timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")
    n = 0
    for it in db.q("SELECT * FROM items WHERE status='trashed' AND decided_at < ?", (limit,)):
        (TRASH / it["file"]).unlink(missing_ok=True)
        (THUMBS / f"{it['id']}.jpg").unlink(missing_ok=True)
        db.x("UPDATE items SET status='deleted', file='' WHERE id=?", (it["id"],))
        n += 1
    return n


def recent_folders() -> list[str]:
    raw = db.get_setting("recent_folders", "")
    return [f for f in raw.split("\n") if f]


def _remember_folder(folder: str):
    rec = [f for f in recent_folders() if f != folder]
    db.set_setting("recent_folders", "\n".join([folder] + rec[:11]))
