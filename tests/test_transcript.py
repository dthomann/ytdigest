import json
from datetime import datetime, timezone

import pytest
import requests
from youtube_transcript_api import (
    AgeRestricted,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeRequestFailed,
)

from ytdigest import transcript as tr
from ytdigest.models import VideoState

from .conftest import FIXTURES_DIR, insert_channel


def load_json3_segments(name):
    data = json.loads((FIXTURES_DIR / name).read_text())
    return [
        {"text": "".join(s["utf8"] for s in e["segs"]), "start": e["tStartMs"] / 1000}
        for e in data["events"]
        if "segs" in e
    ]


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------


def test_dedup_reduces_overlapping_input_to_known_clean_output():
    segments = load_json3_segments("captions_overlap.json3")
    clean = tr.clean_transcript(segments)
    assert clean == (
        "so today we're going to talk about the new release and what it means for you "
        "let's get started"
    )


def test_dedup_no_overlap_is_unaffected():
    segments = load_json3_segments("captions_manual.json3")
    clean = tr.clean_transcript(segments)
    assert clean == "Welcome back to the show. Today we have a special guest."


def test_strip_artifacts_removes_bracket_markers():
    assert tr.strip_artifacts("hello [Music] world [Applause]") == "hello  world "
    assert tr.strip_artifacts(">> next segment") == " next segment"


def test_is_plausible_length():
    # ~150 wpm over 10 minutes = 1500 words
    assert tr.is_plausible_length(1500, 600)
    assert not tr.is_plausible_length(5, 600)  # way too short for 10 minutes
    assert tr.is_plausible_length(100, None)  # no duration known -> can't judge, allow


def test_write_transcript_files(tmp_path):
    segments = [{"text": "hello", "start": 0.0}, {"text": "world", "start": 1.0}]
    txt_path, jsonl_path, chars = tr.write_transcript_files(
        "vid1", "chan1", tmp_path, "hello world", segments
    )
    assert txt_path.read_text() == "hello world"
    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"t": 0.0, "text": "hello"}
    assert chars == len("hello world")


# --------------------------------------------------------------------------------------
# Tier 1
# --------------------------------------------------------------------------------------


class FakeTranscript:
    def __init__(self, language_code, is_generated, snippets, fetch_exc=None):
        self.language_code = language_code
        self.is_generated = is_generated
        self._snippets = snippets
        self._fetch_exc = fetch_exc

    def fetch(self):
        if self._fetch_exc:
            raise self._fetch_exc

        class Fetched:
            def __init__(self, snippets):
                self.snippets = snippets

        return Fetched(self._snippets)


class FakeSnippet:
    def __init__(self, text, start):
        self.text = text
        self.start = start


class FakeTranscriptList:
    def __init__(self, manual=None, generated=None, all_transcripts=None):
        self._manual = manual
        self._generated = generated
        self._all = all_transcripts or []

    def find_manually_created_transcript(self, languages):
        if self._manual:
            return self._manual
        raise NoTranscriptFound("vid", languages, self)

    def find_generated_transcript(self, languages):
        if self._generated:
            return self._generated
        raise NoTranscriptFound("vid", languages, self)

    def __iter__(self):
        return iter(self._all)


class FakeYttApi:
    def __init__(self, list_result=None, list_exc=None, fetch_exc=None):
        self._list_result = list_result
        self._list_exc = list_exc
        self._fetch_exc = fetch_exc

    def list(self, video_id):
        if self._list_exc:
            raise self._list_exc
        return self._list_result


def test_tier1_prefers_manual_over_generated():
    manual = FakeTranscript("en", False, [FakeSnippet("hi", 0.0)])
    generated = FakeTranscript("en", True, [FakeSnippet("hi auto", 0.0)])
    api = FakeYttApi(list_result=FakeTranscriptList(manual=manual, generated=generated))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.ok
    assert outcome.is_auto is False
    assert outcome.segments[0]["text"] == "hi"


def test_tier1_falls_back_to_generated_then_any():
    generated = FakeTranscript("en", True, [FakeSnippet("auto only", 0.0)])
    api = FakeYttApi(list_result=FakeTranscriptList(generated=generated))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.ok
    assert outcome.is_auto is True

    any_track = FakeTranscript("fr", True, [FakeSnippet("francais", 0.0)])
    api2 = FakeYttApi(list_result=FakeTranscriptList(all_transcripts=[any_track]))
    outcome2 = tr.fetch_tier1("vid", ["en", "de"], ytt_api=api2)
    assert outcome2.ok
    assert outcome2.language == "fr"


def test_tier1_no_transcript_available_is_retryable():
    api = FakeYttApi(list_result=FakeTranscriptList())
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert not outcome.ok
    assert not outcome.fatal
    assert not outcome.blocked


def test_tier1_transcripts_disabled_is_fatal():
    api = FakeYttApi(list_exc=TranscriptsDisabled("vid"))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.fatal
    assert not outcome.video_missing


def test_tier1_video_unavailable_is_fatal_and_video_missing():
    api = FakeYttApi(list_exc=VideoUnavailable("vid"))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.fatal
    assert outcome.video_missing


def test_tier1_age_restricted_is_fatal():
    api = FakeYttApi(list_exc=AgeRestricted("vid"))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.fatal


def test_tier1_ip_blocked_is_blocked_and_retryable():
    api = FakeYttApi(list_exc=IpBlocked("vid"))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.blocked
    assert not outcome.fatal


def test_tier1_request_blocked_is_blocked():
    api = FakeYttApi(list_exc=RequestBlocked("vid"))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.blocked


def test_tier1_invalid_xml_on_fetch_is_retryable():
    from xml.etree.ElementTree import ParseError

    manual = FakeTranscript("en", False, [], fetch_exc=ParseError("no element found: line 1, column 0"))
    api = FakeYttApi(list_result=FakeTranscriptList(manual=manual))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert not outcome.ok
    assert not outcome.fatal
    assert not outcome.blocked
    assert "invalid transcript XML" in outcome.reason


def _youtube_request_failed_429(video_id: str = "vid") -> YouTubeRequestFailed:
    response = requests.models.Response()
    response.status_code = 429
    response.url = f"https://www.youtube.com/api/timedtext?v={video_id}"
    http_err = requests.HTTPError(
        f"429 Client Error: Too Many Requests for url: {response.url}",
        response=response,
    )
    return YouTubeRequestFailed(video_id, http_err)


def test_tier1_youtube_request_failed_429_on_list_is_blocked():
    api = FakeYttApi(list_exc=_youtube_request_failed_429())
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.blocked
    assert not outcome.fatal
    assert "tier1 blocked" in outcome.reason


def test_tier1_youtube_request_failed_429_on_fetch_is_blocked():
    manual = FakeTranscript("en", False, [], fetch_exc=_youtube_request_failed_429())
    api = FakeYttApi(list_result=FakeTranscriptList(manual=manual))
    outcome = tr.fetch_tier1("vid", ["en"], ytt_api=api)
    assert outcome.blocked
    assert not outcome.fatal
    assert "tier1 blocked" in outcome.reason


# --------------------------------------------------------------------------------------
# Tier 2
# --------------------------------------------------------------------------------------


def test_tier2_selects_manual_json3_track():
    info = {
        "subtitles": {"en": [{"ext": "json3", "url": "http://example/en_manual.json3"}]},
        "automatic_captions": {"en": [{"ext": "json3", "url": "http://example/en_auto.json3"}]},
    }

    def fake_fetch(url):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"events": [{"tStartMs": 0, "segs": [{"utf8": "manual text"}]}]}

        assert url == "http://example/en_manual.json3"
        return Resp()

    outcome = tr.fetch_tier2("vid", ["en"], extract_info_fn=lambda vid: info, fetch_fn=fake_fetch)
    assert outcome.ok
    assert outcome.is_auto is False
    assert outcome.segments[0]["text"] == "manual text"


def test_tier2_no_json3_track_is_retryable():
    info = {"subtitles": {}, "automatic_captions": {}}
    outcome = tr.fetch_tier2("vid", ["en"], extract_info_fn=lambda vid: info)
    assert not outcome.ok
    assert not outcome.fatal
    assert not outcome.blocked


def test_tier2_prefers_native_en_over_translated_de():
    info = {
        "subtitles": {},
        "automatic_captions": {
            "de": [{"ext": "json3", "url": "http://example/?lang=en&tlang=de&fmt=json3"}],
            "en": [{"ext": "json3", "url": "http://example/?lang=en&fmt=json3"}],
        },
    }
    lang, fmt, is_auto = tr._select_ytdlp_track(info, ["de", "en"])
    assert lang == "en"
    assert "tlang=" not in fmt["url"]
    assert is_auto is True


def test_tier2_download_429_is_blocked():
    info = {
        "automatic_captions": {
            "en": [{"ext": "json3", "url": "http://example/en.json3"}],
        },
    }

    def fake_fetch(url):
        response = type("Resp", (), {"status_code": 429})()
        raise requests.HTTPError("429 Client Error", response=response)

    outcome = tr.fetch_tier2("vid", ["en"], extract_info_fn=lambda vid: info, fetch_fn=fake_fetch)
    assert outcome.blocked
    assert "tier2 blocked" in outcome.reason


def test_subtitle_url_is_translation():
    assert tr._subtitle_url_is_translation("http://x/?lang=en&tlang=de") is True
    assert tr._subtitle_url_is_translation("http://x/?lang=en") is False


# --------------------------------------------------------------------------------------
# Retry scheduling
# --------------------------------------------------------------------------------------


def test_compute_next_retry_uses_backoff_table():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    result = tr.compute_next_retry(1, [6, 12, 24, 48, 96], now=now)
    assert result == "2026-08-05T18:00:00+00:00"

    result5 = tr.compute_next_retry(5, [6, 12, 24, 48, 96], now=now)
    assert result5 == "2026-08-09T12:00:00+00:00"

    # attempts beyond table length clamps to the last entry
    result99 = tr.compute_next_retry(99, [6, 12, 24, 48, 96], now=now)
    assert result99 == result5


# --------------------------------------------------------------------------------------
# process_video / run_transcript_phase (DB integration)
# --------------------------------------------------------------------------------------


def insert_pending_video(conn, video_id, channel_id="UC1", duration=300, attempts=0, next_retry_at=None):
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, duration_seconds,
                             attempts, next_retry_at, discovered_at, updated_at)
        VALUES (?, ?, 'Title', 'needs_transcript', ?, ?, ?, 'now', 'now')
        """,
        (video_id, channel_id, duration, attempts, next_retry_at),
    )
    conn.commit()


def make_success_outcome():
    return tr.TranscriptOutcome(
        ok=True, source="captions_api", language="en", is_auto=True,
        segments=[{"text": "hello world " * 60, "start": 0.0}],
    )


def test_process_video_success_writes_files_and_state(conn, config, tmp_path):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1")
    outcome = tr.process_video(
        conn, conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone(), config,
        tier1_fn=lambda vid, langs: make_success_outcome(),
    )
    assert outcome.ok
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert row["state"] == VideoState.HAS_TRANSCRIPT.value
    assert row["transcript_source"] == "captions_api"
    assert (config.transcripts_dir / "UC1" / "v1.txt").exists()


def test_process_video_success_clears_retry_fields(conn, config, tmp_path):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1", attempts=2, next_retry_at="2026-01-01T12:00:00+00:00")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: make_success_outcome(),
    )
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["state"] == VideoState.HAS_TRANSCRIPT.value
    assert updated["attempts"] == 0
    assert updated["next_retry_at"] is None
    assert updated["last_error"] is None


def test_process_video_fatal_video_missing_skips_tier2(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(
            ok=False, fatal=True, video_missing=True, reason="video private or deleted"
        ),
        tier2_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="should not be called"),
    )
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["state"] == VideoState.FAILED_PERMANENT.value
    assert "deleted" in updated["last_error"]


def test_process_video_captions_disabled_still_tries_tier2_then_fails_permanent(conn, config):
    # tier1 says captions disabled (fatal, but not video_missing) -> tier2 is still attempted
    # tier2 is still attempted when tier1 fails; if tier2 agrees, it's permanent.
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(
            ok=False, fatal=True, reason="captions disabled by uploader"
        ),
        tier2_fn=lambda vid, langs: tr.TranscriptOutcome(
            ok=False, fatal=True, reason="tier2: no json3 subtitle track available"
        ),
    )
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["state"] == VideoState.FAILED_PERMANENT.value


def test_process_video_falls_through_to_tier2_on_retryable_tier1(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="no transcript yet"),
        tier2_fn=lambda vid, langs: make_success_outcome(),
    )
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["state"] == VideoState.HAS_TRANSCRIPT.value


def test_process_video_tier1_429_skips_tier2(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1", attempts=1)
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    called = {"tier2": False}

    def boom_tier2(vid, langs):
        called["tier2"] = True
        return tr.TranscriptOutcome(ok=False, reason="should not run")

    outcome = tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(
            ok=False, blocked=True, reason="tier1 blocked: 429 Too Many Requests"
        ),
        tier2_fn=boom_tier2,
    )
    assert outcome.blocked
    assert not called["tier2"]
    assert not outcome.tier2_failed
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["attempts"] == 1  # not bumped
    assert "tier1 blocked" in updated["last_error"]


def test_process_video_blocked_does_not_bump_attempts(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1", attempts=2)
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    outcome = tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, blocked=True, reason="IP blocked"),
    )
    assert outcome.blocked
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["state"] == VideoState.NEEDS_TRANSCRIPT.value
    assert updated["attempts"] == 2


def test_process_video_retryable_schedules_backoff(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1", attempts=0)
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="no transcript yet"),
        tier2_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="no transcript yet"),
    )
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["attempts"] == 1
    assert updated["next_retry_at"] is not None
    assert updated["state"] == VideoState.NEEDS_TRANSCRIPT.value


def test_process_video_records_tier2_failure(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    outcome = tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="no transcript yet"),
        tier2_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="tier2: no json3"),
    )
    assert outcome.tier2_failed
    assert not outcome.ok


def test_process_video_tier1_block_is_not_tier2_failure(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1")
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    outcome = tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, blocked=True, reason="IP blocked"),
        tier2_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="should not be called"),
    )
    assert not outcome.tier2_failed


def test_process_video_max_attempts_reaches_failed_permanent(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1", attempts=4)  # max_transcript_attempts default is 5
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    tr.process_video(
        conn, row, config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="no transcript yet"),
        tier2_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="no transcript yet"),
    )
    updated = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert updated["state"] == VideoState.FAILED_PERMANENT.value
    assert updated["attempts"] == 5


def test_run_transcript_phase_respects_cap_and_limit(conn, config):
    insert_channel(conn, "UC1")
    for i in range(5):
        insert_pending_video(conn, f"v{i}")

    result = tr.run_transcript_phase(
        conn, config, tier1_fn=lambda vid, langs: make_success_outcome(), limit=2
    )
    assert result.attempted == 2
    assert len(result.succeeded_ids) == 2


def test_run_transcript_phase_aborts_on_block(conn, config):
    insert_channel(conn, "UC1")
    for i in range(5):
        insert_pending_video(conn, f"v{i}")

    calls = []

    def flaky_tier1(vid, langs):
        calls.append(vid)
        if len(calls) == 2:
            return tr.TranscriptOutcome(ok=False, blocked=True, reason="IP blocked")
        return make_success_outcome()

    result = tr.run_transcript_phase(conn, config, tier1_fn=flaky_tier1)
    assert result.aborted
    assert result.abort_reason == "IP blocked"
    assert len(calls) == 2  # stopped immediately, did not touch the remaining 3

    still_queued = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = 'needs_transcript'"
    ).fetchone()["n"]
    assert still_queued == 4  # 1 succeeded, 4 still queued (including the blocked one)


def test_run_transcript_phase_flags_tier2_errors(conn, config):
    insert_channel(conn, "UC1")
    insert_pending_video(conn, "v1")

    result = tr.run_transcript_phase(
        conn,
        config,
        tier1_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="no transcript yet"),
        tier2_fn=lambda vid, langs: tr.TranscriptOutcome(ok=False, reason="tier2 download error"),
    )
    assert result.had_tier2_error
    assert result.retryable_ids == ["v1"]
    assert result.retrying == 1
