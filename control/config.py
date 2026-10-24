"""
Settings come from the environment only: the container runs behind SWAG with an env_file, and there is
nothing to configure that an operator would want in a file. Everything secret (the GitHub App's key,
webhook secret) lives in the data dir, not here.
"""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    public_url: str = "http://localhost:8080"
    """Where GitHub and browsers reach us, scheme and host (and port), no trailing slash."""
    data_dir: Path = Path("/config")
    """Holds control.db and github_app.json. Treat its contents as secrets."""
    github_org: str = "linuxserver-playground"
    """The organisation the GitHub App is created in and installed on."""
    discord_webhook_url: str | None = None
    """Optional; without it notifications are logged and dropped."""
    setup_token: str | None = None
    """Optional; when set, /setup only answers to ?token=<this>, so strangers cannot start the app flow."""
    app_name: str = "LinuxServer CI (playground)"
    """The GitHub App's display name; GitHub derives the slug from it."""
    events_secret: str | None = None
    """Shared with the workflows repo; signs the build events POSTed to /events. Unset means /events is off."""
    backfill_on_start: bool = False
    """Run one GitHub API backfill in the background after boot, once the app is configured and installed."""
    concurrency_limit: int = 20
    """GitHub's concurrent job limit for the org, which the queue page measures in-progress jobs against."""
    scheduler: bool = True
    """Run the fleet discovery and scheduler loop beside the server (once the app is configured and installed)."""
    session_secret: str | None = None
    """Signs the sign-in cookie. Unset means a random one per process: sign-ins do not survive a restart."""

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        fields: dict[str, object] = {}
        if url := env("CONTROL_PUBLIC_URL"):
            fields["public_url"] = url.rstrip("/")
        if data_dir := env("CONTROL_DATA_DIR"):
            fields["data_dir"] = Path(data_dir)
        if org := env("GITHUB_ORG"):
            fields["github_org"] = org
        if webhook := env("DISCORD_WEBHOOK_URL"):
            fields["discord_webhook_url"] = webhook
        if token := env("CONTROL_SETUP_TOKEN"):
            fields["setup_token"] = token
        if name := env("CONTROL_APP_NAME"):
            fields["app_name"] = name
        if secret := env("CONTROL_EVENTS_SECRET"):
            fields["events_secret"] = secret
        if backfill := env("CONTROL_BACKFILL_ON_START"):
            fields["backfill_on_start"] = backfill.strip().lower() in ("1", "true", "yes", "on")
        if limit := env("CONTROL_CONCURRENCY_LIMIT"):
            fields["concurrency_limit"] = int(limit)
        if scheduler := env("CONTROL_SCHEDULER"):
            fields["scheduler"] = scheduler.strip().lower() in ("1", "true", "yes", "on")
        if session_secret := env("CONTROL_SESSION_SECRET"):
            fields["session_secret"] = session_secret
        return cls.model_validate(fields)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "github_app.json"
