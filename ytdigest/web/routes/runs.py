"""Run trigger and status routes."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/runs")


@router.post("/start", response_class=HTMLResponse)
def start_run(request: Request):
    templates = request.app.state.templates
    run_mgr = request.app.state.run_manager
    ok, message = run_mgr.start()
    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        {"run_state": run_mgr.state, "run_message": message, "ok": ok},
    )


@router.get("/status", response_class=HTMLResponse)
def run_status(request: Request):
    templates = request.app.state.templates
    run_mgr = request.app.state.run_manager
    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        {
            "run_state": run_mgr.state,
            "run_message": run_mgr.message,
            "run_id": run_mgr.run_id,
        },
    )


@router.post("/reset", response_class=HTMLResponse)
def reset_run_status(request: Request):
    templates = request.app.state.templates
    run_mgr = request.app.state.run_manager
    run_mgr.reset()
    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        {"run_state": run_mgr.state, "run_message": run_mgr.message},
    )
