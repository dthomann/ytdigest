"""Shared-token login for the web UI."""
from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import AUTH_COOKIE, auth_enabled, auth_token, request_authenticated

router = APIRouter(prefix="/auth")


@router.get("/login", response_class=HTMLResponse)
def web_login_page(request: Request, next: str = "/", error: str | None = None):
    config = request.app.state.config
    templates = request.app.state.templates
    if not auth_enabled(config):
        return RedirectResponse(next or "/", status_code=303)
    if request_authenticated(request, config):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": error == "1"},
    )


@router.post("/login")
def web_login_submit(
    request: Request,
    token: str = Form(...),
    next: str = Form("/"),
):
    config = request.app.state.config
    expected = auth_token(config)
    if not expected or not secrets.compare_digest(token.strip(), expected):
        return RedirectResponse(f"/auth/login?error=1&next={quote(next)}", status_code=303)

    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(safe_next, status_code=303)
    response.set_cookie(
        AUTH_COOKIE,
        expected,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response
