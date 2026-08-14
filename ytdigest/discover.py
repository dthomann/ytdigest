"""RSS polling — one feed per enabled channel, sequential, dedup by primary key."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import requests

from .backfill import META_SEED_CUTOFF, initial_state_for_discovery
from .db import get_meta
from .models import VideoState
from .util import USER_AGENT, jittered_sleep, utcnow_iso

logger = logging.getLogger("ytdigest")

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


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


def default_fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return resp.text


def _normalize_published(entry) -> str | None:
    if getattr(entry, "published_parsed", None):
        return (
            datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        )
    return getattr(entry, "published", None)


def parse_feed(raw_xml: str) -> list[dict]:
    """Parse RSS XML into a list of {video_id, title, published_at, channel_id}."""
    parsed = feedparser.parse(raw_xml)
    out = []
    for entry in parsed.entries:
        video_id = getattr(entry, "yt_videoid", None)
        channel_id = getattr(entry, "yt_channelid", None)
        if not video_id or not channel_id:
            continue
        out.append(
            {
                "video_id": video_id,
                "channel_id": channel_id,
                "title": getattr(entry, "title", None),
                "published_at": _normalize_published(entry),
            }
        )
    return out


def discover_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    fetch_fn=default_fetch,
    dry_run: bool = False,
    seed_cutoff: str | None = None,
) -> ChannelDiscoverCounts:
    """Poll one channel's feed, insert unseen videos. Returns new vs backfilled counts."""
    if seed_cutoff is None:
        seed_cutoff = get_meta(conn, META_SEED_CUTOFF)

    url = FEED_URL.format(channel_id=channel_id)
    raw = fetch_fn(url)
    entries = parse_feed(raw)

    counts = ChannelDiscoverCounts()
    now = utcnow_iso()
    for entry in entries:
        if entry["channel_id"] != channel_id:
            # Feed sometimes reports a different channel_id (e.g. redirects); trust the feed's own value.
            continue
        if dry_run:
            cur = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (entry["video_id"],))
            if cur.fetchone() is None:
                if initial_state_for_discovery(entry["published_at"], seed_cutoff) == VideoState.DELIVERED.value:
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
            (entry["video_id"], channel_id, entry["title"], entry["published_at"], state, now, now),
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


def discover_all(
    conn: sqlite3.Connection,
    config,
    fetch_fn=default_fetch,
    dry_run: bool = False,
) -> DiscoverResult:
    result = DiscoverResult()
    channels = conn.execute(
        "SELECT channel_id, title, consecutive_errors FROM channels WHERE enabled = 1"
    ).fetchall()

    delay_low, delay_high = config.values["rss_delay_seconds"]
    max_errors = config.values["max_channel_consecutive_errors"]

    for i, ch in enumerate(channels):
        result.channels_polled += 1
        now = utcnow_iso()
        try:
            channel_counts = discover_channel(
                conn, ch["channel_id"], fetch_fn=fetch_fn, dry_run=dry_run
            )
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
