"""YouTube OAuth routes (device-code flow)."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ... import db, youtube_oauth
from ..oauth_helpers import oauth_config_from_request, oauth_configured
from ..services.sync_flash import SyncFlash, save_sync_flash
from ..services.youtube_sync import run_youtube_sync

router = APIRouter(prefix="/auth/youtube")


@dataclass
class _PendingDevice:
    device_code: str
    expires_at: float


_pending_device: dict[str, _PendingDevice] = {}


def _purge_pending() -> None:
    now = time.time()
    expired = [sid for sid, pending in _pending_device.items() if pending.expires_at <= now]
    for sid in expired:
        del _pending_device[sid]


@router.get("/start")
def oauth_start(request: Request):
    """Start Google device-code sign-in (works from a headless Pi)."""
    config = request.app.state.config
    if not oauth_configured(config):
        return RedirectResponse("/channels?oauth_setup=1", status_code=303)

    oauth_cfg = oauth_config_from_request(config, request)
    if oauth_cfg is None:
        return RedirectResponse("/channels?oauth_setup=1", status_code=303)

    templates = request.app.state.templates
    try:
        device = youtube_oauth.request_device_code(oauth_cfg)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "youtube_device.html",
            {"error": str(exc), "device": None, "sid": None, "interval_ms": 5000},
        )

    _purge_pending()
    sid = secrets.token_urlsafe(16)
    _pending_device[sid] = _PendingDevice(
        device_code=device.device_code,
        expires_at=time.time() + device.expires_in,
    )
    return templates.TemplateResponse(
        request,
        "youtube_device.html",
        {
            "error": None,
            "device": device,
            "sid": sid,
            "interval_ms": device.interval * 1000,
        },
    )


@router.post("/device/poll")
def device_poll(request: Request, sid: str = Form(...)):
    """Poll Google until the user finishes device-code sign-in."""
    pending = _pending_device.get(sid)
    if pending is None or pending.expires_at <= time.time():
        _pending_device.pop(sid, None)
        return JSONResponse({"status": "expired"})

    config = request.app.state.config
    oauth_cfg = oauth_config_from_request(config, request)
    if oauth_cfg is None:
        return JSONResponse({"status": "error", "error": "OAuth is not configured."}, status_code=400)

    result = youtube_oauth.poll_device_token(oauth_cfg, pending.device_code)
    if result.status != "authorized":
        payload = {"status": result.status}
        if result.error:
            payload["error"] = result.error
        if result.status in ("denied", "expired", "error"):
            _pending_device.pop(sid, None)
        return JSONResponse(payload)

    _pending_device.pop(sid, None)
    conn = db.connect(config.db_path)
    try:
        youtube_oauth.save_tokens(conn, result.token_data or {})
        flash = run_youtube_sync(conn, config, oauth_cfg, connected=True)
        save_sync_flash(config, flash)
    except Exception as exc:
        save_sync_flash(config, SyncFlash(error=str(exc)))
        return JSONResponse({"status": "authorized", "redirect": "/channels?oauth_error=sync_failed"})
    finally:
        conn.close()

    return JSONResponse({"status": "authorized", "redirect": "/channels?oauth=connected"})
