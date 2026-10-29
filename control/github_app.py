"""
The GitHub App that control acts as. It is created by the manifest flow (GitHub makes the app from a
manifest we post, hands back a one-time code, we convert the code into credentials), so nobody copies
private keys around: the credentials land in `${CONTROL_DATA_DIR}/github_app.json` and never leave it.
"""

import datetime
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import jwt
from pydantic import BaseModel

from .config import Settings

log = logging.getLogger(__name__)

API = "https://api.github.com"

# `workflows` lets the app change workflow files and dispatch them; `actions` lets it re-run, cancel and
# dispatch runs; `checks` is read because GitHub creates the check runs for Actions itself; `contents` write
# also attaches the run archive to releases (archive.py). Control never enables Pages or changes environments:
# a maintainer does that for a new image repo, and discovery only warns about one without a site (pages.py).
PERMISSIONS: dict[str, str] = {
    "actions": "write",
    "checks": "read",
    "contents": "write",
    "issues": "read",
    "members": "read",
    "metadata": "read",
    "packages": "write",
    "pull_requests": "write",
    "workflows": "write",
}

# installation and installation_repositories are delivered to every app without being listed, and
# GitHub rejects a manifest that names them; package events cannot be subscribed from a manifest at all.
# check_run and check_suite were subscribed once and never read; an App created before has them ticked in its
# settings, where they only cost deliveries (control drops them unstored, see handlers.STORED_EVENTS).
EVENTS: list[str] = [
    "issue_comment",
    "issues",
    "pull_request",
    "push",
    "release",
    "repository",
    "workflow_job",
    "workflow_run",
]


class AppCredentials(BaseModel):
    """What `POST /app-manifests/{code}/conversions` returns, trimmed to what we use."""

    id: int
    slug: str
    client_id: str
    client_secret: str
    webhook_secret: str
    pem: str
    html_url: str

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.slug}/installations/new"


def manifest(settings: Settings) -> dict[str, Any]:
    """The app manifest GitHub's "create from manifest" form takes; every URL derives from the public URL."""
    base = settings.public_url
    return {
        "name": settings.app_name,
        "url": base,
        "hook_attributes": {"url": f"{base}/webhooks/github", "active": True},
        "redirect_url": f"{base}/setup/callback",
        # Where GitHub sends members after they authorise the sign-in (control/auth.py). An app created
        # before this existed needs the URL added by hand under the app's settings.
        "callback_urls": [f"{base}/login/callback"],
        "setup_url": f"{base}/setup/installed",
        "setup_on_update": True,
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": EVENTS,
    }


async def convert(code: str, client: httpx.AsyncClient) -> AppCredentials:
    """Exchange the one-time code GitHub redirects back with for the app's credentials. No auth needed."""
    response = await client.post(
        f"{API}/app-manifests/{code}/conversions",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    response.raise_for_status()
    return AppCredentials.model_validate(response.json())


class CredentialStore:
    """github_app.json on disk, cached in memory. Only this process writes it, so the cache is safe."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cached: AppCredentials | None = None

    def load(self) -> AppCredentials | None:
        if self._cached is None and self.path.exists():
            self._cached = AppCredentials.model_validate_json(self.path.read_text())
        return self._cached

    def save(self, credentials: AppCredentials) -> None:
        """Written 0600 and atomically: the file holds the private key and webhook secret."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(credentials.model_dump_json(indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._cached = credentials


def app_jwt(credentials: AppCredentials, now: datetime.datetime | None = None) -> str:
    """The JWT that authenticates as the app itself (for installation tokens). Short-lived by design, and
    issued a minute in the past to survive clock skew between us and GitHub."""
    at = now or datetime.datetime.now(datetime.UTC)
    payload = {
        "iat": int(at.timestamp()) - 60,
        "exp": int(at.timestamp()) + 9 * 60,
        "iss": str(credentials.id),
    }
    return jwt.encode(payload, credentials.pem, algorithm="RS256")


class InstallationTokens:
    """Installation access tokens last an hour; we reuse one until five minutes before it expires."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._cache: dict[int, tuple[str, datetime.datetime]] = {}

    async def get(self, credentials: AppCredentials, installation_id: int) -> str:
        now = datetime.datetime.now(datetime.UTC)
        cached = self._cache.get(installation_id)
        if cached and cached[1] - now > datetime.timedelta(minutes=5):
            return cached[0]
        response = await self.client.post(
            f"{API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt(credentials)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        body = response.json()
        expires = datetime.datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        self._cache[installation_id] = (body["token"], expires)
        log.info("new installation token for %s, expires %s", installation_id, expires.isoformat())
        return str(body["token"])
