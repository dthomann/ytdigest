"""YouTube Data API v3 — batched metadata lookups (videos.list, 1 unit/call, up to 50 IDs)."""
from __future__ import annotations

import logging
import sqlite3

import isodate
import requests

from .util import utcnow_iso

logger = logging.getLogger("ytdigest")

API_URL = "https://www.googleapis.com/youtube/v3/videos"
BATCH_SIZE = 50


class QuotaExceededError(Exception):
    pass


def default_fetch(ids: list[str], api_key: str) -> dict:
    resp = requests.get(
        API_URL,
        params={
            "part": "snippet,contentDetails,liveStreamingDetails",
            "id": ",".join(ids),
            "key": api_key,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def parse_duration(iso_duration: str | None) -> int | None:
    if not iso_duration:
        return None
    try:
        return int(isodate.parse_duration(iso_duration).total_seconds())
    except (isodate.ISO8601Error, ValueError):
        logger.warning("could not parse duration %r", iso_duration)
        return None


def _extract(item: dict) -> dict:
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    live = item.get("liveStreamingDetails", {})
    return {
        "video_id": item["id"],
        "title": snippet.get("title"),
        "channel_title": snippet.get("channelTitle"),
        "duration_seconds": parse_duration(content.get("duration")),
        "live_broadcast": snippet.get("liveBroadcastContent", "none"),
        "scheduled_start": live.get("scheduledStartTime"),
        "actual_end": live.get("actualEndTime"),
    }


def fetch_metadata_batch(
    video_ids: list[str], api_key: str, fetch_fn=default_fetch
) -> tuple[list[dict], int]:
    """Fetch metadata for up to 50 video IDs. Returns (items, api_units_used)."""
    if not video_ids:
        return [], 0
    data = fetch_fn(video_ids, api_key)
    items = [_extract(item) for item in data.get("items", [])]
    return items, 1  # videos.list = 1 unit regardless of id count


def fetch_all_metadata(
    video_ids: list[str],
    api_key: str,
    fetch_fn=default_fetch,
    quota_used_today: int = 0,
    quota_daily: int = 10000,
    quota_warn_fraction: float = 0.9,
) -> tuple[list[dict], set[str], int]:
    """Fetch metadata for all given video IDs in batches of 50.

    Returns (items, missing_ids, total_api_units_used). Raises QuotaExceededError if the
    warn threshold would be crossed before making a call.
    """
    all_items: list[dict] = []
    seen_ids: set[str] = set()
    units_used = 0
    threshold = int(quota_daily * quota_warn_fraction)

    for i in range(0, len(video_ids), BATCH_SIZE):
        if quota_used_today + units_used + 1 > threshold:
            raise QuotaExceededError(
                f"Aborting: YouTube Data API quota would exceed "
                f"{quota_warn_fraction:.0%} of daily quota ({quota_daily})."
            )
        batch = video_ids[i : i + BATCH_SIZE]
        items, units = fetch_metadata_batch(batch, api_key, fetch_fn=fetch_fn)
        units_used += units
        for item in items:
            seen_ids.add(item["video_id"])
        all_items.extend(items)

    missing = set(video_ids) - seen_ids
    return all_items, missing, units_used


def apply_metadata(conn: sqlite3.Connection, items: list[dict], missing: set[str]) -> None:
    now = utcnow_iso()
    for item in items:
        conn.execute(
            """
            UPDATE videos
            SET title = ?, duration_seconds = ?, live_broadcast = ?,
                scheduled_start = ?, actual_end = ?, updated_at = ?
            WHERE video_id = ?
            """,
            (
                item["title"],
                item["duration_seconds"],
                item["live_broadcast"],
                item["scheduled_start"],
                item["actual_end"],
                now,
                item["video_id"],
            ),
        )
    for video_id in missing:
        conn.execute(
            """
            UPDATE videos
            SET state = 'failed_permanent', last_error = 'deleted or private (missing from videos.list)',
                updated_at = ?
            WHERE video_id = ?
            """,
            (now, video_id),
        )
    conn.commit()
