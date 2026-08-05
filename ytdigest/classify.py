"""short / live / normal routing.

Upcoming livestreams report duration = P0D, so live status must be checked before duration.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import requests

from .models import VideoKind, VideoState
from .util import utcnow_iso

logger = logging.getLogger("ytdigest")

RECHECK_STATES = {VideoState.DISCOVERED.value, VideoState.LIVE_UPCOMING.value, VideoState.LIVE_NOW.value}

SHORTS_PROBE_LOW = 60
SHORTS_PROBE_HIGH = 180
STALE_UPCOMING_GRACE = timedelta(hours=6)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("could not parse timestamp %r", ts)
        return None


def is_stale_upcoming(
    scheduled_start: str | None,
    published_at: str | None,
    duration_seconds: int | None,
    min_duration_seconds: int,
    now: datetime | None = None,
) -> bool:
    """True when YouTube's upcoming flag is no longer credible."""
    now = now or datetime.now(timezone.utc)

    start = _parse_iso(scheduled_start)
    if start is not None:
        return start + STALE_UPCOMING_GRACE < now

    if duration_seconds is not None and duration_seconds > min_duration_seconds:
        return True

    published = _parse_iso(published_at)
    if published is not None and published + STALE_UPCOMING_GRACE < now:
        return True

    return False


def classify_row(
    live_broadcast: str | None,
    actual_end: str | None,
    duration_seconds: int | None,
    min_duration_seconds: int,
    summarize_finished_livestreams: bool = False,
    scheduled_start: str | None = None,
    published_at: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Pure classification function. Returns (state, kind)."""
    if actual_end is not None:
        if summarize_finished_livestreams:
            return VideoState.NEEDS_TRANSCRIPT.value, VideoKind.LIVE.value
        return VideoState.LIVE_FINISHED.value, VideoKind.LIVE.value
    if live_broadcast == "upcoming":
        if is_stale_upcoming(
            scheduled_start,
            published_at,
            duration_seconds,
            min_duration_seconds,
            now=now,
        ):
            if summarize_finished_livestreams:
                return VideoState.NEEDS_TRANSCRIPT.value, VideoKind.LIVE.value
            return VideoState.LIVE_FINISHED.value, VideoKind.LIVE.value
        return VideoState.LIVE_UPCOMING.value, VideoKind.LIVE.value
    if live_broadcast == "live":
        return VideoState.LIVE_NOW.value, VideoKind.LIVE.value
    if duration_seconds is None:
        return VideoState.DISCOVERED.value, VideoKind.UNKNOWN.value
    if duration_seconds <= min_duration_seconds:
        return VideoState.SKIPPED_SHORT.value, VideoKind.SHORT.value
    return VideoState.NEEDS_TRANSCRIPT.value, VideoKind.NORMAL.value


def probe_is_short(video_id: str, fetch_fn=None) -> bool:
    """HEAD-probe youtube.com/shorts/{id}. 200 = short, redirect to /watch = normal video."""
    fetch_fn = fetch_fn or (
        lambda url: requests.head(url, allow_redirects=False, timeout=10)
    )
    resp = fetch_fn(f"https://www.youtube.com/shorts/{video_id}")
    status = getattr(resp, "status_code", None)
    return status == 200


def classify_all(
    conn: sqlite3.Connection,
    config,
    probe_fetch_fn=None,
) -> dict:
    """Classify every video that needs it. Returns counts by resulting state."""
    min_duration = config.values["min_duration_seconds"]
    summarize_finished = config.values["summarize_finished_livestreams"]
    shorts_probe_enabled = config.values["shorts_probe"]

    placeholders = ",".join("?" for _ in RECHECK_STATES)
    rows = conn.execute(
        f"SELECT * FROM videos WHERE state IN ({placeholders})", tuple(RECHECK_STATES)
    ).fetchall()

    counts: dict[str, int] = {}
    now = utcnow_iso()
    now_dt = datetime.now(timezone.utc)

    for row in rows:
        duration = row["duration_seconds"]
        state, kind = classify_row(
            row["live_broadcast"],
            row["actual_end"],
            duration,
            min_duration,
            summarize_finished,
            scheduled_start=row["scheduled_start"],
            published_at=row["published_at"],
            now=now_dt,
        )

        if (
            shorts_probe_enabled
            and state == VideoState.SKIPPED_SHORT.value
            and duration is not None
            and SHORTS_PROBE_LOW <= duration <= SHORTS_PROBE_HIGH
        ):
            try:
                if not probe_is_short(row["video_id"], fetch_fn=probe_fetch_fn):
                    state, kind = VideoState.NEEDS_TRANSCRIPT.value, VideoKind.NORMAL.value
            except Exception as exc:
                logger.warning("shorts_probe failed for %s: %s", row["video_id"], exc)

        announced_at = row["announced_at"]
        if state == VideoState.LIVE_UPCOMING.value and announced_at is None:
            announced_at = now

        conn.execute(
            """
            UPDATE videos
            SET state = ?, kind = ?, announced_at = ?, updated_at = ?
            WHERE video_id = ?
            """,
            (state, kind, announced_at, now, row["video_id"]),
        )
        counts[state] = counts.get(state, 0) + 1

    conn.commit()
    return counts
