"""Backfill detection — videos published before the seed cutoff are never treated as new."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from .models import VideoState

META_SEED_CUTOFF = "seed_cutoff_date"

_CUTOFF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# States that should never apply to pre-cutoff catalogue videos.
_STUCK_PIPELINE_STATES = (
    VideoState.NEEDS_TRANSCRIPT.value,
    VideoState.DISCOVERED.value,
    VideoState.HAS_TRANSCRIPT.value,
    VideoState.SUMMARIZED.value,
)


def validate_cutoff_date(since: str) -> str:
    """Return `since` if it is a valid YYYY-MM-DD string."""
    if not _CUTOFF_RE.match(since):
        raise ValueError(f"invalid cutoff date {since!r} — expected YYYY-MM-DD")
    datetime.strptime(since, "%Y-%m-%d")  # raises ValueError on impossible dates
    return since


def cutoff_start_utc(since: str) -> datetime:
    """UTC midnight at the start of the seed `--since` day."""
    validate_cutoff_date(since)
    return datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _parse_published_at(published_at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_backfill(published_at: str | None, since: str | None) -> bool:
    """True when `published_at` is strictly before the seed cutoff day (UTC)."""
    if not since or not published_at:
        return False
    published = _parse_published_at(published_at)
    if published is None:
        return False
    return published < cutoff_start_utc(since)


def initial_state_for_discovery(published_at: str | None, since: str | None) -> str:
    if is_backfill(published_at, since):
        return VideoState.DELIVERED.value
    return VideoState.DISCOVERED.value


def fix_stuck_backfill(conn: sqlite3.Connection, since: str, *, now: str) -> list[str]:
    """Mark pipeline-state videos published before `since` as delivered. Returns fixed IDs."""
    validate_cutoff_date(since)
    cutoff_iso = cutoff_start_utc(since).isoformat()
    placeholders = ",".join("?" for _ in _STUCK_PIPELINE_STATES)
    rows = conn.execute(
        f"""
        SELECT video_id FROM videos
        WHERE state IN ({placeholders})
          AND published_at IS NOT NULL
          AND published_at < ?
        """,
        (*_STUCK_PIPELINE_STATES, cutoff_iso),
    ).fetchall()
    if not rows:
        return []

    video_ids = [r["video_id"] for r in rows]
    conn.executemany(
        """
        UPDATE videos
        SET state = ?, next_retry_at = NULL, last_error = NULL,
            attempts = 0, updated_at = ?
        WHERE video_id = ?
        """,
        [(VideoState.DELIVERED.value, now, vid) for vid in video_ids],
    )
    conn.commit()
    return video_ids
