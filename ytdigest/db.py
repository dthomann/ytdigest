"""Schema, migrations, connection helpers. SQLite in WAL mode."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id      TEXT PRIMARY KEY,
    title           TEXT,
    handle          TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    added_at        TEXT NOT NULL,
    last_polled_at  TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL REFERENCES channels(channel_id),
    title               TEXT,
    published_at        TEXT,
    duration_seconds    INTEGER,
    live_broadcast      TEXT,
    scheduled_start      TEXT,
    actual_end          TEXT,
    kind                TEXT,
    state               TEXT NOT NULL,
    announced_at        TEXT,
    transcript_source   TEXT,
    transcript_lang     TEXT,
    transcript_auto     INTEGER,
    transcript_chars    INTEGER,
    summary             TEXT,
    summary_model       TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_retry_at       TEXT,
    last_error          TEXT,
    discovered_at       TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_state ON videos(state, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id, published_at);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    discovered   INTEGER DEFAULT 0,
    summarized   INTEGER DEFAULT 0,
    failed       INTEGER DEFAULT 0,
    api_units    INTEGER DEFAULT 0,
    status       TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    message_id   TEXT PRIMARY KEY,
    video_id     TEXT REFERENCES videos(video_id),
    run_id       INTEGER REFERENCES runs(id),
    sent_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_videos (
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    video_id     TEXT NOT NULL REFERENCES videos(video_id),
    section      TEXT NOT NULL,
    PRIMARY KEY (run_id, video_id)
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider        TEXT PRIMARY KEY,
    refresh_token   TEXT,
    access_token    TEXT,
    expires_at      TEXT,
    updated_at      TEXT NOT NULL
);
"""

MIGRATIONS = [
    "ALTER TABLE channels ADD COLUMN source TEXT",
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations (idempotent)."""
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
