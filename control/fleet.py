"""
Fleet discovery: which branches of which image repos are live, straight from the manifests. A `docker-*` repo
with an `image.yml` on its default branch is an image; every other branch whose own `image.yml` names it in
`release.branch` is live too, as the build workflow decides (CONVENTIONS §2), whether or not the default
branch's `release.tags` advertises its tag yet: a branch is often cut and tested before the README lists it.
The result is the `manifests` table, one row per live branch, which the scheduler walks and the fleet board
lists instead of guessing branches from past builds. Rows are cached by the
blob sha of image.yml, refreshed every 30 minutes and on `push` webhooks for a branch we know.
"""

import asyncio
import base64
import datetime
import json
import logging
from typing import Any

import httpx
import yaml

from . import pages
from .db import utcnow
from .github import GitHub, installation_client
from .model import Manifest, Model, Repo
from .state import State
from .stream import Broadcaster

log = logging.getLogger(__name__)

LAST_DISCOVERY = "last_discovery"
"""kv key: JSON summary of the last fleet discovery (started, finished, counts, error), shown on /status."""
HELPER_REPOS = "helper_repos"
"""kv key: JSON list of the `docker-*` repos the last discovery found without an image.yml on their default
branch (docker-mods, say). They are not images: the fleet board lists them apart and never links an image page."""
REFRESH_INTERVAL = datetime.timedelta(minutes=30)
MAX_CANDIDATE_BRANCHES = 50
"""How many non-default branches of one repo we read image.yml from (webtop has 21)."""


class _Loader(yaml.SafeLoader):
    """YAML dates stay strings, as the resolver's own loader keeps them (a version like 2024-01-01 must not parse)."""


def _timestamp(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(node)) if isinstance(node, yaml.ScalarNode) else str(node.value)


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _timestamp)


def parse_manifest(text: str) -> dict[str, Any]:
    loaded = yaml.load(text, Loader=_Loader)
    if not isinstance(loaded, dict):
        raise ValueError("image.yml is not a mapping")
    return loaded


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def manifest_row(repo: str, branch: str, sha: str, data: dict[str, Any], fetched_at: str | None = None) -> Manifest:
    now = fetched_at or utcnow()
    release = _section(data, "release")
    source = _section(_section(data, "upstream"), "version") or None
    build = _section(data, "build")
    return Manifest(
        repo=repo,
        branch=branch,
        sha=sha,
        fetched_at=now,
        manifest_json=json.dumps(data, separators=(",", ":"), default=str),
        release_tag=str(release["tag"]) if release.get("tag") else None,
        version_source_json=json.dumps(source, separators=(",", ":"), default=str) if source else None,
        package_check=build.get("package_check", True) is not False,
        created_at=now,
        updated_at=now,
    )


def release_tags(data: dict[str, Any]) -> list[str]:
    """Every tag the default branch's manifest says the image publishes, its own first."""
    release = _section(data, "release")
    tags: list[str] = []
    if release.get("tag"):
        tags.append(str(release["tag"]))
    for entry in release.get("tags") or []:
        tag = entry.get("tag") if isinstance(entry, dict) else entry
        if tag and str(tag) not in tags:
            tags.append(str(tag))
    return tags


def release_branch(data: dict[str, Any]) -> str | None:
    release = _section(data, "release")
    return str(release["branch"]) if release.get("branch") else None


class Discovery:
    def __init__(self, github: GitHub, org: str, model: Model, stream: Broadcaster) -> None:
        self.github = github
        self.org = org
        self.model = model
        self.stream = stream
        self.counts: dict[str, int] = {"repos": 0, "branches": 0, "fetched": 0, "unchanged": 0, "errors": 0}
        self.helpers: list[str] = []
        self.found: list[Repo] = []
        """The `docker-*` repos of this run that are not archived (pages.py sets them up)."""

    async def run(self) -> dict[str, int]:
        repos = await self.github.paginate(
            f"/orgs/{self.org}/repos", {"per_page": 100, "type": "all", "sort": "full_name"}
        )
        seen: set[str] = set()
        for data in repos:
            if not isinstance(data, dict) or not str(data.get("name", "")).startswith("docker-"):
                continue
            repo = Repo.from_github(data)
            self.model.upsert_repo(repo)
            seen.add(repo.full_name)
            if repo.archived:
                self.model.delete_manifests(repo.full_name)
                continue
            self.counts["repos"] += 1
            self.found.append(repo)
            try:
                await self.repo(repo)
            except Exception:
                self.counts["errors"] += 1
                log.exception("discovery failed for %s", repo.full_name)
        # Repos that vanished from the org (deleted, transferred) take their branches with them.
        for known in self.model.repos(images_only=True):
            if known.full_name not in seen:
                self.model.delete_manifests(known.full_name)
        self.model.db.set(HELPER_REPOS, json.dumps(sorted(self.helpers)))
        log.info("fleet discovery: %s, helper repos: %s", self.counts, ", ".join(self.helpers) or "none")
        return dict(self.counts)

    async def repo(self, repo: Repo) -> list[str]:
        """Store the live branches of one repo and return their names."""
        default = await self.fetch(repo.full_name, repo.default_branch)
        if default is None:
            log.info("%s has no image.yml on %s: a helper repo, not an image", repo.full_name, repo.default_branch)
            self.model.delete_manifests(repo.full_name)
            self.helpers.append(repo.full_name)
            return []
        sha, data = default
        self.store(repo.full_name, repo.default_branch, sha, data)
        live = [repo.default_branch]
        tags = {str(_section(data, "release").get("tag"))}
        advertised = release_tags(data)[1:]  # the first is the default branch's own
        branches = [
            str(b["name"])
            for b in await self.github.paginate(f"/repos/{repo.full_name}/branches", {"per_page": 100}, limit=200)
            if isinstance(b, dict) and b.get("name") and b["name"] != repo.default_branch
        ]
        # Branches named after an advertised tag first, so the cap never drops one of those.
        candidates = [b for b in branches if b in advertised] + [b for b in branches if b not in advertised]
        if len(candidates) > MAX_CANDIDATE_BRANCHES:
            cap = MAX_CANDIDATE_BRANCHES
            log.warning("%s has %s branches; reading the first %s", repo.full_name, len(candidates), cap)
        for branch in candidates[:MAX_CANDIDATE_BRANCHES]:
            found = await self.fetch(repo.full_name, branch)
            if found is None:
                continue
            branch_sha, branch_data = found
            if release_branch(branch_data) != branch:
                continue  # a dev branch: its image.yml came from another branch
            tag = str(_section(branch_data, "release").get("tag"))
            if tag in tags:
                log.warning("%s: %s publishes %s, which another live branch already does", repo.full_name, branch, tag)
                continue
            self.store(repo.full_name, branch, branch_sha, branch_data)
            live.append(branch)
            tags.add(tag)
        missing = [t for t in advertised if t not in tags]
        if missing:
            log.info("%s: no branch publishes %s", repo.full_name, ", ".join(missing))
        pruned = self.model.prune_manifests(repo.full_name, live)
        if pruned:
            self.stream.publish("manifests", repo.full_name)
        self.counts["branches"] += len(live)
        return live

    async def fetch(self, repo: str, ref: str) -> tuple[str, dict[str, Any]] | None:
        """(blob sha, parsed image.yml) at a ref, or None when the ref or the file does not exist."""
        try:
            body = await self.github.get_json(f"/repos/{repo}/contents/image.yml", {"ref": ref})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not isinstance(body, dict) or body.get("encoding") != "base64":
            return None
        text = base64.b64decode(body.get("content") or "").decode("utf-8")
        try:
            return str(body["sha"]), parse_manifest(text)
        except (ValueError, yaml.YAMLError) as exc:
            log.warning("%s@%s image.yml does not parse: %s", repo, ref, exc)
            return None

    def store(self, repo: str, branch: str, sha: str, data: dict[str, Any]) -> Manifest:
        existing = self.model.manifest(repo, branch)
        if existing and existing.sha == sha:
            self.model.touch_manifest(repo, branch)
            self.counts["unchanged"] += 1
            return existing
        row = manifest_row(repo, branch, sha, data)
        self.model.upsert_manifest(row)
        self.counts["fetched"] += 1
        self.stream.publish("manifests", [repo, branch])
        return row

    async def refresh_branch(self, repo: Repo, branch: str) -> Manifest | None:
        """One branch after a push: re-read image.yml and keep the row if the branch is still live."""
        found = await self.fetch(repo.full_name, branch)
        if found is None:
            if self.model.manifest(repo.full_name, branch):
                keep = [m.branch for m in self.model.manifests(repo.full_name) if m.branch != branch]
                self.model.prune_manifests(repo.full_name, keep)
                self.stream.publish("manifests", [repo.full_name, branch])
            return None
        sha, data = found
        if branch != repo.default_branch and release_branch(data) != branch:
            return None
        return self.store(repo.full_name, branch, sha, data)


async def discover_once(state: State) -> dict[str, Any]:
    """Run one discovery with the installation token and record the outcome under `last_discovery` in kv."""
    started = utcnow()
    record: dict[str, Any] = {"started": started, "finished": None, "counts": None, "error": None}
    github = await installation_client(state)
    if github is None:
        record["error"] = "GitHub App not configured or not installed"
        state.db.set(LAST_DISCOVERY, json.dumps(record))
        return record
    state.db.set(LAST_DISCOVERY, json.dumps(record))
    discovery = Discovery(github, state.settings.github_org, state.model, state.stream)
    try:
        record["counts"] = await discovery.run()
    except Exception as exc:
        log.exception("fleet discovery failed")
        record["error"] = f"{type(exc).__name__}: {exc}"
    from . import mods  # mods reads manifests with this module's loader

    if pages_counts := await pages.ensure(state, github, discovery.found):
        record["pages"] = pages_counts

    try:
        record["mods"] = await mods.discover(github, state.settings.github_org, state.model, state.stream)
    except Exception as exc:
        log.exception("mod discovery failed")
        record["mods_error"] = f"{type(exc).__name__}: {exc}"
    record["finished"] = utcnow()
    state.db.set(LAST_DISCOVERY, json.dumps(record))
    return record


def helper_repos(state: State) -> set[str]:
    """The helper repos of the last discovery; a repo that has gained live branches since is an image again."""
    raw = state.db.get(HELPER_REPOS)
    names = json.loads(raw) if raw else []
    return {str(n) for n in names if not state.model.manifests(str(n))} if isinstance(names, list) else set()


def last_discovery(state: State) -> dict[str, Any] | None:
    raw = state.db.get(LAST_DISCOVERY)
    if not raw:
        return None
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None


def discovery_due(state: State, now: datetime.datetime | None = None) -> bool:
    """Every 30 minutes, measured from the last start; a discovery that never finished counts as stale."""
    last = last_discovery(state)
    if not last or not last.get("started"):
        return True
    at = now or datetime.datetime.now(datetime.UTC)
    return at - datetime.datetime.fromisoformat(str(last["started"])) >= REFRESH_INTERVAL


def push_branch(payload: dict[str, Any]) -> str | None:
    ref = payload.get("ref")
    return ref[len("refs/heads/") :] if isinstance(ref, str) and ref.startswith("refs/heads/") else None


async def refresh_on_push(state: State, repo_full_name: str, branch: str) -> Manifest | None:
    """Called by webhook intake for pushes to a branch we track (or the default branch). Never raises."""
    try:
        repo = state.model.repo(repo_full_name)
        if repo is None or not repo.is_image or repo.archived:
            return None
        if branch != repo.default_branch and state.model.manifest(repo_full_name, branch) is None:
            return None
        github = await installation_client(state)
        if github is None:
            return None
        discovery = Discovery(github, state.settings.github_org, state.model, state.stream)
        if branch == repo.default_branch and not state.model.manifests(repo_full_name):
            # A repo with nothing live yet (new, or image.yml just added): find every live branch now rather
            # than at the next 30-minute discovery, and give it Pages if nothing has yet.
            await discovery.repo(repo)
            await pages.ensure(state, github, [repo])
            return state.model.manifest(repo_full_name, branch)
        return await discovery.refresh_branch(repo, branch)
    except Exception:
        log.exception("manifest refresh after push to %s %s failed", repo_full_name, branch)
        return None


def schedule_refresh(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler; the response never waits on GitHub."""
    branch = push_branch(payload)
    repository = payload.get("repository")
    if not branch or not isinstance(repository, dict) or not repository.get("full_name"):
        return None
    return state.spawn(refresh_on_push(state, str(repository["full_name"]), branch))
