"""Digest browsing routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import db
from ..services.digest_queries import get_latest_run, get_run, get_run_sections, list_runs

router = APIRouter()


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    return iso[:10]


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    config = request.app.state.config
    templates = request.app.state.templates
    conn = db.connect(config.db_path)
    try:
        run = get_latest_run(conn)
        sections = get_run_sections(conn, run.id) if run else {}
        run_mgr = request.app.state.run_manager
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "run": run,
                "sections": sections,
                "run_state": run_mgr.state,
                "run_message": run_mgr.message,
            },
        )
    finally:
        conn.close()


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, offset: int = 0):
    config = request.app.state.config
    templates = request.app.state.templates
    conn = db.connect(config.db_path)
    try:
        runs = list_runs(conn, limit=10, offset=offset)
        ctx = {"runs": runs, "offset": offset, "has_more": len(runs) == 10}
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(request, "partials/run_list.html", ctx)
        return templates.TemplateResponse(request, "runs.html", ctx)
    finally:
        conn.close()


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    config = request.app.state.config
    templates = request.app.state.templates
    conn = db.connect(config.db_path)
    try:
        run = get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        sections = get_run_sections(conn, run_id)
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {"run": run, "sections": sections},
        )
    finally:
        conn.close()
