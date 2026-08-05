from ytdigest import db
from ytdigest.util import utcnow_iso
from ytdigest.web.services import digest_queries


def _seed_run_with_videos(conn):
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO channels (channel_id, title, added_at, enabled) VALUES (?, ?, ?, 1)",
        ("UCtest", "Test Channel", now),
    )
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, summary, discovered_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("vid1", "UCtest", "Video One", "delivered", "Summary one", now, now),
    )
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, status, summarized) VALUES (?, ?, 'ok', 1)",
        (now, now),
    )
    run_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO run_videos (run_id, video_id, section) VALUES (?, ?, 'new_videos')",
        (run_id, "vid1"),
    )
    conn.commit()
    return run_id


def test_get_latest_run(conn):
    run_id = _seed_run_with_videos(conn)
    latest = digest_queries.get_latest_run(conn)
    assert latest is not None
    assert latest.id == run_id
    assert latest.summarized == 1


def test_get_run_videos(conn):
    run_id = _seed_run_with_videos(conn)
    videos = digest_queries.get_run_videos(conn, run_id, "new_videos")
    assert len(videos) == 1
    assert videos[0].video_id == "vid1"
    assert videos[0].summary == "Summary one"


def test_list_runs_pagination(conn):
    _seed_run_with_videos(conn)
    runs = digest_queries.list_runs(conn, limit=5, offset=0)
    assert len(runs) == 1
