import threading
import time

from fastapi.testclient import TestClient

from ytdigest import db
from ytdigest.web.app import create_app
from ytdigest.web.auth import AUTH_COOKIE


def test_web_auth_blocks_mutating_requests_when_enabled(config):
    config.secrets["WEB_AUTH_TOKEN"] = "secret-token"
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)

    resp = client.post("/runs/start")
    assert resp.status_code == 401

    resp = client.get("/runs/status", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/login")


def test_web_auth_login_sets_cookie_and_allows_access(config):
    config.secrets["WEB_AUTH_TOKEN"] = "secret-token"
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)

    resp = client.post("/auth/login", data={"token": "secret-token", "next": "/runs/status"}, follow_redirects=False)
    assert resp.status_code == 303
    assert AUTH_COOKIE in resp.cookies

    authed = TestClient(app)
    authed.cookies.set(AUTH_COOKIE, "secret-token")
    resp = authed.get("/runs/status")
    assert resp.status_code == 200


def test_web_auth_rejects_invalid_login(config):
    config.secrets["WEB_AUTH_TOKEN"] = "secret-token"
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)

    resp = client.post("/auth/login", data={"token": "wrong", "next": "/"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=1" in resp.headers["location"]


def test_web_auth_accepts_bearer_header(config):
    config.secrets["WEB_AUTH_TOKEN"] = "secret-token"
    db.init_db(config.db_path)
    app = create_app(config)
    client = TestClient(app)

    resp = client.post("/runs/start", headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 200


def test_oauth_config_ignores_host_header(config):
    from ytdigest.web.oauth_helpers import oauth_config_from_request

    config.secrets["YOUTUBE_OAUTH_CLIENT_ID"] = "client-id"
    config.secrets["YOUTUBE_OAUTH_CLIENT_SECRET"] = "client-secret"

    class FakeRequest:
        headers = {"host": "evil.example.com:9090"}

    oauth_cfg = oauth_config_from_request(config, FakeRequest())
    assert oauth_cfg is not None
    assert oauth_cfg.client_id == "client-id"
    assert oauth_cfg.client_secret == "client-secret"


def test_run_manager_rejects_concurrent_start(config, monkeypatch):
    import threading
    import time

    from ytdigest.web.services.run_manager import RunManager

    started = threading.Event()
    release = threading.Event()

    def slow_pipeline(conn, config):
        started.set()
        release.wait(timeout=2)
        from ytdigest import pipeline

        return pipeline.RunResult(run_id=1, status="ok")

    monkeypatch.setattr("ytdigest.web.services.run_manager.pipeline.run_pipeline", slow_pipeline)

    mgr = RunManager(config)
    ok1, _ = mgr.start()
    assert ok1 is True
    started.wait(timeout=1)
    ok2, msg2 = mgr.start()
    assert ok2 is False
    assert "already in progress" in msg2
    release.set()
    time.sleep(0.05)


def test_invalid_video_id_rejected_before_download():
    from pathlib import Path

    from ytdigest.transcript import _download_audio

    try:
        _download_audio("not-a-valid-id", Path("/tmp"))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "invalid video_id" in str(exc)
