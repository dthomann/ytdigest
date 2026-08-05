import argparse
import json

from ytdigest import cli, db
from ytdigest.models import VideoState

from .conftest import FIXTURES_DIR, load_fixture_json, load_fixture_text

RSS_BY_CHANNEL = {
    "UCnormal0000000000000000": "rss_normal_channel.xml",
    "UCshorts0000000000000000": "rss_shorts_channel.xml",
    "UClivestream000000000000": "rss_livestream_channel.xml",
}

VIDEO_ITEM_BY_ID = {
    "vid_normal_001": "videos_normal.json",
    "vid_normal_002": "videos_podcast_2h.json",
    "vid_short_001": "videos_short.json",
    "vid_live_upcoming": "videos_live_upcoming.json",
}


class FakeResponse:
    def __init__(self, text=None, json_data=None):
        self._text = text
        self._json = json_data
        self.status_code = 200

    @property
    def text(self):
        return self._text

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def fake_get(url, params=None, headers=None, timeout=None):
    if "feeds/videos.xml" in url:
        channel_id = url.split("channel_id=")[1]
        return FakeResponse(text=load_fixture_text(RSS_BY_CHANNEL[channel_id]))
    if "youtube/v3/videos" in url:
        ids = params["id"].split(",")
        items = []
        for vid in ids:
            fx = VIDEO_ITEM_BY_ID.get(vid)
            if fx:
                items.append(load_fixture_json(fx)["items"][0])
        return FakeResponse(json_data={"items": items})
    raise AssertionError(f"unexpected URL in test: {url}")


def write_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"data_dir: {tmp_path / 'data'}\nrss_delay_seconds: [0, 0]\n")
    (tmp_path / ".env").write_text("YOUTUBE_API_KEY=test-key\n")
    return config_path


def seed_channels(config_path):
    config = cli.load_config(config_path)
    conn = db.init_db(config.db_path)
    now = "2026-08-01T00:00:00+00:00"
    for cid in RSS_BY_CHANNEL:
        conn.execute(
            "INSERT INTO channels (channel_id, title, added_at) VALUES (?, ?, ?)", (cid, cid, now)
        )
    conn.commit()
    conn.close()
    return config


def test_seed_creates_no_needs_transcript_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("ytdigest.discover.requests.get", fake_get)
    monkeypatch.setattr("ytdigest.metadata.requests.get", fake_get)

    config_path = write_config(tmp_path)
    config = seed_channels(config_path)

    args = argparse.Namespace(config=str(config_path), since="2026-08-01")
    cli.cmd_seed(args)

    conn = db.connect(config.db_path)
    needs_transcript = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = ?", (VideoState.NEEDS_TRANSCRIPT.value,)
    ).fetchone()["n"]
    assert needs_transcript == 0

    delivered = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = ?", (VideoState.DELIVERED.value,)
    ).fetchone()["n"]
    skipped = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = ?", (VideoState.SKIPPED_SHORT.value,)
    ).fetchone()["n"]
    assert delivered >= 1  # normal videos + podcast forced to delivered
    assert skipped == 1  # the short


def test_dry_run_performs_zero_writes_and_zero_outbound_calls(tmp_path, monkeypatch):
    calls = []

    def tracking_get(*args, **kwargs):
        calls.append(args)
        raise AssertionError("dry-run must not make outbound calls")

    monkeypatch.setattr("ytdigest.discover.requests.get", tracking_get)
    monkeypatch.setattr("ytdigest.metadata.requests.get", tracking_get)

    config_path = write_config(tmp_path)
    config = seed_channels(config_path)

    args = argparse.Namespace(
        config=str(config_path), dry_run=True, limit=None, channel=None
    )
    cli.cmd_run(args)

    assert calls == []
    conn = db.connect(config.db_path)
    assert conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0


def test_full_run_writes_digest_and_delivers(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ytdigest.discover.requests.get", fake_get)
    monkeypatch.setattr("ytdigest.metadata.requests.get", fake_get)

    config_path = write_config(tmp_path)
    config = seed_channels(config_path)

    args = argparse.Namespace(config=str(config_path), dry_run=False, limit=None, channel="stdout")
    cli.cmd_run(args)

    digest_files = list((tmp_path / "data" / "digests").glob("*.md"))
    assert len(digest_files) == 1
    content = digest_files[0].read_text()
    assert "new videos" in content

    captured = capsys.readouterr()
    assert "new videos" in captured.out

    conn = db.connect(config.db_path)
    needs_transcript = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = ?", (VideoState.NEEDS_TRANSCRIPT.value,)
    ).fetchone()["n"]
    assert needs_transcript == 2  # the two normal videos, queued for stage 2
