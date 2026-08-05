import argparse

import pytest

from ytdigest import cli, db
from ytdigest.backfill import META_SEED_CUTOFF, fix_stuck_backfill, initial_state_for_discovery, is_backfill, validate_cutoff_date
from ytdigest.db import get_meta
from ytdigest.models import VideoState

from .conftest import insert_channel
from .test_cli import fake_get, seed_channels, write_config


def test_validate_cutoff_date():
    assert validate_cutoff_date("2026-08-05") == "2026-08-05"
    with pytest.raises(ValueError):
        validate_cutoff_date("08-05-2026")


def test_is_backfill():
    since = "2026-08-05"
    assert is_backfill("2026-08-03T13:39:22+00:00", since) is True
    assert is_backfill("2026-08-05T00:00:00+00:00", since) is False
    assert is_backfill("2026-08-05T14:49:32+00:00", since) is False
    assert is_backfill(None, since) is False
    assert is_backfill("2026-08-03T13:39:22+00:00", None) is False


def test_initial_state_for_discovery():
    assert initial_state_for_discovery("2026-08-03T13:39:22+00:00", "2026-08-05") == VideoState.DELIVERED.value
    assert initial_state_for_discovery("2026-08-05T14:00:00+00:00", "2026-08-05") == VideoState.DISCOVERED.value


def test_fix_stuck_backfill(conn):
    insert_channel(conn, "UC1")
    now = "2026-08-05T18:00:00+00:00"
    conn.execute(
        """
        INSERT INTO videos
            (video_id, channel_id, title, state, published_at, discovered_at, updated_at,
             attempts, next_retry_at, last_error)
        VALUES
            ('old_stuck', 'UC1', 'Old', 'needs_transcript', '2026-08-03T13:39:22+00:00',
             '2026-08-05T17:52:44+00:00', '2026-08-05T17:53:01+00:00', 1,
             '2026-08-05T23:53:04+00:00', '429'),
            ('new_ok', 'UC1', 'New', 'needs_transcript', '2026-08-05T14:49:32+00:00',
             '2026-08-05T15:06:42+00:00', '2026-08-05T15:06:42+00:00', 0, NULL, NULL)
        """
    )
    conn.commit()

    fixed = fix_stuck_backfill(conn, "2026-08-05", now=now)
    assert fixed == ["old_stuck"]

    old = conn.execute("SELECT * FROM videos WHERE video_id='old_stuck'").fetchone()
    assert old["state"] == VideoState.DELIVERED.value
    assert old["next_retry_at"] is None
    assert old["last_error"] is None
    assert old["attempts"] == 0

    new = conn.execute("SELECT state FROM videos WHERE video_id='new_ok'").fetchone()
    assert new["state"] == VideoState.NEEDS_TRANSCRIPT.value


def test_seed_stores_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr("ytdigest.discover.requests.get", fake_get)
    monkeypatch.setattr("ytdigest.metadata.requests.get", fake_get)

    config_path = write_config(tmp_path)
    seed_channels(config_path)
    args = argparse.Namespace(config=str(config_path), since="2026-08-05")
    cli.cmd_seed(args)

    config = cli.load_config(config_path)
    conn = db.connect(config.db_path)
    assert get_meta(conn, META_SEED_CUTOFF) == "2026-08-05"


def test_fix_backfill_cli(tmp_path, capsys):
    config_path = write_config(tmp_path)
    config = cli.load_config(config_path)
    conn = db.init_db(config.db_path)
    insert_channel(conn, "UC1")
    conn.execute(
        """
        INSERT INTO videos
            (video_id, channel_id, state, published_at, discovered_at, updated_at)
        VALUES ('q7_bPQFUIts', 'UC1', 'needs_transcript', '2026-08-03T13:39:22+00:00',
                '2026-08-05T17:52:44+00:00', '2026-08-05T17:53:01+00:00')
        """
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(config=str(config_path), since="2026-08-05")
    cli.cmd_fix_backfill(args)

    conn = db.connect(config.db_path)
    row = conn.execute("SELECT state FROM videos WHERE video_id='q7_bPQFUIts'").fetchone()
    assert row["state"] == VideoState.DELIVERED.value
    assert get_meta(conn, META_SEED_CUTOFF) == "2026-08-05"
    out = capsys.readouterr().out
    assert "q7_bPQFUIts" in out
