"""
The FastAPI app: the dashboard, a health check, webhook intake, the /events endpoint and the setup flow.
`create_app` takes settings so tests can point it at a temp data dir; `python -m control` builds it from
the environment.
"""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware

from . import auth, events, heal, manage, publish, scheduler, setup, views, webhooks
from .backfill import backfill_once
from .config import Settings
from .db import Database
from .github_app import CredentialStore, InstallationTokens
from .model import Model
from .notify import Discord
from .state import State, state_of
from .stream import Broadcaster
from .templating import package_version

log = logging.getLogger(__name__)


def build_state(settings: Settings, db: Database, http: httpx.AsyncClient) -> State:
    """Everything a handler needs; shared by the server's lifespan and the CLI commands."""
    return State(
        settings=settings,
        db=db,
        credentials=CredentialStore(settings.credentials_path),
        discord=Discord(settings.discord_webhook_url, http),
        http=http,
        tokens=InstallationTokens(http),
        model=Model(db),
        stream=Broadcaster(),
        session_secret=settings.session_secret or secrets.token_hex(32),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.db_path)
        db.migrate()
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": f"lsio-control/{package_version()}"}) as http:
            state = build_state(settings, db, http)
            app.state.control = state
            log.info(
                "control %s ready, public url %s, data in %s", package_version(), settings.public_url, settings.data_dir
            )
            if not settings.session_secret:
                log.warning("CONTROL_SESSION_SECRET is unset; using a random one, so sign-ins end at every restart")
            # The backfill and the scheduler talk to GitHub for a while (forever, for the scheduler); they run
            # beside the server and are cancelled on shutdown.
            tasks: list[asyncio.Task[object]] = []
            if settings.backfill_on_start:
                tasks.append(asyncio.create_task(backfill_once(state)))
            if settings.scheduler:
                tasks.append(asyncio.create_task(scheduler.run(state)))
                tasks.append(asyncio.create_task(heal.heal(state, delay=0)))  # jobs left running while control was down
            if settings.publish_watch:
                tasks.append(asyncio.create_task(publish.run(state)))
            try:
                yield
            finally:
                for task in [*tasks, *state.background]:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

    app = FastAPI(title="LinuxServer CI control", version=package_version(), lifespan=lifespan, docs_url=None)
    if settings.allowed_origins:
        # The website lives on another origin and drives the JSON API with the session cookie: only the listed
        # origins, never `*`, with credentials, and the headers the JSON actions send. Everything else is same-origin.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token"],
        )
    app.include_router(webhooks.router)
    app.include_router(events.router)
    app.include_router(setup.router)
    app.include_router(auth.router)
    app.include_router(manage.router)
    app.include_router(views.router)

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, object]:
        return {"status": "ok", "db": state_of(request).db.healthy()}

    return app
