FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

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
