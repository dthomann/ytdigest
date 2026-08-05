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
        "last_error": kwargs.get("last_error"),
    }
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO videos ({cols}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()


def test_build_digest_includes_summaries_and_announcements(conn):
    insert_channel(conn, "UC1", title="Chan One")
    insert_video(
        conn, "v1", "UC1", VideoState.SUMMARIZED.value, title="New Video", kind="normal",
        summary="A concrete summary of what happened.",
    )
    insert_video(
        conn, "v2", "UC1", VideoState.LIVE_UPCOMING.value, title="Live Soon", kind="live",
        scheduled_start="2026-08-06T18:00:00Z",
    )

    d = build_digest(
        conn, summarized_ids=["v1"], upcoming_ids=["v2"], warnings=["dead channel"],
        transcript_pending=3,
    )
    assert len(d.new_videos) == 1
    assert d.new_videos[0].title == "New Video"
    assert d.new_videos[0].summary == "A concrete summary of what happened."
    assert len(d.live_announcements) == 1
    assert d.transcript_pending == 3
    assert d.warnings == ["dead channel"]


def test_build_digest_includes_failed_transcripts(conn):
    insert_channel(conn, "UC1")
    insert_video(
        conn, "v1", "UC1", VideoState.FAILED_PERMANENT.value, title="Ghost Video",
        last_error="captions disabled by uploader",
    )
    d = build_digest(conn, summarized_ids=[], upcoming_ids=[], failed_transcript_ids=["v1"])
    assert len(d.failed_transcripts) == 1
    assert d.failed_transcripts[0].last_error == "captions disabled by uploader"
    text = render_markdown(d)
    assert "Couldn't get a transcript" in text
    assert "captions disabled by uploader" in text


def test_render_markdown_header_counts(conn):
    insert_channel(conn, "UC1")
    insert_video(conn, "v1", "UC1", VideoState.SUMMARIZED.value, title="X", kind="normal", summary="Summary text.")
    d = build_digest(conn, summarized_ids=["v1"], upcoming_ids=[], transcript_pending=1)
    text = render_markdown(d)
    assert "1 new videos" in text
    assert "0 livestreams announced" in text
    assert "1 transcript pending" in text
    assert "https://youtu.be/v1" in text
    assert "Summary text." in text


def test_write_digest_file_always_created(conn, tmp_path):
    d = build_digest(conn, summarized_ids=[], upcoming_ids=[], date="2026-08-05")
    path = write_digest_file(d, tmp_path / "digests")
    assert path.exists()
    assert path.name == "2026-08-05.md"
    assert "No new videos today" in path.read_text()
