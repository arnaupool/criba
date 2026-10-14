"""Revisión desde Telegram: botones en los mensajes del bot y actualización del mensaje según se decide."""
import asyncio
import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from . import library, notify
from .db import db, now

log = logging.getLogger("criba.tgreview")
MAX_FOLDERS = 3


def _quick_folders(suggested: str | None) -> list[str]:
    out = [suggested] if suggested is not None else []
    for f in library.recent_folders():
        if f not in out:
            out.append(f)
    return out[:MAX_FOLDERS]


def _short(folder: str) -> str:
    return folder.split("/")[-1] or "Biblioteca"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else f"{n} {many}"


def render(batch: dict, with_url: bool = True) -> tuple[str, InlineKeyboardMarkup | None]:
    items = db.q("SELECT i.id, i.status, i.dest FROM items i JOIN tg_batch_items b ON b.item_id=i.id "
                 "WHERE b.batch_id=?", (batch["id"],))
    pending = [i for i in items if i["status"] == "pending"]
    kept = [i for i in items if i["status"] == "kept"]
    gone = [i for i in items if i["status"] in ("trashed", "deleted")]

    parts = []
    dests = sorted({k["dest"] or "" for k in kept})
    if kept:
        if len(dests) == 1:
            parts.append(f"📁 {_plural(len(kept), 'Guardado', 'guardados')} en {dests[0] or 'Biblioteca'}")
        else:
            parts.append(f"📁 {len(kept)} guardados en {', '.join(d or 'Biblioteca' for d in dests)}")
    if gone:
        parts.append(f"🗑 {_plural(len(gone), 'Borrado', 'borrados')}")
    if pending:
        if not kept and not gone:
            parts.append(f"📥 {_plural(len(pending), 'En la cola', 'nuevos en la cola')}")
        else:
            parts.append(f"📥 {len(pending)} pendiente{'s' if len(pending) != 1 else ''}")
    text = f"{batch['title']}\n{' · '.join(parts) or 'Sin elementos'}"

    rows = []
    bid = batch["id"]
    every = " todo" if len(pending) > 1 else ""
    if pending:
        folders = json.loads(batch["folders"])
        if folders:
            rows.append([InlineKeyboardButton(f"📁 {_short(f)}", callback_data=f"cb:{bid}:f{k}")
                         for k, f in enumerate(folders)])
        last = [InlineKeyboardButton(f"🗑 Borrar{every}", callback_data=f"cb:{bid}:t")]
        if with_url and (link := notify.ui_link()):
            last.append(InlineKeyboardButton("🌐 Revisar en la web", url=link))
        rows.append(last)
    elif kept or any(i["status"] == "trashed" for i in gone):
        rows.append([InlineKeyboardButton("↩ Devolver a la cola", callback_data=f"cb:{bid}:r")])
    return text, (InlineKeyboardMarkup(rows) if rows else None)


async def _edit(batch: dict):
    if notify.bot is None:
        return
    for with_url in (True, False):
        text, markup = render(batch, with_url)
        try:
            await notify.bot.edit_message_text(text, chat_id=batch["chat_id"], message_id=batch["message_id"],
                                               reply_markup=markup, disable_web_page_preview=True)
            return
        except BadRequest as e:
            err = str(e).lower()
            if "not modified" in err:
                return
            if with_url and "url" in err:  # Telegram no acepta CRIBA_URL (p. ej. una IP local) en un botón
                continue
            log.warning("No se pudo actualizar el mensaje %s: %s", batch["message_id"], e)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo actualizar el mensaje %s: %s", batch["message_id"], e)
            return


def _create(chat_id: int, message_id: int, title: str, ids: list[int], suggested: str | None) -> dict:
    bid = db.x("INSERT INTO tg_batches(chat_id,message_id,title,folders,created_at) VALUES(?,?,?,?,?)",
               (chat_id, message_id, title, json.dumps(_quick_folders(suggested)), now()))
    for iid in ids:
        db.x("INSERT OR IGNORE INTO tg_batch_items(batch_id,item_id) VALUES(?,?)", (bid, iid))
    return db.one("SELECT * FROM tg_batches WHERE id=?", (bid,))


async def attach(chat_id: int, message_id: int, title: str, ids: list[int], suggested: str | None = None):
    """Convierte un mensaje ya enviado en uno con botones de revisión."""
    if not ids:
        return
    await _edit(_create(chat_id, message_id, title, ids, suggested))


async def send(chat_id: int, title: str, ids: list[int], suggested: str | None = None):
    """Envía un mensaje nuevo con botones de revisión."""
    if notify.bot is None or not ids:
        return
    batch = _create(chat_id, 0, title, ids, suggested)
    try:
        try:
            text, markup = render(batch)
            msg = await notify.bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        except BadRequest as e:
            if "url" not in str(e).lower():
                raise
            text, markup = render(batch, with_url=False)
            msg = await notify.bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        db.x("UPDATE tg_batches SET message_id=? WHERE id=?", (msg.message_id, batch["id"]))
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo avisar a %s: %s", chat_id, e)
        db.x("DELETE FROM tg_batches WHERE id=?", (batch["id"],))


async def refresh_items(ids: list[int]):
    """Actualiza los mensajes de Telegram que incluyen alguno de estos elementos (tras revisar en la web)."""
    if notify.bot is None or not ids:
        return
    marks = ",".join("?" * len(ids))
    batches = db.q(f"SELECT DISTINCT t.* FROM tg_batches t JOIN tg_batch_items b ON b.batch_id=t.id "
                   f"WHERE b.item_id IN ({marks}) AND t.message_id != 0", ids)
    for b in batches:
        await _edit(b)


async def on_callback(update, ctx):
    from .config import TELEGRAM_ALLOWED_IDS
    q = update.callback_query
    if not update.effective_user or update.effective_user.id not in TELEGRAM_ALLOWED_IDS:
        return await q.answer("No autorizado", show_alert=True)
    try:
        _, bid, action = q.data.split(":")
        batch = db.one("SELECT * FROM tg_batches WHERE id=?", (int(bid),))
    except (ValueError, AttributeError):
        batch = None
    if not batch:
        return await q.answer("Este mensaje ya no se puede usar", show_alert=True)
    ids = [r["item_id"] for r in db.q("SELECT item_id FROM tg_batch_items WHERE batch_id=?", (batch["id"],))]
    try:
        if action == "t":
            n = await asyncio.to_thread(library.trash_items, ids)
            msg = f"🗑 {_plural(n, 'Borrado', 'borrados')}"
        elif action == "r":
            n = await asyncio.to_thread(library.restore_items, ids)
            msg = f"↩ {_plural(n, 'Devuelto', 'devueltos')} a la cola"
        elif action.startswith("f"):
            folder = json.loads(batch["folders"])[int(action[1:])]
            moved = await asyncio.to_thread(library.move_items, ids, folder)
            msg = f"📁 {_plural(len(moved), 'Guardado', 'guardados')} en {folder or 'Biblioteca'}"
        else:
            return await q.answer()
    except (library.PathError, IndexError, OSError) as e:
        return await q.answer(f"❌ {e}", show_alert=True)
    await q.answer(msg)
    await refresh_items(ids)
