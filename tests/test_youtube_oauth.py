from datetime import datetime, timedelta, timezone

from ytdigest import db, youtube_oauth
from ytdigest.youtube_oauth import OAuthConfig, OAuthExpired


def _oauth():
    return OAuthConfig(client_id="cid", client_secret="secret")


def test_request_device_code_parses_google_response(monkeypatch):
    class FakeResp:
        ok = True
        status_code = 200

        def json(self):
            return {
                "device_code": "dev-1",
                "user_code": "ABCD-EFGH",
                "verification_url": "https://www.google.com/device",
                "expires_in": 1800,
                "interval": 5,
            }

    monkeypatch.setattr(youtube_oauth.requests, "post", lambda *a, **k: FakeResp())
    device = youtube_oauth.request_device_code(_oauth())
    assert device.user_code == "ABCD-EFGH"
    assert device.device_code == "dev-1"
    assert device.interval == 5
    assert device.verification_url == "https://www.google.com/device"


def test_request_device_code_accepts_verification_uri(monkeypatch):
    class FakeResp:
        ok = True
        status_code = 200

        def json(self):
            return {
                "device_code": "dev-1",
                "user_code": "WXYZ",
                "verification_uri": "https://google.com/device",
                "expires_in": 600,
                "interval": 5,
            }

    monkeypatch.setattr(youtube_oauth.requests, "post", lambda *a, **k: FakeResp())
    device = youtube_oauth.request_device_code(_oauth())
    assert device.verification_url == "https://google.com/device"


def test_poll_device_token_pending_and_authorized(monkeypatch):
    pending = {"error": "authorization_pending"}
    success = {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}

    class FakeResp:
        def __init__(self, payload, ok=False, status_code=400):
            self._payload = payload
            self.ok = ok
            self.status_code = status_code

        def json(self):
            return self._payload

    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(pending)
        return FakeResp(success, ok=True, status_code=200)

    monkeypatch.setattr(youtube_oauth.requests, "post", fake_post)
    assert youtube_oauth.poll_device_token(_oauth(), "dev").status == "pending"
    result = youtube_oauth.poll_device_token(_oauth(), "dev")
    assert result.status == "authorized"
    assert result.token_data["refresh_token"] == "ref"


def test_poll_device_token_maps_google_errors(monkeypatch):
    class FakeResp:
        ok = False
        status_code = 400

        def __init__(self, error):
            self._error = error

        def json(self):
            return {"error": self._error}

    monkeypatch.setattr(
        youtube_oauth.requests, "post", lambda *a, **k: FakeResp("slow_down")
    )
    assert youtube_oauth.poll_device_token(_oauth(), "dev").status == "slow_down"
    monkeypatch.setattr(
        youtube_oauth.requests, "post", lambda *a, **k: FakeResp("access_denied")
    )
    assert youtube_oauth.poll_device_token(_oauth(), "dev").status == "denied"
    monkeypatch.setattr(
        youtube_oauth.requests, "post", lambda *a, **k: FakeResp("expired_token")
    )
    assert youtube_oauth.poll_device_token(_oauth(), "dev").status == "expired"


def test_expired_refresh_clears_tokens(conn, monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    youtube_oauth.save_tokens(
        conn,
        {"access_token": "old", "refresh_token": "dead", "expires_in": 3600},
    )
    conn.execute(
        "UPDATE oauth_tokens SET expires_at = ? WHERE provider = 'youtube'",
        (past,),
    )
    conn.commit()

    def boom(*a, **k):
        raise OAuthExpired("YouTube sign-in expired. Reconnect from Channels.")

    monkeypatch.setattr(youtube_oauth, "refresh_access_token", boom)
    try:
        youtube_oauth.get_valid_access_token(conn, _oauth())
        assert False, "expected OAuthExpired"
    except OAuthExpired:
        pass
    assert youtube_oauth.is_connected(conn) is False
