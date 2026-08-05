"""YouTube OAuth2 (readonly) for subscription sync."""
from __future__ import annotations

import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from scripts.resolve_channels import ResolvedChannel

YOUTUBE_PROVIDER = "youtube"
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SUBSCRIPTIONS_URL = "https://www.googleapis.com/youtube/v3/subscriptions"


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


def authorization_url(oauth: OAuthConfig, state: str) -> str:
    params = {
        "client_id": oauth.client_id,
        "redirect_uri": oauth.redirect_uri,
        "response_type": "code",
        "scope": READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(oauth: OAuthConfig, code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": oauth.client_id,
            "client_secret": oauth.client_secret,
            "redirect_uri": oauth.redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(oauth: OAuthConfig, refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": oauth.client_id,
            "client_secret": oauth.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def save_tokens(conn: sqlite3.Connection, token_data: dict, *, existing_refresh: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    expires_at = None
    if "expires_in" in token_data:
        expires = datetime.now(timezone.utc).timestamp() + token_data["expires_in"]
        expires_at = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()
    refresh = token_data.get("refresh_token") or existing_refresh
    conn.execute(
        """
        INSERT INTO oauth_tokens (provider, refresh_token, access_token, expires_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(provider) DO UPDATE SET
            refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
            access_token = excluded.access_token,
            expires_at = excluded.expires_at,
            updated_at = excluded.updated_at
        """,
        (YOUTUBE_PROVIDER, refresh, token_data.get("access_token"), expires_at, now),
    )
    conn.commit()


def get_tokens(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM oauth_tokens WHERE provider = ?", (YOUTUBE_PROVIDER,)
    ).fetchone()


def is_connected(conn: sqlite3.Connection) -> bool:
    row = get_tokens(conn)
    return row is not None and bool(row["refresh_token"] or row["access_token"])


def get_valid_access_token(conn: sqlite3.Connection, oauth: OAuthConfig) -> str | None:
    row = get_tokens(conn)
    if row is None:
        return None

    access = row["access_token"]
    expires_at = row["expires_at"]
    if access and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp > datetime.now(timezone.utc):
                return access
        except ValueError:
            pass

    refresh = row["refresh_token"]
    if not refresh:
        return access

    token_data = refresh_access_token(oauth, refresh)
    save_tokens(conn, token_data, existing_refresh=refresh)
    return token_data.get("access_token")


def fetch_subscriptions(access_token: str) -> list[ResolvedChannel]:
    out: list[ResolvedChannel] = []
    page_token = None
    while True:
        params = {
            "part": "snippet",
            "mine": "true",
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(
            SUBSCRIPTIONS_URL,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            resource = item["snippet"]["resourceId"]
            if resource.get("kind") != "youtube#channel":
                continue
            out.append(
                ResolvedChannel(
                    channel_id=resource["channelId"],
                    title=item["snippet"].get("title"),
                )
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out
