from datetime import datetime, timezone

from ytdigest.classify import classify_all, classify_row
from ytdigest.metadata import _extract, parse_duration
from ytdigest.models import VideoKind, VideoState

from .conftest import insert_channel, load_fixture_json


def test_duration_parsing():
    assert parse_duration("P0D") == 0
    assert parse_duration("PT2H13M7S") == 7987
    assert parse_duration("PT45S") == 45
    assert parse_duration(None) is None


def test_classify_normal_video():
    state, kind = classify_row("none", None, 492, min_duration_seconds=180)
    assert state == VideoState.NEEDS_TRANSCRIPT.value
    assert kind == VideoKind.NORMAL.value


def test_classify_long_podcast():
    state, kind = classify_row("none", None, 7987, min_duration_seconds=180)
    assert state == VideoState.NEEDS_TRANSCRIPT.value
    assert kind == VideoKind.NORMAL.value


def test_classify_short():
    state, kind = classify_row("none", None, 45, min_duration_seconds=180)
    assert state == VideoState.SKIPPED_SHORT.value
    assert kind == VideoKind.SHORT.value


def test_classify_short_boundary_is_skipped():
    # duration == min_duration_seconds is still skipped (<=)
    state, kind = classify_row("none", None, 180, min_duration_seconds=180)
    assert state == VideoState.SKIPPED_SHORT.value


def test_classify_upcoming_livestream_p0d_not_swallowed_by_duration_branch():
    # P0D parses to 0 seconds, which is <= min_duration; live status must win.
    state, kind = classify_row("upcoming", None, 0, min_duration_seconds=180)
    assert state == VideoState.LIVE_UPCOMING.value
    assert kind == VideoKind.LIVE.value


def test_classify_upcoming_with_schedule_stays_upcoming():
    scheduled = "2026-08-06T18:00:00Z"
    now = datetime(2026, 8, 6, 19, 0, 0, tzinfo=timezone.utc)
    state, kind = classify_row(
        "upcoming",
        None,
        0,
        min_duration_seconds=180,
        scheduled_start=scheduled,
        now=now,
    )
    assert state == VideoState.LIVE_UPCOMING.value
    assert kind == VideoKind.LIVE.value


def test_classify_stale_upcoming_vod_with_duration():
    state, kind = classify_row(
        "upcoming",
        None,
        3600,
        min_duration_seconds=180,
        scheduled_start=None,
    )
    assert state == VideoState.LIVE_FINISHED.value
    assert kind == VideoKind.LIVE.value


def test_classify_stale_upcoming_old_published_at():
    state, kind = classify_row(
        "upcoming",
        None,
        0,
        min_duration_seconds=180,
        scheduled_start=None,
        published_at="2025-12-28T12:41:54+00:00",
        now=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
    )
    assert state == VideoState.LIVE_FINISHED.value
    assert kind == VideoKind.LIVE.value


def test_classify_stale_upcoming_past_scheduled_start():
    state, kind = classify_row(
        "upcoming",
        None,
        0,
        min_duration_seconds=180,
        scheduled_start="2026-08-04T18:00:00Z",
        now=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
    )
    assert state == VideoState.LIVE_FINISHED.value
    assert kind == VideoKind.LIVE.value


def test_classify_recent_upcoming_without_schedule_stays_upcoming():
    state, kind = classify_row(
        "upcoming",
        None,
        0,
        min_duration_seconds=180,
        scheduled_start=None,
        published_at="2026-08-05T09:00:00+00:00",
        now=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
    )
    assert state == VideoState.LIVE_UPCOMING.value
    assert kind == VideoKind.LIVE.value
    state, kind = classify_row(
        "upcoming",
        None,
        3600,
        min_duration_seconds=180,
        scheduled_start=None,
    )
    assert state == VideoState.LIVE_FINISHED.value
    assert kind == VideoKind.LIVE.value


def test_classify_upcoming_with_actual_end_is_finished():
    state, kind = classify_row("upcoming", "2026-08-04T21:04:00Z", 0, min_duration_seconds=180)
    assert state == VideoState.LIVE_FINISHED.value
    assert kind == VideoKind.LIVE.value


def test_classify_live_now():
    state, kind = classify_row("live", None, 0, min_duration_seconds=180)
    assert state == VideoState.LIVE_NOW.value
    assert kind == VideoKind.LIVE.value


def test_classify_finished_livestream_terminal_by_default():
    state, kind = classify_row("none", "2026-08-04T21:04:00Z", 10860, min_duration_seconds=180)
    assert state == VideoState.LIVE_FINISHED.value
    assert kind == VideoKind.LIVE.value


def test_classify_finished_livestream_routes_to_transcript_when_enabled():
    state, kind = classify_row(
        "none", "2026-08-04T21:04:00Z", 10860, min_duration_seconds=180,
        summarize_finished_livestreams=True,
    )
    assert state == VideoState.NEEDS_TRANSCRIPT.value
    assert kind == VideoKind.LIVE.value


def test_classify_missing_duration_stays_discovered():
    state, kind = classify_row("none", None, None, min_duration_seconds=180)
    assert state == VideoState.DISCOVERED.value
    assert kind == VideoKind.UNKNOWN.value


def test_all_fixtures_route_to_correct_state():
    cases = [
        ("videos_normal.json", VideoState.NEEDS_TRANSCRIPT.value),
        ("videos_podcast_2h.json", VideoState.NEEDS_TRANSCRIPT.value),
        ("videos_short.json", VideoState.SKIPPED_SHORT.value),
        ("videos_live_upcoming.json", VideoState.LIVE_UPCOMING.value),
        ("videos_live_now.json", VideoState.LIVE_NOW.value),
        ("videos_live_finished.json", VideoState.LIVE_FINISHED.value),
    ]
    for fixture, expected_state in cases:
        item = _extract(load_fixture_json(fixture)["items"][0])
        state, _ = classify_row(
            item["live_broadcast"], item["actual_end"], item["duration_seconds"],
            min_duration_seconds=180,
        )
        assert state == expected_state, f"{fixture}: expected {expected_state}, got {state}"


def test_classify_all_sets_announced_at_once(conn, config):
    insert_channel(conn, "UClive")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, live_broadcast,
                             duration_seconds, discovered_at, updated_at)
        VALUES ('v1', 'UClive', 'Upcoming', 'discovered', 'upcoming', 0, 'now', 'now')
        """
    )
    conn.commit()

    classify_all(conn, config)
    row = conn.execute("SELECT * FROM videos WHERE video_id = 'v1'").fetchone()
    assert row["state"] == VideoState.LIVE_UPCOMING.value
    first_announced = row["announced_at"]
    assert first_announced is not None

    # re-classify on a later run: state is re-checked but announced_at must not change
    classify_all(conn, config)
    row2 = conn.execute("SELECT * FROM videos WHERE video_id = 'v1'").fetchone()
    assert row2["announced_at"] == first_announced
