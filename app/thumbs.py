"""Miniaturas: Pillow para imágenes, ffmpeg para vídeos."""
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

from .config import THUMBS

SIZE = 480


def make_thumb(item_id: int, src: Path, kind: str) -> Path | None:
    out = THUMBS / f"{item_id}.jpg"
    if out.exists():
        return out
    try:
        if kind == "image":
            with Image.open(src) as im:
                im.seek(0)
                im = ImageOps.exif_transpose(im)
                im.thumbnail((SIZE, SIZE))
                if im.mode != "RGB":
                    im = im.convert("RGB")
                im.save(out, "JPEG", quality=80)
            return out
        if kind == "video":
            for ss in ("1", "0"):
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", str(src), "-frames:v", "1",
                     "-vf", f"scale='min({SIZE},iw)':-2", str(out)],
                    timeout=60, check=False)
                if out.exists() and out.stat().st_size:
                    return out
    except Exception:
        pass
    return None
