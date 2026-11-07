"""
The hub's hourly Refresh (`workflow_dispatch` of refresh.yml: stars, the mods and PRoot Apps indexes, the store
repositories), started here because GitHub skips most runs of the hub's own hourly cron, and which repositories
of the org rebuild from the hub's data. When the hub and those repositories deploy is deploys.py's.
"""

import datetime
import logging

from .github import GitHub
from .state import State

log = logging.getLogger(__name__)

HUB_REFRESH_FILE = "refresh.yml"
HUB_REFRESH_INTERVAL = datetime.timedelta(hours=1)
LAST_HUB_REFRESH = "last_hub_refresh"
"""kv key: ISO time of the last hub refresh dispatch that GitHub accepted."""


def in_org(state: State, name: str) -> bool:
    """Whether the org has this repository, as control last saw it. A second control for another org (Labs: no site
    repository of its own, and no hub until it is created) then nudges only what exists there."""
    return state.model.repo(f"{state.settings.github_org}/{name}") is not None


def consumers(state: State) -> tuple[str, ...]:
    """The repos that rebuild from hub data: the configured list, always including the site repo, less any the org
    does not have."""
    names = list(state.settings.hub_consumers)
    if state.settings.site_repo not in names:
        names.insert(0, state.settings.site_repo)
    return tuple(name for name in names if in_org(state, name))


def hub_refresh_due(state: State, now: datetime.datetime | None = None) -> bool:
    last = state.db.get(LAST_HUB_REFRESH)
    if not last:
        return True
    at = now or datetime.datetime.now(datetime.UTC)
    return at - datetime.datetime.fromisoformat(last) >= HUB_REFRESH_INTERVAL


async def dispatch_hub_refresh(state: State, github: GitHub, now: datetime.datetime | None = None) -> bool:
    """`workflow_dispatch` of the hub's Refresh on main, the same as its cron. Never raises; does nothing in an
    org without a hub repository."""
    if not in_org(state, state.settings.hub_repo):
        return False
    at = now or datetime.datetime.now(datetime.UTC)
    repo = f"{state.settings.github_org}/{state.settings.hub_repo}"
    try:
        await github.post(f"/repos/{repo}/actions/workflows/{HUB_REFRESH_FILE}/dispatches", {"ref": "main"})
    except Exception:
        log.exception("hub refresh dispatch failed")
        return False
    state.db.set(LAST_HUB_REFRESH, at.isoformat(timespec="seconds"))
    log.info("hub refresh dispatched")
    return True
