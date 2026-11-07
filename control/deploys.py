"""
The hub's and the website's deploys, on control's clock.

Nothing else starts them: no workflow dispatches another, and neither repository deploys on a push or a cron. A
change that needs a deploy is *requested* here (a live release, a push to the repository's `main`, the hub's
refreshed data, a new hub deploy for the site, a repository created or deleted, a Pages retry from recover.py), and
the scheduler's minute loop (`tick`) dispatches the repository's `deploy.yml` at its next slot when anything was
requested. Slots are SLOT apart, the hub's at the top of each and the site's half a slot later, so a hub deploy is
usually done by the site's next slot; a dispatch also waits until SLOT has passed since the previous one and until
no Deploy of the repository (or, for the site, of the hub) is running. So each repository deploys at most nine
times an hour, below GitHub Pages' ten builds an hour for a site published from a branch, and not at all when
nothing changed.

A dispatch is a promise to look, not to publish: each Deploy compares what it built with its `gh-pages` branch and
pushes only when they differ, so a run that finds nothing new leaves Pages alone.

Two backstops keep the data honest without anything asking: the hub is requested when it has not been dispatched
for BACKSTOP (its reconcile notices deleted repositories and replaced bundles), and the site once a day after
FULL_REFRESH_AT UTC with `refresh_all`, which refetches every build the site keeps.

State lives in kv, one JSON value per repository (`deploys:<owner>/<repo>`): what is requested and why, the tags the
hub must ingest again (`force`), and the last dispatch. Never raises into callers.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import batch, relay

if TYPE_CHECKING:
    from .github import GitHub
    from .state import State

log = logging.getLogger(__name__)

SLOT = datetime.timedelta(seconds=400)
"""Nine slots an hour per repository."""
SITE_OFFSET = SLOT / 2
DEPLOY_FILE = "deploy.yml"
DEPLOY_WORKFLOW = "Deploy"
BACKSTOP = datetime.timedelta(hours=1)
FULL_REFRESH_AT = datetime.time(4, 17)
REASONS_KEPT = 20
"""Reasons kept per pending request (the oldest go; the count stays exact)."""
FORCE_KEPT = 100
HISTORY_KEPT = 20
REFRESH_MESSAGE = "Refresh data"
"""The commit message of the hub Refresh's `data` commits (its refresh-run.yml), which need a Deploy to be served."""
KEY = "deploys:"
FULL_REFRESH_DAY = "deploys_full_refresh_day"
"""kv key: the UTC date of the last daily full refresh of the website."""


@dataclass(frozen=True)
class Target:
    repo: str
    kind: str
    """`hub` or `site`."""
    offset: datetime.timedelta


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _iso(value: datetime.datetime) -> str:
    return value.isoformat(timespec="seconds")


def _at(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def hub(state: State) -> str:
    return f"{state.settings.github_org}/{state.settings.hub_repo}"


def targets(state: State) -> list[Target]:
    """The hub and the repositories that rebuild from its data, as far as the org has them."""
    found = []
    if relay.in_org(state, state.settings.hub_repo):
        found.append(Target(hub(state), "hub", datetime.timedelta(0)))
    for name in relay.consumers(state):
        found.append(Target(f"{state.settings.github_org}/{name}", "site", SITE_OFFSET))
    return found


def target(state: State, repo: str) -> Target | None:
    return next((t for t in targets(state) if t.repo == repo), None)


def _load(state: State, repo: str) -> dict[str, Any]:
    raw = state.db.get(KEY + repo)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _save(state: State, repo: str, value: dict[str, Any]) -> None:
    state.db.set(KEY + repo, json.dumps(value, sort_keys=True))


def request(
    state: State,
    repo: str,
    reason: str,
    *,
    force: str | None = None,
    refresh_all: bool = False,
    now: datetime.datetime | None = None,
) -> bool:
    """Ask for a Deploy of `repo` at its next slot. `force` is a `repo@tag` the hub ingests again. False for a
    repository that is not a deploy target here. Never raises."""
    try:
        if target(state, repo) is None:
            return False
        value = _load(state, repo)
        pending = value.get("pending") or {"since": _iso(now or _now()), "count": 0, "reasons": [], "force": []}
        pending["count"] = int(pending.get("count") or 0) + 1
        pending["reasons"] = [*pending.get("reasons", []), reason][-REASONS_KEPT:]
        if force and force not in pending.get("force", []):
            pending["force"] = [*pending.get("force", []), force][-FORCE_KEPT:]
        if refresh_all:
            pending["refresh_all"] = True
        value["pending"] = pending
        _save(state, repo, value)
        log.info("deploy of %s requested: %s", repo, reason)
        return True
    except Exception:
        log.exception("requesting a deploy of %s failed", repo)
        return False


def slot_of(at: datetime.datetime, offset: datetime.timedelta) -> int:
    return int((at.timestamp() - offset.total_seconds()) // SLOT.total_seconds())


def next_slot(at: datetime.datetime, offset: datetime.timedelta) -> datetime.datetime:
    start = (slot_of(at, offset) + 1) * SLOT.total_seconds() + offset.total_seconds()
    return datetime.datetime.fromtimestamp(start, datetime.UTC)


def _running(state: State, repo: str, now: datetime.datetime) -> bool:
    """A Deploy of the repo queued or running. One left 'running' for days lost its webhook and holds nothing."""
    return any(
        run.repo == repo and run.workflow_name == DEPLOY_WORKFLOW and now - _at(run.created_at) < batch.STALE_RUN
        for run in state.model.active_runs()
    )


def _inputs(t: Target, pending: dict[str, Any]) -> dict[str, str]:
    if t.kind == "hub" and pending.get("force"):
        return {"force": ",".join(pending["force"])}
    if t.kind == "site" and pending.get("refresh_all"):
        return {"refresh_all": "true"}
    return {}


def _backstops(state: State, now: datetime.datetime) -> None:
    for t in targets(state):
        if t.kind != "hub":
            continue
        value = _load(state, t.repo)
        last = value.get("last_dispatch_at")
        if not value.get("pending") and (not last or now - _at(last) >= BACKSTOP):
            request(state, t.repo, "hourly reconcile", now=now)
    day = now.date().isoformat()
    if now.time() >= FULL_REFRESH_AT and state.db.get(FULL_REFRESH_DAY) != day:
        state.db.set(FULL_REFRESH_DAY, day)
        for t in targets(state):
            if t.kind == "site":
                request(state, t.repo, "daily full refresh", refresh_all=True, now=now)


async def tick(state: State, github: GitHub, now: datetime.datetime | None = None) -> int:
    """Dispatch every requested Deploy whose slot has come. Returns how many went out. Never raises."""
    at = now or _now()
    sent = 0
    try:
        _backstops(state, at)
        for t in targets(state):
            if await _maybe_dispatch(state, github, t, at):
                sent += 1
    except Exception:
        log.exception("deploys tick failed")
    return sent


async def _maybe_dispatch(state: State, github: GitHub, t: Target, at: datetime.datetime) -> bool:
    value = _load(state, t.repo)
    pending = value.get("pending")
    if not pending:
        return False
    slot = slot_of(at, t.offset)
    if slot <= int(value.get("last_slot", -1)):
        return False
    last = value.get("last_dispatch_at")
    if last and at - _at(last) < SLOT:
        return False
    if _running(state, t.repo, at) or (t.kind == "site" and _running(state, hub(state), at)):
        return False
    inputs = _inputs(t, pending)
    body: dict[str, Any] = {"ref": "main", **({"inputs": inputs} if inputs else {})}
    try:
        await github.post(f"/repos/{t.repo}/actions/workflows/{DEPLOY_FILE}/dispatches", body)
    except Exception as exc:
        # Stays requested; the next tick tries again within the same slot.
        log.warning("deploy dispatch of %s failed: %s: %s", t.repo, type(exc).__name__, exc)
        value["last_error"] = {"at": _iso(at), "error": f"{type(exc).__name__}: {exc}"[:300]}
        _save(state, t.repo, value)
        return False
    entry = {
        "at": _iso(at),
        "count": pending.get("count"),
        "since": pending.get("since"),
        "reasons": pending.get("reasons", [])[-5:],
        "inputs": inputs,
    }
    # Requests that came in while the dispatch was in flight stay for the next slot.
    current = _load(state, t.repo)
    later = _after(current.get("pending"), pending, at)
    value = {
        "last_dispatch_at": _iso(at),
        "last_slot": slot,
        "history": [entry, *current.get("history", [])][:HISTORY_KEPT],
        **({"pending": later} if later else {}),
    }
    _save(state, t.repo, value)
    log.info(
        "deploy of %s dispatched for %s request(s) since %s: %s",
        t.repo,
        pending.get("count"),
        pending.get("since"),
        "; ".join(pending.get("reasons", [])[-3:]),
    )
    return True


def _after(current: Any, sent: dict[str, Any], at: datetime.datetime) -> dict[str, Any] | None:
    """What of `current` (the pending request now) the dispatch of `sent` did not cover."""
    if not isinstance(current, dict):
        return None
    extra = int(current.get("count") or 0) - int(sent.get("count") or 0)
    if extra <= 0:
        return None
    later: dict[str, Any] = {
        "since": _iso(at),
        "count": extra,
        "reasons": current.get("reasons", [])[-extra:],
        "force": [f for f in current.get("force", []) if f not in sent.get("force", [])],
    }
    if current.get("refresh_all") and not sent.get("refresh_all"):
        later["refresh_all"] = True
    return later


def observe(state: State, event: str, payload: dict[str, Any]) -> None:
    """Webhooks that make a deploy due. Never raises."""
    try:
        if event == "push":
            _observe_push(state, payload)
        elif event == "repository":
            name = str((payload.get("repository") or {}).get("name") or "")
            if name.startswith("docker-") and payload.get("action") in (
                "created",
                "deleted",
                "archived",
                "unarchived",
                "renamed",
                "publicized",
                "privatized",
            ):
                request(state, hub(state), f"{name} {payload.get('action')}")
    except Exception:
        log.exception("reading a webhook for deploys failed")


def _observe_push(state: State, payload: dict[str, Any]) -> None:
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    ref = str(payload.get("ref") or "")
    if payload.get("deleted") or not ref.startswith("refs/heads/"):
        return
    branch = ref.removeprefix("refs/heads/")
    t = target(state, repo)
    if t is None:
        return
    sha = str(payload.get("after") or "")[:7]
    if branch == "main":
        request(state, repo, f"main {sha}")
    elif t.kind == "hub" and branch == "data":
        message = str((payload.get("head_commit") or {}).get("message") or "")
        if message.startswith(REFRESH_MESSAGE):
            request(state, repo, "refreshed data")
    elif t.kind == "hub" and branch == "gh-pages":
        for other in targets(state):
            if other.kind == "site":
                request(state, other.repo, f"hub published {sha}")


def status(state: State, now: datetime.datetime | None = None) -> dict[str, Any]:
    """For /api/status.json: each repository's pending request, last dispatch and next slot."""
    at = now or _now()
    out: dict[str, Any] = {}
    for t in targets(state):
        value = _load(state, t.repo)
        out[t.repo] = {
            "pending": value.get("pending"),
            "last_dispatch_at": value.get("last_dispatch_at"),
            "next_slot_at": _iso(next_slot(at, t.offset)),
            "last_error": value.get("last_error"),
            "history": value.get("history", [])[:5],
        }
    return out
