"""Vídeos que los navegadores no reproducen (H.265/HEVC de TikTok o Instagram, etc.) → H.264.

Firefox no reproduce HEVC nunca y Chrome en Windows solo con una extensión; H.264 lo reproduce todo,
también el iPhone. La conversión es con ffmpeg (libx264, calidad alta) y se salta en vídeos muy largos.
"""
import subprocess
from pathlib import Path

from .db import db

PLAYABLE = {"h264"}
MAX_SECONDS = 30 * 60  # más largo que esto no se convierte solo (tarda y ocupa mucho)


class TranscodeError(Exception):
    pass


def auto_enabled() -> bool:
    return db.get_setting("transcode_auto", "1") == "1"


def probe(path: Path) -> tuple[str | None, float]:
    """(códec de vídeo, duración en segundos) según ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name:format=duration",
             "-of", "default=nw=1", str(path)], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, 0.0
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    try:
        duration = float(info.get("duration", 0) or 0)
    except ValueError:
        duration = 0.0
    return info.get("codec_name"), duration


def needs_transcode(path: Path) -> bool:
    codec, _ = probe(path)
    return codec is not None and codec not in PLAYABLE


def to_h264(src: Path, limit: bool = True) -> Path:
    """Convierte src a H.264/AAC en un .mp4 nuevo junto a él y devuelve su ruta (src no se toca)."""
    codec, duration = probe(src)
    if codec is None:
        raise TranscodeError("No es un vídeo que ffmpeg sepa leer")
    if limit and duration > MAX_SECONDS:
        raise TranscodeError(f"Dura más de {MAX_SECONDS // 60} min: no se convierte automáticamente")
    out = src.with_name(src.stem + ".h264.mp4")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
        capture_output=True, text=True, timeout=max(600, int(duration * 4)))
    if r.returncode != 0 or not out.exists() or not out.stat().st_size:
        out.unlink(missing_ok=True)
        raise TranscodeError(f"ffmpeg no pudo convertirlo: {(r.stderr or '').strip()[-300:]}")
    return out
