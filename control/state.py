"""
What a request handler needs, in one place, so routers do not reach into `request.app.state` piecemeal
and tests can build the same object without a server.
"""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request

from .config import Settings
from .db import Database
from .github_app import CredentialStore, InstallationTokens
from .model import Model
from .notify import Discord
from .stream import Broadcaster

log = logging.getLogger(__name__)

INSTALLATION_ID = "installation_id"
"""kv key: the id of the app's installation on the org, once it is installed."""
LAST_BACKFILL = "last_backfill"
"""kv key: JSON summary of the last backfill (started, finished, counts, error), shown on /status."""


@dataclass
class State:
    settings: Settings
    db: Database
    credentials: CredentialStore
    discord: Discord
    http: httpx.AsyncClient
    tokens: InstallationTokens
    model: Model
    stream: Broadcaster
    session_secret: str
    """Signs the sign-in cookie; from CONTROL_SESSION_SECRET or random per process."""
    background: set[asyncio.Task[Any]] = field(default_factory=set)
    """Fire-and-forget tasks (manifest refreshes, member-triggered discovery) kept referenced until done."""

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Run a coroutine beside the request without awaiting it; failures are logged, never lost silently."""
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self._reap)
        return task

    def _reap(self, task: asyncio.Task[Any]) -> None:
        self.background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    @property
    def installation_id(self) -> int | None:
        value = self.db.get(INSTALLATION_ID)
        return int(value) if value else None


def state_of(request: Request) -> State:
    state: State = request.app.state.control
    return state
