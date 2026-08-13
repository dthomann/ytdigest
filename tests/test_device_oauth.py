from fastapi.testclient import TestClient

from ytdigest import db, youtube_oauth
from ytdigest.web.app import create_app
from ytdigest.youtube_oauth import DeviceCode, DevicePollResult


def _enable_oauth(config):
    config.secrets["YOUTUBE_OAUTH_CLIENT_ID"] = "tv-client-id"
    config.secrets["YOUTUBE_OAUTH_CLIENT_SECRET"] = "tv-secret"
    db.init_db(config.db_path)
    return create_app(config)


def test_device_start_shows_user_code(config, monkeypatch):
    def fake_request(oauth):
        return DeviceCode(
            device_code="hidden-device",
            user_code="ABCD-EFGH",
            verification_url="https://www.google.com/device",
            expires_in=1800,
            interval=5,
        )

    monkeypatch.setattr(youtube_oauth, "request_device_code", fake_request)
    client = TestClient(_enable_oauth(config))
    resp = client.get("/auth/youtube/start")
    assert resp.status_code == 200
    assert "ABCD-EFGH" in resp.text
    assert "https://www.google.com/device" in resp.text
    assert "hidden-device" not in resp.text


def test_device_poll_pending_then_authorized(config, monkeypatch):
    def fake_request(oauth):
        return DeviceCode(
            device_code="dev-code",
            user_code="WXYZ",
            verification_url="https://www.google.com/device",
            expires_in=1800,
            interval=5,
        )

    monkeypatch.setattr(youtube_oauth, "request_device_code", fake_request)
    app = _enable_oauth(config)
    client = TestClient(app)
    start = client.get("/auth/youtube/start")
    assert start.status_code == 200
    sid = start.context["sid"]

    monkeypatch.setattr(
        youtube_oauth,
        "poll_device_token",
        lambda oauth, code: DevicePollResult(status="pending"),
    )
    pending = client.post("/auth/youtube/device/poll", data={"sid": sid})
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    monkeypatch.setattr(
        youtube_oauth,
        "poll_device_token",
        lambda oauth, code: DevicePollResult(
            status="authorized",
            token_data={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
        ),
    )

    def fake_sync(conn, config, oauth_cfg, *, connected=False):
        from ytdigest.web.services.sync_flash import SyncFlash

        return SyncFlash(connected=connected, added_count=0)

    monkeypatch.setattr("ytdigest.web.routes.auth.run_youtube_sync", fake_sync)
    done = client.post("/auth/youtube/device/poll", data={"sid": sid})
    assert done.json()["status"] == "authorized"
    assert "/channels" in done.json()["redirect"]

    conn = db.connect(config.db_path)
    try:
        assert youtube_oauth.is_connected(conn) is True
    finally:
        conn.close()


def test_channels_connect_uses_device_start(config):
    config.secrets["YOUTUBE_OAUTH_CLIENT_ID"] = "tv-client-id"
    config.secrets["YOUTUBE_OAUTH_CLIENT_SECRET"] = "tv-secret"
    db.init_db(config.db_path)
    client = TestClient(create_app(config))
    resp = client.get("/channels")
    assert resp.status_code == 200
    assert 'href="/auth/youtube/start"' in resp.text
    assert "google.com/device" in resp.text
