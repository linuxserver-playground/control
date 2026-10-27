"""
The state model: typed rows for every table migration 2 created, converters from the GitHub objects that
webhooks and the REST API share (a `workflow_run` is the same JSON in both), and the idempotent upserts
and queries the handlers, backfill and dashboard use. Upserts are keyed by GitHub's ids and guarded so a
late or replayed delivery never moves a row backwards.
"""

import datetime
import json
import sqlite3
from typing import Any

from pydantic import BaseModel, field_validator

from .db import Database, utcnow

STATUS_RANK = "CASE {0} WHEN 'completed' THEN 2 WHEN 'in_progress' THEN 1 ELSE 0 END"


def norm_ts(value: Any) -> str | None:
    """GitHub sends `2026-10-24T12:00:00Z`; we store everything as `+00:00` ISO so strings compare in order."""
    if not value or not isinstance(value, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    return when.astimezone(datetime.UTC).isoformat(timespec="seconds")


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []
    return list(value) if isinstance(value, list) else []


def _login(value: Any) -> str | None:
    return str(value["login"]) if isinstance(value, dict) and value.get("login") else None


def image_name_of(repo_name: str) -> str | None:
    return repo_name[len("docker-") :] if repo_name.startswith("docker-") else None


# -- rows ---------------------------------------------------------------------------------------------------------


class Repo(BaseModel):
    full_name: str
    name: str
    default_branch: str
    archived: bool
    is_image: bool
    image_name: str | None
    last_seen: str
    created_at: str
    updated_at: str

    @classmethod
    def from_github(cls, data: dict[str, Any], seen_at: str | None = None) -> "Repo":
        name = str(data["name"])
        now = seen_at or utcnow()
        return cls(
            full_name=str(data["full_name"]),
            name=name,
            default_branch=str(data.get("default_branch") or "main"),
            archived=bool(data.get("archived", False)),
            is_image=name.startswith("docker-"),
            image_name=image_name_of(name),
            last_seen=now,
            created_at=norm_ts(data.get("created_at")) or now,
            updated_at=norm_ts(data.get("updated_at")) or now,
        )


IGNORED_WORKFLOW_FILES = frozenset({".github/workflows/pr-report.yml"})
"""Workflows of image repositories that are not builds. The trusted PR report runs on the default branch
(workflow_run, pull_request_target), so storing its runs would make them look like that branch's latest build."""
IGNORED_WORKFLOW_NAMES = frozenset({"PR report"})


def is_build_run(data: dict[str, Any]) -> bool:
    """False for a run of an ignored workflow, by file (`path`, which may carry `@ref`) or by name."""
    path = str(data.get("path") or "").split("@", 1)[0]
    return path not in IGNORED_WORKFLOW_FILES and data.get("name") not in IGNORED_WORKFLOW_NAMES


def is_build_job(data: dict[str, Any]) -> bool:
    return data.get("workflow_name") not in IGNORED_WORKFLOW_NAMES


class Run(BaseModel):
    id: int
    repo: str
    workflow_name: str | None
    event: str | None
    branch: str | None
    head_sha: str | None
    run_number: int | None
    status: str | None
    conclusion: str | None
    started_at: str | None
    html_url: str | None
    actor: str | None
    display_title: str | None
    attempt: int
    created_at: str
    updated_at: str

    @classmethod
    def from_github(cls, repo: str, data: dict[str, Any]) -> "Run":
        now = utcnow()
        return cls(
            id=int(data["id"]),
            repo=repo,
            workflow_name=data.get("name"),
            event=data.get("event"),
            branch=data.get("head_branch"),
            head_sha=data.get("head_sha"),
            run_number=data.get("run_number"),
            status=data.get("status"),
            conclusion=data.get("conclusion"),
            started_at=norm_ts(data.get("run_started_at")),
            html_url=data.get("html_url"),
            actor=_login(data.get("actor")) or _login(data.get("triggering_actor")),
            display_title=data.get("display_title"),
            attempt=int(data.get("run_attempt") or 1),
            created_at=norm_ts(data.get("created_at")) or now,
            updated_at=norm_ts(data.get("updated_at")) or now,
        )

    @property
    def active(self) -> bool:
        return self.status != "completed"


class Step(BaseModel):
    name: str
    status: str | None = None
    conclusion: str | None = None
    number: int | None = None
    started_at: str | None = None
    completed_at: str | None = None


class Job(BaseModel):
    id: int
    run_id: int
    repo: str
    name: str
    status: str | None
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    runner_name: str | None
    runner_group: str | None
    labels: list[str]
    steps: list[Step]
    html_url: str | None
    created_at: str
    updated_at: str

    @field_validator("labels", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> list[str]:
        return [str(v) for v in _json_list(value)]

    @field_validator("steps", mode="before")
    @classmethod
    def _steps(cls, value: Any) -> list[Any]:
        return _json_list(value)

    @classmethod
    def from_github(cls, repo: str, data: dict[str, Any]) -> "Job":
        now = utcnow()
        steps = [
            Step(
                name=str(step.get("name", "")),
                status=step.get("status"),
                conclusion=step.get("conclusion"),
                number=step.get("number"),
                started_at=norm_ts(step.get("started_at")),
                completed_at=norm_ts(step.get("completed_at")),
            )
            for step in data.get("steps") or []
            if isinstance(step, dict)
        ]
        return cls(
            id=int(data["id"]),
            run_id=int(data["run_id"]),
            repo=repo,
            name=str(data.get("name") or ""),
            status=data.get("status"),
            conclusion=data.get("conclusion"),
            started_at=norm_ts(data.get("started_at")),
            completed_at=norm_ts(data.get("completed_at")),
            runner_name=data.get("runner_name"),
            runner_group=data.get("runner_group_name"),
            labels=[str(label) for label in data.get("labels") or []],
            steps=steps,
            html_url=data.get("html_url"),
            created_at=norm_ts(data.get("created_at")) or norm_ts(data.get("started_at")) or now,
            updated_at=norm_ts(data.get("completed_at")) or norm_ts(data.get("started_at")) or now,
        )

    def duration_seconds(self, now: datetime.datetime | None = None) -> float | None:
        """Wall time so far (in progress) or total (completed); None before the job started."""
        if not self.started_at:
            return None
        start = datetime.datetime.fromisoformat(self.started_at)
        end = (
            datetime.datetime.fromisoformat(self.completed_at)
            if self.completed_at
            else (now or datetime.datetime.now(datetime.UTC))
        )
        return max(0.0, (end - start).total_seconds())


class PullRequest(BaseModel):
    repo: str
    number: int
    title: str
    author: str | None
    state: str
    draft: bool
    head_sha: str | None
    head_fork: bool
    merged_at: str | None
    review_state: str | None
    html_url: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_github(cls, repo: str, data: dict[str, Any]) -> "PullRequest":
        now = utcnow()
        head = data.get("head") or {}
        head_repo = head.get("repo") if isinstance(head, dict) else None
        return cls(
            repo=repo,
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            author=_login(data.get("user")),
            state=str(data.get("state") or "open"),
            draft=bool(data.get("draft", False)),
            head_sha=head.get("sha") if isinstance(head, dict) else None,
            # A deleted fork leaves head.repo null; treat that as a fork too since it certainly is not us.
            head_fork=not isinstance(head_repo, dict) or head_repo.get("full_name") != repo,
            merged_at=norm_ts(data.get("merged_at")),
            review_state=None,
            html_url=data.get("html_url"),
            created_at=norm_ts(data.get("created_at")) or now,
            updated_at=norm_ts(data.get("updated_at")) or now,
        )


class Issue(BaseModel):
    repo: str
    number: int
    title: str
    author: str | None
    state: str
    labels: list[str]
    html_url: str | None
    created_at: str
    updated_at: str

    @field_validator("labels", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> list[str]:
        return [str(v) for v in _json_list(value)]

    @classmethod
    def from_github(cls, repo: str, data: dict[str, Any]) -> "Issue":
        now = utcnow()
        labels = [
            str(label["name"]) if isinstance(label, dict) else str(label)
            for label in data.get("labels") or []
            if not isinstance(label, dict) or label.get("name")
        ]
        return cls(
            repo=repo,
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            author=_login(data.get("user")),
            state=str(data.get("state") or "open"),
            labels=labels,
            html_url=data.get("html_url"),
            created_at=norm_ts(data.get("created_at")) or now,
            updated_at=norm_ts(data.get("updated_at")) or now,
        )


class Release(BaseModel):
    repo: str
    tag: str
    name: str | None
    prerelease: bool
    published_at: str | None
    html_url: str | None
    body: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_github(cls, repo: str, data: dict[str, Any]) -> "Release":
        now = utcnow()
        return cls(
            repo=repo,
            tag=str(data["tag_name"]),
            name=data.get("name"),
            prerelease=bool(data.get("prerelease", False)),
            published_at=norm_ts(data.get("published_at")),
            html_url=data.get("html_url"),
            body=data.get("body"),
            created_at=norm_ts(data.get("created_at")) or now,
            updated_at=norm_ts(data.get("published_at")) or norm_ts(data.get("created_at")) or now,
        )


class Platform(BaseModel):
    arch: str
    result: str | None = None
    """success | failure | cancelled | skipped; digest and size exist in the bundle only on success."""


class Build(BaseModel):
    repo: str
    run_id: int
    run_url: str | None
    image: str
    branch: str | None
    mode: str | None
    pull_request: int | None
    reason: str | None
    result: str | None
    """None while the build is running (a `build.started` without its summary yet)."""
    failed_job: str | None
    version_tag: str | None
    release_tag: str | None
    upstream: str | None
    upstream_clean: str | None
    semver: str | None
    ls_build: int | None
    test_success: bool | None
    release_url: str | None
    artifact_url: str | None
    index_digest: str | None
    platforms: list[Platform]
    received_at: str
    bundle_json: str | None = None
    discord_message_id: str | None = None
    created_at: str
    updated_at: str
    publish_state: str | None = None
    """For a released live build: `pending` until its site page is live, then `live`; `overdue` past the deadline,
    `expired` when control stopped watching. None for everything that has no site page."""
    publish_started_at: str | None = None
    published_at: str | None = None
    publish_note: str | None = None
    """Where an overdue build is stuck, as control diagnosed it."""

    @field_validator("platforms", mode="before")
    @classmethod
    def _platforms(cls, value: Any) -> list[Any]:
        return [{"arch": v} if isinstance(v, str) else v for v in _json_list(value)]

    @classmethod
    def from_event(cls, event: dict[str, Any], received_at: str | None = None) -> "Build":
        """A builds row from a control-event (either type); the bundle fills in what the envelope lacks."""
        now = received_at or utcnow()
        repo = str(event["repository"])
        bundle = event.get("bundle") if isinstance(event.get("bundle"), dict) else None
        version = bundle.get("version", {}) if bundle else {}
        test = bundle.get("test", {}) if bundle else {}
        image = event.get("image") or (bundle or {}).get("image") or image_name_of(repo.rsplit("/", 1)[-1])
        summary = event.get("type") == "build.summary"
        return cls(
            repo=repo,
            run_id=int(event["run_id"]),
            run_url=event.get("run_url") or (bundle or {}).get("workflow", {}).get("run_url"),
            image=str(image or repo.rsplit("/", 1)[-1]),
            branch=event.get("branch") or (bundle or {}).get("branch"),
            mode=event.get("mode") or (bundle or {}).get("mode"),
            pull_request=event.get("pull_request") or (bundle or {}).get("pull_request"),
            reason=event.get("reason") or (bundle or {}).get("reason"),
            result=event.get("result") if summary else None,
            failed_job=event.get("failed_job"),
            version_tag=version.get("tag"),
            release_tag=version.get("release_tag"),
            upstream=version.get("upstream"),
            upstream_clean=version.get("upstream_clean"),
            semver=version.get("semver"),
            ls_build=version.get("ls_build"),
            test_success=test.get("success") if summary and bundle else None,
            release_url=event.get("release_url"),
            artifact_url=event.get("artifact_url"),
            index_digest=(bundle or {}).get("index_digest"),
            platforms=[
                Platform(arch=str(p.get("arch")), result=p.get("result"))
                for p in (bundle or {}).get("platforms", [])
                if isinstance(p, dict) and p.get("arch")
            ],
            received_at=now,
            bundle_json=json.dumps(bundle, separators=(",", ":")) if bundle else None,
            created_at=norm_ts(event.get("sent")) or now,
            updated_at=norm_ts(event.get("sent")) or now,
        )

    @property
    def running(self) -> bool:
        return self.result is None

    @property
    def archs(self) -> list[str]:
        return [p.arch for p in self.platforms]

    def publishes(self, release_tag: str) -> bool:
        """Does this live build publish `release_tag` (`latest`, `develop`…)? Bundles before the field existed
        are matched by the tag prefix convention (§3)."""
        if self.release_tag is not None:
            return self.release_tag == release_tag
        if release_tag == "latest":
            return True
        return bool(self.version_tag and self.version_tag.startswith(f"{release_tag}-"))

    @property
    def bundle(self) -> dict[str, Any] | None:
        if not self.bundle_json:
            return None
        loaded = json.loads(self.bundle_json)
        return loaded if isinstance(loaded, dict) else None


class SchedulerDecision(BaseModel):
    id: int
    at: str
    image: str
    branch: str
    kind: str = "upstream"
    """upstream | release | packages | manual: which schedule (or which member action) produced the decision."""
    outcome: str
    """unchanged | skipped | dispatch | error."""
    detail: str | None
    created_at: str
    updated_at: str

    @property
    def info(self) -> dict[str, Any]:
        """The detail column parsed when it is JSON (the scheduler writes JSON; older rows were free text)."""
        if not self.detail:
            return {}
        try:
            loaded = json.loads(self.detail)
        except ValueError:
            return {"message": self.detail}
        return loaded if isinstance(loaded, dict) else {"message": self.detail}


class BatchRun(BaseModel):
    """A finished run of a batch build project, as control evaluated it (batch.py)."""

    run_id: int
    attempt: int
    repo: str
    workflow: str | None
    conclusion: str | None
    html_url: str | None
    completed_at: str
    failed: list[str]
    """Every unit (job name) that failed in this run."""
    newly_failing: list[str]
    """The failed units that passed (or were unknown) before: what Discord was told about."""
    recovered: list[str]
    alerted: bool
    """Whether a Discord message went out; runs from before control watched are evaluated silently."""
    evaluated_at: str

    @field_validator("failed", "newly_failing", "recovered", mode="before")
    @classmethod
    def _units(cls, value: Any) -> list[str]:
        return [str(v) for v in _json_list(value)]


class BatchUnit(BaseModel):
    """The last known state of one unit of a batch project: `pass` or `fail`, and since when."""

    repo: str
    workflow: str
    unit: str
    state: str
    since: str
    run_id: int
    run_created_at: str
    html_url: str | None
    updated_at: str


class Manifest(BaseModel):
    """One live branch's image.yml, as fleet discovery last read it."""

    repo: str
    branch: str
    sha: str
    """The blob sha of image.yml on that branch; a refresh that finds the same sha only touches fetched_at."""
    fetched_at: str
    manifest_json: str
    release_tag: str | None
    version_source_json: str | None
    package_check: bool
    created_at: str
    updated_at: str

    @property
    def manifest(self) -> dict[str, Any]:
        loaded = json.loads(self.manifest_json)
        return loaded if isinstance(loaded, dict) else {}

    @property
    def version_source(self) -> dict[str, Any]:
        if not self.version_source_json:
            return {}
        loaded = json.loads(self.version_source_json)
        return loaded if isinstance(loaded, dict) else {}

    @property
    def image(self) -> str:
        name = self.manifest.get("name")
        return str(name) if name else (image_name_of(self.repo.rsplit("/", 1)[-1]) or self.repo)

    @property
    def source_type(self) -> str | None:
        kind = self.version_source.get("type")
        return str(kind) if kind else None


class ModLine(BaseModel):
    """One version line of a mod: the default one (name None, the one `:<branch>` follows) or a named
    `upstream.lines` entry, dispatched with that name as the build's `line` input."""

    name: str | None
    source: dict[str, Any]


class ModManifest(BaseModel):
    """One docker-mods branch's mod.yml, as the last mod discovery read it (mods.py)."""

    branch: str
    commit_sha: str
    """The branch head the file was read at; an unchanged head only touches fetched_at."""
    fetched_at: str
    mod_json: str
    created_at: str
    updated_at: str

    @property
    def mod(self) -> dict[str, Any]:
        loaded = json.loads(self.mod_json)
        return loaded if isinstance(loaded, dict) else {}

    @property
    def upstream(self) -> dict[str, Any]:
        upstream = self.mod.get("upstream")
        return upstream if isinstance(upstream, dict) else {}

    @property
    def version_source(self) -> dict[str, Any]:
        source = self.upstream.get("version")
        return source if isinstance(source, dict) else {}

    @property
    def lines(self) -> list["ModLine"]:
        """The version lines the mod check resolves: the default line (`upstream.version`, name None), then
        `upstream.lines` in mod.yml's order."""
        if not self.version_source:
            return []
        lines = [ModLine(name=None, source=self.version_source)]
        for line in self.upstream.get("lines") or []:
            if isinstance(line, dict) and isinstance(line.get("version"), dict) and line.get("name"):
                lines.append(ModLine(name=str(line["name"]), source=line["version"]))
        return lines

    @property
    def platforms(self) -> list[str]:
        platforms = self.mod.get("platforms")
        return [str(p) for p in platforms] if isinstance(platforms, list) and platforms else ["amd64", "arm64"]

    @property
    def scheduled(self) -> bool:
        """An upstream block on the branch that publishes the mod (`<target>-<name>`), and not deprecated."""
        mod = self.mod
        deprecated = mod.get("deprecated")
        if isinstance(deprecated, dict) and deprecated.get("status"):
            return False
        return bool(self.version_source) and self.branch == f"{mod.get('target')}-{mod.get('name')}"


class Skip(BaseModel):
    """A flag that holds the scheduler back: scope is `image`, `image/branch` or `image/branch/version`."""

    scope: str
    reason: str | None
    set_by: str
    set_at: str
    created_at: str
    updated_at: str

    def matches(self, image: str, branch: str, version: str | None = None) -> bool:
        parts = self.scope.split("/")
        if parts[0] != image:
            return False
        if len(parts) == 1:
            return True
        if parts[1] != branch:
            return False
        return len(parts) == 2 or (version is not None and "/".join(parts[2:]) == version)


class Action(BaseModel):
    """One management action by a signed-in member; shown publicly on /management."""

    id: int
    at: str
    actor: str
    action: str
    target: str | None
    result: str
    detail: str | None
    created_at: str
    updated_at: str


BUILD_COLUMNS = ", ".join(name for name in Build.model_fields if name != "bundle_json")
PUBLICATION_COLUMNS = ("discord_message_id", "publish_state", "publish_started_at", "published_at", "publish_note")
"""Columns control sets itself; a (re)delivered event never overwrites them."""


# -- store --------------------------------------------------------------------------------------------------------


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    values: dict[str, Any],
    keys: tuple[str, ...],
    keep: tuple[str, ...] = (),
    guard: str | None = None,
) -> bool:
    """INSERT ... ON CONFLICT DO UPDATE for every non-key column except created_at; `keep` columns only take a
    new value when the new one is not NULL; `guard` is a WHERE on the update so stale data cannot win."""
    columns = list(values)
    updates = [c for c in columns if c not in keys and c != "created_at"]
    assignments = ", ".join(
        f"{c} = COALESCE(excluded.{c}, {table}.{c})" if c in keep else f"{c} = excluded.{c}" for c in updates
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {assignments}"
    )
    if guard:
        sql += f" WHERE {guard}"
    cursor = conn.execute(sql, tuple(_sql_value(v) for v in values.values()))
    return cursor.rowcount > 0


def _sql_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, list | dict):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return value


def _job_row(row: sqlite3.Row) -> Job:
    data = dict(row)
    data["steps"] = data.pop("steps_json")
    return Job(**data)


class Model:
    """All reads and writes of the state tables. One instance per app, wrapping the Database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- repos ----------------------------------------------------------------------------------------------------

    def upsert_repo(self, repo: Repo) -> None:
        with self.db.connect() as conn:
            _upsert(conn, "repos", repo.model_dump(), ("full_name",))

    def touch_repo(self, data: dict[str, Any]) -> Repo | None:
        """Every webhook carries `repository`; keep the row current from whatever arrives."""
        if not isinstance(data, dict) or not data.get("full_name") or not data.get("name"):
            return None
        repo = Repo.from_github(data)
        self.upsert_repo(repo)
        return repo

    def delete_repo(self, full_name: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM repos WHERE full_name = ?", (full_name,))

    def repos(self, images_only: bool = False) -> list[Repo]:
        sql = "SELECT * FROM repos" + (" WHERE is_image = 1" if images_only else "") + " ORDER BY name"
        with self.db.connect() as conn:
            return [Repo(**dict(row)) for row in conn.execute(sql).fetchall()]

    def repo(self, full_name: str) -> Repo | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        return Repo(**dict(row)) if row else None

    def repo_by_name(self, name: str) -> Repo | None:
        """`docker-sonarr` or `org/docker-sonarr` both find the row; URLs use the short form."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM repos WHERE full_name = ? OR name = ?", (name, name)).fetchone()
        return Repo(**dict(row)) if row else None

    # -- runs -----------------------------------------------------------------------------------------------------

    def upsert_run(self, run: Run) -> bool:
        with self.db.connect() as conn:
            return _upsert(conn, "runs", run.model_dump(), ("id",), guard="excluded.updated_at >= runs.updated_at")

    def run(self, run_id: int) -> Run | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run(**dict(row)) if row else None

    def active_runs(self) -> list[Run]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status IS NULL OR status != 'completed' ORDER BY created_at DESC"
            ).fetchall()
        return [Run(**dict(row)) for row in rows]

    def recent_runs(self, limit: int = 50, repo: str | None = None) -> list[Run]:
        sql = "SELECT * FROM runs WHERE status = 'completed'"
        params: list[Any] = []
        if repo:
            sql += " AND repo = ?"
            params.append(repo)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Run(**dict(row)) for row in rows]

    def active_run_for_branch(self, repo: str, branch: str) -> Run | None:
        """A run of this branch that has not completed: the scheduler will not dispatch on top of it."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE repo = ? AND branch = ? AND (status IS NULL OR status != 'completed') "
                "ORDER BY created_at DESC LIMIT 1",
                (repo, branch),
            ).fetchone()
        return Run(**dict(row)) if row else None

    def latest_run_for_branch(self, repo: str, branch: str) -> Run | None:
        """The newest run of a branch, whatever its state: the release check waits for a young one to settle."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE repo = ? AND branch = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (repo, branch),
            ).fetchone()
        return Run(**dict(row)) if row else None

    def latest_branch_runs(self, repo: str) -> list[Run]:
        """The newest push or dispatch run of every branch of a repo (pull request runs are left out)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs r WHERE repo = ? AND event IN ('push', 'workflow_dispatch') AND id = ("
                "SELECT id FROM runs WHERE repo = r.repo AND branch = r.branch "
                "AND event IN ('push', 'workflow_dispatch') ORDER BY created_at DESC, id DESC LIMIT 1) ORDER BY branch",
                (repo,),
            ).fetchall()
        return [Run(**dict(row)) for row in rows]

    def run_after(
        self,
        repo: str,
        branch: str,
        event: str,
        after: str,
        within_seconds: int = 900,
        exclude_run_id: int | None = None,
    ) -> Run | None:
        """The first run of `event` on a branch created after `after`: how a dispatch decision finds its run,
        and how a package check finds the release build it dispatched (`exclude_run_id` keeps it from
        matching itself)."""
        limit = (datetime.datetime.fromisoformat(after) + datetime.timedelta(seconds=within_seconds)).isoformat(
            timespec="seconds"
        )
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE repo = ? AND branch = ? AND event = ? AND created_at >= ? "
                "AND created_at <= ? AND id IS NOT ? ORDER BY created_at LIMIT 1",
                (repo, branch, event, after, limit, exclude_run_id),
            ).fetchone()
        return Run(**dict(row)) if row else None

    def latest_run_for_sha(self, repo: str, head_sha: str, event: str | None = None) -> Run | None:
        sql = "SELECT * FROM runs WHERE repo = ? AND head_sha = ?"
        params: list[Any] = [repo, head_sha]
        if event:
            sql += " AND event = ?"
            params.append(event)
        sql += " ORDER BY created_at DESC, attempt DESC LIMIT 1"
        with self.db.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return Run(**dict(row)) if row else None

    # -- jobs -----------------------------------------------------------------------------------------------------

    def upsert_job(self, job: Job) -> bool:
        guard = f"{STATUS_RANK.format('excluded.status')} >= {STATUS_RANK.format('jobs.status')}"
        values = job.model_dump()
        values["steps_json"] = values.pop("steps")
        with self.db.connect() as conn:
            return _upsert(conn, "jobs", values, ("id",), guard=guard)

    def job(self, job_id: int) -> Job | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    def jobs_for_run(self, run_id: int) -> list[Job]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? ORDER BY COALESCE(started_at, created_at), id", (run_id,)
            ).fetchall()
        return [_job_row(row) for row in rows]

    def runs_with_unfinished_jobs(self) -> list[tuple[str, int]]:
        """(repo, run id) of completed runs that still hold a job not marked completed: a lost job webhook."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT jobs.repo AS repo, jobs.run_id AS run_id FROM jobs JOIN runs ON runs.id = jobs.run_id "
                "WHERE runs.status = 'completed' AND (jobs.status IS NULL OR jobs.status != 'completed') "
                "ORDER BY jobs.run_id"
            ).fetchall()
        return [(str(row["repo"]), int(row["run_id"])) for row in rows]

    def jobs_with_status(self, status: str) -> list[Job]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY COALESCE(started_at, created_at)", (status,)
            ).fetchall()
        return [_job_row(row) for row in rows]

    def count_jobs(self, status: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = ?", (status,)).fetchone()
        return int(row["n"])

    def job_durations(self, repo: str, name: str, limit: int = 200) -> list[float]:
        """Wall seconds of the most recent completed jobs with this name in this repo, for stuck detection."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT started_at, completed_at FROM jobs WHERE repo = ? AND name = ? AND status = 'completed' "
                "AND started_at IS NOT NULL AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT ?",
                (repo, name, limit),
            ).fetchall()
        durations: list[float] = []
        for row in rows:
            start = datetime.datetime.fromisoformat(row["started_at"])
            end = datetime.datetime.fromisoformat(row["completed_at"])
            durations.append(max(0.0, (end - start).total_seconds()))
        return durations

    def open_jobs(self, in_progress_since: str, queued_since: str) -> list[Job]:
        """Jobs holding or waiting for a runner right now: in progress (started after `in_progress_since`) or
        queued (created after `queued_since`), in runs that have not completed. The age limits and the run check
        drop jobs whose completion webhook was lost, so they never count against the org's slots for ever."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT jobs.* FROM jobs LEFT JOIN runs ON runs.id = jobs.run_id "
                "WHERE (runs.status IS NULL OR runs.status != 'completed') AND ("
                "(jobs.status = 'in_progress' AND COALESCE(jobs.started_at, jobs.created_at) >= ?) "
                "OR (jobs.status = 'queued' AND jobs.created_at >= ?))",
                (in_progress_since, queued_since),
            ).fetchall()
        return [_job_row(row) for row in rows]

    # -- batch projects -------------------------------------------------------------------------------------------

    def completed_runs(self, repo: str, workflows: tuple[str, ...], since: str) -> list[Run]:
        """Finished non-PR runs of these workflows that completed after `since`, oldest first."""
        marks = ", ".join("?" for _ in workflows)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE repo = ? AND status = 'completed' AND workflow_name IN ({marks}) "
                "AND (event IS NULL OR event != 'pull_request') AND updated_at >= ? ORDER BY updated_at, id",
                (repo, *workflows, since),
            ).fetchall()
        return [Run(**dict(row)) for row in rows]

    def run_durations(self, repo: str, workflow: str, limit: int = 20) -> list[float]:
        """Wall seconds of the newest successful runs of a workflow, for the overdue check."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(started_at, created_at) AS start, updated_at FROM runs WHERE repo = ? "
                "AND workflow_name = ? AND status = 'completed' AND conclusion = 'success' "
                "ORDER BY updated_at DESC LIMIT ?",
                (repo, workflow, limit),
            ).fetchall()
        return [
            max(
                0.0,
                (
                    datetime.datetime.fromisoformat(r["updated_at"]) - datetime.datetime.fromisoformat(r["start"])
                ).total_seconds(),
            )
            for r in rows
        ]

    def batch_run_seen(self, run_id: int, attempt: int) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM batch_runs WHERE run_id = ? AND attempt = ?", (run_id, attempt)
            ).fetchone()
        return row is not None

    def add_batch_run(self, run: BatchRun) -> None:
        with self.db.connect() as conn:
            _upsert(conn, "batch_runs", run.model_dump(), ("run_id", "attempt"))

    def batch_runs(self, repo: str, limit: int = 10) -> list[BatchRun]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_runs WHERE repo = ? ORDER BY completed_at DESC LIMIT ?", (repo, limit)
            ).fetchall()
        return [BatchRun(**dict(row)) for row in rows]

    def batch_unit(self, repo: str, workflow: str, unit: str) -> BatchUnit | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batch_units WHERE repo = ? AND workflow = ? AND unit = ?", (repo, workflow, unit)
            ).fetchone()
        return BatchUnit(**dict(row)) if row else None

    def upsert_batch_unit(self, unit: BatchUnit) -> None:
        with self.db.connect() as conn:
            _upsert(conn, "batch_units", unit.model_dump(), ("repo", "workflow", "unit"))

    def batch_units(self, repo: str, state: str | None = None) -> list[BatchUnit]:
        sql = "SELECT * FROM batch_units WHERE repo = ?"
        params: list[Any] = [repo]
        if state:
            sql += " AND state = ?"
            params.append(state)
        with self.db.connect() as conn:
            rows = conn.execute(sql + " ORDER BY workflow, unit", params).fetchall()
        return [BatchUnit(**dict(row)) for row in rows]

    # -- pull requests --------------------------------------------------------------------------------------------

    def upsert_pull_request(self, pr: PullRequest) -> bool:
        with self.db.connect() as conn:
            return _upsert(
                conn,
                "pull_requests",
                pr.model_dump(),
                ("repo", "number"),
                keep=("review_state",),
                guard="excluded.updated_at >= pull_requests.updated_at",
            )

    def set_review_state(self, repo: str, number: int, review_state: str, at: str | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE pull_requests SET review_state = ?, updated_at = MAX(updated_at, ?) "
                "WHERE repo = ? AND number = ?",
                (review_state, norm_ts(at) or utcnow(), repo, number),
            )

    def pull_request(self, repo: str, number: int) -> PullRequest | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM pull_requests WHERE repo = ? AND number = ?", (repo, number)).fetchone()
        return PullRequest(**dict(row)) if row else None

    def open_pull_requests(self) -> list[PullRequest]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM pull_requests WHERE state = 'open' ORDER BY created_at").fetchall()
        return [PullRequest(**dict(row)) for row in rows]

    # -- issues ---------------------------------------------------------------------------------------------------

    def upsert_issue(self, issue: Issue) -> bool:
        with self.db.connect() as conn:
            return _upsert(
                conn, "issues", issue.model_dump(), ("repo", "number"), guard="excluded.updated_at >= issues.updated_at"
            )

    def issue(self, repo: str, number: int) -> Issue | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE repo = ? AND number = ?", (repo, number)).fetchone()
        return Issue(**dict(row)) if row else None

    def open_issues(self) -> list[Issue]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM issues WHERE state = 'open' ORDER BY created_at").fetchall()
        return [Issue(**dict(row)) for row in rows]

    # -- releases -------------------------------------------------------------------------------------------------

    def upsert_release(self, release: Release) -> bool:
        with self.db.connect() as conn:
            return _upsert(conn, "releases", release.model_dump(), ("repo", "tag"))

    def delete_release(self, repo: str, tag: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM releases WHERE repo = ? AND tag = ?", (repo, tag))

    def release(self, repo: str, tag: str) -> Release | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM releases WHERE repo = ? AND tag = ?", (repo, tag)).fetchone()
        return Release(**dict(row)) if row else None

    def recent_releases(self, limit: int = 100) -> list[Release]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM releases ORDER BY COALESCE(published_at, created_at) DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Release(**dict(row)) for row in rows]

    def releases_for_repo(self, repo: str, limit: int = 50) -> list[Release]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM releases WHERE repo = ? ORDER BY COALESCE(published_at, created_at) DESC LIMIT ?",
                (repo, limit),
            ).fetchall()
        return [Release(**dict(row)) for row in rows]

    # -- builds ---------------------------------------------------------------------------------------------------

    def upsert_build(self, build: Build) -> bool:
        """A summary always wins; a `build.started` only creates or refreshes a row that has no result yet."""
        guard = None if build.result is not None else "builds.result IS NULL"
        with self.db.connect() as conn:
            return _upsert(
                conn, "builds", build.model_dump(), ("repo", "run_id"), keep=PUBLICATION_COLUMNS, guard=guard
            )

    def start_publication(self, repo: str, run_id: int, now: str) -> None:
        """Hold a released build at `pending` until its site page is live; a replayed summary changes nothing."""
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE builds SET publish_state = 'pending', publish_started_at = ? "
                "WHERE repo = ? AND run_id = ? AND publish_state IS NULL",
                (now, repo, run_id),
            )

    def set_publication(
        self, repo: str, run_id: int, state: str, note: str | None = None, published_at: str | None = None
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE builds SET publish_state = ?, publish_note = ?, published_at = COALESCE(?, published_at) "
                "WHERE repo = ? AND run_id = ?",
                (state, note, published_at, repo, run_id),
            )

    def publications(self) -> list[Build]:
        """Released builds whose site page control is still waiting for, oldest first."""
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {BUILD_COLUMNS} FROM builds WHERE publish_state IN ('pending', 'overdue') "
                "ORDER BY publish_started_at"
            ).fetchall()
        return [Build(**dict(row)) for row in rows]

    def set_build_message(self, repo: str, run_id: int, message_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE builds SET discord_message_id = ? WHERE repo = ? AND run_id = ?", (message_id, repo, run_id)
            )

    def build(self, repo: str, run_id: int) -> Build | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM builds WHERE repo = ? AND run_id = ?", (repo, run_id)).fetchone()
        return Build(**dict(row)) if row else None

    def builds_for_run(self, run_id: int) -> list[Build]:
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT {BUILD_COLUMNS} FROM builds WHERE run_id = ?", (run_id,)).fetchall()
        return [Build(**dict(row)) for row in rows]

    def builds_for_image(self, image: str, limit: int = 100) -> list[Build]:
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {BUILD_COLUMNS} FROM builds WHERE image = ? ORDER BY received_at DESC, rowid DESC LIMIT ?",
                (image, limit),
            ).fetchall()
        return [Build(**dict(row)) for row in rows]

    def recent_builds(self, limit: int = 100) -> list[Build]:
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {BUILD_COLUMNS} FROM builds ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Build(**dict(row)) for row in rows]

    def build_for_tag(self, repo: str, tag: str, with_bundle: bool = False) -> Build | None:
        """The live build that produced a release tag (the releases feed shows its reason and upstream; a
        summary's `previous.tag` names it when its packages are compared, bundle included)."""
        columns = "*" if with_bundle else BUILD_COLUMNS
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT {columns} FROM builds WHERE repo = ? AND version_tag = ? AND mode = 'live' "
                "ORDER BY received_at DESC, rowid DESC LIMIT 1",
                (repo, tag),
            ).fetchone()
        return Build(**dict(row)) if row else None

    def latest_builds_per_branch(self, repo: str, mode: str = "live") -> list[Build]:
        """The newest build of each branch of a repo in one mode: the fleet board's live branches."""
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {BUILD_COLUMNS} FROM builds WHERE repo = ? AND mode = ? AND rowid IN ("
                "  SELECT rowid FROM builds b2 WHERE b2.repo = builds.repo AND b2.mode = builds.mode "
                "  AND b2.branch IS builds.branch ORDER BY received_at DESC, rowid DESC LIMIT 1) ORDER BY branch",
                (repo, mode),
            ).fetchall()
        return [Build(**dict(row)) for row in rows]

    def latest_live_build(self, image: str, release_tag: str) -> Build | None:
        """The newest finished live build that publishes `release_tag` (`latest` for the default branch).
        Builds before the bundle carried `release_tag` are matched by the tag prefix convention (§3)."""
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT {BUILD_COLUMNS} FROM builds WHERE image = ? AND mode = 'live' AND result IS NOT NULL AND ("
                "  release_tag = ? OR (release_tag IS NULL AND (? = 'latest' OR version_tag LIKE ? || '-%')))"
                " ORDER BY received_at DESC, rowid DESC LIMIT 1",
                (image, release_tag, release_tag, release_tag),
            ).fetchone()
        return Build(**dict(row)) if row else None

    def latest_build_for_branch(self, repo: str, branch: str) -> Build | None:
        """The newest finished live build of a branch: the scheduler's hint for the released upstream version."""
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT {BUILD_COLUMNS} FROM builds WHERE repo = ? AND branch = ? AND mode = 'live' "
                "AND result IS NOT NULL ORDER BY received_at DESC, rowid DESC LIMIT 1",
                (repo, branch),
            ).fetchone()
        return Build(**dict(row)) if row else None

    # -- manifests ------------------------------------------------------------------------------------------------

    def upsert_manifest(self, manifest: Manifest) -> bool:
        with self.db.connect() as conn:
            return _upsert(conn, "manifests", manifest.model_dump(), ("repo", "branch"))

    def touch_manifest(self, repo: str, branch: str, fetched_at: str | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manifests SET fetched_at = ? WHERE repo = ? AND branch = ?",
                (fetched_at or utcnow(), repo, branch),
            )

    def manifest(self, repo: str, branch: str) -> Manifest | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM manifests WHERE repo = ? AND branch = ?", (repo, branch)).fetchone()
        return Manifest(**dict(row)) if row else None

    def manifests(self, repo: str | None = None) -> list[Manifest]:
        sql = "SELECT * FROM manifests"
        params: list[Any] = []
        if repo:
            sql += " WHERE repo = ?"
            params.append(repo)
        sql += " ORDER BY repo, branch"
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Manifest(**dict(row)) for row in rows]

    def prune_manifests(self, repo: str, keep: list[str]) -> int:
        """Drop the branches of a repo that discovery no longer found live (branch deleted or tag dropped)."""
        with self.db.connect() as conn:
            if keep:
                marks = ", ".join("?" * len(keep))
                cursor = conn.execute(
                    f"DELETE FROM manifests WHERE repo = ? AND branch NOT IN ({marks})", (repo, *keep)
                )
            else:
                cursor = conn.execute("DELETE FROM manifests WHERE repo = ?", (repo,))
        return int(cursor.rowcount)

    def delete_manifests(self, repo: str) -> None:
        self.prune_manifests(repo, [])

    # -- mods -----------------------------------------------------------------------------------------------------

    def upsert_mod(self, mod: ModManifest) -> bool:
        with self.db.connect() as conn:
            return _upsert(conn, "mod_manifests", mod.model_dump(), ("branch",))

    def touch_mod(self, branch: str, fetched_at: str | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE mod_manifests SET fetched_at = ? WHERE branch = ?", (fetched_at or utcnow(), branch))

    def mod_manifest(self, branch: str) -> ModManifest | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM mod_manifests WHERE branch = ?", (branch,)).fetchone()
        return ModManifest(**dict(row)) if row else None

    def mod_manifests(self, scheduled: bool = False) -> list[ModManifest]:
        """Every mod branch, or only those the scheduler checks."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM mod_manifests ORDER BY branch").fetchall()
        mods = [ModManifest(**dict(row)) for row in rows]
        return [m for m in mods if m.scheduled] if scheduled else mods

    def delete_mod(self, branch: str) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute("DELETE FROM mod_manifests WHERE branch = ?", (branch,))
        return cursor.rowcount > 0

    def prune_mods(self, keep: list[str]) -> int:
        """Drop the branches the last discovery no longer found (deleted, or mod.yml gone or invalid)."""
        with self.db.connect() as conn:
            if keep:
                marks = ", ".join("?" * len(keep))
                cursor = conn.execute(f"DELETE FROM mod_manifests WHERE branch NOT IN ({marks})", tuple(keep))
            else:
                cursor = conn.execute("DELETE FROM mod_manifests")
        return int(cursor.rowcount)

    # -- skips ----------------------------------------------------------------------------------------------------

    def set_skip(self, scope: str, reason: str | None, set_by: str) -> None:
        now = utcnow()
        skip = Skip(scope=scope, reason=reason, set_by=set_by, set_at=now, created_at=now, updated_at=now)
        with self.db.connect() as conn:
            _upsert(conn, "skips", skip.model_dump(), ("scope",))

    def remove_skip(self, scope: str) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute("DELETE FROM skips WHERE scope = ?", (scope,))
        return cursor.rowcount > 0

    def skips(self) -> list[Skip]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM skips ORDER BY scope").fetchall()
        return [Skip(**dict(row)) for row in rows]

    def skips_for(self, image: str, branch: str, version: str | None = None) -> list[Skip]:
        """Every flag that holds `image/branch` back; version-scoped flags only count once `version` is known."""
        return [skip for skip in self.skips() if skip.matches(image, branch, version)]

    # -- scheduler ------------------------------------------------------------------------------------------------

    def add_decision(
        self,
        image: str,
        branch: str,
        outcome: str,
        detail: str | None = None,
        kind: str = "upstream",
        at: str | None = None,
    ) -> int:
        now = at or utcnow()
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO scheduler_decisions (at, image, branch, kind, outcome, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, image, branch, kind, outcome, detail, now, now),
            )
        return int(cursor.lastrowid or 0)

    def decision(self, decision_id: int) -> SchedulerDecision | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM scheduler_decisions WHERE id = ?", (decision_id,)).fetchone()
        return SchedulerDecision(**dict(row)) if row else None

    def recent_decisions(
        self, limit: int = 100, image: str | None = None, outcome: str | None = None, kind: str | None = None
    ) -> list[SchedulerDecision]:
        sql = "SELECT * FROM scheduler_decisions"
        clauses: list[str] = []
        params: list[Any] = []
        if image:
            clauses.append("image = ?")
            params.append(image)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SchedulerDecision(**dict(row)) for row in rows]

    def last_decision(
        self, image: str, branch: str, kind: str | None = None, outcome: str | None = None
    ) -> SchedulerDecision | None:
        sql = "SELECT * FROM scheduler_decisions WHERE image = ? AND branch = ?"
        params: list[Any] = [image, branch]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        sql += " ORDER BY id DESC LIMIT 1"
        with self.db.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return SchedulerDecision(**dict(row)) if row else None

    def branch_decisions(
        self, image: str, branch: str, outcome: str | None = None, limit: int = 50
    ) -> list[SchedulerDecision]:
        """The newest decisions of one branch, newest first."""
        sql = "SELECT * FROM scheduler_decisions WHERE image = ? AND branch = ?"
        params: list[Any] = [image, branch]
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SchedulerDecision(**dict(row)) for row in rows]

    def consecutive_errors(self, image: str, branch: str, kind: str = "upstream") -> int:
        """How many of the newest decisions of a branch are errors, counting back to the last non-error."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT outcome FROM scheduler_decisions WHERE image = ? AND branch = ? AND kind = ? "
                "ORDER BY id DESC LIMIT 50",
                (image, branch, kind),
            ).fetchall()
        count = 0
        for row in rows:
            if row["outcome"] != "error":
                break
            count += 1
        return count

    def decision_images(self) -> list[str]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT DISTINCT image FROM scheduler_decisions ORDER BY image").fetchall()
        return [str(row["image"]) for row in rows]

    # -- actions --------------------------------------------------------------------------------------------------

    def add_action(self, actor: str, action: str, target: str | None, result: str, detail: str | None = None) -> int:
        now = utcnow()
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO actions (at, actor, action, target, result, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, actor, action, target, result, detail, now, now),
            )
        return int(cursor.lastrowid or 0)

    def recent_actions(self, limit: int = 100) -> list[Action]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Action(**dict(row)) for row in rows]

    def count_actions_since(self, actor: str, since: str, editing: bool = False) -> int:
        """Member actions, or with `editing` the website editor's (`edit-*`): each has its own rate limit."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM actions WHERE actor = ? AND at >= ? AND (action LIKE 'edit-%') = ?",
                (actor, since, editing),
            ).fetchone()
        return int(row["n"])

    # -- status ---------------------------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        tables = (
            "repos",
            "runs",
            "jobs",
            "pull_requests",
            "issues",
            "releases",
            "builds",
            "manifests",
            "mod_manifests",
            "skips",
            "actions",
            "scheduler_decisions",
        )
        with self.db.connect() as conn:
            return {t: int(conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]) for t in tables}


# -- stuck detection ----------------------------------------------------------------------------------------------

DEFAULT_STUCK_SECONDS = 60 * 60
MIN_SAMPLES = 10


def p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * 95 // 100))  # ceil(n * 0.95)
    return ordered[rank - 1]


def stuck_threshold(samples: list[float]) -> float:
    """Seconds after which an in-progress job counts as stuck: the p95 of its history, or an hour."""
    if len(samples) < MIN_SAMPLES:
        return DEFAULT_STUCK_SECONDS
    return p95(samples)


def is_stuck(job: Job, samples: list[float], now: datetime.datetime | None = None) -> bool:
    if job.status != "in_progress":
        return False
    elapsed = job.duration_seconds(now)
    return elapsed is not None and elapsed > stuck_threshold(samples)
