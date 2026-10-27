"""Mods with an upstream block: discovery from every docker-mods branch, the GHCR tag check and the hourly mod
check of the scheduler."""

import datetime
import json
from typing import Any

import httpx
import pytest
import yaml

from control import batch, mods, scheduler
from control.github import GitHub, GraphQLError
from control.model import ModManifest, Run
from control.state import State

from .conftest import mock_client
from .test_scheduler import NOW, FakeDispatch, FakeResolver, decisions

ORG = "example-org"
MODS_REPO = f"{ORG}/docker-mods"
CLOUDFLARED: dict[str, Any] = {
    "schema": 1,
    "name": "cloudflared",
    "target": "universal",
    "summary": "Adds cloudflared to any container.",
    "maintainers": ["aptalca"],
    "platforms": ["amd64", "arm64"],
    "upstream": {
        "url": "https://github.com/cloudflare/cloudflared",
        "version": {"type": "github_release", "repo": "cloudflare/cloudflared"},
    },
}
TSHOOT: dict[str, Any] = {
    "schema": 1,
    "name": "tshoot",
    "target": "universal",
    "summary": "Adds troubleshooting tools to any container.",
    "maintainers": ["thelamer"],
}


def node(branch: str, data: dict[str, Any] | str | None, oid: str = "c" * 40) -> dict[str, Any]:
    text = data if isinstance(data, str) or data is None else yaml.safe_dump(data)
    return {"name": branch, "target": {"oid": oid, "file": {"object": {"text": text}} if text is not None else None}}


class FakeRefs:
    """GitHub's GraphQL endpoint answering the refs query, a page at a time."""

    def __init__(self, pages: list[list[dict[str, Any]]] | None) -> None:
        self.pages = pages
        self.queries: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        body = json.loads(request.content)
        self.queries.append(body["variables"])
        if self.pages is None:
            return httpx.Response(200, json={"data": {"repository": None}})
        index = int(body["variables"]["after"] or 0)
        more = index + 1 < len(self.pages)
        refs = {"pageInfo": {"hasNextPage": more, "endCursor": str(index + 1)}, "nodes": self.pages[index]}
        # GitHub answers a branch without the file with a NOT_FOUND error beside the data, as for master.
        errors = [
            {"type": "NOT_FOUND", "path": ["repository", "refs", "nodes", i, "target", "file"], "message": "no file"}
            for i, n in enumerate(self.pages[index])
            if n["target"]["file"] is None
        ]
        return httpx.Response(
            200, json={"data": {"repository": {"refs": refs}}, **({"errors": errors} if errors else {})}
        )


async def discover(state: State, pages: list[list[dict[str, Any]]] | None) -> tuple[dict[str, int], FakeRefs]:
    fake = FakeRefs(pages)
    async with mock_client(fake) as http:
        counts = await mods.discover(GitHub(http, "ghs_token"), ORG, state.model, state.stream)
    return counts, fake


def store(state: State, branch: str = "universal-cloudflared", data: dict[str, Any] | None = None) -> ModManifest:
    now = NOW.isoformat(timespec="seconds")
    row = ModManifest(
        branch=branch,
        commit_sha="c" * 40,
        fetched_at=now,
        mod_json=json.dumps(data or CLOUDFLARED),
        created_at=now,
        updated_at=now,
    )
    state.model.upsert_mod(row)
    return row


class Registry:
    """The GHCR tag check: the tags that exist, every lookup recorded."""

    def __init__(self, tags: set[str] | None = None, error: Exception | None = None) -> None:
        self.tags = tags or set()
        self.error = error
        self.lookups: list[str] = []

    async def __call__(self, tag: str) -> bool:
        self.lookups.append(tag)
        if self.error:
            raise self.error
        return tag in self.tags


async def check(
    state: State,
    mod: ModManifest,
    resolver: FakeResolver,
    registry: Registry,
    clock: datetime.datetime = NOW,
    github: FakeDispatch | None = None,
) -> tuple[scheduler.Decision, FakeDispatch]:
    github = github or FakeDispatch()
    async with mock_client(github) as http:
        state.discord.client = http
        sched = scheduler.Scheduler(
            state, GitHub(http, "ghs_token"), resolver=resolver, clock=lambda: clock, mod_tag_exists=registry
        )
        return await sched.check_mod(mod), github


# -- discovery ----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discovery_reads_every_branch_and_keeps_valid_mods(state: State) -> None:
    pages = [
        [node("master", None), node("template", TSHOOT), node("universal-cloudflared", CLOUDFLARED)],
        [node("universal-tshoot", TSHOOT), node("universal-broken", "schema: 1\nname: broken\n"), node("x", None)],
    ]
    counts, fake = await discover(state, pages)
    assert [q["after"] for q in fake.queries] == [None, "1"]
    assert counts == {"branches": 3, "fetched": 2, "unchanged": 0, "invalid": 1, "scheduled": 1}
    rows = state.model.mod_manifests()
    assert [m.branch for m in rows] == ["universal-cloudflared", "universal-tshoot"]
    assert [m.branch for m in state.model.mod_manifests(scheduled=True)] == ["universal-cloudflared"]
    cloudflared = rows[0]
    assert (
        cloudflared.platforms == ["amd64", "arm64"] and cloudflared.version_source["repo"] == "cloudflare/cloudflared"
    )
    assert mods.resolver_manifest(cloudflared) == {
        "upstream": CLOUDFLARED["upstream"],
        "build": {"platforms": ["amd64", "arm64"]},
    }

    # The same heads again only touch the rows; a deleted branch goes.
    counts, _ = await discover(state, [[node("universal-cloudflared", CLOUDFLARED)]])
    assert counts["unchanged"] == 1 and counts["fetched"] == 0 and counts["scheduled"] == 1
    assert [m.branch for m in state.model.mod_manifests()] == ["universal-cloudflared"]
    # An answer without the repository prunes nothing: the rows stay until a real answer says otherwise.
    counts, _ = await discover(state, None)
    assert counts["branches"] == 0 and [m.branch for m in state.model.mod_manifests()] == ["universal-cloudflared"]


def test_only_the_publishing_branch_of_a_live_mod_is_scheduled(state: State) -> None:
    assert store(state).scheduled
    assert not store(state, "universal-cloudflared-wip").scheduled  # a work branch never publishes
    assert not store(state, "universal-tshoot", TSHOOT).scheduled
    deprecated = {**CLOUDFLARED, "deprecated": {"status": True}}
    assert not store(state, data=deprecated).scheduled


# -- the registry -------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tag_exists_asks_ghcr_with_an_anonymous_token() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def ghcr(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.url.path == "/token":
            assert request.url.params["scope"] == "repository:example-org/mods:pull"
            return httpx.Response(200, json={"token": "anon"})
        assert "application/vnd.oci.image.index.v1+json" in request.headers["Accept"]
        tag = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response({"universal-cloudflared-2026.10.5": 200, "boom": 500}.get(tag, 404))

    async with mock_client(ghcr) as http:
        assert await mods.tag_exists(http, "Example-Org", "universal-cloudflared-2026.10.5")
        assert not await mods.tag_exists(http, ORG, "universal-cloudflared-2026.10.6")
        with pytest.raises(httpx.HTTPStatusError):
            await mods.tag_exists(http, ORG, "boom")
    assert seen[1] == ("HEAD", "/v2/example-org/mods/manifests/universal-cloudflared-2026.10.5", "Bearer anon")


# -- the mod check ------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_built_version_is_unchanged(state: State) -> None:
    mod = store(state)
    resolver = FakeResolver("2026.10.5")
    registry = Registry({"universal-cloudflared-2026.10.5"})
    decision, github = await check(state, mod, resolver, registry)
    assert decision.outcome == "unchanged" and github.posts == []
    assert (decision.image, decision.branch, decision.kind) == ("mods", "universal-cloudflared", "mod")
    assert decision.detail["tag"] == "universal-cloudflared-2026.10.5" and decision.detail["released"] == "2026.10.5"
    assert resolver.calls[0] == (mods.resolver_manifest(mod), "ghs_token")


@pytest.mark.anyio
async def test_a_new_version_dispatches_the_mod_build_with_it(state: State) -> None:
    mod = store(state)
    registry = Registry({"universal-cloudflared-2026.10.5"})
    await check(state, mod, FakeResolver("2026.10.5"), registry)
    decision, github = await check(state, mod, FakeResolver("2026.10.6"), registry, NOW + datetime.timedelta(hours=1))
    assert decision.outcome == "dispatch"
    assert github.posts == [
        (
            f"/repos/{MODS_REPO}/actions/workflows/build.yml/dispatches",
            {"ref": "universal-cloudflared", "inputs": {"mod_version": "2026.10.6"}},
        )
    ]
    assert decision.detail["released"] == "2026.10.5" and decision.detail["reason"] == "upstream changed"
    # Within the cooldown nothing goes out again.
    again, github = await check(state, mod, FakeResolver("2026.10.6"), registry, NOW + datetime.timedelta(hours=2))
    assert again.outcome == "skipped" and again.detail["reason"] == "cooldown" and github.posts == []


@pytest.mark.anyio
async def test_a_failed_build_of_the_version_is_retried_daily(state: State) -> None:
    mod = store(state)
    first, _ = await check(state, mod, FakeResolver("2026.10.6"), Registry())
    assert first.outcome == "dispatch" and first.detail["reason"] == "version not built yet"
    at = state.model.decision(first.id or 0)
    assert at is not None
    later = (NOW + datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
    state.model.upsert_run(
        Run(
            id=7,
            repo=MODS_REPO,
            workflow_name="Build",
            event="workflow_dispatch",
            branch="universal-cloudflared",
            head_sha="c" * 40,
            run_number=1,
            status="completed",
            conclusion="failure",
            started_at=later,
            html_url="https://github.com/run/7",
            actor="app",
            display_title="Build",
            attempt=1,
            created_at=later,
            updated_at=later,
        )
    )
    held, github = await check(state, mod, FakeResolver("2026.10.6"), Registry(), NOW + datetime.timedelta(hours=3))
    assert held.outcome == "skipped" and held.detail["run_id"] == 7 and github.posts == []
    # A newer version is tried at once; the same one again after a day.
    newer, github = await check(state, mod, FakeResolver("2026.10.7"), Registry(), NOW + datetime.timedelta(hours=3))
    assert newer.outcome == "dispatch" and github.posts
    retry, github = await check(state, mod, FakeResolver("2026.10.7"), Registry(), NOW + datetime.timedelta(days=2))
    assert retry.outcome == "dispatch"


@pytest.mark.anyio
async def test_skips_unresolvable_sources_and_registry_errors(state: State) -> None:
    combined = {"type": "combined", "sources": [{"type": "command", "run": "echo 1"}, {"type": "pip", "package": "x"}]}
    sneaky = store(state, data={**CLOUDFLARED, "upstream": {"version": combined}})
    decision, _ = await check(state, sneaky, FakeResolver("1"), Registry())
    assert decision.outcome == "skipped" and decision.detail["reason"] == "source not resolvable by control"

    mod = store(state)
    state.model.set_skip("mods/universal-cloudflared/2026.10.6", "broken release", "octocat")
    held, github = await check(state, mod, FakeResolver("2026.10.6"), Registry())
    assert held.outcome == "skipped" and held.detail["scope"] == "mods/universal-cloudflared/2026.10.6"
    assert github.posts == []
    error, _ = await check(state, mod, FakeResolver("2026.10.5"), Registry(error=httpx.ConnectError("down")))
    assert error.outcome == "error" and error.detail["message"].startswith("registry: ConnectError")
    failing, _ = await check(state, mod, FakeResolver(error=RuntimeError("rate limited")), Registry())
    assert failing.outcome == "error" and failing.detail["message"] == "RuntimeError: rate limited"


@pytest.mark.anyio
async def test_tick_checks_scheduled_mods_hourly_and_lists_them_upcoming(
    state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(batch, "PROJECTS", ())
    state.db.set("last_discovery", json.dumps({"started": NOW.isoformat()}))  # discovery not due
    store(state)
    store(state, "universal-tshoot", TSHOOT)
    registry = Registry({"universal-cloudflared-2026.10.5"})
    now = NOW
    async with mock_client(FakeDispatch()) as http:
        sched = scheduler.Scheduler(
            state,
            GitHub(http, "ghs_token"),
            resolver=FakeResolver("2026.10.5"),
            clock=lambda: now,
            mod_tag_exists=registry,
        )
        first = await sched.tick()
        assert [(d.image, d.branch, d.kind, d.outcome) for d in first] == [
            ("mods", "universal-cloudflared", "mod", "unchanged")
        ]
        now = NOW + datetime.timedelta(minutes=1)
        assert await sched.tick() == []
        now = NOW + datetime.timedelta(hours=1, minutes=1)
        assert [d.kind for d in await sched.tick()] == ["mod"]
    rows = scheduler.upcoming([], NOW, mod_manifests=state.model.mod_manifests(scheduled=True), org=ORG)
    assert [(r["image"], r["branch"], r["kind"]) for r in rows] == [("mods", "universal-cloudflared", "mod")]
    assert [d.kind for d in decisions(state)] == ["mod", "mod"]


@pytest.mark.anyio
async def test_other_graphql_errors_still_raise(state: State) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None, "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]})

    async with mock_client(broken) as http:
        with pytest.raises(GraphQLError):
            await mods.discover(GitHub(http, "ghs_token"), ORG, state.model, state.stream)


# -- version lines ------------------------------------------------------------------------------------------------


def go(jq: str, url: str = "https://go.dev/dl/?mode=json") -> dict[str, Any]:
    return {"type": "json", "url": url, "jq": jq, "transform": [{"regex": "^go", "replace": ""}]}


GOLANG: dict[str, Any] = {
    "schema": 1,
    "name": "golang",
    "target": "code-server",
    "summary": "This mod adds golang/go to code-server.",
    "maintainers": ["thelamer"],
    "platforms": ["amd64"],
    "upstream": {
        "url": "https://go.dev/",
        "version": go(".[0].version"),
        "tags": ["major", "minor"],
        "lines": [
            {"name": "previous", "version": go(".[1].version"), "tags": ["minor"]},
            {"name": "unstable", "version": go(".[0].version", "https://go.dev/dl/?mode=json&include=all")},
        ],
    },
}


class LineResolver(FakeResolver):
    """A version per source jq/url: the default line, previous and unstable resolve apart."""

    def __init__(self, versions: dict[str, str]) -> None:
        super().__init__()
        self.versions = versions

    async def __call__(self, manifest: dict[str, Any], token: str | None) -> dict[str, Any]:
        self.calls.append((manifest, token))
        source = manifest["upstream"]["version"]
        key = "unstable" if source["url"].endswith("include=all") else source["jq"]
        upstream = self.versions[key]
        return {"upstream": upstream, "upstream_clean": upstream, "source_url": source["url"]}


VERSIONS = {".[0].version": "1.27.3", ".[1].version": "1.26.10", "unstable": "1.28rc1"}


def golang_run(state: State, run_id: int, at: datetime.datetime, status: str = "completed") -> None:
    stamp = at.isoformat(timespec="seconds")
    state.model.upsert_run(
        Run(
            id=run_id,
            repo=MODS_REPO,
            workflow_name="Build",
            event="workflow_dispatch",
            branch="code-server-golang",
            head_sha="c" * 40,
            run_number=run_id,
            status=status,
            conclusion="success" if status == "completed" else None,
            started_at=stamp,
            html_url=f"https://github.com/run/{run_id}",
            actor="app",
            display_title="Build",
            attempt=1,
            created_at=stamp,
            updated_at=stamp,
        )
    )


def test_lines_are_the_default_line_then_mod_yml_order(state: State) -> None:
    mod = store(state, "code-server-golang", GOLANG)
    assert [line.name for line in mod.lines] == [None, "previous", "unstable"]
    assert mods.resolver_manifest(mod, mod.lines[1]) == {
        "upstream": {"version": GOLANG["upstream"]["lines"][0]["version"]},
        "build": {"platforms": ["amd64"]},
    }
    assert mods.resolver_manifest(mod, mod.lines[0]) == mods.resolver_manifest(mod)
    assert [line.name for line in store(state).lines] == [None]


@pytest.mark.anyio
async def test_each_missing_line_is_dispatched_in_turn_with_its_name(state: State) -> None:
    mod = store(state, "code-server-golang", GOLANG)
    registry = Registry({"code-server-golang-1.27.3"})
    first, github = await check(state, mod, LineResolver(VERSIONS), registry)
    assert first.outcome == "dispatch"
    assert github.posts == [
        (
            f"/repos/{MODS_REPO}/actions/workflows/build.yml/dispatches",
            {"ref": "code-server-golang", "inputs": {"mod_version": "1.26.10", "line": "previous"}},
        )
    ]
    assert first.detail["line"] == "previous" and first.detail["tag"] == "code-server-golang-1.26.10"
    assert first.detail["lines"] == [
        {"line": None, "upstream": "1.27.3", "state": "built"},
        {"line": "previous", "upstream": "1.26.10", "state": "missing"},
        {"line": "unstable", "upstream": "1.28rc1", "state": "missing"},
    ]
    # Its build is running: the whole branch waits, so two builds of the mod never overlap.
    golang_run(state, 11, NOW + datetime.timedelta(minutes=1), status="in_progress")
    held, github = await check(state, mod, LineResolver(VERSIONS), registry, NOW + datetime.timedelta(hours=1))
    assert held.outcome == "skipped" and held.detail["reason"] == "run in progress" and github.posts == []
    # It passed: the next missing line goes out at the next check.
    golang_run(state, 11, NOW + datetime.timedelta(minutes=1))
    registry.tags.add("code-server-golang-1.26.10")
    second, github = await check(state, mod, LineResolver(VERSIONS), registry, NOW + datetime.timedelta(hours=2))
    assert second.outcome == "dispatch" and second.detail["line"] == "unstable"
    assert github.posts[0][1]["inputs"] == {"mod_version": "1.28rc1", "line": "unstable"}
    registry.tags.add("code-server-golang-1.28rc1")
    done, github = await check(state, mod, LineResolver(VERSIONS), registry, NOW + datetime.timedelta(hours=3))
    assert done.outcome == "unchanged" and done.detail["upstream"] == "1.27.3" and github.posts == []
    assert [entry["state"] for entry in done.detail["lines"]] == ["built", "built", "built"]


@pytest.mark.anyio
async def test_the_default_line_goes_first_and_a_shared_version_builds_once(state: State) -> None:
    mod = store(state, "code-server-golang", GOLANG)
    # No release candidate out: the unstable line resolves to the newest stable release, which the default line
    # builds with its floating tags; the unstable line never builds it on its own.
    versions = {**VERSIONS, ".[0].version": "1.28.0", "unstable": "1.28.0"}
    first, github = await check(state, mod, LineResolver(versions), Registry({"code-server-golang-1.26.10"}))
    assert github.posts[0][1]["inputs"] == {"mod_version": "1.28.0", "line": ""}
    assert first.detail["line"] is None and first.detail["reason"] == "version not built yet"
    assert first.detail["lines"][2] == {"line": "unstable", "upstream": "1.28.0", "state": "same"}


@pytest.mark.anyio
async def test_a_line_that_fails_to_resolve_does_not_hold_the_others(state: State) -> None:
    mod = store(state, "code-server-golang", GOLANG)

    class Flaky(LineResolver):
        async def __call__(self, manifest: dict[str, Any], token: str | None) -> dict[str, Any]:
            if manifest["upstream"]["version"]["url"].endswith("include=all"):
                raise RuntimeError("go.dev down")
            return await super().__call__(manifest, token)

    tags = {"code-server-golang-1.27.3", "code-server-golang-1.26.10"}
    error, github = await check(state, mod, Flaky(VERSIONS), Registry(tags))
    assert error.outcome == "error" and error.detail["line"] == "unstable" and github.posts == []
    assert error.detail["message"] == "RuntimeError: go.dev down"
    newer, github = await check(
        state, mod, Flaky({**VERSIONS, ".[1].version": "1.26.11"}), Registry(tags), NOW + datetime.timedelta(hours=1)
    )
    assert newer.outcome == "dispatch" and github.posts[0][1]["inputs"]["line"] == "previous"


# -- push refresh -------------------------------------------------------------------------------------------------


def push(branch: str, files: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    commits = [{"id": "a" * 40, "added": [], "modified": files or [], "removed": []}]
    return {"ref": f"refs/heads/{branch}", "repository": {"full_name": MODS_REPO}, "commits": commits, **extra}


def test_only_pushes_that_touch_mod_yml_refresh(state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    assert mods.touches_mod_yml(push("universal-tshoot", ["mod.yml", "README.md"]))
    assert not mods.touches_mod_yml(push("universal-tshoot", ["root/etc/x"]))
    assert mods.touches_mod_yml(push("universal-new", [], created=True))
    assert mods.touches_mod_yml(push("universal-gone", [], deleted=True))
    assert mods.touches_mod_yml({"commits": [{"modified": []}] * 20})
    spawned: list[Any] = []

    def spawn(coro: Any) -> Any:
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(state, "spawn", spawn)
    mods.schedule_refresh(state, push("universal-tshoot", ["root/etc/x"]))
    mods.schedule_refresh(state, push("master", ["mod.yml"]))
    mods.schedule_refresh(state, {**push("universal-tshoot", ["mod.yml"]), "repository": {"full_name": "x/y"}})
    assert spawned == []
    mods.schedule_refresh(state, push("universal-tshoot", ["mod.yml"]))
    assert len(spawned) == 1


class FakeRef:
    """GitHub's GraphQL endpoint answering the one-ref query."""

    def __init__(self, nodes: dict[str, dict[str, Any] | None]) -> None:
        self.nodes = nodes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        node = self.nodes.get(variables["ref"].removeprefix("refs/heads/"))
        errors = []
        if node is not None and node["target"]["file"] is None:
            errors = [{"type": "NOT_FOUND", "path": ["repository", "ref", "target", "file"], "message": "no file"}]
        body = {"data": {"repository": {"ref": node}}, **({"errors": errors} if errors else {})}
        return httpx.Response(200, json=body)


@pytest.mark.anyio
async def test_refresh_branch_adds_replaces_and_drops_one_mod(state: State) -> None:
    fake = FakeRef(
        {
            "universal-new": node("universal-new", {**TSHOOT, "name": "new"}, "d" * 40),
            "universal-cloudflared": node(
                "universal-cloudflared", {k: v for k, v in CLOUDFLARED.items() if k != "upstream"}, "e" * 40
            ),
            "universal-bare": node("universal-bare", None),
        }
    )
    store(state)
    store(state, "universal-bare", TSHOOT)
    async with mock_client(fake) as http:
        github = GitHub(http, "ghs_token")
        added = await mods.refresh_branch(github, ORG, "universal-new", state.model, state.stream)
        assert added and added.commit_sha == "d" * 40 and state.model.mod_manifest("universal-new")
        changed = await mods.refresh_branch(github, ORG, "universal-cloudflared", state.model, state.stream)
        assert changed and not changed.scheduled  # the upstream block is gone, so is the hourly check
        # mod.yml removed, or the branch gone: the row goes.
        assert await mods.refresh_branch(github, ORG, "universal-bare", state.model, state.stream) is None
        assert await mods.refresh_branch(github, ORG, "universal-missing", state.model, state.stream) is None
    assert [m.branch for m in state.model.mod_manifests()] == ["universal-cloudflared", "universal-new"]


@pytest.mark.anyio
async def test_a_deleted_branch_is_dropped_without_asking_github(state: State) -> None:
    store(state)
    assert await mods.refresh_on_push(state, "universal-cloudflared", deleted=True) is None
    assert state.model.mod_manifests() == []
