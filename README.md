# Criba

Descarga fotos y vídeos a una **cola de revisión** y te deja decidir, uno a uno o en bloque, si **borrarlos** o
**guardarlos en una carpeta** de tu biblioteca (la que ya tienes organizada en Unraid).

Entran a la cola por tres caminos:

- **Bot de Telegram**: le mandas un enlace (Instagram, X, Reddit, TikTok, YouTube, Bluesky, webs…) o una foto o vídeo.
- **Seguimientos**: añades el perfil de alguien y cada cierto tiempo se descarga lo que haya publicado desde la última vez.
- **Web**: pegas enlaces con el botón ＋ o usas el marcador «Enviar a Criba» del navegador.

Por debajo usa [gallery-dl](https://github.com/mikf/gallery-dl) y [yt-dlp](https://github.com/yt-dlp/yt-dlp), que se
actualizan solos en cada arranque.

## Instalación en Unraid

1. **Sube este repositorio a GitHub** (por ejemplo `arnaupool/criba`). La Action de `.github/workflows/docker.yml`
   construye la imagen y la publica en `ghcr.io/arnaupool/criba:latest` en cada cambio.
2. La primera vez, en GitHub → tu perfil → *Packages* → `criba` → *Package settings*, pon el paquete como **Public**
   (así Unraid lo descarga sin iniciar sesión).
3. En Unraid, guarda `unraid/criba.xml` como `/boot/config/plugins/dockerMan/templates-user/my-criba.xml`
   (o, en la pestaña *Docker*, añade el repositorio en *Template repositories*).
4. *Docker* → *Add Container* → plantilla **criba**, y rellena:
   - **Biblioteca**: tu carpeta de fotos y vídeos (p. ej. `/mnt/user/Fotos`).
   - **Datos**: `/mnt/user/appdata/criba`.
   - **Contraseña de la interfaz**.
   - **Token del bot** e **IDs autorizados** (ver abajo).
   - **URL pública** si la expones con un proxy inverso.

Con `docker compose` en lugar de la plantilla, usa `docker-compose.yml`.

### Proxy inverso

Apunta tu dominio (Nginx Proxy Manager, SWAG, Cloudflare Tunnel…) al puerto 8080 del contenedor. La sesión dura 90 días
y la cookie se marca como segura cuando el proxy envía `X-Forwarded-Proto: https`. Si subes vídeos grandes no hace falta
tocar nada: la web solo los reproduce.

### Bot de Telegram

1. Habla con [@BotFather](https://t.me/BotFather), `/newbot`, y copia el token en **TELEGRAM_TOKEN**.
   Tiene que ser un bot nuevo: un mismo token no puede estar escuchando en dos sitios a la vez.
2. Arranca el contenedor, escribe `/id` a tu bot y pon ese número en **TELEGRAM_ALLOWED_IDS**. Reinicia el contenedor.
3. Comandos: mandar un enlace, `/seguir <url> [nuevo|N|todo]`, `/seguimientos`, `/cola`.

Telegram no deja a los bots descargar archivos de más de **20 MB** que les mandes directamente; para eso, envía el enlace.

### Cookies (Instagram, X…)

Instagram y X solo dejan descargar con sesión iniciada. Usa una **cuenta secundaria**: entra con ella en el navegador,
exporta las cookies con la extensión *Get cookies.txt LOCALLY* y súbelas en **Ajustes → Cookies**. Cuando un seguimiento
falle tres veces seguidas recibirás un aviso; normalmente basta con volver a subir las cookies.

## Uso

**Revisar**: en el PC, cuadrícula con selección múltiple (clic en el círculo, Ctrl/Mayús + clic, Ctrl+A) o modo
«una a una». En el móvil se abre directamente una a una:

| Acción | PC | Móvil |
|---|---|---|
| Guardar en… | `G` / `Intro` | deslizar → |
| Borrar (papelera) | `Supr` / `X` | deslizar ← |
| Siguiente / anterior | `→` / `←` | deslizar ↑ / ↓ |
| Carpetas rápidas (sugerida y recientes) | `1`–`4` | botones de abajo |
| Descargar enlace | `N` | botón ＋ |

Al guardar se puede crear una carpeta nueva dentro de la seleccionada. Los archivos se guardan como
`fecha_autor_id.ext` (p. ej. `2026-10-14_usuario_ABC123.jpg`). Lo repetido (mismo contenido) se descarta solo.

Lo borrado va a la **papelera** y se elimina definitivamente a los 7 días. Desde *Revisar → Papelera* o *Guardados* puedes
devolver cualquier cosa a la cola.

**Seguimientos**: al añadir un perfil eliges qué hacer con lo que ya tenía publicado: ignorarlo (solo lo nuevo), bajar
sus últimos N archivos o bajar todo el histórico. Puedes darle una **carpeta sugerida**, que aparecerá como botón rápido
al revisar sus archivos.

## Datos

Todo el estado vive en `/data`: `criba.sqlite` (base de datos), `inbox/` (pendientes), `papelera/`, `cookies/`,
`archives/` (lo ya visto de cada perfil) y `config/gallery-dl/config.json` si quieres ajustes avanzados de gallery-dl.
Haz copia de seguridad de esa carpeta (por ejemplo con el plugin *Appdata Backup*).

## Desarrollo

```bash
docker build -t criba:dev .
docker run --rm -p 8090:8080 -e UI_PASSWORD=dev -e AUTO_UPDATE=0 -v "$PWD/data:/data" -v "$PWD/library:/library" criba:dev
```
