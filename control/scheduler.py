"""
The central scheduler that replaces every per-repo cron. Two schedules walk the live branches that fleet
discovery found:

- upstream check, once an hour per branch, spread across the hour by a hash of `repo/branch`: resolve the
  upstream version with the resolver the build uses (vendored, so both agree), read what the branch last
  released from its Release tags (`[T-]<upstream>-ls<N>`, CONVENTIONS §3), and dispatch a build when they
  differ;
- package check, once a week per branch with `build.package_check` on, spread across the week's hours.

Every decision lands in `scheduler_decisions` (`unchanged`, `skipped`, `dispatch`, `error`) with a JSON
detail, and the page and the fleet board read from there. Dispatching is always `workflow_dispatch` on
`build.yml` with the branch as `ref`: `repository_dispatch` would only ever run the default branch.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import re
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel

from .fleet import discover_once, discovery_due
from .github import GitHub, installation_client
from .model import Manifest, Model, Release
from .state import State
from .vendor import resolve_version as rv

log = logging.getLogger(__name__)

KIND_UPSTREAM = "upstream"
KIND_PACKAGES = "packages"
KIND_MANUAL = "manual"
COOLDOWN = datetime.timedelta(hours=2)
"""No second dispatch for a branch within this long of the last one, whatever triggered it."""
ERROR_ALERT_AFTER = 6
"""Consecutive resolver failures for one branch before Discord hears about it (once per streak)."""
TICK_SECONDS = 60.0
RESOLVE_TIMEOUT = 180.0
HOURS_PER_WEEK = 24 * 7
UNRESOLVABLE = {"command", "deb_repo"}
"""Sources control will not run: `command` is arbitrary shell from a repo and `deb_repo` needs docker. Both
still build on the weekly package check, where the build resolves them itself."""
WORKFLOW_FILE = "build.yml"


class Decision(BaseModel):
    image: str
    branch: str
    kind: str
    outcome: str
    detail: dict[str, Any]
    id: int | None = None

    def line(self) -> str:
        detail = ", ".join(f"{k}={v}" for k, v in self.detail.items() if v is not None)
        return f"{self.image}/{self.branch} [{self.kind}] {self.outcome}" + (f": {detail}" if detail else "")


# -- spreading ----------------------------------------------------------------------------------------------------


def slot(repo: str, branch: str) -> tuple[int, int]:
    """(minute of the hour, hour of the week) for a branch; stable across restarts and replicas."""
    digest = int(hashlib.sha256(f"{repo}/{branch}".encode()).hexdigest(), 16)
    return digest % 60, (digest // 60) % HOURS_PER_WEEK


def _hour(at: datetime.datetime) -> datetime.datetime:
    return at.replace(minute=0, second=0, microsecond=0)


def _week(at: datetime.datetime) -> datetime.datetime:
    """Monday 00:00 UTC of the week containing `at`."""
    return _hour(at).replace(hour=0) - datetime.timedelta(days=at.weekday())


def last_upstream_slot(repo: str, branch: str, now: datetime.datetime) -> datetime.datetime:
    """The most recent moment this branch's hourly check fell due (now or earlier)."""
    minute, _ = slot(repo, branch)
    due = _hour(now) + datetime.timedelta(minutes=minute)
    return due if due <= now else due - datetime.timedelta(hours=1)


def next_upstream_check(repo: str, branch: str, now: datetime.datetime) -> datetime.datetime:
    return last_upstream_slot(repo, branch, now) + datetime.timedelta(hours=1)


def last_package_slot(repo: str, branch: str, now: datetime.datetime) -> datetime.datetime:
    minute, hour = slot(repo, branch)
    due = _week(now) + datetime.timedelta(hours=hour, minutes=minute)
    return due if due <= now else due - datetime.timedelta(weeks=1)


def next_package_check(repo: str, branch: str, now: datetime.datetime) -> datetime.datetime:
    return last_package_slot(repo, branch, now) + datetime.timedelta(weeks=1)


def due(last_slot: datetime.datetime, last_checked: str | None) -> bool:
    """A check is due when the latest slot passed and nothing was recorded since it."""
    return last_checked is None or datetime.datetime.fromisoformat(last_checked) < last_slot


# -- released version ---------------------------------------------------------------------------------------------


def tag_pattern(release_tag: str) -> re.Pattern[str]:
    if release_tag == "latest":
        return re.compile(r"^(?P<upstream>.+)-ls(?P<n>\d+)$")
    return re.compile(rf"^{re.escape(release_tag)}-(?P<upstream>.+)-ls(?P<n>\d+)$")


def released_from_tag(tag: str, release_tag: str, other_tags: list[str]) -> str | None:
    """The `<upstream>` part of a release tag that belongs to the branch publishing `release_tag`, as
    next_tag.py matches it: the unprefixed branch never claims `develop-1.2.3-ls4`."""
    match = tag_pattern(release_tag).match(tag)
    if not match:
        return None
    if release_tag == "latest" and any(o and o != "latest" and tag.startswith(f"{o}-") for o in other_tags):
        return None
    return match.group("upstream")


def released_version(
    releases: list[Release], branch: str, release_tag: str, other_tags: list[str]
) -> tuple[str | None, str | None]:
    """(upstream, tag) of the branch's newest release. A Release whose target names another branch is not
    ours; a sha target is accepted on the tag shape alone, like next_tag.py."""
    for release in releases:
        upstream = released_from_tag(release.tag, release_tag, other_tags)
        if upstream is not None:
            return upstream, release.tag
    return None, None


def release_tag_of(manifest: Manifest, default_branch: str) -> str:
    if manifest.release_tag:
        return manifest.release_tag
    return "latest" if manifest.branch == default_branch else manifest.branch


# -- resolving ----------------------------------------------------------------------------------------------------


def resolve_with_token(manifest: dict[str, Any], token: str | None) -> dict[str, Any]:
    """Run the vendored resolver in an empty directory (nothing of the repo is checked out here) with the
    installation token on api.github.com calls so upstream lookups share our rate limit, not the anonymous
    one. The resolver's HTTP hook is module state, so checks run one at a time."""
    original = rv.http_get

    def get(url: str, headers: dict[str, str] | None = None, method: str = "GET", timeout: int = 60) -> Any:
        merged = dict(headers or {})
        if token and "api.github.com" in url and "Authorization" not in merged:
            merged["Authorization"] = f"Bearer {token}"
        return original(url, headers=merged, method=method, timeout=timeout)

    rv.http_get = get
    try:
        with tempfile.TemporaryDirectory(prefix="control-resolve-") as empty:
            result: dict[str, Any] = rv.resolve(manifest, empty)
            return result
    finally:
        rv.http_get = original


Resolver = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]


async def resolve_in_thread(manifest: dict[str, Any], token: str | None) -> dict[str, Any]:
    return await asyncio.wait_for(asyncio.to_thread(resolve_with_token, manifest, token), RESOLVE_TIMEOUT)


# -- the scheduler ------------------------------------------------------------------------------------------------


class Scheduler:
    def __init__(
        self,
        state: State,
        github: GitHub | None,
        dry_run: bool = False,
        resolver: Resolver = resolve_in_thread,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.state = state
        self.github = github
        self.dry_run = dry_run
        self.resolver = resolver
        self.sleep = sleep
        self.model: Model = state.model

    # -- decisions ------------------------------------------------------------------------------------------------

    def record(self, image: str, branch: str, kind: str, outcome: str, detail: dict[str, Any]) -> Decision:
        decision = Decision(image=image, branch=branch, kind=kind, outcome=outcome, detail=detail)
        if not self.dry_run:
            decision.id = self.model.add_decision(image, branch, outcome, json.dumps(detail), kind)
            self.state.stream.publish("scheduler_decisions", decision.id)
        log.info("%s", decision.line())
        return decision

    def blocked(self, manifest: Manifest, image: str) -> dict[str, Any] | None:
        """The skip flag or in-flight run that stops any dispatch for this branch, or None."""
        skips = self.model.skips_for(image, manifest.branch)
        if skips:
            return {"reason": "skip", "scope": skips[0].scope, "note": skips[0].reason}
        run = self.model.active_run_for_branch(manifest.repo, manifest.branch)
        if run:
            return {"reason": "run in progress", "run_url": run.html_url, "run_id": run.id}
        return None

    def in_cooldown(self, image: str, branch: str, now: datetime.datetime) -> str | None:
        last = self.model.last_decision(image, branch, outcome="dispatch")
        if last and now - datetime.datetime.fromisoformat(last.at) < COOLDOWN:
            return last.at
        return None

    async def check_upstream(self, manifest: Manifest, kind: str = KIND_UPSTREAM) -> Decision:
        image = manifest.image
        branch = manifest.branch
        now = datetime.datetime.now(datetime.UTC)
        source = manifest.source_type
        if blocked := self.blocked(manifest, image):
            return self.record(image, branch, kind, "skipped", blocked)
        if source == "os":
            return self.record(image, branch, kind, "skipped", {"reason": "os image: builds on the package check"})
        if source in UNRESOLVABLE or source is None:
            why = {"reason": "source not resolvable by control", "source": source}
            return self.record(image, branch, kind, "skipped", why)
        if dispatched_at := self.in_cooldown(image, branch, now):
            return self.record(image, branch, kind, "skipped", {"reason": "cooldown", "dispatched_at": dispatched_at})

        try:
            resolved = await self.resolver(manifest.manifest, self.github.token if self.github else None)
        except rv.AssetsNotReady as exc:
            return self.record(image, branch, kind, "skipped", {"reason": "assets not ready", "message": str(exc)})
        except Exception as exc:
            decision = self.record(image, branch, kind, "error", {"message": f"{type(exc).__name__}: {exc}"})
            await self.alert_on_errors(image, branch, str(exc))
            return decision

        upstream = str(resolved["upstream_clean"])
        released, released_tag = self.released(manifest)
        detail: dict[str, Any] = {
            "upstream": upstream,
            "released": released,
            "released_tag": released_tag,
            "source_url": resolved.get("source_url"),
        }
        if skips := self.model.skips_for(image, branch, upstream):
            detail.update(reason="skip", scope=skips[0].scope, note=skips[0].reason)
            return self.record(image, branch, kind, "skipped", detail)
        if released == upstream:
            return self.record(image, branch, kind, "unchanged", detail)
        detail["reason"] = "no release yet" if released is None else "upstream changed"
        return await self.dispatch(manifest.repo, branch, image, kind, "upstream", detail)

    async def check_packages(self, manifest: Manifest, kind: str = KIND_PACKAGES) -> Decision:
        image = manifest.image
        if blocked := self.blocked(manifest, image):
            return self.record(image, manifest.branch, kind, "skipped", blocked)
        why = {"reason": "weekly package check"}
        return await self.dispatch(manifest.repo, manifest.branch, image, kind, "packages", why)

    async def dispatch(
        self, repo: str, branch: str, image: str, kind: str, reason: str, detail: dict[str, Any]
    ) -> Decision:
        """`workflow_dispatch` of build.yml on the branch; 204 means GitHub queued it (the run shows up by webhook)."""
        detail = {**detail, "dispatch_reason": reason, "dry_run": self.dry_run or None}
        if self.dry_run:
            return self.record(image, branch, kind, "dispatch", detail)
        if self.github is None:
            detail["message"] = "GitHub App not configured or not installed"
            return self.record(image, branch, kind, "error", detail)
        try:
            await self.github.post(
                f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
                {"ref": branch, "inputs": {"reason": reason}},
            )
        except httpx.HTTPError as exc:
            detail["message"] = f"{type(exc).__name__}: {exc}"
            return self.record(image, branch, kind, "error", detail)
        return self.record(image, branch, kind, "dispatch", detail)

    def released(self, manifest: Manifest) -> tuple[str | None, str | None]:
        """What the branch last shipped: its newest Release tag, else the last build summary as a hint."""
        repo = self.model.repo(manifest.repo)
        default_branch = repo.default_branch if repo else manifest.branch
        release_tag = release_tag_of(manifest, default_branch)
        siblings = [m for m in self.model.manifests(manifest.repo) if m.branch != manifest.branch]
        others = [release_tag_of(m, default_branch) for m in siblings]
        upstream, tag = released_version(
            self.model.releases_for_repo(manifest.repo, 100), manifest.branch, release_tag, others
        )
        if upstream is not None:
            return upstream, tag
        build = self.model.latest_build_for_branch(manifest.repo, manifest.branch)
        if build and build.upstream_clean:
            return build.upstream_clean, build.version_tag
        return None, None

    async def alert_on_errors(self, image: str, branch: str, message: str) -> None:
        if self.dry_run or self.model.consecutive_errors(image, branch) != ERROR_ALERT_AFTER:
            return
        await self.state.discord.post(
            f"scheduler: the upstream check for **{image}** `{branch}` failed {ERROR_ALERT_AFTER} times in a row: "
            f"{message}\n{self.state.settings.public_url}/scheduler?image={image}&outcome=error"
        )

    # -- the loop -------------------------------------------------------------------------------------------------

    async def tick(self, now: datetime.datetime | None = None) -> list[Decision]:
        """One pass: refresh the fleet when due, then every check whose slot has passed since its last record."""
        at = now or datetime.datetime.now(datetime.UTC)
        if discovery_due(self.state, at):
            await discover_once(self.state)
        decisions: list[Decision] = []
        for manifest in self.model.manifests():
            image = manifest.image
            last = self.model.last_decision(image, manifest.branch, KIND_UPSTREAM)
            if due(last_upstream_slot(manifest.repo, manifest.branch, at), last.at if last else None):
                decisions.append(await self.check_upstream(manifest))
            if not manifest.package_check:
                continue
            last = self.model.last_decision(image, manifest.branch, KIND_PACKAGES)
            if due(last_package_slot(manifest.repo, manifest.branch, at), last.at if last else None):
                decisions.append(await self.check_packages(manifest))
        return decisions

    async def run_forever(self) -> None:
        log.info("scheduler running, tick every %.0fs", TICK_SECONDS)
        while True:
            try:
                self.github = await installation_client(self.state)
                if self.github is None:
                    log.debug("scheduler idle: GitHub App not configured or not installed")
                else:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await self.sleep(TICK_SECONDS)


async def run(state: State) -> None:
    """The background task the app starts; it waits for the installation itself."""
    await Scheduler(state, None).run_forever()


async def run_once(
    state: State, image: str | None = None, branch: str | None = None, dry_run: bool = False
) -> list[Decision]:
    """`python -m control schedule --once`: the upstream check for every (or one) live branch, slots ignored."""
    github = await installation_client(state)
    scheduler = Scheduler(state, github, dry_run=dry_run)
    if github is not None and (discovery_due(state) or not state.model.manifests()):
        await discover_once(state)
    decisions: list[Decision] = []
    for manifest in state.model.manifests():
        if image and manifest.image != image and manifest.repo.rsplit("/", 1)[-1] != image:
            continue
        if branch and manifest.branch != branch:
            continue
        decisions.append(await scheduler.check_upstream(manifest, KIND_MANUAL))
    return decisions


def upcoming(
    manifests: list[Manifest], now: datetime.datetime, horizon: datetime.timedelta = datetime.timedelta(hours=24)
) -> list[dict[str, Any]]:
    """The next upstream check of every live branch and any package check inside the horizon, soonest first."""
    rows: list[dict[str, Any]] = []
    for manifest in manifests:
        rows.append(
            {
                "image": manifest.image,
                "branch": manifest.branch,
                "kind": KIND_UPSTREAM,
                "at": next_upstream_check(manifest.repo, manifest.branch, now).isoformat(timespec="seconds"),
            }
        )
        if manifest.package_check:
            packages = next_package_check(manifest.repo, manifest.branch, now)
            if packages - now <= horizon:
                rows.append(
                    {
                        "image": manifest.image,
                        "branch": manifest.branch,
                        "kind": KIND_PACKAGES,
                        "at": packages.isoformat(timespec="seconds"),
                    }
                )
    return sorted(rows, key=lambda r: (str(r["at"]), str(r["image"]), str(r["branch"])))
