"""Avisos por Telegram desacoplados del resto (bot.py registra el bot al arrancar).

Todos los mensajes llevan un botón «🌐 Abrir Criba» si hay CRIBA_URL. Telegram rechaza en los botones
direcciones que no sean públicas (una IP local, p. ej.): entonces el mensaje se manda sin botón.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from .config import PUBLIC_URL, TELEGRAM_ALLOWED_IDS

log = logging.getLogger("criba.notify")
bot = None  # telegram.Bot, lo asigna bot.py


def ui_link(path: str = "") -> str:
    return f"{PUBLIC_URL}/{path.lstrip('/')}" if PUBLIC_URL else ""


def web_button(path: str = "") -> InlineKeyboardButton | None:
    link = ui_link(path)
    return InlineKeyboardButton("🌐 Abrir Criba", url=link) if link else None


def web_markup(path: str = "", rows: list | None = None) -> InlineKeyboardMarkup | None:
    """Teclado con las filas dadas y, al final, el botón a la web."""
    rows = [r for r in (rows or []) if r]
    if b := web_button(path):
        rows.append([b])
    return InlineKeyboardMarkup(rows) if rows else None


def without_url_buttons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    rows = [[b for b in row if not b.url] for row in markup.inline_keyboard]
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(rows) if rows else None


def _url_rejected(e: Exception) -> bool:
    return isinstance(e, BadRequest) and "url" in str(e).lower()


async def safe_send(chat_id: int, text: str, markup=None):
    """send_message con reintento sin los botones de enlace si Telegram no acepta la URL."""
    try:
        return await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    except BadRequest as e:
        if markup is None or not _url_rejected(e):
            raise
        return await bot.send_message(chat_id, text, reply_markup=without_url_buttons(markup),
                                      disable_web_page_preview=True)


async def safe_edit(chat_id: int, message_id: int, text: str, markup=None):
    try:
        return await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup,
                                           disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return None
        if markup is None or not _url_rejected(e):
            raise
        return await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                           reply_markup=without_url_buttons(markup), disable_web_page_preview=True)


async def send(text: str, chat_id: int | None = None, path: str = ""):
    if bot is None:
        return
    targets = [chat_id] if chat_id else sorted(TELEGRAM_ALLOWED_IDS)
    for cid in targets:
        try:
            await safe_send(cid, text, web_markup(path))
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo avisar a %s: %s", cid, e)


async def edit(chat_id: int, message_id: int, text: str, path: str = ""):
    if bot is None:
        return
    try:
        await safe_edit(chat_id, message_id, text, web_markup(path))
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo editar el mensaje: %s", e)
        await send(text, chat_id, path)
