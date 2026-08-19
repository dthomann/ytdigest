import pytest
import requests

from ytdigest.discover import (
    discover_all,
    discover_channel,
    parse_channels_response,
    parse_playlist_items,
    resolve_uploads_playlists,
)
from ytdigest.metadata import QuotaExceededError
from ytdigest.models import VideoState

from .conftest import insert_channel

UPLOADS_PLAYLIST = "UUnormal0000000000000000"
NORMAL_PLAYLIST_RESPONSE = {
    "items": [
        {
            "contentDetails": {
                "videoId": "vid_normal_001",
                "videoPublishedAt": "2026-08-03T14:00:00+00:00",
            }
        },
        {
            "contentDetails": {
                "videoId": "vid_normal_002",
                "videoPublishedAt": "2026-08-02T09:00:00+00:00",
            }
        },
    ]
}


def make_playlist_fetch(response_by_playlist):
    def fetch(playlist_id, api_key, max_results=20):
        if playlist_id not in response_by_playlist:
            raise requests.HTTPError(response=type("R", (), {"status_code": 404})())
        return response_by_playlist[playlist_id]

    return fetch


def make_channels_fetch(uploads_by_channel):
    def fetch(channel_ids, api_key):
        items = []
        for cid in channel_ids:
            uploads = uploads_by_channel.get(cid)
            if uploads is not None:
                items.append(
                    {
                        "id": cid,
                        "contentDetails": {"relatedPlaylists": {"uploads": uploads}},
                    }
                )
        return {"items": items}

    return fetch


def test_parse_channels_response_extracts_uploads_playlists():
    data = {
        "items": [
            {
                "id": "UCnormal0000000000000000",
                "contentDetails": {
                    "relatedPlaylists": {"uploads": UPLOADS_PLAYLIST}
                },
            }
        ]
    }
    parsed = parse_channels_response(data)
    assert parsed == {"UCnormal0000000000000000": UPLOADS_PLAYLIST}


def test_parse_playlist_items_extracts_entries():
    entries = parse_playlist_items(NORMAL_PLAYLIST_RESPONSE, "UCnormal0000000000000000")
    assert len(entries) == 2
    ids = {e["video_id"] for e in entries}
    assert ids == {"vid_normal_001", "vid_normal_002"}
    e = next(e for e in entries if e["video_id"] == "vid_normal_001")
    assert e["channel_id"] == "UCnormal0000000000000000"
    assert e["published_at"] == "2026-08-03T14:00:00+00:00"


def test_discover_channel_inserts_unseen_videos(conn):
    insert_channel(conn, "UCnormal0000000000000000")
    playlist_fetch = make_playlist_fetch({UPLOADS_PLAYLIST: NORMAL_PLAYLIST_RESPONSE})
    counts, units = discover_channel(
        conn,
        "UCnormal0000000000000000",
        UPLOADS_PLAYLIST,
        "test-key",
        playlist_fetch_fn=playlist_fetch,
    )
    assert counts.new == 2
    assert counts.backfilled == 0
    assert units == 1
    rows = conn.execute("SELECT * FROM videos").fetchall()
    assert len(rows) == 2
    assert all(r["state"] == VideoState.DISCOVERED.value for r in rows)


def test_discover_channel_backfills_pre_cutoff_videos(conn):
    from ytdigest.backfill import META_SEED_CUTOFF
    from ytdigest.db import set_meta

    insert_channel(conn, "UCnormal0000000000000000")
    set_meta(conn, META_SEED_CUTOFF, "2026-08-06")
    playlist_fetch = make_playlist_fetch({UPLOADS_PLAYLIST: NORMAL_PLAYLIST_RESPONSE})
    counts, _ = discover_channel(
        conn,
        "UCnormal0000000000000000",
        UPLOADS_PLAYLIST,
        "test-key",
        playlist_fetch_fn=playlist_fetch,
    )
    assert counts.new == 0
    assert counts.backfilled == 2
    rows = conn.execute("SELECT * FROM videos").fetchall()
    assert len(rows) == 2
    assert all(r["state"] == VideoState.DELIVERED.value for r in rows)


def test_reappearing_playlist_entries_produce_zero_new_rows(conn):
    insert_channel(conn, "UCnormal0000000000000000")
    playlist_fetch = make_playlist_fetch({UPLOADS_PLAYLIST: NORMAL_PLAYLIST_RESPONSE})
    discover_channel(
        conn,
        "UCnormal0000000000000000",
        UPLOADS_PLAYLIST,
        "test-key",
        playlist_fetch_fn=playlist_fetch,
    )
    conn.commit()
    counts, _ = discover_channel(
        conn,
        "UCnormal0000000000000000",
        UPLOADS_PLAYLIST,
        "test-key",
        playlist_fetch_fn=playlist_fetch,
    )
    assert counts.new == 0
    assert counts.backfilled == 0
    rows = conn.execute("SELECT * FROM videos").fetchall()
    assert len(rows) == 2


def test_resolve_uploads_playlists_caches_playlist_ids(conn):
    insert_channel(conn, "UCnormal0000000000000000")
    channels_fetch = make_channels_fetch(
        {"UCnormal0000000000000000": UPLOADS_PLAYLIST}
    )
    mapping, units = resolve_uploads_playlists(
        conn,
        ["UCnormal0000000000000000"],
        "test-key",
        channels_fetch_fn=channels_fetch,
    )
    assert mapping == {"UCnormal0000000000000000": UPLOADS_PLAYLIST}
    assert units == 1
    row = conn.execute(
        "SELECT uploads_playlist_id FROM channels WHERE channel_id = ?",
        ("UCnormal0000000000000000",),
    ).fetchone()
    assert row["uploads_playlist_id"] == UPLOADS_PLAYLIST


def test_discover_all_isolates_per_channel_failures(conn, config):
    insert_channel(conn, "UCnormal0000000000000000", title="Normal")
    insert_channel(conn, "UCbroken00000000000000000", title="Broken")

    def playlist_fetch(playlist_id, api_key, max_results=20):
        if playlist_id == "UUbroken00000000000000000":
            raise ConnectionError("simulated network failure")
        return NORMAL_PLAYLIST_RESPONSE

    channels_fetch = make_channels_fetch(
        {
            "UCnormal0000000000000000": UPLOADS_PLAYLIST,
            "UCbroken00000000000000000": "UUbroken00000000000000000",
        }
    )

    result = discover_all(
        conn,
        config,
        channels_fetch_fn=channels_fetch,
        playlist_fetch_fn=playlist_fetch,
    )
    assert result.channels_polled == 2
    assert result.channels_failed == 1
    assert result.new_videos == 2
    assert any("Broken" in w for w in result.dead_channel_warnings)

    broken = conn.execute(
        "SELECT * FROM channels WHERE channel_id = 'UCbroken00000000000000000'"
    ).fetchone()
    assert broken["consecutive_errors"] == 1
    assert "simulated network failure" in broken["last_error"]

    normal = conn.execute(
        "SELECT * FROM channels WHERE channel_id = 'UCnormal0000000000000000'"
    ).fetchone()
    assert normal["consecutive_errors"] == 0


def test_dead_channel_warning_after_max_consecutive_errors(conn, config):
    insert_channel(conn, "UCbroken00000000000000000", title="Broken")
    conn.execute(
        "UPDATE channels SET consecutive_errors = 9, uploads_playlist_id = 'UUbroken00000000000000000' "
        "WHERE channel_id = 'UCbroken00000000000000000'"
    )
    conn.commit()

    def playlist_fetch(playlist_id, api_key, max_results=20):
        raise ConnectionError("still broken")

    result = discover_all(conn, config, playlist_fetch_fn=playlist_fetch)
    assert len(result.dead_channel_warnings) == 1
    assert "Broken" in result.dead_channel_warnings[0]
    assert "has failed 10 consecutive polls" in result.dead_channel_warnings[0]


def test_discover_does_not_retry_immediately(conn, config):
    insert_channel(conn, "UCbroken00000000000000000", title="Broken")
    conn.execute(
        "UPDATE channels SET uploads_playlist_id = 'UUbroken00000000000000000' "
        "WHERE channel_id = 'UCbroken00000000000000000'"
    )
    conn.commit()
    calls = {"n": 0}

    def playlist_fetch(playlist_id, api_key, max_results=20):
        calls["n"] += 1
        raise ConnectionError("still down")

    result = discover_all(conn, config, playlist_fetch_fn=playlist_fetch)
    assert calls["n"] == 1
    assert result.channels_failed == 1
    assert len(result.dead_channel_warnings) == 1
    assert "Broken" in result.dead_channel_warnings[0]
    assert "still down" in result.dead_channel_warnings[0]
    broken = conn.execute("SELECT consecutive_errors FROM channels").fetchone()
    assert broken["consecutive_errors"] == 1


def test_dry_run_discover_makes_no_writes(conn, config):
    insert_channel(conn, "UCnormal0000000000000000")
    channels_fetch = make_channels_fetch(
        {"UCnormal0000000000000000": UPLOADS_PLAYLIST}
    )
    playlist_fetch = make_playlist_fetch({UPLOADS_PLAYLIST: NORMAL_PLAYLIST_RESPONSE})
    result = discover_all(
        conn,
        config,
        channels_fetch_fn=channels_fetch,
        playlist_fetch_fn=playlist_fetch,
        dry_run=True,
    )
    assert result.new_videos == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == 0
    row = conn.execute("SELECT last_polled_at FROM channels").fetchone()
    assert row["last_polled_at"] is None


def test_discover_all_aborts_on_quota_threshold(conn, config):
    insert_channel(conn, "UCnormal0000000000000000")
    conn.execute(
        "UPDATE channels SET uploads_playlist_id = ? WHERE channel_id = ?",
        (UPLOADS_PLAYLIST, "UCnormal0000000000000000"),
    )
    conn.commit()

    with pytest.raises(QuotaExceededError):
        discover_all(
            conn,
            config,
            quota_used_today=9000,
            quota_daily=10000,
            quota_warn_fraction=0.9,
            playlist_fetch_fn=make_playlist_fetch({UPLOADS_PLAYLIST: NORMAL_PLAYLIST_RESPONSE}),
        )
