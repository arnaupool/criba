"""«Solo imagen»: cambiar un vídeo de la cola por uno de sus fotogramas (fotos con música, etc.)."""
import shutil
import subprocess
import time
from pathlib import Path

from . import library
from .config import INBOX, THUMBS, TMP
from .db import db, now
from .downloader import _sha256
from .library import clean_name, unique_path


class FrameError(Exception):
    pass


def _extract(src: Path, t: float, out: Path):
    for ss in (max(t, 0.0), 0.0):  # si el instante pedido no da imagen (p. ej. pasado el final), el primero
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ss:.3f}", "-i", str(src),
                        "-frames:v", "1", "-q:v", "2", str(out)], timeout=120, check=False)
        if out.exists() and out.stat().st_size:
            return
    raise FrameError("No se ha podido sacar un fotograma de este vídeo")


def video_to_frame(iid: int, t: float = 0.0) -> int:
    """Crea en la cola un elemento imagen con el fotograma del segundo t y manda el vídeo a la papelera
    (se puede deshacer con frame_undo). Devuelve el id del elemento nuevo."""
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it or it["status"] != "pending" or it["kind"] != "video":
        raise FrameError("Solo se puede con vídeos pendientes de revisar")
    tmp = TMP / f"frame_{iid}_{int(time.time() * 1000)}.jpg"
    _extract(library.item_path(it), t, tmp)
    stem = lambda name: Path(name or "video").stem  # noqa: E731
    orig, save = stem(it["orig_name"]) + ".jpg", stem(it["save_name"] or it["orig_name"]) + ".jpg"
    new = db.x(
        """INSERT INTO items(file, orig_name, save_name, kind, size, sha256, source, source_url, post_url, author,
                             caption, posted_at, subscription_id, job_id, created_at, from_item)
           VALUES('', ?, ?, 'image', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (orig, save, tmp.stat().st_size, _sha256(tmp), it["source"], it["source_url"], it["post_url"],
         it["author"], it["caption"], it["posted_at"], it["subscription_id"], it["job_id"], now(), iid))
    dst = unique_path(INBOX, f"{new}_{clean_name(orig)}")
    shutil.move(str(tmp), str(dst))
    db.x("UPDATE items SET file=? WHERE id=?", (dst.name, new))
    library.trash_items([iid])
    # los mensajes de Telegram que incluían el vídeo pasan a incluir la imagen
    db.x("UPDATE OR IGNORE tg_batch_items SET item_id=? WHERE item_id=?", (new, iid))
    return new


def frame_undo(frame_id: int) -> int:
    """Deshace video_to_frame: el vídeo vuelve a la cola y la imagen desaparece. Devuelve el id del vídeo."""
    fr = db.one("SELECT * FROM items WHERE id=?", (frame_id,))
    if not fr or not fr["from_item"]:
        raise FrameError("Esa imagen no viene de un vídeo")
    if fr["status"] != "pending":
        raise FrameError("La imagen ya se ha guardado o borrado")
    vid = fr["from_item"]
    if not library.restore_items([vid]):
        raise FrameError("El vídeo ya no está en la papelera")
    (INBOX / fr["file"]).unlink(missing_ok=True)
    (THUMBS / f"{frame_id}.jpg").unlink(missing_ok=True)
    db.x("UPDATE items SET status='deleted', file='', decided_at=? WHERE id=?", (now(), frame_id))
    db.x("UPDATE OR IGNORE tg_batch_items SET item_id=? WHERE item_id=?", (vid, frame_id))
    return vid
