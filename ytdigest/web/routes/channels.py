"""Channel management routes."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import channels as channels_mod, db, sync, youtube_oauth
from ..oauth_helpers import oauth_config_from_request, oauth_configured
from ..services.sync_flash import load_and_clear_sync_flash, save_sync_flash
from ..services.youtube_sync import run_youtube_sync

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from scripts.resolve_channels import resolve_one  # noqa: E402

router = APIRouter(prefix="/channels")


@router.get("", response_class=HTMLResponse)
def channels_page(request: Request):
    config = request.app.state.config
    templates = request.app.state.templates
    conn = db.connect(config.db_path)
    try:
        chs = channels_mod.list_channels(conn)
        connected = youtube_oauth.is_connected(conn)
        sync_flash = load_and_clear_sync_flash(config)
        oauth_error = request.query_params.get("oauth_error")
        oauth_setup = request.query_params.get("oauth_setup") == "1"
        return templates.TemplateResponse(
            request,
            "channels.html",
            {
                "channels": chs,
                "youtube_connected": connected,
                "oauth_configured": oauth_configured(config),
                "sync_flash": sync_flash,
                "oauth_error": oauth_error,
                "oauth_setup": oauth_setup,
            },
        )
    finally:
        conn.close()


@router.get("/youtube-sync")
def youtube_sync_start(request: Request):
    """One click: connect to Google if needed, otherwise run sync."""
    config = request.app.state.config
    if not oauth_configured(config):
        return RedirectResponse("/channels?oauth_setup=1", status_code=303)

    conn = db.connect(config.db_path)
    try:
        connected = youtube_oauth.is_connected(conn)
    finally:
        conn.close()

    if not connected:
        return RedirectResponse("/auth/youtube/start", status_code=303)

    return RedirectResponse("/channels/youtube-sync/run", status_code=303)


@router.get("/youtube-sync/run")
def youtube_sync_run(request: Request):
    """Run sync for an already-connected account (browser redirect flow)."""
    config = request.app.state.config
    oauth_cfg = oauth_config_from_request(config, request)
    if oauth_cfg is None:
        return RedirectResponse("/channels?oauth_setup=1", status_code=303)

    conn = db.connect(config.db_path)
    try:
        flash = run_youtube_sync(conn, config, oauth_cfg)
        save_sync_flash(config, flash)
    finally:
        conn.close()

    return RedirectResponse("/channels?sync=done", status_code=303)


@router.post("/add")
def add_channel(request: Request, channel_input: str = Form(...)):
    config = request.app.state.config
    conn = db.connect(config.db_path)
    try:
        resolved = resolve_one(channel_input, api_key=config.secrets.get("YOUTUBE_API_KEY") or None)
        channels_mod.add_channel(
            conn, resolved.channel_id, title=resolved.title, handle=resolved.handle, source="manual"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return RedirectResponse("/channels", status_code=303)


@router.post("/{channel_id}/toggle")
def toggle_channel(request: Request, channel_id: str, enabled: str = Form(...)):
    enabled_bool = enabled.lower() == "true"
    conn = db.connect(request.app.state.config.db_path)
    try:
        if not channels_mod.set_enabled(conn, channel_id, enabled_bool):
            raise HTTPException(status_code=404, detail="Channel not found")
    finally:
        conn.close()
    return RedirectResponse("/channels", status_code=303)


@router.post("/{channel_id}/remove")
def remove_channel_route(request: Request, channel_id: str):
    conn = db.connect(request.app.state.config.db_path)
    try:
        if not channels_mod.remove_channel(conn, channel_id):
            raise HTTPException(status_code=404, detail="Channel not found")
    finally:
        conn.close()
    return RedirectResponse("/channels", status_code=303)


@router.post("/sync/remove", response_class=HTMLResponse)
def apply_removals(request: Request, remove_ids: list[str] = Form(default=[])):
    templates = request.app.state.templates
    conn = db.connect(request.app.state.config.db_path)
    try:
        removed = sync.apply_sync_removals(conn, remove_ids) if remove_ids else 0
        return templates.TemplateResponse(
            request,
            "partials/sync_results.html",
            {"removed_count": removed, "message": f"Removed {removed} channel(s)"},
        )
    finally:
        conn.close()
