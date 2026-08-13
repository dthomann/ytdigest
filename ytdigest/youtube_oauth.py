"""YouTube OAuth2 (readonly) for subscription sync."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from scripts.resolve_channels import ResolvedChannel

YOUTUBE_PROVIDER = "youtube"
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SUBSCRIPTIONS_URL = "https://www.googleapis.com/youtube/v3/subscriptions"


class OAuthExpired(Exception):
    """Refresh token rejected (Testing expiry, revoked, or wrong client)."""


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class DevicePollResult:
    status: str  # pending | slow_down | denied | expired | authorized | error
    token_data: dict | None = None
    error: str | None = None


def request_device_code(oauth: OAuthConfig) -> DeviceCode:
    resp = requests.post(
        DEVICE_CODE_URL,
        data={"client_id": oauth.client_id, "scope": READONLY_SCOPE},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Device-code request failed ({resp.status_code})") from exc
    if not resp.ok:
        detail = data.get("error_description") or data.get("error") or resp.text[:200]
        raise RuntimeError(f"Device-code request failed: {detail}")
    url = data.get("verification_url") or data.get("verification_uri") or "https://www.google.com/device"
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_url=url,
        expires_in=int(data.get("expires_in", 1800)),
        interval=max(1, int(data.get("interval", 5))),
    )


def poll_device_token(oauth: OAuthConfig, device_code: str) -> DevicePollResult:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": oauth.client_id,
            "client_secret": oauth.client_secret,
            "device_code": device_code,
            "grant_type": DEVICE_GRANT,
        },
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        return DevicePollResult(status="error", error=f"HTTP {resp.status_code}")
    if resp.ok and data.get("access_token"):
        return DevicePollResult(status="authorized", token_data=data)
    err = data.get("error") or ""
    if err == "authorization_pending":
        return DevicePollResult(status="pending")
    if err == "slow_down":
        return DevicePollResult(status="slow_down")
    if err == "access_denied":
        return DevicePollResult(status="denied")
    if err == "expired_token":
        return DevicePollResult(status="expired")
    detail = data.get("error_description") or err or f"HTTP {resp.status_code}"
    return DevicePollResult(status="error", error=detail)


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
    if resp.status_code == 400:
        raise OAuthExpired("YouTube sign-in expired. Reconnect from Channels.")
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


def clear_tokens(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM oauth_tokens WHERE provider = ?", (YOUTUBE_PROVIDER,))
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

    try:
        token_data = refresh_access_token(oauth, refresh)
    except OAuthExpired:
        clear_tokens(conn)
        raise
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
