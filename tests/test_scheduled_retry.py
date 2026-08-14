from datetime import datetime, timezone

from ytdigest import scheduled_retry
from ytdigest.db import get_meta
from ytdigest.discover import DiscoverResult
from ytdigest.pipeline import run_pipeline
from ytdigest.summarize import SummarizePhaseResult
from ytdigest.transcript import TranscriptPhaseResult

from .conftest import insert_channel

NOW = datetime(2026, 8, 14, 7, 0, tzinfo=timezone.utc)


def _stub_phases(monkeypatch, *, channels_failed=0, transcript=None):
    monkeypatch.setattr(
        "ytdigest.pipeline.discover.discover_all",
        lambda *a, **k: DiscoverResult(
            channels_polled=2,
            channels_failed=channels_failed,
            dead_channel_warnings=["Broken (UCx) poll failed (1 consecutive): timeout"]
            if channels_failed
            else [],
        ),
    )
    monkeypatch.setattr(
        "ytdigest.pipeline.transcript_mod.run_transcript_phase",
        lambda *a, **k: transcript or TranscriptPhaseResult(),
    )
    monkeypatch.setattr(
        "ytdigest.pipeline.summarize_mod.run_summarize_phase",
        lambda *a, **k: SummarizePhaseResult(),
    )


def test_schedule_next_from_primary(conn, config):
    note = scheduled_retry.schedule_next(conn, config, is_retry_run=False, now=NOW)
    assert note.startswith("scheduled retry 1/3 at ")
    assert scheduled_retry.retry_attempt(conn) == 1
    assert scheduled_retry.retry_at(conn) == "2026-08-14T08:00:00+00:00"
    assert not scheduled_retry.is_retry_due(conn, NOW)
    assert scheduled_retry.is_retry_due(conn, datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc))


def test_schedule_next_exhausts_after_max(conn, config):
    scheduled_retry.schedule_next(conn, config, is_retry_run=False, now=NOW)
    scheduled_retry.schedule_next(conn, config, is_retry_run=True, now=NOW)
    scheduled_retry.schedule_next(conn, config, is_retry_run=True, now=NOW)
    note = scheduled_retry.schedule_next(conn, config, is_retry_run=True, now=NOW)
    assert note is None
    assert scheduled_retry.retry_attempt(conn) == 0
    assert scheduled_retry.retry_at(conn) is None


def test_align_transcript_retries(conn, config):
    insert_channel(conn, "UC1")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, state, discovered_at, updated_at, next_retry_at)
        VALUES ('v1', 'UC1', 'needs_transcript', 'now', 'now', '2026-08-14T13:00:00+00:00')
        """
    )
    conn.commit()
    scheduled_retry.align_transcript_retries(conn, ["v1"], "2026-08-14T08:00:00+00:00")
    row = conn.execute("SELECT next_retry_at FROM videos WHERE video_id='v1'").fetchone()
    assert row["next_retry_at"] == "2026-08-14T08:00:00+00:00"


def test_manual_run_does_not_schedule_retry(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(monkeypatch, channels_failed=1)

    result = run_pipeline(conn, config, use_lock=False, channel="stdout", now=NOW)
    assert result.status == "partial"
    assert get_meta(conn, scheduled_retry.META_RETRY_AT) is None
    assert not any(n.startswith("scheduled retry") for n in result.notes)


def test_scheduled_feed_failure_queues_retry(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(monkeypatch, channels_failed=1)

    result = run_pipeline(
        conn, config, use_lock=False, channel="stdout", scheduled=True, now=NOW
    )
    assert result.status == "partial"
    assert "1/2 channels failed RSS poll" in result.notes[0]
    assert any("scheduled retry 1/3" in n for n in result.notes)
    assert scheduled_retry.retry_attempt(conn) == 1


def test_retry_only_skips_when_not_due(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(monkeypatch, channels_failed=1)

    result = run_pipeline(
        conn,
        config,
        use_lock=False,
        channel="stdout",
        scheduled=True,
        retry_only=True,
        now=NOW,
    )
    assert result.skipped
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0


def test_retry_only_skips_during_digest_hour(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(monkeypatch, channels_failed=1)
    five = datetime(2026, 8, 14, 5, 0, tzinfo=timezone.utc)
    scheduled_retry.schedule_next(conn, config, is_retry_run=False, now=five)
    digest_now = datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)
    assert scheduled_retry.is_retry_due(conn, digest_now)

    result = run_pipeline(
        conn,
        config,
        use_lock=False,
        channel="stdout",
        scheduled=True,
        retry_only=True,
        now=digest_now,
    )
    assert result.skipped


def test_retry_only_runs_when_due(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(monkeypatch, channels_failed=1)
    scheduled_retry.schedule_next(conn, config, is_retry_run=False, now=NOW)
    due = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)

    result = run_pipeline(
        conn,
        config,
        use_lock=False,
        channel="stdout",
        scheduled=True,
        retry_only=True,
        now=due,
    )
    assert not result.skipped
    assert any("scheduled retry 2/3" in n for n in result.notes)
    assert scheduled_retry.retry_attempt(conn) == 2


def test_scheduled_tier2_error_queues_retry(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(
        monkeypatch,
        transcript=TranscriptPhaseResult(
            attempted=1,
            retrying=1,
            errors=["v1: tier2 download error"],
            had_tier2_error=True,
            retryable_ids=["v1"],
        ),
    )
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, discovered_at, updated_at,
                             next_retry_at)
        VALUES ('v1', 'UC1', 'T', 'needs_transcript', 'now', 'now', '2026-08-14T13:00:00+00:00')
        """
    )
    conn.commit()

    result = run_pipeline(
        conn, config, use_lock=False, channel="stdout", scheduled=True, now=NOW
    )
    assert any("scheduled retry 1/3" in n for n in result.notes)
    row = conn.execute("SELECT next_retry_at FROM videos WHERE video_id='v1'").fetchone()
    assert row["next_retry_at"] == "2026-08-14T08:00:00+00:00"


def test_scheduled_tier1_only_error_does_not_queue_retry(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(
        monkeypatch,
        transcript=TranscriptPhaseResult(
            attempted=1,
            retrying=1,
            errors=["v1: no transcript yet"],
            had_tier2_error=False,
            retryable_ids=["v1"],
        ),
    )

    result = run_pipeline(
        conn, config, use_lock=False, channel="stdout", scheduled=True, now=NOW
    )
    assert not any("scheduled retry" in n for n in result.notes)
    assert get_meta(conn, scheduled_retry.META_RETRY_AT) is None


def test_scheduled_success_clears_retry(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(monkeypatch, channels_failed=0)
    scheduled_retry.schedule_next(conn, config, is_retry_run=False, now=NOW)

    result = run_pipeline(
        conn, config, use_lock=False, channel="stdout", scheduled=True, now=NOW
    )
    assert result.status == "ok"
    assert get_meta(conn, scheduled_retry.META_RETRY_AT) is None
    assert scheduled_retry.retry_attempt(conn) == 0


def test_scheduled_retries_exhaust(conn, config, monkeypatch):
    insert_channel(conn, "UC1")
    config.secrets["GEMINI_API_KEY"] = "k"
    _stub_phases(monkeypatch, channels_failed=1)
    scheduled_retry.schedule_next(conn, config, is_retry_run=False, now=NOW)
    scheduled_retry.schedule_next(conn, config, is_retry_run=True, now=NOW)
    scheduled_retry.schedule_next(conn, config, is_retry_run=True, now=NOW)
    due = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)

    result = run_pipeline(
        conn,
        config,
        use_lock=False,
        channel="stdout",
        scheduled=True,
        retry_only=True,
        now=due,
    )
    assert any("scheduled retries exhausted (3/3)" in n for n in result.notes)
    assert get_meta(conn, scheduled_retry.META_RETRY_AT) is None
