"""Channel CRUD helpers shared by CLI and web UI."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .util import utcnow_iso

# Older DBs used "sync" for YouTube subs and "import"/NULL for everything else.
_SUBSCRIBED_SOURCES = frozenset({"subscribed", "sync"})


def display_source(source: str | None) -> str:
    return "subscribed" if source in _SUBSCRIBED_SOURCES else "manual"


@dataclass
class ChannelRow:
    channel_id: str
    title: str | None
    handle: str | None
    enabled: bool
    source: str | None
    added_at: str
    consecutive_errors: int
    last_error: str | None

    @property
    def display_source(self) -> str:
        return display_source(self.source)


def list_channels(conn: sqlite3.Connection) -> list[ChannelRow]:
    rows = conn.execute(
        """
        SELECT channel_id, title, handle, enabled, source, added_at,
               consecutive_errors, last_error
        FROM channels
        ORDER BY CASE WHEN source IN ('subscribed', 'sync') THEN 1 ELSE 0 END,
                 COALESCE(title, channel_id) COLLATE NOCASE
        """
    ).fetchall()
    return [
        ChannelRow(
            channel_id=r["channel_id"],
            title=r["title"],
            handle=r["handle"],
            enabled=bool(r["enabled"]),
            source=r["source"],
            added_at=r["added_at"],
            consecutive_errors=r["consecutive_errors"],
            last_error=r["last_error"],
        )
        for r in rows
    ]


def get_channel(conn: sqlite3.Connection, channel_id: str) -> ChannelRow | None:
    r = conn.execute(
        """
        SELECT channel_id, title, handle, enabled, source, added_at,
               consecutive_errors, last_error
        FROM channels WHERE channel_id = ?
        """,
        (channel_id,),
    ).fetchone()
    if r is None:
        return None
    return ChannelRow(
        channel_id=r["channel_id"],
        title=r["title"],
        handle=r["handle"],
        enabled=bool(r["enabled"]),
        source=r["source"],
        added_at=r["added_at"],
        consecutive_errors=r["consecutive_errors"],
        last_error=r["last_error"],
    )


def add_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    title: str | None = None,
    handle: str | None = None,
    source: str = "manual",
    enable: bool = True,
) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO channels (channel_id, title, handle, added_at, enabled, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            enabled = excluded.enabled,
            title = COALESCE(excluded.title, channels.title),
            handle = COALESCE(excluded.handle, channels.handle)
        """,
        (channel_id, title, handle, now, 1 if enable else 0, source),
    )
    conn.commit()


def import_channels(
    conn: sqlite3.Connection,
    resolved: list,
    *,
    source: str = "manual",
) -> int:
    now = utcnow_iso()
    added = 0
    for r in resolved:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO channels (channel_id, title, handle, added_at, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (r.channel_id, r.title, r.handle, now, source),
        )
        added += cur.rowcount
    conn.commit()
    return added


def set_enabled(conn: sqlite3.Connection, channel_id: str, enabled: bool) -> bool:
    cur = conn.execute(
        "UPDATE channels SET enabled = ? WHERE channel_id = ?",
        (1 if enabled else 0, channel_id),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_channel(conn: sqlite3.Connection, channel_id: str) -> bool:
    cur = conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    conn.commit()
    return cur.rowcount > 0


def format_unhealthy_channel_lines(conn: sqlite3.Connection) -> list[str]:
    """One WARNING line per channel with consecutive_errors > 0 (CLI + Telegram /status)."""
    rows = conn.execute(
        """
        SELECT channel_id, title, consecutive_errors, last_error
        FROM channels
        WHERE consecutive_errors > 0
        ORDER BY consecutive_errors DESC, COALESCE(title, channel_id) COLLATE NOCASE
        """
    ).fetchall()
    lines = []
    for row in rows:
        label = row["title"] or row["channel_id"]
        suffix = ""
        err = (row["last_error"] or "").strip().replace("\n", " ")
        if err:
            if len(err) > 80:
                err = err[:77] + "..."
            suffix = f" ({err})"
        lines.append(
            f"WARNING: {label} — {row['consecutive_errors']} consecutive errors{suffix}"
        )
    return lines
