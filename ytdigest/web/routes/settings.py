"""Settings: systemd services and daily run schedule."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import ConfigError
from ...systemd_units import (
    SystemdError,
    get_services_snapshot,
    install_timer_service,
    install_web_service,
    schedule_process_exit,
    uninstall_timer_service,
    uninstall_web_service,
    update_run_schedule,
)

router = APIRouter(prefix="/settings")


def _flash_url(message: str, *, ok: bool) -> str:
    from urllib.parse import quote

    kind = "ok" if ok else "error"
    return f"/settings?flash={quote(message)}&flash_ok={'1' if ok else '0'}"


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request):
    config = request.app.state.config
    templates = request.app.state.templates
    flash = request.query_params.get("flash")
    flash_ok = request.query_params.get("flash_ok") == "1"
    try:
        snapshot = get_services_snapshot(config)
        setup_error = None
    except SystemdError as exc:
        snapshot = None
        setup_error = str(exc)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "snapshot": snapshot,
            "setup_error": setup_error,
            "flash": flash,
            "flash_ok": flash_ok,
        },
    )


@router.post("/schedule", response_class=HTMLResponse)
def save_schedule(
    request: Request,
    digest_hour: int = Form(...),
    timezone: str = Form(...),
):
    config = request.app.state.config
    try:
        message = update_run_schedule(
            config,
            digest_hour=digest_hour,
            timezone=timezone.strip(),
        )
        return RedirectResponse(_flash_url(message, ok=True), status_code=303)
    except (ConfigError, SystemdError) as exc:
        return RedirectResponse(_flash_url(str(exc), ok=False), status_code=303)


@router.post("/web/install", response_class=HTMLResponse)
def web_install(request: Request):
    config = request.app.state.config
    try:
        result = install_web_service(config)
        if result.handoff:
            schedule_process_exit()
        return RedirectResponse(_flash_url(result.message, ok=True), status_code=303)
    except SystemdError as exc:
        return RedirectResponse(_flash_url(str(exc), ok=False), status_code=303)


@router.post("/web/uninstall", response_class=HTMLResponse)
def web_uninstall(request: Request):
    config = request.app.state.config
    try:
        message = uninstall_web_service(config)
        return RedirectResponse(_flash_url(message, ok=True), status_code=303)
    except SystemdError as exc:
        return RedirectResponse(_flash_url(str(exc), ok=False), status_code=303)


@router.post("/timer/install", response_class=HTMLResponse)
def timer_install(request: Request):
    config = request.app.state.config
    try:
        message = install_timer_service(config)
        return RedirectResponse(_flash_url(message, ok=True), status_code=303)
    except SystemdError as exc:
        return RedirectResponse(_flash_url(str(exc), ok=False), status_code=303)


@router.post("/timer/uninstall", response_class=HTMLResponse)
def timer_uninstall(request: Request):
    config = request.app.state.config
    try:
        message = uninstall_timer_service(config)
        return RedirectResponse(_flash_url(message, ok=True), status_code=303)
    except SystemdError as exc:
        return RedirectResponse(_flash_url(str(exc), ok=False), status_code=303)
