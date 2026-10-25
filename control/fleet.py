"""
Fleet discovery: which branches of which image repos are live, straight from the manifests. Every
`docker-*` repo of the org has an `image.yml` on its default branch whose `release.tags` names every tag
the image publishes; each other tag belongs to the branch whose own `image.yml` says `release.tag` is it
(CONVENTIONS §3). The result is the `manifests` table, one row per live branch, which the scheduler
walks and the fleet board lists instead of guessing branches from past builds. Rows are cached by the
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

from .db import utcnow
from .github import GitHub, installation_client
from .model import Manifest, Model, Repo
from .state import State
from .stream import Broadcaster

log = logging.getLogger(__name__)

LAST_DISCOVERY = "last_discovery"
"""kv key: JSON summary of the last fleet discovery (started, finished, counts, error), shown on /status."""
REFRESH_INTERVAL = datetime.timedelta(minutes=30)
MAX_CANDIDATE_BRANCHES = 20
"""How many non-default branches we are willing to read image.yml from when hunting for a tag's branch."""


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
            try:
                await self.repo(repo)
            except Exception:
                self.counts["errors"] += 1
                log.exception("discovery failed for %s", repo.full_name)
        # Repos that vanished from the org (deleted, transferred) take their branches with them.
        for known in self.model.repos(images_only=True):
            if known.full_name not in seen:
                self.model.delete_manifests(known.full_name)
        log.info("fleet discovery: %s", self.counts)
        return dict(self.counts)

    async def repo(self, repo: Repo) -> list[str]:
        """Store the live branches of one repo and return their names."""
        default = await self.fetch(repo.full_name, repo.default_branch)
        if default is None:
            log.warning("%s has no image.yml on %s", repo.full_name, repo.default_branch)
            self.model.delete_manifests(repo.full_name)
            return []
        sha, data = default
        self.store(repo.full_name, repo.default_branch, sha, data)
        live = [repo.default_branch]
        wanted = release_tags(data)[1:]  # the first is the default branch's own
        if wanted:
            branches = [
                str(b["name"])
                for b in await self.github.paginate(f"/repos/{repo.full_name}/branches", {"per_page": 100}, limit=200)
                if isinstance(b, dict) and b.get("name") and b["name"] != repo.default_branch
            ]
            # A branch named after its tag is the convention, so try those first, then the rest.
            candidates = [b for b in branches if b in wanted] + [b for b in branches if b not in wanted]
            for branch in candidates[:MAX_CANDIDATE_BRANCHES]:
                if not wanted:
                    break
                found = await self.fetch(repo.full_name, branch)
                if found is None:
                    continue
                branch_sha, branch_data = found
                tag = _section(branch_data, "release").get("tag")
                if tag in wanted and release_branch(branch_data) == branch:
                    self.store(repo.full_name, branch, branch_sha, branch_data)
                    live.append(branch)
                    wanted.remove(tag)
            if wanted:
                log.info("%s: no branch publishes %s", repo.full_name, ", ".join(wanted))
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
    try:
        record["counts"] = await Discovery(github, state.settings.github_org, state.model, state.stream).run()
    except Exception as exc:
        log.exception("fleet discovery failed")
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["finished"] = utcnow()
    state.db.set(LAST_DISCOVERY, json.dumps(record))
    return record


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
            # than at the next 30-minute discovery.
            await discovery.repo(repo)
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
