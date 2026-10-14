"""Cola de trabajos (enlaces y seguimientos) y planificador de comprobaciones."""
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from . import notify
from .db import db, now
from .downloader import (cleanup_tmp, device_dir, download_link, download_subscription, ingest, job_tmp,
                         last_error_line, prepare_device_files, purge_device, site_of, tail)
from .library import purge_trash

log = logging.getLogger("criba.worker")
_wake = {"link": asyncio.Event(), "sub": asyncio.Event(), "device": asyncio.Event()}


def enqueue_link(url: str, origin: str, chat_id: int | None = None, message_id: int | None = None) -> int:
    jid = db.x("INSERT INTO jobs(kind,url,origin,chat_id,message_id,created_at) VALUES('link',?,?,?,?,?)",
               (url, origin, chat_id, message_id, now()))
    _wake["link"].set()
    return jid


def enqueue_device(url: str) -> int:
    """Descarga para el navegador que la pide: no pasa por la cola de revisión."""
    jid = db.x("INSERT INTO jobs(kind,url,origin,created_at) VALUES('device',?,'web',?)", (url, now()))
    _wake["device"].set()
    return jid


def enqueue_sub(sub_id: int, origin: str = "scheduler") -> int | None:
    if db.one("SELECT id FROM jobs WHERE subscription_id=? AND status IN ('queued','running')", (sub_id,)):
        return None
    sub = db.one("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
    if not sub:
        return None
    jid = db.x("INSERT INTO jobs(kind,url,subscription_id,origin,created_at) VALUES('sub',?,?,?,?)",
               (sub["url"], sub_id, origin, now()))
    _wake["sub"].set()
    return jid


def _claim(kind: str) -> dict | None:
    job = db.one("SELECT * FROM jobs WHERE status='queued' AND kind=? ORDER BY id LIMIT 1", (kind,))
    if job and db.x("UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                    (now(), job["id"])):
        return job
    return None


def _summary(new: int, dup: int) -> str:
    s = f"{new} nuevo{'s' if new != 1 else ''} en la cola"
    if dup:
        s += f" ({dup} repetido{'s' if dup != 1 else ''} descartado{'s' if dup != 1 else ''})"
    return s


async def _process_link(job: dict):
    tmp = job_tmp(job["id"])
    try:
        code, out = await download_link(job["url"], tmp)
        ids, dups = await asyncio.to_thread(ingest, tmp, "link" if job["origin"] != "telegram" else "telegram",
                                            job["url"], job["id"])
    finally:
        cleanup_tmp(tmp)
    ok = bool(ids or dups)
    db.x("UPDATE jobs SET status=?, finished_at=?, new_items=?, dup_items=?, log=? WHERE id=?",
         ("done" if ok else "error", now(), len(ids), dups, tail(out), job["id"]))
    if job["chat_id"] and ids:
        from . import tgreview  # import tardío: necesita python-telegram-bot
        if job["message_id"]:
            await tgreview.attach(job["chat_id"], job["message_id"], f"🔗 {job['url']}", ids)
        else:
            await tgreview.send(job["chat_id"], f"🔗 {job['url']}", ids)
    elif job["chat_id"]:
        if ok:
            text = f"♻️ Ya lo tenías: {dups} repetido{'s' if dups != 1 else ''}\n{job['url']}"
        else:
            text = f"❌ No he podido descargar nada de\n{job['url']}\n\n{last_error_line(out)}"
        if job["message_id"]:
            await notify.edit(job["chat_id"], job["message_id"], text)
        else:
            await notify.send(text, job["chat_id"])


async def _process_sub(job: dict):
    sub = db.one("SELECT * FROM subscriptions WHERE id=?", (job["subscription_id"],))
    if not sub:
        db.x("UPDATE jobs SET status='error', finished_at=?, log='Seguimiento eliminado' WHERE id=?",
             (now(), job["id"]))
        return
    tmp = job_tmp(job["id"])
    try:
        code, out, note = await download_subscription(sub, tmp)
        ids, dups = await asyncio.to_thread(ingest, tmp, "sub", sub["url"], job["id"], sub,
                                            {"author": sub["name"]})
    finally:
        cleanup_tmp(tmp)
    failed = code not in (0,) and not ids
    if failed:
        note = ""
    nxt = datetime.now(timezone.utc) + timedelta(minutes=sub["interval_min"] * random.uniform(0.9, 1.1))
    db.x("""UPDATE subscriptions SET last_check=?, next_check=?, last_new=?, total=total+?,
            baseline_done = CASE WHEN ? THEN baseline_done ELSE 1 END,
            last_error=?, errors_in_row = CASE WHEN ? THEN errors_in_row+1 ELSE 0 END WHERE id=?""",
         (now(), nxt.isoformat(timespec="seconds"), len(ids), len(ids), failed,
          last_error_line(out) if failed else None, failed, sub["id"]))
    # la nota va delante del registro ya recortado para que no se pierda
    log_text = f"{note}\n\n{tail(out)}" if note else tail(out)
    db.x("UPDATE jobs SET status=?, finished_at=?, new_items=?, dup_items=?, log=?, note=? WHERE id=?",
         ("error" if failed else "done", now(), len(ids), dups, log_text, note or None, job["id"]))
    if ids and notify.bot:
        from .bot import notify_new_items  # import tardío para evitar ciclos
        await notify_new_items(sub, ids)
    errors = (sub["errors_in_row"] + 1) if failed else 0
    if errors == 3:
        await notify.send(f"⚠️ «{sub['name']}» ({site_of(sub['url'])}) lleva 3 comprobaciones fallando.\n"
                          f"{last_error_line(out)}\n\nSi es Instagram o X, puede que haya que renovar las cookies.")


async def _process_device(job: dict):
    tmp = job_tmp(job["id"])
    try:
        code, out = await download_link(job["url"], tmp)
        files = await asyncio.to_thread(prepare_device_files, tmp, job["url"], device_dir(job["id"]))
    finally:
        cleanup_tmp(tmp)
    n = len(files)
    note = f"{n} archivo{'s' if n != 1 else ''} para el dispositivo" if n else None
    db.x("UPDATE jobs SET status=?, finished_at=?, log=?, note=? WHERE id=?",
         ("done" if n else "error", now(), tail(out), note, job["id"]))


async def worker(kind: str):
    handler = {"link": _process_link, "sub": _process_sub, "device": _process_device}[kind]
    while True:
        job = _claim(kind)
        if not job:
            _wake[kind].clear()
            try:
                await asyncio.wait_for(_wake[kind].wait(), 30)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await handler(job)
        except Exception as e:  # noqa: BLE001
            log.exception("Fallo en el trabajo %s", job["id"])
            db.x("UPDATE jobs SET status='error', finished_at=?, log=? WHERE id=?",
                 (now(), f"Error interno: {e!r}", job["id"]))


async def scheduler():
    last_purge = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            for sub in db.q("SELECT id FROM subscriptions WHERE enabled=1 AND (next_check IS NULL OR next_check<=?)",
                            (now(),)):
                enqueue_sub(sub["id"])
            await asyncio.to_thread(purge_device)
            if loop.time() - last_purge > 3600:
                last_purge = loop.time()
                n = await asyncio.to_thread(purge_trash)
                if n:
                    log.info("Papelera: %d archivos borrados definitivamente", n)
                db.x("DELETE FROM jobs WHERE finished_at < ?",
                     ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds"),))
        except Exception:  # noqa: BLE001
            log.exception("Fallo en el planificador")
        await asyncio.sleep(60)


def recover():
    """Al arrancar, los trabajos que quedaron a medias vuelven a la cola."""
    db.x("UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'")
