"""Avisos por Telegram desacoplados del resto (bot.py registra el bot al arrancar)."""
import logging

from .config import PUBLIC_URL, TELEGRAM_ALLOWED_IDS

log = logging.getLogger("criba.notify")
bot = None  # telegram.Bot, lo asigna bot.py


def ui_link(path: str = "") -> str:
    return f"{PUBLIC_URL}/{path.lstrip('/')}" if PUBLIC_URL else ""


async def send(text: str, chat_id: int | None = None):
    if bot is None:
        return
    targets = [chat_id] if chat_id else sorted(TELEGRAM_ALLOWED_IDS)
    for cid in targets:
        try:
            await bot.send_message(cid, text, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo avisar a %s: %s", cid, e)


async def edit(chat_id: int, message_id: int, text: str):
    if bot is None:
        return
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo editar el mensaje: %s", e)
        await send(text, chat_id)
