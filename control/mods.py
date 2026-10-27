"""
Docker mods the scheduler checks: the docker-mods branches whose mod.yml has an `upstream` block (CONVENTIONS §3,
"Mods"). They replace the External-Triggers jobs of the Jenkins era, which looked up a tool's version every hour and
started the mod's build when Docker Hub had no `<branch>-<version>` tag yet.

Discovery reads every branch's mod.yml in one GraphQL query (a branch head that has not moved keeps its row) into
`mod_manifests`, at the same 30-minute cadence as the image fleet. A push to a branch that touches mod.yml (or
creates or deletes the branch) re-reads that one branch at once, as a push does for an image's image.yml.

The scheduler resolves a scheduled mod's `upstream.version` with the vendored resolver, as the build does, and asks
GHCR whether `mods:<branch>-<version>` exists: that tag is the record of what was built. A mod with
`upstream.lines` has a version per line, each looked up on its own (scheduler.Scheduler._check_mod). Decisions are
recorded as image `mods`, branch `<target>-<name>`, so the scheduler page, skip flags (`mods/<branch>[/<version>]`)
and the run links work as they do for images.
"""

import asyncio
import json
import logging
from typing import Any

import httpx
import yaml
from contracts import ContractError, validate

from .db import utcnow
from .fleet import parse_manifest
from .github import GitHub, GraphQLError, installation_client
from .model import Model, ModLine, ModManifest
from .state import State
from .stream import Broadcaster

log = logging.getLogger(__name__)

REPO_NAME = "docker-mods"
IMAGE = "mods"
"""The image name mod decisions are recorded under, and the GHCR package every mod is a tag of."""
SKIP_BRANCHES = frozenset({"master", "main", "template"})
UNSCHEDULED = frozenset({"os", "command", "deb_repo"})
"""Sources control never runs (see scheduler.UNRESOLVABLE); for a mod not even inside a `combined` source."""
REGISTRY = "https://ghcr.io"
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

MOD_YML = "mod.yml"
REF = """
query($owner: String!, $name: String!, $ref: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $ref) {
      name
      target { ... on Commit { oid file(path: "mod.yml") { object { ... on Blob { text } } } } }
    }
  }
}
"""
REFS = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target { ... on Commit { oid file(path: "mod.yml") { object { ... on Blob { text } } } } }
      }
    }
  }
}
"""


def repo_of(org: str) -> str:
    return f"{org}/{REPO_NAME}"


def source_types(source: dict[str, Any]) -> list[str]:
    """Every `type` in a version source, the parts of a `combined` one included."""
    types = [str(source.get("type"))]
    for part in source.get("sources") or []:
        if isinstance(part, dict):
            types.extend(source_types(part))
    return types


def resolver_manifest(mod: ModManifest, line: ModLine | None = None) -> dict[str, Any]:
    """mod.yml in the shape the resolver reads from image.yml: `upstream.version` (the line's source; the default
    line without one) and `build.platforms`. The build hands it the same shape (workflows scripts/mod_version.py)."""
    if line is not None and line.name is not None:
        return {"upstream": {"version": line.source}, "build": {"platforms": mod.platforms}}
    return {"upstream": mod.upstream, "build": {"platforms": mod.platforms}}


def version_tag(branch: str, upstream_clean: str) -> str:
    return f"{branch}-{upstream_clean}"


async def tag_exists(http: httpx.AsyncClient, org: str, tag: str) -> bool:
    """Whether `ghcr.io/<org>/mods:<tag>` exists, with an anonymous pull token (the package is public)."""
    repository = f"{org.lower()}/{IMAGE}"
    response = await http.get(f"{REGISTRY}/token", params={"scope": f"repository:{repository}:pull"})
    response.raise_for_status()
    token = response.json().get("token")
    headers = {"Accept": MANIFEST_TYPES, **({"Authorization": f"Bearer {token}"} if token else {})}
    head = await http.head(f"{REGISTRY}/v2/{repository}/manifests/{tag}", headers=headers)
    if head.status_code == 404:
        return False
    head.raise_for_status()
    return True


async def refs_page(github: GitHub, org: str, after: str | None) -> Any:
    """One page of the refs query. A branch without mod.yml (master) comes back as a NOT_FOUND error on its `file`
    beside the data of every other branch; those errors are expected, any other raises."""
    variables = {"owner": org, "name": REPO_NAME, "after": after}
    body = (await github.post("/graphql", {"query": REFS, "variables": variables})).json()
    errors = [
        e
        for e in body.get("errors") or []
        if not (isinstance(e, dict) and e.get("type") == "NOT_FOUND" and (e.get("path") or [None])[-1] == "file")
    ]
    if errors:
        raise GraphQLError(errors)
    return body.get("data")


def mod_text(node: dict[str, Any]) -> tuple[str, str | None]:
    """(head commit, mod.yml text or None) of a ref node of the refs or ref query."""
    target = node.get("target") or {}
    if not isinstance(target, dict):
        return "", None
    blob = (target.get("file") or {}).get("object") or {}
    text = blob.get("text") if isinstance(blob, dict) else None
    return str(target.get("oid") or ""), text if isinstance(text, str) else None


def store_node(
    node: dict[str, Any], model: Model, stream: Broadcaster, counts: dict[str, int] | None = None
) -> ModManifest | None:
    """Keep one branch's mod.yml: the stored row when the head has not moved, else the file parsed and validated.
    None when the branch has no mod.yml or an invalid one (the caller drops its row)."""
    counts = counts if counts is not None else {}
    branch = str(node.get("name") or "")
    commit, text = mod_text(node)
    if text is None:
        return None
    counts["branches"] = counts.get("branches", 0) + 1
    existing = model.mod_manifest(branch)
    if existing and existing.commit_sha == commit:
        model.touch_mod(branch)
        counts["unchanged"] = counts.get("unchanged", 0) + 1
        return existing
    try:
        mod = parse_manifest(text)
        validate("mod", mod)
    except (ValueError, yaml.YAMLError, ContractError) as exc:
        counts["invalid"] = counts.get("invalid", 0) + 1
        log.warning("%s@%s mod.yml is not valid: %s", REPO_NAME, branch, exc)
        return None
    now = utcnow()
    row = ModManifest(
        branch=branch,
        commit_sha=commit,
        fetched_at=now,
        mod_json=json.dumps(mod, separators=(",", ":"), default=str),
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    model.upsert_mod(row)
    stream.publish("mod_manifests", branch)
    counts["fetched"] = counts.get("fetched", 0) + 1
    return row


async def refresh_branch(
    github: GitHub, org: str, branch: str, model: Model, stream: Broadcaster
) -> ModManifest | None:
    """One branch after a push: read its mod.yml now and keep, replace or drop its row, as discovery would."""
    variables = {"owner": org, "name": REPO_NAME, "ref": f"refs/heads/{branch}"}
    body = (await github.post("/graphql", {"query": REF, "variables": variables})).json()
    errors = [
        e
        for e in body.get("errors") or []
        if not (isinstance(e, dict) and e.get("type") == "NOT_FOUND" and (e.get("path") or [None])[-1] == "file")
    ]
    if errors:
        raise GraphQLError(errors)
    repository = (body.get("data") or {}).get("repository") or {}
    node = repository.get("ref") if isinstance(repository, dict) else None
    row = store_node(node, model, stream) if isinstance(node, dict) else None
    if row is None and model.delete_mod(branch):
        stream.publish("mod_manifests", branch)
    return row


def touches_mod_yml(payload: dict[str, Any]) -> bool:
    """Whether a push created or deleted the branch or changed its mod.yml. GitHub lists at most 20 commits in a
    push payload; a longer push counts as touching it."""
    if payload.get("created") or payload.get("deleted"):
        return True
    commits = [c for c in payload.get("commits") or [] if isinstance(c, dict)]
    if len(commits) >= 20:
        return True
    return any(MOD_YML in (commit.get(key) or []) for commit in commits for key in ("added", "modified", "removed"))


async def refresh_on_push(state: State, branch: str, deleted: bool) -> ModManifest | None:
    """Called by webhook intake for a push to a docker-mods branch that touched mod.yml. Never raises."""
    try:
        if deleted:
            if state.model.delete_mod(branch):
                state.stream.publish("mod_manifests", branch)
                log.info("mod branch %s deleted; dropped its mod.yml", branch)
            return None
        github = await installation_client(state)
        if github is None:
            return None
        row = await refresh_branch(github, state.settings.github_org, branch, state.model, state.stream)
        log.info("mod branch %s pushed; mod.yml %s", branch, "kept" if row else "gone or invalid")
        return row
    except Exception:
        log.exception("mod.yml refresh after push to %s failed", branch)
        return None


def schedule_refresh(state: State, payload: dict[str, Any]) -> asyncio.Task[Any] | None:
    """Fire-and-forget from the webhook handler for a push to the org's docker-mods: images get the same from
    fleet.schedule_refresh. A branch that is not a mod (master, template) or a push that left mod.yml alone does
    nothing; the 30-minute discovery stays the backstop."""
    repository = payload.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != repo_of(state.settings.github_org):
        return None
    ref = payload.get("ref")
    branch = ref[len("refs/heads/") :] if isinstance(ref, str) and ref.startswith("refs/heads/") else None
    if not branch or branch in SKIP_BRANCHES or not touches_mod_yml(payload):
        return None
    return state.spawn(refresh_on_push(state, branch, bool(payload.get("deleted"))))


async def discover(github: GitHub, org: str, model: Model, stream: Broadcaster) -> dict[str, int]:
    """Store every mod branch's valid mod.yml and drop the rows of branches that are gone. Without a docker-mods
    repo in the org this keeps nothing."""
    counts = {"branches": 0, "fetched": 0, "unchanged": 0, "invalid": 0, "scheduled": 0}
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    found = False
    while True:
        data = await refs_page(github, org, after)
        repository = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository, dict):
            break
        found = True
        refs = repository.get("refs") or {}
        nodes.extend(n for n in refs.get("nodes") or [] if isinstance(n, dict))
        page = refs.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
    keep: list[str] = []
    for node in nodes:
        branch = str(node.get("name") or "")
        if not branch or branch in SKIP_BRANCHES:
            continue
        row = store_node(node, model, stream, counts)
        if row is not None:
            keep.append(branch)
            counts["scheduled"] += row.scheduled
    if found and model.prune_mods(keep):
        stream.publish("mod_manifests", REPO_NAME)
    log.info("mod discovery: %s", counts)
    return counts
