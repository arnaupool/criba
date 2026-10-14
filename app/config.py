"""Configuración leída de variables de entorno."""
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


LIBRARY = Path(_env("CRIBA_LIBRARY", "/library")).resolve()
DATA = Path(_env("CRIBA_DATA", "/data")).resolve()

INBOX = DATA / "inbox"
TRASH = DATA / "papelera"
THUMBS = DATA / "thumbs"
TMP = DATA / "tmp"
# Descargas «a este dispositivo»: se sirven al navegador y se borran pasado DEVICE_HOURS
DEVICE = DATA / "dispositivo"
ARCHIVES = DATA / "archives"
COOKIES = DATA / "cookies"
DB_PATH = DATA / "criba.sqlite"

TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN")
TELEGRAM_ALLOWED_IDS = {
    int(x) for x in _env("TELEGRAM_ALLOWED_IDS").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()
}
# URL con la que se llega a la interfaz desde fuera (para los enlaces de los mensajes de Telegram)
PUBLIC_URL = _env("CRIBA_URL").rstrip("/")
UI_PASSWORD = _env("UI_PASSWORD")
NOTIFY_NEW = _env("NOTIFY_NEW", "1") not in ("0", "false", "no")
TRASH_DAYS = int(_env("TRASH_DAYS", "7") or 7)
DEVICE_HOURS = 1
VERSION = _env("CRIBA_VERSION", "dev")
COMMIT = _env("CRIBA_COMMIT")
DEFAULT_INTERVAL = int(_env("DEFAULT_INTERVAL_MIN", "120") or 120)
# Directorio donde el arranque instala versiones nuevas de gallery-dl / yt-dlp
PYUPD = DATA / "pyupd"

for d in (DATA, INBOX, TRASH, THUMBS, TMP, ARCHIVES, COOKIES, DEVICE):
    d.mkdir(parents=True, exist_ok=True)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".avif", ".jfif"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".3gp", ".ts"}


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return "other"
