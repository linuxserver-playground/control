"""
Settings come from the environment only: the container runs behind SWAG with an env_file, and there is
nothing to configure that an operator would want in a file. Everything secret (the GitHub App's key,
webhook secret) lives in the data dir, not here.
"""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator


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
    site_url: str = "https://linuxserver-playground.github.io/site"
    """Public URL of the website, the single pane of glass: Discord embeds link to its image, build and
    scheduler pages. No trailing slash."""
    discord_verbose: bool = False
    """Also post the quiet scheduler embeds (a rebuild dispatched for an unreleased head, a skip flag that
    held a branch back). The error-streak alert and the build embeds post regardless."""
    hub_url: str = "https://linuxserver-playground.github.io/hub"
    """The hub's Pages site; control reads a build's entry there to say where an overdue publication is stuck."""
    publish_deadline_minutes: int = 30
    """A released build's site page normally appears within minutes; past this it is reported on Discord."""
    publish_expiry_hours: int = 24
    """After this control stops watching an overdue publication and says so."""
    publish_watch: bool = True
    """Hold released builds' Discord messages until their website page is live (publish.py). Tests turn it off."""
    hub_repo: str = "hub"
    """Repository (in github_org) that receives a repository_dispatch `image-released` after a live release."""
    site_repo: str = "site"
    """Repository (in github_org) that receives a repository_dispatch `hub-updated` when the hub's Deploy succeeds."""
    hub_consumers: tuple[str, ...] = ("site",)
    """Repositories (in github_org) that rebuild from hub data; each gets `hub-updated`. Env HUB_CONSUMERS (csv)."""
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    """A classic OAuth App used only for sign-in. Its consent screen lists plain scopes (read:user, read:org)
    instead of the GitHub App's "act on your behalf" text; membership is then read with the user's own token.
    Without it, sign-in falls back to the GitHub App's OAuth client and the installation token."""
    events_secret: str | None = None
    """Shared with the workflows repo; signs the build events POSTed to /events. Unset means /events is off."""
    backfill_on_start: bool = False
    """Run one GitHub API backfill in the background after boot, once the app is configured and installed."""
    concurrency_limit: int = 20
    """GitHub's concurrent job limit for the org, which the queue page measures in-progress jobs against."""
    batch_reserve: int = 4
    """Job slots a batch build project (batch.py) never sizes into: kept free for image, hub and site builds."""
    batch_min_parallel: int = 2
    """The narrowest batch run control starts, so a batch always progresses even on a busy fleet."""
    batch_max_parallel: int = 16
    """The widest batch run control starts, whatever is free; each project's workflow may cap it lower."""
    batch_live: tuple[str, ...] = ("wheelie", "proot-apps", "selkies-layers")
    """Batch projects control dispatches for real. The others are sized and recorded as dry runs, because their
    own cron still fires until the repo's `SCHEDULED_BY_CONTROL` variable is set. Env CONTROL_BATCH_LIVE (csv)."""
    deliveries_keep_days: int = 14
    """Webhook deliveries older than this are deleted daily (housekeeping.py); 0 keeps them all. Env
    CONTROL_DELIVERIES_KEEP_DAYS."""
    snapshots_keep: int = 3
    """Daily database snapshots kept in `<data dir>/snapshots/`; 0 turns them off. Env CONTROL_SNAPSHOTS_KEEP."""
    run_archive: bool = True
    """Attach `run.json` and `logs.zip` to every live release once its run completes (archive.py): GitHub deletes
    run logs after 90 days and the Release is the permanent record. Tests turn it off. Env CONTROL_RUN_ARCHIVE."""
    run_archive_per_pass: int = 3
    """Runs archived per minute at most, so the first pass over older releases stays gentle on the API budget."""
    run_archive_attempts: int = 8
    """Tries before control gives up on archiving a run (backing off from 5 minutes to 6 hours) and says so."""
    run_archive_max_logs_mb: int = 500
    """A run's log archive larger than this (compressed) is not attached; run.json still is."""
    pages_check: bool = True
    """Warn once (log and Discord) about every image repo without a GitHub Pages site, whose build files then never
    reach the website, and list them in the discovery record (pages.py). Read-only: control never enables Pages."""
    heal_pages: bool = True
    """Redeploy an image repo's Pages site (`reason: pages` on its default branch) when a deploy failed, backing off
    (recover.py). A failed deploy leaves the previous site up, stale; repeating one is always safe."""
    heal_pages_backoff_minutes: tuple[int, ...] = (5, 30, 120, 1440)
    """Wait before each retry: the first after the failure, each next after the previous dispatch; the last step
    repeats. Env CONTROL_HEAL_PAGES_BACKOFF_MINUTES (csv)."""
    heal_pages_attempts: int = 4
    """Retries before control gives up on a repo's Pages site and says so (about a day with the default steps)."""
    heal_reruns: bool = True
    """Re-run the failed jobs of a live build run once when its failing step is plainly infrastructure (checkout,
    runner setup, registry login, artifacts, the release upload), never the image build, the tests or a check."""
    heal_rerun_delay_minutes: int = 5
    """How long after the run failed control re-runs it: a flake upstream of us (GHCR, the artifact service) often
    lasts a few minutes."""
    heal_rerun_window_hours: int = 6
    """A failed run not re-run within this long (the fleet stayed busy) is given up on and reported; failures
    older than this when control first sees them are left alone."""
    heal_per_tick: int = 3
    """Heal actions (dispatches and re-runs together) control starts per minute at most, so an outage that failed
    many repos at once does not come back as a burst."""
    scheduler: bool = True
    """Run the fleet discovery and scheduler loop beside the server (once the app is configured and installed)."""
    session_secret: str | None = None
    """Signs the sign-in cookie. Unset means a random one per process: sign-ins do not survive a restart."""
    allowed_origins: tuple[str, ...] = ("https://linuxserver-playground.github.io",)
    """Web origins (`scheme://host[:port]`) allowed to drive the JSON API from the browser: they get CORS with
    credentials, may be `next` after sign-in, and make the session cookie `SameSite=None` so it travels with
    their fetches. Env CONTROL_ALLOWED_ORIGINS (csv); empty disables all of that. Never `*`."""

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        origins: list[str] = []
        for raw in value:
            origin = raw.strip().rstrip("/").lower()
            if not origin:
                continue
            scheme, sep, host = origin.partition("://")
            if scheme not in ("http", "https") or not sep or not host or "/" in host or "*" in host:
                raise ValueError(f"allowed origin must be scheme://host[:port], got {raw!r}")
            origins.append(origin)
        return tuple(origins)

    @field_validator("heal_pages_backoff_minutes")
    @classmethod
    def _backoff(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(step < 1 for step in value):
            raise ValueError("the Pages backoff needs at least one step, each a positive number of minutes")
        return value

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
        if site := env("SITE_URL"):
            fields["site_url"] = site.rstrip("/")
        if verbose := env("CONTROL_DISCORD_VERBOSE"):
            fields["discord_verbose"] = verbose.strip().lower() in ("1", "true", "yes", "on")
        if hub_url := env("HUB_URL"):
            fields["hub_url"] = hub_url.rstrip("/")
        if deadline := env("CONTROL_PUBLISH_DEADLINE_MINUTES"):
            fields["publish_deadline_minutes"] = int(deadline)
        if expiry := env("CONTROL_PUBLISH_EXPIRY_HOURS"):
            fields["publish_expiry_hours"] = int(expiry)
        if watch := env("CONTROL_PUBLISH_WATCH"):
            fields["publish_watch"] = watch.strip().lower() in ("1", "true", "yes", "on")
        if hub_repo := env("HUB_REPO"):
            fields["hub_repo"] = hub_repo
        if site_repo := env("SITE_REPO"):
            fields["site_repo"] = site_repo
        if consumers := env("HUB_CONSUMERS"):
            fields["hub_consumers"] = tuple(c.strip() for c in consumers.split(",") if c.strip())
        if oauth_id := env("OAUTH_CLIENT_ID"):
            fields["oauth_client_id"] = oauth_id
        if oauth_secret := env("OAUTH_CLIENT_SECRET"):
            fields["oauth_client_secret"] = oauth_secret
        if secret := env("CONTROL_EVENTS_SECRET"):
            fields["events_secret"] = secret
        if backfill := env("CONTROL_BACKFILL_ON_START"):
            fields["backfill_on_start"] = backfill.strip().lower() in ("1", "true", "yes", "on")
        if limit := env("CONTROL_CONCURRENCY_LIMIT"):
            fields["concurrency_limit"] = int(limit)
        if reserve := env("CONTROL_BATCH_RESERVE"):
            fields["batch_reserve"] = int(reserve)
        if minimum := env("CONTROL_BATCH_MIN_PARALLEL"):
            fields["batch_min_parallel"] = int(minimum)
        if maximum := env("CONTROL_BATCH_MAX_PARALLEL"):
            fields["batch_max_parallel"] = int(maximum)
        if (batch_live := env("CONTROL_BATCH_LIVE")) is not None:
            fields["batch_live"] = tuple(p.strip() for p in batch_live.split(",") if p.strip())
        if keep_days := env("CONTROL_DELIVERIES_KEEP_DAYS"):
            fields["deliveries_keep_days"] = int(keep_days)
        if snapshots := env("CONTROL_SNAPSHOTS_KEEP"):
            fields["snapshots_keep"] = int(snapshots)
        if archive := env("CONTROL_RUN_ARCHIVE"):
            fields["run_archive"] = archive.strip().lower() in ("1", "true", "yes", "on")
        if per_pass := env("CONTROL_RUN_ARCHIVE_PER_PASS"):
            fields["run_archive_per_pass"] = int(per_pass)
        if attempts := env("CONTROL_RUN_ARCHIVE_ATTEMPTS"):
            fields["run_archive_attempts"] = int(attempts)
        if max_logs := env("CONTROL_RUN_ARCHIVE_MAX_LOGS_MB"):
            fields["run_archive_max_logs_mb"] = int(max_logs)
        if pages := env("CONTROL_PAGES_CHECK"):
            fields["pages_check"] = pages.strip().lower() in ("1", "true", "yes", "on")
        if heal_pages := env("CONTROL_HEAL_PAGES"):
            fields["heal_pages"] = heal_pages.strip().lower() in ("1", "true", "yes", "on")
        if backoff := env("CONTROL_HEAL_PAGES_BACKOFF_MINUTES"):
            fields["heal_pages_backoff_minutes"] = tuple(int(m) for m in backoff.split(",") if m.strip())
        if heal_attempts := env("CONTROL_HEAL_PAGES_ATTEMPTS"):
            fields["heal_pages_attempts"] = int(heal_attempts)
        if reruns := env("CONTROL_HEAL_RERUNS"):
            fields["heal_reruns"] = reruns.strip().lower() in ("1", "true", "yes", "on")
        if rerun_delay := env("CONTROL_HEAL_RERUN_DELAY_MINUTES"):
            fields["heal_rerun_delay_minutes"] = int(rerun_delay)
        if rerun_window := env("CONTROL_HEAL_RERUN_WINDOW_HOURS"):
            fields["heal_rerun_window_hours"] = int(rerun_window)
        if per_tick := env("CONTROL_HEAL_PER_TICK"):
            fields["heal_per_tick"] = int(per_tick)
        if scheduler := env("CONTROL_SCHEDULER"):
            fields["scheduler"] = scheduler.strip().lower() in ("1", "true", "yes", "on")
        if session_secret := env("CONTROL_SESSION_SECRET"):
            fields["session_secret"] = session_secret
        if (origins := env("CONTROL_ALLOWED_ORIGINS")) is not None:
            fields["allowed_origins"] = tuple(o.strip() for o in origins.split(",") if o.strip())
        return cls.model_validate(fields)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "github_app.json"
