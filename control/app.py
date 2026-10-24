"""
The FastAPI app: a status page, a health check, webhook intake and the setup flow. `create_app` takes
settings so tests can point it at a temp data dir; `python -m control` builds it from the environment.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from . import setup, webhooks
from .config import Settings
from .db import Database
from .github_app import CredentialStore, InstallationTokens
from .model import Model
from .notify import Discord
from .state import State, state_of
from .stream import Broadcaster
from .templating import render

log = logging.getLogger(__name__)


def package_version() -> str:
    try:
        return version("lsio-control")
    except PackageNotFoundError:
        return "dev"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.db_path)
        db.migrate()
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": f"lsio-control/{package_version()}"}) as http:
            app.state.control = State(
                settings=settings,
                db=db,
                credentials=CredentialStore(settings.credentials_path),
                discord=Discord(settings.discord_webhook_url, http),
                http=http,
                tokens=InstallationTokens(http),
                model=Model(db),
                stream=Broadcaster(),
            )
            log.info(
                "control %s ready, public url %s, data in %s", package_version(), settings.public_url, settings.data_dir
            )
            yield

    app = FastAPI(title="LinuxServer CI control", version=package_version(), lifespan=lifespan, docs_url=None)
    app.include_router(webhooks.router)
    app.include_router(setup.router)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        state = state_of(request)
        return HTMLResponse(
            render(
                "index.html",
                settings=state.settings,
                version=package_version(),
                credentials=state.credentials.load(),
                installation_id=state.installation_id,
                discord=state.discord.configured,
                deliveries_count=state.db.count_deliveries(),
                deliveries=state.db.recent_deliveries(20),
            )
        )

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, object]:
        return {"status": "ok", "db": state_of(request).db.healthy()}

    return app
