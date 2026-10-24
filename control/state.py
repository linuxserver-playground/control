"""
What a request handler needs, in one place, so routers do not reach into `request.app.state` piecemeal
and tests can build the same object without a server.
"""

from dataclasses import dataclass

import httpx
from fastapi import Request

from .config import Settings
from .db import Database
from .github_app import CredentialStore, InstallationTokens
from .notify import Discord

INSTALLATION_ID = "installation_id"
"""kv key: the id of the app's installation on the org, once it is installed."""


@dataclass
class State:
    settings: Settings
    db: Database
    credentials: CredentialStore
    discord: Discord
    http: httpx.AsyncClient
    tokens: InstallationTokens

    @property
    def installation_id(self) -> int | None:
        value = self.db.get(INSTALLATION_ID)
        return int(value) if value else None


def state_of(request: Request) -> State:
    state: State = request.app.state.control
    return state
