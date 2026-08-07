"""YouTube OAuth routes."""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ... import db, youtube_oauth
from ..oauth_helpers import oauth_config_from_request, oauth_configured
from ..services.sync_flash import SyncFlash, save_sync_flash
from ..services.youtube_sync import run_youtube_sync

router = APIRouter(prefix="/auth/youtube")

_oauth_states: dict[str, float] = {}
_OAUTH_STATE_TTL_SECONDS = 600


def _store_oauth_state(state: str) -> None:
    now = time.time()
    expired = [key for key, expires_at in _oauth_states.items() if expires_at <= now]
    for key in expired:
        del _oauth_states[key]
    _oauth_states[state] = now + _OAUTH_STATE_TTL_SECONDS


def _consume_oauth_state(state: str) -> bool:
    expires_at = _oauth_states.pop(state, None)
    return expires_at is not None and expires_at > time.time()


@router.get("/start")
def oauth_start(request: Request):
    """Send the user straight to Google's sign-in page."""
    if not oauth_configured(request.app.state.config):
        return RedirectResponse("/channels?oauth_setup=1", status_code=303)

    oauth_cfg = oauth_config_from_request(request.app.state.config, request)
    if oauth_cfg is None:
        return RedirectResponse("/channels?oauth_setup=1", status_code=303)

    state = secrets.token_urlsafe(16)
    _store_oauth_state(state)
    url = youtube_oauth.authorization_url(oauth_cfg, state)
    return RedirectResponse(url)


@router.get("/callback")
def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return RedirectResponse(f"/channels?oauth_error={error}", status_code=303)
    if not code or not state or not _consume_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid OAuth callback")

    config = request.app.state.config
    oauth_cfg = oauth_config_from_request(config, request)
    if oauth_cfg is None:
        return RedirectResponse("/channels?oauth_setup=1", status_code=303)

    conn = db.connect(config.db_path)
    try:
        token_data = youtube_oauth.exchange_code(oauth_cfg, code)
        youtube_oauth.save_tokens(conn, token_data)
        flash = run_youtube_sync(conn, config, oauth_cfg, connected=True)
        save_sync_flash(config, flash)
    except Exception as exc:
        save_sync_flash(config, SyncFlash(error=str(exc)))
        return RedirectResponse("/channels?oauth_error=sync_failed", status_code=303)
    finally:
        conn.close()

    return RedirectResponse("/channels?oauth=connected", status_code=303)
