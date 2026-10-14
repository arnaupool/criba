"""Bot de Telegram: recibe enlaces (y archivos) y los manda a la cola de revisión."""
import asyncio
import logging
import re

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import notify
from .config import TELEGRAM_ALLOWED_IDS, TELEGRAM_TOKEN
from .db import db
from .downloader import cleanup_tmp, ingest, job_tmp
from .subscriptions import add_subscription, parse_initial
from .worker import enqueue_link

log = logging.getLogger("criba.bot")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
app: Application | None = None
info: dict = {}

HELP = (
    "Mándame enlaces y descargo las fotos y vídeos a la cola de revisión.\n\n"
    "También puedes enviarme fotos o vídeos directamente (Telegram limita a 20 MB los que reciben los bots).\n\n"
    "/seguir <url> [nuevo|N|todo] — vigilar un perfil y bajar lo que publique\n"
    "/seguimientos — lista de perfiles vigilados\n"
    "/cola — cuántos elementos hay pendientes de revisar\n"
    "/id — tu ID de Telegram"
)


def _allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in TELEGRAM_ALLOWED_IDS)


async def _deny(update: Update):
    uid = update.effective_user.id if update.effective_user else "?"
    await update.effective_message.reply_text(
        f"No estás autorizado. Tu ID es {uid}; añádelo a TELEGRAM_ALLOWED_IDS en la configuración del contenedor.")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.effective_message.reply_text(HELP)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Tu ID de Telegram es {update.effective_user.id}")


async def cmd_cola(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    n = db.one("SELECT COUNT(*) n FROM items WHERE status='pending'")["n"]
    running = db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")["n"]
    text = f"📥 {n} pendiente{'s' if n != 1 else ''} de revisar"
    if running:
        text += f"\n⏳ {running} descarga{'s' if running != 1 else ''} en curso"
    if link := notify.ui_link():
        text += f"\n{link}"
    await update.effective_message.reply_text(text, disable_web_page_preview=True)


async def cmd_seguir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        return await update.effective_message.reply_text(
            "Uso: /seguir <url del perfil> [nuevo|N|todo]\n"
            "nuevo (por defecto): solo lo que publique a partir de ahora\n"
            "N: además, sus últimos N archivos\n"
            "todo: todo lo que ha publicado (puede ser mucho)")
    try:
        initial = parse_initial(ctx.args[1] if len(ctx.args) > 1 else "nuevo")
        sub = add_subscription(ctx.args[0], initial=initial)
    except ValueError as e:
        return await update.effective_message.reply_text(f"❌ {e}")
    await update.effective_message.reply_text(
        f"👀 Siguiendo a «{sub['name']}» ({sub['site']}), cada {sub['interval_min']} min.\n"
        "Lo configuras (intervalo, carpeta sugerida…) desde la web.")


async def cmd_seguimientos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    subs = db.q("SELECT * FROM subscriptions ORDER BY name")
    if not subs:
        return await update.effective_message.reply_text("No sigues a nadie todavía. Usa /seguir <url>.")
    lines = []
    for s in subs:
        mark = "⏸" if not s["enabled"] else ("⚠️" if s["errors_in_row"] else "✅")
        lines.append(f"{mark} {s['name']} ({s['site']}) · {s['total']} descargados")
    await update.effective_message.reply_text("\n".join(lines))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    msg = update.effective_message
    urls = list(dict.fromkeys(URL_RE.findall(msg.text or msg.caption or "")))
    for ent in (msg.entities or []):
        if ent.url:
            urls.append(ent.url)
    urls = list(dict.fromkeys(u.rstrip(").,;!?") for u in urls))
    if not urls:
        return await msg.reply_text("Mándame un enlace (o una foto / vídeo). /start para ver la ayuda.")
    for url in urls:
        reply = await msg.reply_text(f"⏳ Descargando…\n{url}", disable_web_page_preview=True)
        enqueue_link(url, "telegram", msg.chat_id, reply.message_id)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    msg = update.effective_message
    if msg.photo:
        att, name = msg.photo[-1], f"telegram_{msg.photo[-1].file_unique_id}.jpg"
    else:
        att = msg.video or msg.animation or msg.document or msg.video_note
        if att is None:
            return
        ext = ".mp4" if (msg.video or msg.animation or msg.video_note) else ""
        name = getattr(att, "file_name", None) or f"telegram_{att.file_unique_id}{ext}"
    if (att.file_size or 0) > 20 * 1024 * 1024:
        return await msg.reply_text("❌ Telegram no deja a los bots descargar archivos de más de 20 MB. "
                                    "Mándame el enlace de donde lo sacaste.")
    await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    tmp = job_tmp(-msg.message_id)
    try:
        f = await att.get_file()
        await f.download_to_drive(tmp / name)
        origin = msg.forward_origin
        author = None
        if origin is not None:
            chat = getattr(origin, "chat", None) or getattr(origin, "sender_user", None)
            author = getattr(chat, "username", None) or getattr(chat, "title", None) or getattr(
                origin, "sender_user_name", None)
        ids, dups = await asyncio.to_thread(ingest, tmp, "telegram", None, None, None,
                                            {"author": author, "caption": msg.caption})
    finally:
        cleanup_tmp(tmp)
    if ids:
        await msg.reply_text("✅ Añadido a la cola")
    elif dups:
        await msg.reply_text("♻️ Ya lo tenías (repetido)")
    else:
        await msg.reply_text("❌ No es una foto ni un vídeo que sepa guardar")


async def notify_new_items(sub: dict, ids: list[int]):
    from .config import NOTIFY_NEW
    if not NOTIFY_NEW:
        return
    n = len(ids)
    text = f"🆕 {sub['name']}: {n} nuevo{'s' if n != 1 else ''} en la cola"
    if link := notify.ui_link(f"?sub={sub['id']}"):
        text += f"\n{link}"
    await notify.send(text)


async def start():
    global app
    if not TELEGRAM_TOKEN:
        log.info("Sin TELEGRAM_TOKEN: bot desactivado")
        return
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help", "ayuda"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cola", cmd_cola))
    app.add_handler(CommandHandler("seguir", cmd_seguir))
    app.add_handler(CommandHandler("seguimientos", cmd_seguimientos))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE | filters.Document.ALL, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=False)
        me = await app.bot.get_me()
        info.update(username=me.username, ok=True)
        notify.bot = app.bot
        log.info("Bot de Telegram activo: @%s", me.username)
    except Exception as e:  # noqa: BLE001
        info.update(ok=False, error=str(e))
        log.error("No se pudo arrancar el bot de Telegram: %s", e)
        app = None


async def stop():
    if app:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
