"""
Docker mods the scheduler checks: the docker-mods branches whose mod.yml has an `upstream` block (CONVENTIONS §3,
"Mods"). They replace the External-Triggers jobs of the Jenkins era, which looked up a tool's version every hour and
started the mod's build when Docker Hub had no `<branch>-<version>` tag yet.

Discovery reads every branch's mod.yml in one GraphQL query (a branch head that has not moved keeps its row) into
`mod_manifests`, at the same 30-minute cadence as the image fleet. The scheduler resolves a scheduled mod's
`upstream.version` with the vendored resolver, as the build does, and asks GHCR whether
`mods:<branch>-<version>` exists: that tag is the record of what was built. Decisions are recorded as image
`mods`, branch `<target>-<name>`, so the scheduler page, skip flags (`mods/<branch>[/<version>]`) and the run
links work as they do for images.
"""

import json
import logging
from typing import Any

import httpx
import yaml
from contracts import ContractError, validate

from .db import utcnow
from .fleet import parse_manifest
from .github import GitHub
from .model import Model, ModManifest
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


def resolver_manifest(mod: ModManifest) -> dict[str, Any]:
    """mod.yml in the shape the resolver reads from image.yml: `upstream.version` and `build.platforms`. The
    build's plan job hands it the same shape (workflows mod-build.yml)."""
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


async def discover(github: GitHub, org: str, model: Model, stream: Broadcaster) -> dict[str, int]:
    """Store every mod branch's valid mod.yml and drop the rows of branches that are gone. Without a docker-mods
    repo in the org this keeps nothing."""
    counts = {"branches": 0, "fetched": 0, "unchanged": 0, "invalid": 0, "scheduled": 0}
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    found = False
    while True:
        data = await github.graphql(REFS, {"owner": org, "name": REPO_NAME, "after": after})
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
        target = node.get("target") or {}
        blob = ((target.get("file") or {}).get("object") or {}) if isinstance(target, dict) else {}
        text = blob.get("text") if isinstance(blob, dict) else None
        if not branch or branch in SKIP_BRANCHES or not isinstance(text, str):
            continue
        counts["branches"] += 1
        commit = str(target.get("oid") or "")
        existing = model.mod_manifest(branch)
        if existing and existing.commit_sha == commit:
            model.touch_mod(branch)
            counts["unchanged"] += 1
            keep.append(branch)
            counts["scheduled"] += existing.scheduled
            continue
        try:
            mod = parse_manifest(text)
            validate("mod", mod)
        except (ValueError, yaml.YAMLError, ContractError) as exc:
            counts["invalid"] += 1
            log.warning("%s@%s mod.yml is not valid: %s", REPO_NAME, branch, exc)
            continue
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
        counts["fetched"] += 1
        counts["scheduled"] += row.scheduled
        keep.append(branch)
    if found and model.prune_mods(keep):
        stream.publish("mod_manifests", REPO_NAME)
    log.info("mod discovery: %s", counts)
    return counts
