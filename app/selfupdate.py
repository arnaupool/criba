"""Actualiza gallery-dl y yt-dlp en /data/pyupd (lo llama entrypoint.sh al arrancar y Ajustes a mano).

gallery-dl puede instalarse desde su código en desarrollo (Ajustes): cuando Instagram cambia algo, el
arreglo suele estar ahí días antes de que salga versión nueva en PyPI.
"""
import subprocess
import sys

from .config import PYUPD
from .db import db

GDL_DEV = "gallery-dl @ https://codeberg.org/mikf/gallery-dl/archive/master.tar.gz"


def gdl_dev() -> bool:
    return db.get_setting("gdl_channel") == "dev"


def command() -> list[str]:
    return [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "-q", "-U",
            "--target", str(PYUPD), GDL_DEV if gdl_dev() else "gallery-dl", "yt-dlp[default,curl-cffi]"]


if __name__ == "__main__":
    print(f"Actualizando gallery-dl ({'desarrollo' if gdl_dev() else 'estable'}) y yt-dlp...", flush=True)
    sys.exit(subprocess.call(command()))
