"""Optional shared-token auth for the web UI."""
from __future__ import annotations

import ipaddress
import logging
import secrets
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..config import Config

logger = logging.getLogger("ytdigest.web.auth")

AUTH_COOKIE = "ytdigest_auth"
AUTH_HEADER = "X-YTDigest-Token"
LOGIN_PATH = "/auth/login"


def auth_token(config: Config) -> str:
    return config.secrets.get("WEB_AUTH_TOKEN", "")


def auth_enabled(config: Config) -> bool:
    return bool(auth_token(config))


def _is_loopback_host(host: str) -> bool:
    bare = host.split(":", 1)[0].strip("[]")
    if bare in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def web_exposed_without_auth(config: Config) -> bool:
    """True when the server binds beyond loopback but no auth token is configured."""
    if auth_enabled(config):
        return False
    host = config.values.get("web_host", "127.0.0.1")
    return host not in ("127.0.0.1", "localhost", "::1")


def request_authenticated(request: Request, config: Config) -> bool:
    token = auth_token(config)
    if not token:
        return True

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        supplied = auth_header[7:]
        if secrets.compare_digest(supplied, token):
            return True

    header_token = request.headers.get(AUTH_HEADER, "")
    if header_token and secrets.compare_digest(header_token, token):
        return True

    cookie_token = request.cookies.get(AUTH_COOKIE, "")
    return bool(cookie_token and secrets.compare_digest(cookie_token, token))


def _is_public_path(path: str) -> bool:
    return path == LOGIN_PATH or path.startswith("/static/")


def install_auth_middleware(app, config: Config) -> None:
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if not auth_enabled(config):
            return await call_next(request)

        if _is_public_path(request.url.path):
            return await call_next(request)

        if request_authenticated(request, config):
            return await call_next(request)

        if request.method == "GET":
            next_url = quote(str(request.url.path))
            if request.url.query:
                next_url = quote(str(request.url))
            return RedirectResponse(f"{LOGIN_PATH}?next={next_url}", status_code=303)

        return Response("Unauthorized", status_code=401)


def warn_if_unauthenticated_exposure(config: Config) -> None:
    if web_exposed_without_auth(config):
        logger.warning(
            "web UI listens on %s without WEB_AUTH_TOKEN — anyone on the network can "
            "manage channels, trigger runs, and install systemd services. Set WEB_AUTH_TOKEN "
            "in .env or bind web_host to 127.0.0.1",
            config.values.get("web_host"),
        )
