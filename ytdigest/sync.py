"""YouTube subscription sync — compare remote subs with local DB (sync-down only)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import channels as channels_mod
from .util import utcnow_iso


@dataclass
class SyncResult:
    added: list[channels_mod.ChannelRow] = field(default_factory=list)
    suggested_removals: list[channels_mod.ChannelRow] = field(default_factory=list)
    unchanged_disabled: list[channels_mod.ChannelRow] = field(default_factory=list)


def compare_subscriptions(
    conn: sqlite3.Connection,
    youtube_channel_ids: set[str],
    youtube_titles: dict[str, str | None] | None = None,
) -> SyncResult:
    """Compare YouTube subscription IDs with local channels.

    - New on YouTube, not in DB → added (auto-applied by apply_sync)
    - Enabled locally, not on YouTube → suggested_removals
    - Disabled locally → unchanged_disabled (never auto-touched)
    """
    youtube_titles = youtube_titles or {}
    local = channels_mod.list_channels(conn)
    local_by_id = {c.channel_id: c for c in local}
    yt_ids = youtube_channel_ids

    result = SyncResult()

    for cid in yt_ids - set(local_by_id):
        result.added.append(
            channels_mod.ChannelRow(
                channel_id=cid,
                title=youtube_titles.get(cid),
                handle=None,
                enabled=True,
                source="sync",
                added_at=utcnow_iso(),
                consecutive_errors=0,
                last_error=None,
            )
        )

    for ch in local:
        if not ch.enabled:
            result.unchanged_disabled.append(ch)
        elif ch.channel_id not in yt_ids:
            result.suggested_removals.append(ch)

    return result


def apply_sync_additions(conn: sqlite3.Connection, to_add: list[channels_mod.ChannelRow]) -> int:
    count = 0
    for ch in to_add:
        channels_mod.add_channel(
            conn,
            ch.channel_id,
            title=ch.title,
            handle=ch.handle,
            source="sync",
            enable=True,
        )
        count += 1
    return count


def apply_sync_removals(conn: sqlite3.Connection, channel_ids: list[str]) -> int:
    removed = 0
    for cid in channel_ids:
        if channels_mod.remove_channel(conn, cid):
            removed += 1
    return removed
