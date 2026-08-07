"""OAuth config helpers for the web UI."""
from __future__ import annotations

from ..config import Config
from .. import youtube_oauth


def oauth_configured(config: Config) -> bool:
    return bool(
        config.secrets.get("YOUTUBE_OAUTH_CLIENT_ID")
        and config.secrets.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    )


def oauth_config_from_request(config: Config, _request) -> youtube_oauth.OAuthConfig | None:
    client_id = config.secrets.get("YOUTUBE_OAUTH_CLIENT_ID")
    client_secret = config.secrets.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    public_url = config.values.get("web_public_url")
    if public_url:
        redirect_uri = public_url.rstrip("/") + "/auth/youtube/callback"
    else:
        # Never derive redirect URIs from the Host header — that enables header injection.
        redirect_uri = f"http://127.0.0.1:{config.web_port}/auth/youtube/callback"
    return youtube_oauth.OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
