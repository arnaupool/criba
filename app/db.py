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
    log TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, kind);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
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
