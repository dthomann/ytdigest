"""Scheduled-run retry state: 1h later full pipeline, up to N times.

Used only when systemd invokes `ytdigest run --scheduled`. Manual CLI / web / bot runs
do not write this state. The companion `ytdigest-retry.timer` polls every 15 minutes
and runs `--scheduled --retry-only`, which no-ops unless a retry is due.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import get_meta, set_meta
from .models import VideoState

META_RETRY_AT = "scheduled_retry_at"
META_RETRY_ATTEMPT = "scheduled_retry_attempt"


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def retry_attempt(conn: sqlite3.Connection) -> int:
    raw = get_meta(conn, META_RETRY_ATTEMPT)
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def retry_at(conn: sqlite3.Connection) -> str | None:
    return get_meta(conn, META_RETRY_AT)


def is_retry_due(conn: sqlite3.Connection, now: datetime) -> bool:
    if retry_attempt(conn) < 1:
        return False
    due = _parse_iso(retry_at(conn) or "")
    return due is not None and due <= now


def in_digest_hour(config, now: datetime) -> bool:
    tz = ZoneInfo(config.values["timezone"])
    return now.astimezone(tz).hour == config.values["digest_hour"]


def clear(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM meta WHERE key IN (?, ?)",
        (META_RETRY_AT, META_RETRY_ATTEMPT),
    )
    conn.commit()


def schedule_next(
    conn: sqlite3.Connection,
    config,
    *,
    is_retry_run: bool,
    now: datetime,
) -> str | None:
    """Record the next 1h retry, or clear state if the cap is exhausted.

    Returns a note for run logs, or None when retries are exhausted (state cleared).
    """
    max_n = config.values["max_scheduled_retries"]
    delay_h = config.values["scheduled_retry_delay_hours"]
    current = retry_attempt(conn)
    next_attempt = current + 1 if is_retry_run else 1
    if next_attempt > max_n:
        clear(conn)
        return None

    due = (now + timedelta(hours=delay_h)).replace(microsecond=0).isoformat()
    set_meta(conn, META_RETRY_AT, due)
    set_meta(conn, META_RETRY_ATTEMPT, str(next_attempt))
    return f"scheduled retry {next_attempt}/{max_n} at {due}"


def align_transcript_retries(
    conn: sqlite3.Connection, video_ids: list[str], due_at: str
) -> None:
    """Make retryable videos eligible when the 1h full-run retry fires."""
    for video_id in video_ids:
        conn.execute(
            """
            UPDATE videos
            SET next_retry_at = ?
            WHERE video_id = ? AND state = ?
            """,
            (due_at, video_id, VideoState.NEEDS_TRANSCRIPT.value),
        )
    if video_ids:
        conn.commit()
