"""OAuth config helpers for the web UI."""
from __future__ import annotations

from ..config import Config
from .. import youtube_oauth


def oauth_configured(config: Config) -> bool:
    return bool(
        config.secrets.get("YOUTUBE_OAUTH_CLIENT_ID")
        and config.secrets.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    )


def oauth_config_from_request(config: Config, _request=None) -> youtube_oauth.OAuthConfig | None:
    client_id = config.secrets.get("YOUTUBE_OAUTH_CLIENT_ID")
    client_secret = config.secrets.get("YOUTUBE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    return youtube_oauth.OAuthConfig(client_id=client_id, client_secret=client_secret)
