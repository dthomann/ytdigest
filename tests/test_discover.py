from ytdigest.discover import discover_all, discover_channel, parse_feed
from ytdigest.models import VideoState

from .conftest import insert_channel, load_fixture_text


def test_parse_feed_extracts_entries():
    raw = load_fixture_text("rss_normal_channel.xml")
    entries = parse_feed(raw)
    assert len(entries) == 2
    ids = {e["video_id"] for e in entries}
    assert ids == {"vid_normal_001", "vid_normal_002"}
    e = next(e for e in entries if e["video_id"] == "vid_normal_001")
    assert e["title"] == "A Deep Dive Into Something Real"
    assert e["channel_id"] == "UCnormal0000000000000000"
    assert e["published_at"] is not None


def test_discover_channel_inserts_unseen_videos(conn):
    insert_channel(conn, "UCnormal0000000000000000")
    fetch = lambda url: load_fixture_text("rss_normal_channel.xml")
    counts = discover_channel(conn, "UCnormal0000000000000000", fetch_fn=fetch)
    assert counts.new == 2
    assert counts.backfilled == 0
    rows = conn.execute("SELECT * FROM videos").fetchall()
    assert len(rows) == 2
    assert all(r["state"] == VideoState.DISCOVERED.value for r in rows)


def test_discover_channel_backfills_pre_cutoff_videos(conn):
    from ytdigest.db import set_meta
    from ytdigest.backfill import META_SEED_CUTOFF

    insert_channel(conn, "UCnormal0000000000000000")
    set_meta(conn, META_SEED_CUTOFF, "2026-08-06")
    fetch = lambda url: load_fixture_text("rss_normal_channel.xml")
    counts = discover_channel(conn, "UCnormal0000000000000000", fetch_fn=fetch)
    assert counts.new == 0
    assert counts.backfilled == 2
    rows = conn.execute("SELECT * FROM videos").fetchall()
    assert len(rows) == 2
    assert all(r["state"] == VideoState.DELIVERED.value for r in rows)


def test_reappearing_rss_entries_produce_zero_new_rows(conn):
    insert_channel(conn, "UCnormal0000000000000000")
    fetch = lambda url: load_fixture_text("rss_normal_channel.xml")
    discover_channel(conn, "UCnormal0000000000000000", fetch_fn=fetch)
    conn.commit()
    # feed re-polled, same entries reappear (feeds reorder / republish on edit)
    counts = discover_channel(conn, "UCnormal0000000000000000", fetch_fn=fetch)
    assert counts.new == 0
    assert counts.backfilled == 0
    rows = conn.execute("SELECT * FROM videos").fetchall()
    assert len(rows) == 2


def test_discover_all_isolates_per_channel_failures(conn, config):
    insert_channel(conn, "UCnormal0000000000000000", title="Normal")
    insert_channel(conn, "UCbroken00000000000000000", title="Broken")

    def fetch(url):
        if "UCbroken00000000000000000" in url:
            raise ConnectionError("simulated network failure")
        return load_fixture_text("rss_normal_channel.xml")

    result = discover_all(conn, config, fetch_fn=fetch)
    assert result.channels_polled == 2
    assert result.channels_failed == 1
    assert result.new_videos == 2

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
        "UPDATE channels SET consecutive_errors = 9 WHERE channel_id = 'UCbroken00000000000000000'"
    )
    conn.commit()

    def fetch(url):
        raise ConnectionError("still broken")

    result = discover_all(conn, config, fetch_fn=fetch)
    assert len(result.dead_channel_warnings) == 1
    assert "Broken" in result.dead_channel_warnings[0]


def test_dry_run_discover_makes_no_writes(conn, config):
    insert_channel(conn, "UCnormal0000000000000000")
    fetch = lambda url: load_fixture_text("rss_normal_channel.xml")
    result = discover_all(conn, config, fetch_fn=fetch, dry_run=True)
    assert result.new_videos == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == 0
    row = conn.execute("SELECT last_polled_at FROM channels").fetchone()
    assert row["last_polled_at"] is None
