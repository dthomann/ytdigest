import argparse

import pytest

from ytdigest import cli, db
from ytdigest.models import VideoState

from .conftest import insert_channel
from .test_cli import fake_summarize_phase, fake_transcript_phase, write_config


def seed_one_channel(config_path):
    config = cli.load_config(config_path)
    conn = db.init_db(config.db_path)
    insert_channel(conn, "UC1", title="Chan One")
    conn.close()
    return config


def insert_video(conn, video_id, state, **kwargs):
    fields = {
        "video_id": video_id,
        "channel_id": "UC1",
        "title": kwargs.get("title", "Title"),
        "state": state,
        "attempts": kwargs.get("attempts", 0),
        "last_error": kwargs.get("last_error"),
        "summary": kwargs.get("summary"),
        "discovered_at": "now",
        "updated_at": "now",
    }
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO videos ({cols}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()


# --------------------------------------------------------------------------------------
# retry
# --------------------------------------------------------------------------------------


def test_retry_single_video_resets_state(tmp_path, capsys):
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.FAILED_PERMANENT.value, attempts=5, last_error="captions disabled")
    conn.close()

    args = argparse.Namespace(config=str(config_path), video_id="v1", all_failed=False)
    cli.cmd_retry(args)

    conn = db.connect(config.db_path)
    row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
    assert row["state"] == VideoState.NEEDS_TRANSCRIPT.value
    assert row["attempts"] == 0
    assert row["last_error"] is None


def test_retry_all_failed_resets_every_failed_video(tmp_path):
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.FAILED_PERMANENT.value)
    insert_video(conn, "v2", VideoState.FAILED_PERMANENT.value)
    insert_video(conn, "v3", VideoState.DELIVERED.value)
    conn.close()

    args = argparse.Namespace(config=str(config_path), video_id=None, all_failed=True)
    cli.cmd_retry(args)

    conn = db.connect(config.db_path)
    needs = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = ?", (VideoState.NEEDS_TRANSCRIPT.value,)
    ).fetchone()["n"]
    assert needs == 2
    untouched = conn.execute("SELECT state FROM videos WHERE video_id='v3'").fetchone()
    assert untouched["state"] == VideoState.DELIVERED.value


def test_retry_non_failed_video_errors(tmp_path):
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.DELIVERED.value)
    conn.close()

    args = argparse.Namespace(config=str(config_path), video_id="v1", all_failed=False)
    with pytest.raises(SystemExit):
        cli.cmd_retry(args)


def test_retry_unknown_video_errors(tmp_path):
    config_path = write_config(tmp_path)
    seed_one_channel(config_path)
    args = argparse.Namespace(config=str(config_path), video_id="nope", all_failed=False)
    with pytest.raises(SystemExit):
        cli.cmd_retry(args)


# --------------------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------------------


def test_export_txt_format(tmp_path, capsys):
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.SUMMARIZED.value, title="My Video", summary="The summary.")
    conn.close()

    txt_dir = config.transcripts_dir / "UC1"
    txt_dir.mkdir(parents=True)
    (txt_dir / "v1.txt").write_text("full transcript text")

    args = argparse.Namespace(config=str(config_path), video_id="v1", format="txt")
    cli.cmd_export(args)
    captured = capsys.readouterr()
    assert captured.out.strip() == "full transcript text"


def test_export_md_format_includes_summary_and_metadata(tmp_path, capsys):
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.SUMMARIZED.value, title="My Video", summary="The summary.")
    conn.close()

    txt_dir = config.transcripts_dir / "UC1"
    txt_dir.mkdir(parents=True)
    (txt_dir / "v1.txt").write_text("full transcript text")

    args = argparse.Namespace(config=str(config_path), video_id="v1", format="md")
    cli.cmd_export(args)
    captured = capsys.readouterr()
    assert "# My Video" in captured.out
    assert "The summary." in captured.out
    assert "full transcript text" in captured.out
    assert "Chan One" in captured.out


def test_export_missing_transcript_file_errors(tmp_path):
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.NEEDS_TRANSCRIPT.value)
    conn.close()

    args = argparse.Namespace(config=str(config_path), video_id="v1", format="txt")
    with pytest.raises(SystemExit):
        cli.cmd_export(args)


def test_export_unknown_video_errors(tmp_path):
    config_path = write_config(tmp_path)
    seed_one_channel(config_path)
    args = argparse.Namespace(config=str(config_path), video_id="nope", format="txt")
    with pytest.raises(SystemExit):
        cli.cmd_export(args)


# --------------------------------------------------------------------------------------
# fetch-transcripts / summarize standalone commands
# --------------------------------------------------------------------------------------


def test_fetch_transcripts_standalone_uses_transcript_phase(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ytdigest.cli.transcript_mod.run_transcript_phase", fake_transcript_phase)
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.NEEDS_TRANSCRIPT.value)
    conn.close()

    args = argparse.Namespace(config=str(config_path), limit=None, catch_up=False)
    cli.cmd_fetch_transcripts(args)

    conn = db.connect(config.db_path)
    row = conn.execute("SELECT state FROM videos WHERE video_id='v1'").fetchone()
    assert row["state"] == VideoState.HAS_TRANSCRIPT.value
    captured = capsys.readouterr()
    assert "succeeded 1" in captured.out


def test_summarize_standalone_requires_gemini_key(tmp_path):
    config_path = write_config(tmp_path)
    (tmp_path / ".env").write_text("YOUTUBE_API_KEY=test-key\n")  # no GEMINI_API_KEY
    seed_one_channel(config_path)
    args = argparse.Namespace(config=str(config_path))
    with pytest.raises(SystemExit):
        cli.cmd_summarize(args)


def test_summarize_standalone_uses_summarize_phase(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ytdigest.cli.summarize_mod.run_summarize_phase", fake_summarize_phase)
    config_path = write_config(tmp_path)
    (tmp_path / ".env").write_text("YOUTUBE_API_KEY=test-key\nGEMINI_API_KEY=test-key\n")
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.HAS_TRANSCRIPT.value)
    conn.close()

    args = argparse.Namespace(config=str(config_path))
    cli.cmd_summarize(args)

    conn = db.connect(config.db_path)
    row = conn.execute("SELECT state, summary FROM videos WHERE video_id='v1'").fetchone()
    assert row["state"] == VideoState.SUMMARIZED.value
    assert row["summary"]
    captured = capsys.readouterr()
    assert "succeeded 1" in captured.out


# --------------------------------------------------------------------------------------
# deliver standalone command
# --------------------------------------------------------------------------------------


def test_deliver_standalone_delivers_from_current_state(tmp_path, capsys):
    config_path = write_config(tmp_path)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.SUMMARIZED.value, title="Ready Video", summary="Done.")
    conn.close()

    args = argparse.Namespace(config=str(config_path), channel="stdout")
    cli.cmd_deliver(args)

    captured = capsys.readouterr()
    assert "Done." in captured.out

    conn = db.connect(config.db_path)
    row = conn.execute("SELECT state FROM videos WHERE video_id='v1'").fetchone()
    assert row["state"] == VideoState.DELIVERED.value


# --------------------------------------------------------------------------------------
# failure alerting
# --------------------------------------------------------------------------------------


EMPTY_FEED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
    'xmlns="http://www.w3.org/2005/Atom"></feed>'
)


def test_run_sends_alert_on_quota_exceeded(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ytdigest.cli.transcript_mod.run_transcript_phase", fake_transcript_phase)
    monkeypatch.setattr("ytdigest.cli.summarize_mod.run_summarize_phase", fake_summarize_phase)

    config_path = write_config(tmp_path)
    # No YOUTUBE_API_KEY -> metadata fetch is skipped with a quota_error-style note -> status partial
    (tmp_path / ".env").write_text("")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    config = seed_one_channel(config_path)
    conn = db.connect(config.db_path)
    insert_video(conn, "v1", VideoState.DISCOVERED.value)  # forces a metadata fetch attempt
    conn.close()

    args = argparse.Namespace(config=str(config_path), dry_run=False, limit=None, channel="stdout", catch_up=False, scheduled=False, retry_only=False)
    cli.cmd_run(args)

    err = capsys.readouterr().err
    assert "status=partial" in err
