"""SQLite: elementos de la cola, seguimientos y trabajos de descarga."""
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    file TEXT NOT NULL,            -- ruta actual (relativa a inbox / papelera / biblioteca según estado)
    orig_name TEXT NOT NULL,
    save_name TEXT,                -- nombre al guardar: fecha_autor_id.ext
    kind TEXT NOT NULL,           -- image | video | other
    size INTEGER,
    sha256 TEXT,
    source TEXT NOT NULL,          -- link | sub | telegram | web
    source_url TEXT,
    post_url TEXT,
    author TEXT,
    caption TEXT,
    posted_at TEXT,
    subscription_id INTEGER,
    job_id INTEGER,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | kept | trashed | deleted
    dest TEXT,                     -- carpeta de la biblioteca (si kept)
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS items_sha ON items(sha256);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    site TEXT,
    interval_min INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    initial_count INTEGER NOT NULL DEFAULT 0,  -- 0 = solo publicaciones nuevas
    baseline_done INTEGER NOT NULL DEFAULT 0,
    folder TEXT,                   -- carpeta sugerida al revisar
    created_at TEXT NOT NULL,
    last_check TEXT,
    next_check TEXT,
    last_error TEXT,
    errors_in_row INTEGER NOT NULL DEFAULT 0,
    last_new INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- link | sub
    url TEXT NOT NULL,
    subscription_id INTEGER,
    origin TEXT,                   -- telegram | web | scheduler
    chat_id INTEGER,
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | error
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    new_items INTEGER NOT NULL DEFAULT 0,
    dup_items INTEGER NOT NULL DEFAULT 0,
    log TEXT,
    note TEXT                      -- resumen legible (p. ej. «Marcados N archivos existentes como vistos»)
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, kind);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Mensajes de Telegram con botones de revisión y los elementos que abarca cada uno
CREATE TABLE IF NOT EXISTS tg_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    folders TEXT NOT NULL,         -- JSON: carpetas de los botones rápidos
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_batch_items (
    batch_id INTEGER NOT NULL REFERENCES tg_batches(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL,
    PRIMARY KEY (batch_id, item_id)
);
CREATE INDEX IF NOT EXISTS tg_batch_items_item ON tg_batch_items(item_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path=DB_PATH):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Columnas añadidas después de la primera versión."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "note" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN note TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
        if "from_item" not in cols:  # imagen sacada de un vídeo («Solo imagen»): id del vídeo
            self.conn.execute("ALTER TABLE items ADD COLUMN from_item INTEGER")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(subscriptions)")}
        if "parts" not in cols:
            # parts: qué secciones del perfil se miran (Instagram); NULL = las de por defecto.
            # parts_done: secciones cuyo contenido ya existente se ha marcado como visto.
            self.conn.execute("ALTER TABLE subscriptions ADD COLUMN parts TEXT")
            self.conn.execute("ALTER TABLE subscriptions ADD COLUMN parts_done TEXT")
            # hasta ahora solo se miraban las publicaciones
            self.conn.execute("UPDATE subscriptions SET parts_done='posts' WHERE site='instagram' AND baseline_done=1")

    def q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        """Ejecuta y devuelve lastrowid (o filas afectadas si no es INSERT)."""
        with self._lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def get_setting(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key: str, value: str):
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


db = DB()
