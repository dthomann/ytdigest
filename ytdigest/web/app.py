"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..config import Config
from .. import db
from .routes import auth, channels, digest, runs
from .services.run_manager import RunManager

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def create_app(config: Config) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(config.db_path)
        yield

    app = FastAPI(title="ytdigest", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.templates = TEMPLATES
    app.state.run_manager = RunManager(config)

    static_dir = WEB_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(runs.router)
    app.include_router(digest.router)
    app.include_router(channels.router)
    app.include_router(auth.router)

    return app
