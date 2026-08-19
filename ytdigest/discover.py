"""YouTube Data API v3 — discover new uploads via cached uploads playlist IDs."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field

import requests

from .backfill import META_SEED_CUTOFF, initial_state_for_discovery
from .db import get_meta
from .metadata import QuotaExceededError
from .models import VideoState
from .util import jittered_sleep, utcnow_iso

logger = logging.getLogger("ytdigest")

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
BATCH_SIZE = 50
PLAYLIST_PAGE_SIZE = 20
MAX_RETRIES = 3
REQUEST_TIMEOUT = 20


@dataclass
class ChannelDiscoverCounts:
    new: int = 0
    backfilled: int = 0


@dataclass
class DiscoverResult:
    channels_polled: int = 0
    channels_failed: int = 0
    new_videos: int = 0
    backfilled_videos: int = 0
    dead_channel_warnings: list[str] = field(default_factory=list)
    api_units: int = 0


def _quota_threshold(quota_daily: int, quota_warn_fraction: float) -> int:
    return int(quota_daily * quota_warn_fraction)


def _check_quota(
    quota_used_today: int,
    units_needed: int,
    quota_daily: int,
    quota_warn_fraction: float,
) -> None:
    threshold = _quota_threshold(quota_daily, quota_warn_fraction)
    if quota_used_today + units_needed > threshold:
        raise QuotaExceededError(
            f"Aborting: YouTube Data API quota would exceed "
            f"{quota_warn_fraction:.0%} of daily quota ({quota_daily})."
        )


def _request_json(url: str, params: dict) -> dict:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                resp.raise_for_status()
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            if status == 404:
                raise
            if status in (429, None) or (status is not None and status >= 500):
                if attempt < MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("request failed without exception")


def default_channels_fetch(channel_ids: list[str], api_key: str) -> dict:
    return _request_json(
        CHANNELS_URL,
        {
            "part": "contentDetails",
            "id": ",".join(channel_ids),
            "key": api_key,
        },
    )


def default_playlist_items_fetch(
    playlist_id: str, api_key: str, max_results: int = PLAYLIST_PAGE_SIZE
) -> dict:
    return _request_json(
        PLAYLIST_ITEMS_URL,
        {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": max_results,
            "key": api_key,
        },
    )


def parse_channels_response(data: dict) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for item in data.get("items", []):
        channel_id = item.get("id")
        if not channel_id:
            continue
        uploads = (
            item.get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads")
        )
        out[channel_id] = uploads or None
    return out


def parse_playlist_items(data: dict, channel_id: str) -> list[dict]:
    out: list[dict] = []
    for item in data.get("items", []):
        content = item.get("contentDetails", {})
        video_id = content.get("videoId")
        if not video_id:
            continue
        out.append(
            {
                "video_id": video_id,
                "channel_id": channel_id,
                "title": None,
                "published_at": content.get("videoPublishedAt"),
            }
        )
    return out


def resolve_uploads_playlists(
    conn: sqlite3.Connection,
    channel_ids: list[str],
    api_key: str,
    *,
    channels_fetch_fn=default_channels_fetch,
    quota_used_today: int = 0,
    quota_daily: int = 10000,
    quota_warn_fraction: float = 0.9,
) -> tuple[dict[str, str | None], int]:
    """Resolve uploads playlist IDs for channels. Returns mapping and API units used."""
    if not channel_ids:
        return {}, 0

    resolved: dict[str, str | None] = {}
    units_used = 0

    for i in range(0, len(channel_ids), BATCH_SIZE):
        batch = channel_ids[i : i + BATCH_SIZE]
        _check_quota(
            quota_used_today + units_used,
            1,
            quota_daily,
            quota_warn_fraction,
        )
        data = channels_fetch_fn(batch, api_key)
        units_used += 1
        batch_resolved = parse_channels_response(data)
        now = utcnow_iso()
        for channel_id in batch:
            uploads_id = batch_resolved.get(channel_id)
            resolved[channel_id] = uploads_id
            conn.execute(
                """
                UPDATE channels
                SET uploads_playlist_id = ?, last_error = ?
                WHERE channel_id = ?
                """,
                (
                    uploads_id,
                    None if uploads_id else "no uploads playlist (topic channel?)",
                    channel_id,
                ),
            )
        conn.commit()
        logger.debug(
            "resolved uploads playlists for %d channel(s) at %s",
            len(batch),
            now,
        )

    return resolved, units_used


def _insert_entries(
    conn: sqlite3.Connection,
    channel_id: str,
    entries: list[dict],
    *,
    dry_run: bool,
    seed_cutoff: str | None,
) -> ChannelDiscoverCounts:
    counts = ChannelDiscoverCounts()
    now = utcnow_iso()
    for entry in entries:
        if entry["channel_id"] != channel_id:
            continue
        if dry_run:
            cur = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (entry["video_id"],))
            if cur.fetchone() is None:
                if (
                    initial_state_for_discovery(entry["published_at"], seed_cutoff)
                    == VideoState.DELIVERED.value
                ):
                    counts.backfilled += 1
                else:
                    counts.new += 1
            continue

        state = initial_state_for_discovery(entry["published_at"], seed_cutoff)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO videos
                (video_id, channel_id, title, published_at, state, discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["video_id"],
                channel_id,
                entry["title"],
                entry["published_at"],
                state,
                now,
                now,
            ),
        )
        if cur.rowcount:
            if state == VideoState.DELIVERED.value:
                counts.backfilled += 1
                logger.info(
                    "backfilled %s (published %s before seed cutoff %s)",
                    entry["video_id"],
                    entry["published_at"],
                    seed_cutoff,
                )
            else:
                counts.new += 1
    return counts


def discover_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    uploads_playlist_id: str,
    api_key: str,
    *,
    playlist_fetch_fn=default_playlist_items_fetch,
    dry_run: bool = False,
    seed_cutoff: str | None = None,
    quota_used_today: int = 0,
    quota_daily: int = 10000,
    quota_warn_fraction: float = 0.9,
    retry_resolve: bool = True,
    channels_fetch_fn=default_channels_fetch,
) -> tuple[ChannelDiscoverCounts, int]:
    """Poll one channel's uploads playlist. Returns counts and API units used."""
    if seed_cutoff is None:
        seed_cutoff = get_meta(conn, META_SEED_CUTOFF)

    units_used = 0
    _check_quota(quota_used_today, 1, quota_daily, quota_warn_fraction)

    try:
        data = playlist_fetch_fn(uploads_playlist_id, api_key)
        units_used += 1
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404 and retry_resolve:
            logger.warning(
                "uploads playlist %s for channel %s returned 404; re-resolving",
                uploads_playlist_id,
                channel_id,
            )
            conn.execute(
                "UPDATE channels SET uploads_playlist_id = NULL WHERE channel_id = ?",
                (channel_id,),
            )
            conn.commit()
            mapping, resolve_units = resolve_uploads_playlists(
                conn,
                [channel_id],
                api_key,
                channels_fetch_fn=channels_fetch_fn,
                quota_used_today=quota_used_today + units_used,
                quota_daily=quota_daily,
                quota_warn_fraction=quota_warn_fraction,
            )
            units_used += resolve_units
            new_playlist_id = mapping.get(channel_id)
            if not new_playlist_id:
                raise ValueError(
                    f"could not resolve uploads playlist for channel {channel_id}"
                ) from exc
            return discover_channel(
                conn,
                channel_id,
                new_playlist_id,
                api_key,
                playlist_fetch_fn=playlist_fetch_fn,
                dry_run=dry_run,
                seed_cutoff=seed_cutoff,
                quota_used_today=quota_used_today + units_used,
                quota_daily=quota_daily,
                quota_warn_fraction=quota_warn_fraction,
                retry_resolve=False,
                channels_fetch_fn=channels_fetch_fn,
            )
        raise

    entries = parse_playlist_items(data, channel_id)
    counts = _insert_entries(
        conn,
        channel_id,
        entries,
        dry_run=dry_run,
        seed_cutoff=seed_cutoff,
    )
    return counts, units_used


def discover_all(
    conn: sqlite3.Connection,
    config,
    *,
    api_key: str | None = None,
    dry_run: bool = False,
    quota_used_today: int = 0,
    quota_daily: int | None = None,
    quota_warn_fraction: float | None = None,
    channels_fetch_fn=default_channels_fetch,
    playlist_fetch_fn=default_playlist_items_fetch,
) -> DiscoverResult:
    result = DiscoverResult()
    api_key = api_key or config.secrets.get("YOUTUBE_API_KEY")
    quota_daily = quota_daily if quota_daily is not None else config.values["youtube_api_quota_daily"]
    quota_warn_fraction = (
        quota_warn_fraction
        if quota_warn_fraction is not None
        else config.values["youtube_api_quota_warn_fraction"]
    )

    channels = conn.execute(
        """
        SELECT channel_id, title, consecutive_errors, uploads_playlist_id
        FROM channels
        WHERE enabled = 1
        """
    ).fetchall()

    if not api_key:
        for ch in channels:
            result.channels_polled += 1
            result.channels_failed += 1
            label = ch["title"] or ch["channel_id"]
            msg = "YOUTUBE_API_KEY not set — cannot discover via YouTube API"
            result.dead_channel_warnings.append(f"{label} ({ch['channel_id']}) poll failed: {msg}")
            if not dry_run:
                conn.execute(
                    """
                    UPDATE channels
                    SET consecutive_errors = consecutive_errors + 1, last_error = ?
                    WHERE channel_id = ?
                    """,
                    (msg, ch["channel_id"]),
                )
        if not dry_run:
            conn.commit()
        return result

    missing_playlist_ids = [
        ch["channel_id"] for ch in channels if not ch["uploads_playlist_id"]
    ]
    try:
        _, resolve_units = resolve_uploads_playlists(
            conn,
            missing_playlist_ids,
            api_key,
            channels_fetch_fn=channels_fetch_fn,
            quota_used_today=quota_used_today,
            quota_daily=quota_daily,
            quota_warn_fraction=quota_warn_fraction,
        )
        result.api_units += resolve_units
        quota_used_today += resolve_units
    except QuotaExceededError:
        raise

    channels = conn.execute(
        """
        SELECT channel_id, title, consecutive_errors, uploads_playlist_id
        FROM channels
        WHERE enabled = 1
        """
    ).fetchall()

    delay_low, delay_high = config.values["rss_delay_seconds"]
    max_errors = config.values["max_channel_consecutive_errors"]

    for i, ch in enumerate(channels):
        result.channels_polled += 1
        now = utcnow_iso()
        uploads_playlist_id = ch["uploads_playlist_id"]
        if not uploads_playlist_id:
            result.channels_failed += 1
            errors = ch["consecutive_errors"] + 1
            exc_msg = "no uploads playlist (topic channel?)"
            logger.warning("discover skipped channel %s: %s", ch["channel_id"], exc_msg)
            if not dry_run:
                conn.execute(
                    """
                    UPDATE channels
                    SET last_polled_at = ?, consecutive_errors = ?, last_error = ?
                    WHERE channel_id = ?
                    """,
                    (now, errors, exc_msg, ch["channel_id"]),
                )
            label = ch["title"] or ch["channel_id"]
            if errors >= max_errors:
                result.dead_channel_warnings.append(
                    f"{label} ({ch['channel_id']}) has failed {errors} consecutive polls: {exc_msg}"
                )
            else:
                result.dead_channel_warnings.append(
                    f"{label} ({ch['channel_id']}) poll failed ({errors} consecutive): {exc_msg}"
                )
            if not dry_run:
                conn.commit()
            if i < len(channels) - 1:
                jittered_sleep(delay_low, delay_high, dry_run=dry_run)
            continue

        try:
            channel_counts, units = discover_channel(
                conn,
                ch["channel_id"],
                uploads_playlist_id,
                api_key,
                playlist_fetch_fn=playlist_fetch_fn,
                dry_run=dry_run,
                quota_used_today=quota_used_today + result.api_units,
                quota_daily=quota_daily,
                quota_warn_fraction=quota_warn_fraction,
                channels_fetch_fn=channels_fetch_fn,
            )
            result.api_units += units
            result.new_videos += channel_counts.new
            result.backfilled_videos += channel_counts.backfilled
            if not dry_run:
                conn.execute(
                    """
                    UPDATE channels
                    SET last_polled_at = ?, consecutive_errors = 0, last_error = NULL
                    WHERE channel_id = ?
                    """,
                    (now, ch["channel_id"]),
                )
        except QuotaExceededError:
            raise
        except Exception as exc:
            result.channels_failed += 1
            errors = ch["consecutive_errors"] + 1
            logger.warning("discover failed for channel %s: %s", ch["channel_id"], exc)
            if not dry_run:
                conn.execute(
                    """
                    UPDATE channels
                    SET last_polled_at = ?, consecutive_errors = ?, last_error = ?
                    WHERE channel_id = ?
                    """,
                    (now, errors, str(exc), ch["channel_id"]),
                )
            label = ch["title"] or ch["channel_id"]
            if errors >= max_errors:
                result.dead_channel_warnings.append(
                    f"{label} ({ch['channel_id']}) has failed {errors} consecutive polls: {exc}"
                )
            else:
                result.dead_channel_warnings.append(
                    f"{label} ({ch['channel_id']}) poll failed ({errors} consecutive): {exc}"
                )
        if not dry_run:
            conn.commit()
        if i < len(channels) - 1:
            jittered_sleep(delay_low, delay_high, dry_run=dry_run)

    return result
