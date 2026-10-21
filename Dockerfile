# --- cobalt: descargador de reserva para enlaces (https://github.com/imputnet/cobalt, AGPL-3.0) ---
# Se construye aparte y Criba lo arranca dentro del mismo contenedor, solo en localhost.
FROM node:24-trixie-slim AS cobalt
ARG COBALT_REF=a636575b09de1fc55d9b8cd98cac88f5f2f16b42
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
RUN corepack enable
WORKDIR /src
RUN git clone https://github.com/imputnet/cobalt.git . && git checkout "$COBALT_REF"
RUN pnpm install --prod --frozen-lockfile \
    && pnpm deploy --filter=@imput/cobalt-api --prod /prod/api

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

# Node y cobalt (mismo Debian que la etapa de arriba, así sus módulos compilados funcionan aquí)
COPY --from=cobalt /usr/local/bin/node /usr/local/bin/node
COPY --from=cobalt /prod/api /cobalt
COPY --from=cobalt /src/.git /cobalt/.git

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# La GitHub Action pasa la fecha y el commit de la build (se muestran en Ajustes)
ARG CRIBA_VERSION=dev
ARG CRIBA_COMMIT=
ENV CRIBA_VERSION=$CRIBA_VERSION \
    CRIBA_COMMIT=$CRIBA_COMMIT \
    CRIBA_LIBRARY=/library \
    CRIBA_DATA=/data \
    PYTHONUNBUFFERED=1 \
    PORT=8080
VOLUME ["/data", "/library"]
EXPOSE 8080
# 99:100 = nobody:users, el usuario habitual de los contenedores en Unraid
USER 99:100
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
