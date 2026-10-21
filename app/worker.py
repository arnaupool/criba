"""Cola de trabajos (enlaces y seguimientos) y planificador de comprobaciones."""
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from . import notify
from .config import TMP
from .db import db, now
from .downloader import (COOKIES_BAD_MSG, RATE_PAUSE_H, JobCancelled, blocked_reason, cancel_job, cleanup_tmp,
                         cookie_name, cookies_for, current_job, device_dir, download_link, download_subscription,
                         forget_cancel, ingest, job_tmp, last_error_line, login_problem, mark_cookies_bad,
                         mark_ratelimited, prepare_device_files, purge_device, rate_problem, site_of, tail,
                         TRANSIENT_ALERT, transient_problem, VERBOSE_ORIGIN, verbose_job, ig_follows, ig_tray,
                         ig_username, tray_covers, _gdl_args, _run)
from .config import ARCHIVES
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
    if sub["site"] == "instagram":
        _take_ig_slot()
    _wake["sub"].set()
    return jid


# ---- Instagram: comprobaciones repartidas y bandeja de historias
IG_GAP_MIN = (20, 30)        # minutos entre dos consultas automáticas a Instagram (al azar en ese rango)
TRAY_EVERY_MIN = (210, 270)  # bandeja de historias cada ~4 h (duran 24 h)
FOLLOW_RECHECK_DAYS = 7


def _take_ig_slot():
    nxt = datetime.now(timezone.utc) + timedelta(minutes=random.uniform(*IG_GAP_MIN))
    db.set_setting("ig_slot", nxt.isoformat(timespec="seconds"))


def _ig_slot_free() -> bool:
    return (db.get_setting("ig_slot") or "") <= now() and not db.one(
        "SELECT j.id FROM jobs j LEFT JOIN subscriptions s ON s.id=j.subscription_id WHERE j.status IN "
        "('queued','running') AND (j.kind='tray' OR (j.kind='sub' AND s.site='instagram'))")


def tray_subs() -> list[dict]:
    return [s for s in db.q("SELECT * FROM subscriptions WHERE enabled=1 AND site='instagram' AND baseline_done=1")
            if tray_covers(s)]


def enqueue_tray(origin: str = "scheduler") -> int | None:
    if db.one("SELECT id FROM jobs WHERE kind='tray' AND status IN ('queued','running')"):
        return None
    jid = db.x("INSERT INTO jobs(kind,url,origin,created_at) VALUES('tray',?,?,?)",
               ("https://www.instagram.com/stories/me/", origin, now()))
    _take_ig_slot()
    _wake["sub"].set()
    return jid


def _claim(kinds: tuple[str, ...]) -> dict | None:
    marks = ",".join("?" * len(kinds))
    job = db.one(f"SELECT * FROM jobs WHERE status='queued' AND kind IN ({marks}) ORDER BY id LIMIT 1", kinds)
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
    hint = "" if ok else await _check_login(job["url"], out)
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
            text = f"❌ No he podido descargar nada de\n{job['url']}\n\n{hint or last_error_line(out)}"
        if job["message_id"]:
            await notify.edit(job["chat_id"], job["message_id"], text)
        else:
            await notify.send(text, job["chat_id"])


async def _check_login(url: str, out: str) -> str:
    """Si el sitio ha pedido iniciar sesión: con cookies, se marcan como caducadas (y se avisa una vez);
    sin cookies, se sugiere subirlas. Si ha respondido 429, se pausa ese sitio unas horas.
    Devuelve el texto para el usuario ("" si no era nada de eso)."""
    if not login_problem(out):
        if rate_problem(out):
            site = site_of(url)
            msg = (f"{site} responde «demasiadas peticiones» (429). Dejo de consultar {site} durante "
                   f"{RATE_PAUSE_H} h para que no marquen la cuenta; se reanuda solo (o desde Ajustes).")
            if mark_ratelimited(url):
                await notify.send(f"⏸ {msg}", path="#ajustes")
            return msg
        return ""
    name = cookie_name(url)
    if cookies_for(url):
        msg = COOKIES_BAD_MSG.format(name=name)
        if mark_cookies_bad(url):
            await notify.send(f"⚠️ {msg}", path="#ajustes")
        return msg
    return f"{name} pide iniciar sesión para esto: sube cookies de una cuenta en Ajustes → Cookies."


async def _process_sub(job: dict):
    sub = db.one("SELECT * FROM subscriptions WHERE id=?", (job["subscription_id"],))
    if not sub:
        db.x("UPDATE jobs SET status='error', finished_at=?, log='Seguimiento eliminado' WHERE id=?",
             (now(), job["id"]))
        return
    tmp = job_tmp(job["id"])
    try:
        code, out, note, parts_done = await download_subscription(sub, tmp)
        # en las comprobaciones normales, lo publicado antes de seguir el perfil no es nuevo
        # (p. ej. una publicación antigua que acaban de fijar arriba); en el alta sí se quiere
        stats: dict = {}
        ids, dups = await asyncio.to_thread(ingest, tmp, "sub", sub["url"], job["id"], sub,
                                            {"author": sub["name"]},
                                            sub["created_at"] if sub["baseline_done"] else None, stats)
    finally:
        cleanup_tmp(tmp)
    if stats.get("old"):
        n = stats["old"]
        note = "; ".join(x for x in (note, f"{n} anterior{'es' if n != 1 else ''} a seguir el perfil "
                                          f"descartad{'os' if n != 1 else 'o'} (p. ej. fijadas)") if x)
    failed = code not in (0,) and not ids
    login_hint = await _check_login(sub["url"], out) if failed else ""
    # corte temporal del sitio (p. ej. Instagram «redirect to home page», intermitente): no es un fallo;
    # lo ya visto queda en el archivo y la siguiente comprobación sigue donde se quedó
    transient = failed and not login_hint and transient_problem(out)
    if failed:  # «Marcados 0…» confundiría; los demás avisos (p. ej. falta de cookies) sí se dejan
        note = "; ".join(n for n in note.split("; ") if n and not n.startswith("Marcados"))
    if transient:
        note = "; ".join(x for x in (note, f"{site_of(sub['url'])} cortó la comprobación (temporal): "
                                           "se reintenta en la siguiente") if x)
    nxt = datetime.now(timezone.utc) + timedelta(minutes=sub["interval_min"] * random.uniform(0.9, 1.1))
    db.x("""UPDATE subscriptions SET last_check=?, next_check=?, last_new=?, total=total+?,
            baseline_done = CASE WHEN ? THEN baseline_done ELSE 1 END,
            parts_done = CASE WHEN ? AND baseline_done=0 THEN parts_done ELSE ? END,
            last_error=?, errors_in_row = CASE WHEN ? THEN errors_in_row+1 ELSE 0 END WHERE id=?""",
         (now(), nxt.isoformat(timespec="seconds"), len(ids), len(ids), failed, failed, parts_done,
          last_error_line(out) if failed and not transient else None, failed, sub["id"]))
    if not failed:
        await _check_follow(sub)
    # la nota va delante del registro ya recortado para que no se pierda
    log_text = f"{note}\n\n{tail(out)}" if note else tail(out)
    db.x("UPDATE jobs SET status=?, finished_at=?, new_items=?, dup_items=?, log=?, note=? WHERE id=?",
         ("error" if failed and not transient else "done", now(), len(ids), dups, log_text, note or None,
          job["id"]))
    if ids and notify.bot:
        from .bot import notify_new_items  # import tardío para evitar ciclos
        await notify_new_items(sub, ids)
    errors = (sub["errors_in_row"] + 1) if failed else 0
    if login_hint or blocked_reason(sub["url"]):  # esos ya se avisan aparte
        return
    if transient and errors == TRANSIENT_ALERT:
        await notify.send(f"⚠️ «{sub['name']}»: {site_of(sub['url'])} ha cortado {errors} comprobaciones seguidas.\n"
                          f"{last_error_line(out)}\n\nSuele pasarse solo; si sigue, prueba a quitar Reels de ese "
                          "perfil o a renovar las cookies.", path="#seguimientos")
    elif failed and not transient and errors == 3:
        await notify.send(f"⚠️ «{sub['name']}» ({site_of(sub['url'])}) lleva 3 comprobaciones fallando.\n"
                          f"{last_error_line(out)}\n\nSi es Instagram o X, puede que haya que renovar las cookies.",
                          path="#seguimientos")


async def _process_tray(job: dict):
    """Bandeja de historias: una petición dice qué perfiles seguidos tienen historias nuevas y solo se
    descargan las de esos (con el enlace por ID que trae la bandeja: no hay que buscar al usuario)."""
    nxt = datetime.now(timezone.utc) + timedelta(minutes=random.uniform(*TRAY_EVERY_MIN))
    db.set_setting("tray_next", nxt.isoformat(timespec="seconds"))
    db.set_setting("tray_last", now())
    url = "https://www.instagram.com/stories/me/"
    if reason := blocked_reason(url):
        db.x("UPDATE jobs SET status='error', finished_at=?, log=? WHERE id=?", (now(), reason, job["id"]))
        return
    tray, out = await ig_tray()
    logs, total, failed = [out], 0, not tray and out.startswith("[error]")
    if failed:
        await _check_login(url, out)
    for sub in tray_subs():
        entry = tray.get(ig_username(sub["url"]))
        if not entry or entry[1] <= (sub["stories_seen"] or 0):
            continue
        story_url, latest = entry
        tmp = job_tmp(f"{job['id']}_{sub['id']}")
        try:
            args = _gdl_args(story_url, tmp) + ["--download-archive", str(ARCHIVES / f"sub_{sub['id']}.sqlite")]
            code, sout = await _run(args + [story_url])
            stats: dict = {}
            ids, dups = await asyncio.to_thread(ingest, tmp, "sub", sub["url"], job["id"], sub,
                                                {"author": sub["name"]}, sub["created_at"], stats)
        finally:
            cleanup_tmp(tmp)
        logs.append(f"$ historias de {sub['name']}: {len(ids)} nuevas\n{sout}")
        if code == 0 or ids:
            db.x("UPDATE subscriptions SET stories_seen=?, total=total+?, last_new=? WHERE id=?",
                 (latest, len(ids), len(ids), sub["id"]))
        total += len(ids)
        if ids and notify.bot:
            from .bot import notify_new_items  # import tardío para evitar ciclos
            await notify_new_items(sub, ids)
    db.x("UPDATE jobs SET status=?, finished_at=?, new_items=?, log=?, note=? WHERE id=?",
         ("error" if failed else "done", now(), total, tail("\n".join(logs)),
          None if failed else f"{len(tray)} perfiles con historias en la bandeja; {total} nuevas", job["id"]))


async def _check_follow(sub: dict):
    """Instagram: ¿la cuenta de las cookies sigue el perfil? Si no, sus historias no salen en la bandeja
    y se miran perfil a perfil; se avisa una vez. Se vuelve a comprobar cada FOLLOW_RECHECK_DAYS días."""
    if sub["site"] != "instagram" or not cookies_for(sub["url"]) or blocked_reason(sub["url"]):
        return
    checked = sub.get("follows_checked") or ""
    if checked and checked > (datetime.now(timezone.utc) - timedelta(days=FOLLOW_RECHECK_DAYS)).isoformat():
        return
    follows, _ = await ig_follows(sub)
    if follows is None:
        return
    db.x("UPDATE subscriptions SET ig_follows=?, follows_checked=? WHERE id=?", (int(follows), now(), sub["id"]))
    if not follows and sub.get("ig_follows") != 0:
        await notify.send(f"👤 La cuenta de las cookies no sigue a {sub['name']}. Sus historias se mirarán perfil a "
                          "perfil (más peticiones a Instagram). Si la sigues desde esa cuenta, se mirarán en la "
                          "bandeja junto con las demás.", path="#seguimientos")


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


HANDLERS = {"link": "_process_link", "sub": "_process_sub", "device": "_process_device", "tray": "_process_tray"}


async def worker(kind: str):
    # la bandeja de historias va por la misma cola que los seguimientos: así Instagram nunca recibe dos a la vez
    kinds = ("sub", "tray") if kind == "sub" else (kind,)
    while True:
        job = _claim(kinds)
        handler = globals()[HANDLERS[job["kind"]]] if job else None
        if not job:
            _wake[kind].clear()
            try:
                await asyncio.wait_for(_wake[kind].wait(), 30)
            except asyncio.TimeoutError:
                pass
            continue
        token = current_job.set(job["id"])
        vtoken = verbose_job.set(job["origin"] == VERBOSE_ORIGIN)
        try:
            await handler(job)
        except JobCancelled:
            await _mark_cancelled(job)
        except Exception as e:  # noqa: BLE001
            log.exception("Fallo en el trabajo %s", job["id"])
            db.x("UPDATE jobs SET status='error', finished_at=?, log=? WHERE id=?",
                 (now(), f"Error interno: {e!r}", job["id"]))
            if job["subscription_id"]:  # sin esto el planificador lo volvería a encolar cada minuto
                sub = db.one("SELECT interval_min FROM subscriptions WHERE id=?", (job["subscription_id"],))
                if sub:
                    nxt = datetime.now(timezone.utc) + timedelta(minutes=sub["interval_min"])
                    db.x("UPDATE subscriptions SET next_check=?, last_check=?, last_error=? WHERE id=?",
                         (nxt.isoformat(timespec="seconds"), now(), f"Error interno: {e!r}"[:300],
                          job["subscription_id"]))
        finally:
            current_job.reset(token)
            verbose_job.reset(vtoken)
            forget_cancel(job["id"])


async def _mark_cancelled(job: dict):
    row = db.one("SELECT log FROM jobs WHERE id=?", (job["id"],)) or {}
    db.x("UPDATE jobs SET status='cancelled', finished_at=?, log=? WHERE id=?",
         (now(), tail((row.get("log") or "") + "\n⛔ Cancelada"), job["id"]))
    cleanup_tmp(TMP / f"job_{job['id']}")
    if job["subscription_id"]:  # la siguiente comprobación, a su hora normal
        sub = db.one("SELECT interval_min FROM subscriptions WHERE id=?", (job["subscription_id"],))
        if sub:
            nxt = datetime.now(timezone.utc) + timedelta(minutes=sub["interval_min"])
            db.x("UPDATE subscriptions SET next_check=?, last_check=? WHERE id=?",
                 (nxt.isoformat(timespec="seconds"), now(), job["subscription_id"]))
    if job["chat_id"] and job["message_id"]:
        await notify.edit(job["chat_id"], job["message_id"], f"⛔ Descarga cancelada\n{job['url']}")


def cancel(job_id: int) -> str | None:
    """Cancela un trabajo en espera o en marcha. Devuelve el estado en que queda, o None si ya había acabado."""
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] not in ("queued", "running"):
        return None
    if job["status"] == "queued" and db.x(
            "UPDATE jobs SET status='cancelled', finished_at=?, log='⛔ Cancelada antes de empezar' "
            "WHERE id=? AND status='queued'", (now(), job_id)):
        return "cancelled"
    cancel_job(job_id)  # en marcha: el worker lo verá al terminar el paso actual (se mata ya)
    return "cancelling"


def active_hours() -> tuple[bool, int, int]:
    """Franja en la que el planificador lanza comprobaciones (hora local del contenedor, TZ).
    Por defecto de 9 a 23 h, como lo usaría una persona."""
    enabled = db.get_setting("hours_enabled", "1") == "1"
    return enabled, int(db.get_setting("hours_from", "9")), int(db.get_setting("hours_to", "23"))


def in_active_hours(hour: int | None = None) -> bool:
    enabled, start, end = active_hours()
    if not enabled or start == end:
        return True
    h = datetime.now().astimezone().hour if hour is None else hour
    return start <= h < end if start < end else (h >= start or h < end)  # p. ej. 22–6 cruza la medianoche


def schedule_due():
    """Encola lo que toca. Fuera de horario no se lanza nada (lo pendiente sale al abrir la franja;
    «Comprobar ahora» va siempre)."""
    if not in_active_hours():
        return
    due = db.q("SELECT id, site FROM subscriptions WHERE enabled=1 AND "
               "(next_check IS NULL OR next_check<=?) ORDER BY next_check", (now(),))
    for s in due:
        if s["site"] != "instagram":
            enqueue_sub(s["id"])
    # Instagram: de una en una y con 20–30 min entre consultas; la bandeja primero (las historias caducan)
    if _ig_slot_free():
        if (tray_subs() and (db.get_setting("tray_next") or "") <= now()
                and not blocked_reason("https://www.instagram.com/")):
            enqueue_tray()
        elif ig := next((s for s in due if s["site"] == "instagram"), None):
            enqueue_sub(ig["id"])


async def scheduler():
    last_purge = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            schedule_due()
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
    db.x("UPDATE jobs SET status='queued', started_at=NULL, log=NULL WHERE status='running'")
