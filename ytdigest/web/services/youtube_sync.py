"""Run YouTube subscription sync (shared by routes)."""
from __future__ import annotations

import sqlite3

from ... import sync, youtube_oauth
from ...config import Config
from .sync_flash import SyncFlash


def run_youtube_sync(
    conn: sqlite3.Connection,
    config: Config,
    oauth_cfg: youtube_oauth.OAuthConfig,
    *,
    connected: bool = False,
) -> SyncFlash:
    try:
        access = youtube_oauth.get_valid_access_token(conn, oauth_cfg)
    except youtube_oauth.OAuthExpired as exc:
        return SyncFlash(error=str(exc))
    if not access:
        return SyncFlash(error="Not connected to YouTube.")

    subs = youtube_oauth.fetch_subscriptions(access)
    yt_ids = {s.channel_id for s in subs}
    yt_titles = {s.channel_id: s.title for s in subs}
    result = sync.compare_subscriptions(conn, yt_ids, yt_titles)
    added_count = sync.apply_sync_additions(conn, result.added)
    sync.update_subscription_sources(conn, yt_ids)

    return SyncFlash(
        connected=connected,
        added_count=added_count,
        added_titles=[ch.title or ch.channel_id for ch in result.added],
        suggested_removals=[
            {"channel_id": ch.channel_id, "title": ch.title or ch.channel_id}
            for ch in result.suggested_removals
        ],
        unchanged_disabled_count=len(result.unchanged_disabled),
    )
