"""Run trigger and status routes."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import db
from ..services.digest_queries import get_latest_run, get_run_sections

router = APIRouter(prefix="/runs")


def _status_context(request: Request, extra: dict | None = None) -> dict:
    run_mgr = request.app.state.run_manager
    ctx = {
        "run_state": run_mgr.state,
        "run_message": run_mgr.message,
        "run_id": run_mgr.run_id,
        "include_digest_oob": False,
    }
    if extra:
        ctx.update(extra)
    return ctx


def _latest_digest_oob(request: Request) -> dict:
    config = request.app.state.config
    conn = db.connect(config.db_path)
    try:
        run = get_latest_run(conn)
        sections = get_run_sections(conn, run.id) if run else {}
        return {"run": run, "sections": sections, "include_digest_oob": True}
    finally:
        conn.close()


@router.post("/start", response_class=HTMLResponse)
def start_run(request: Request):
    templates = request.app.state.templates
    run_mgr = request.app.state.run_manager
    ok, message = run_mgr.start()
    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        _status_context(request, {"run_message": message, "ok": ok}),
    )


@router.get("/status", response_class=HTMLResponse)
def run_status(request: Request):
    templates = request.app.state.templates
    run_mgr = request.app.state.run_manager
    extra = {}
    if run_mgr.consume_pending_digest_refresh():
        extra = _latest_digest_oob(request)
    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        _status_context(request, extra),
    )


@router.post("/reset", response_class=HTMLResponse)
def reset_run_status(request: Request):
    templates = request.app.state.templates
    run_mgr = request.app.state.run_manager
    run_mgr.reset()
    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        _status_context(request),
    )
