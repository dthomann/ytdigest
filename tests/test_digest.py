from ytdigest.digest import build_digest, render_markdown, write_digest_file
from ytdigest.models import VideoState

from .conftest import insert_channel


def insert_video(conn, video_id, channel_id, state, title="Title", **kwargs):
    fields = {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": title,
        "state": state,
        "discovered_at": "now",
        "updated_at": "now",
        "kind": kwargs.get("kind"),
        "scheduled_start": kwargs.get("scheduled_start"),
        "published_at": kwargs.get("published_at"),
        "summary": kwargs.get("summary"),
        "announced_at": kwargs.get("announced_at"),
    }
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO videos ({cols}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()


def test_build_digest_includes_pending_videos_and_announcements(conn):
    insert_channel(conn, "UC1", title="Chan One")
    insert_video(conn, "v1", "UC1", VideoState.NEEDS_TRANSCRIPT.value, title="New Video", kind="normal")
    insert_video(
        conn, "v2", "UC1", VideoState.LIVE_UPCOMING.value, title="Live Soon", kind="live",
        scheduled_start="2026-08-06T18:00:00Z",
    )

    d = build_digest(conn, new_video_ids=["v1"], newly_announced_ids=["v2"], warnings=["dead channel"])
    assert len(d.new_videos) == 1
    assert d.new_videos[0].title == "New Video"
    assert len(d.live_announcements) == 1
    assert d.transcript_pending == 1
    assert d.warnings == ["dead channel"]


def test_shorts_are_not_surfaced_in_digest(conn):
    insert_channel(conn, "UC1")
    insert_video(conn, "v1", "UC1", VideoState.SKIPPED_SHORT.value, kind="short")
    d = build_digest(conn, new_video_ids=["v1"], newly_announced_ids=[], warnings=[])
    assert d.new_videos == []


def test_render_markdown_header_counts(conn):
    insert_channel(conn, "UC1")
    insert_video(conn, "v1", "UC1", VideoState.NEEDS_TRANSCRIPT.value, title="X", kind="normal")
    d = build_digest(conn, new_video_ids=["v1"], newly_announced_ids=[], warnings=[])
    text = render_markdown(d)
    assert "1 new videos" in text
    assert "0 livestreams announced" in text
    assert "1 transcript pending" in text
    assert "https://youtu.be/v1" in text


def test_write_digest_file_always_created(conn, tmp_path):
    d = build_digest(conn, new_video_ids=[], newly_announced_ids=[], warnings=[], date="2026-08-05")
    path = write_digest_file(d, tmp_path / "digests")
    assert path.exists()
    assert path.name == "2026-08-05.md"
    assert "No new videos today" in path.read_text()
