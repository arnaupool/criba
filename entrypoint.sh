#!/bin/sh
# Arranque de Criba: umask al estilo Unraid y actualización de los descargadores.
umask "${UMASK:-000}"
export HOME="${CRIBA_DATA:-/data}/home"
export XDG_CACHE_HOME="${CRIBA_DATA:-/data}/cache"
export XDG_CONFIG_HOME="${CRIBA_DATA:-/data}/config"
mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME"

if [ "${AUTO_UPDATE:-1}" = "1" ]; then
  echo "Actualizando gallery-dl y yt-dlp..."
  timeout 300 python -m pip install --no-cache-dir --disable-pip-version-check -q -U \
    --target "${CRIBA_DATA:-/data}/pyupd" gallery-dl "yt-dlp[default]" \
    || echo "No se pudo actualizar (se usa la versión de la imagen)"
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips='*'
