import datetime
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from control import handlers, views
from control.model import DEFAULT_STUCK_SECONDS, Job, is_stuck, p95, stuck_threshold
from control.state import State
from control.stream import Broadcaster, sse

from .conftest import ACTIVE_SEED, FINISHED_SEED, REPO, fixture, seed

PAGES: dict[str, type[BaseModel]] = {
    "/": views.FleetPage,
    "/runs": views.RunsPage,
    "/runs/5001": views.RunPage,
    "/queue": views.QueuePage,
    "/pulls": views.PullsPage,
    "/issues": views.IssuesPage,
    "/releases": views.ReleasesPage,
    "/builds/example-org/docker-sonarr/5001": views.BuildPage,
    "/images/sonarr": views.ImagePage,
    "/images/sonarr/latest": views.ImagePage,
    "/scheduler": views.SchedulerPage,
    "/status": views.StatusPage,
    "/management": views.ManagementPage,
}


def api_path(path: str) -> str:
    return "/api/fleet.json" if path == "/" else f"/api{path}.json"


@pytest.mark.parametrize("path", sorted(PAGES))
def test_every_page_renders_and_validates_as_json(client: TestClient, state: State, path: str) -> None:
    seed(state, *FINISHED_SEED)
    state.model.add_decision("sonarr", "main", "skip", "built 2h ago")
    page = client.get(path)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '<main id="main">' in page.text
    data = client.get(api_path(path))
    assert data.status_code == 200
    PAGES[path].model_validate(data.json())


def test_pages_render_empty(client: TestClient) -> None:
    for path in ("/", "/runs", "/queue", "/pulls", "/issues", "/releases", "/scheduler", "/status", "/management"):
        assert client.get(path).status_code == 200, path
    assert client.get("/runs/1").status_code == 404
    assert client.get("/builds/example-org/docker-sonarr/1").status_code == 404
    assert client.get("/images/nothing").status_code == 404
    assert client.get("/api/images/nothing/latest.json").status_code == 404
    assert client.get("/api/runs/1.json").status_code == 404


def test_fleet_board_rows(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    rows = views.FleetPage.model_validate(client.get("/api/fleet.json").json()).rows
    assert [(r.image, r.branch, r.default_branch) for r in rows] == [("radarr", "main", True), ("sonarr", "main", True)]
    radarr, sonarr = rows
    assert radarr.result is None and radarr.released is None
    assert sonarr.result == "success" and sonarr.version_tag == "4.0.20.3014-ls1" and sonarr.run_id == 5001
    # Without a scheduler decision the build's own upstream stands in, and it matches the release: no drift.
    assert sonarr.released == "4.0.20.3014-ls1" and sonarr.upstream == "4.0.20.3014" and sonarr.drift == "same"
    assert sonarr.next_check is None and sonarr.skip == []  # discovery has not found this branch live yet
    page = client.get("/")
    assert "4.0.20.3014-ls1" in page.text and 'href="/builds/example-org/docker-sonarr/5001"' in page.text
    assert 'href="/images/sonarr"' in page.text and 'href="/images/sonarr/latest"' in page.text
    assert "/api/stream" in page.text  # the live refresh script


def test_fleet_board_extra_live_branch_and_prefixed_release(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    summary = fixture("control.build.summary")
    summary.update(run_id=5002, branch="develop", result="failure")
    summary["bundle"].update(branch="develop")
    summary["bundle"]["version"].update(tag="develop-5.0.1.1234-ls7", release_tag="develop", upstream="5.0.1.1234")
    handlers.process(state.model, state.stream, "control:build.summary", summary)
    release = fixture("release.published")
    release["release"].update(tag_name="develop-5.0.1.1234-ls7", published_at="2026-10-24T13:00:00Z")
    handlers.process(state.model, state.stream, "release", release)

    rows = {(r.image, r.branch): r for r in views.fleet_page(state).rows}
    assert rows[("sonarr", "develop")].result == "failure"
    assert rows[("sonarr", "develop")].released == "develop-5.0.1.1234-ls7"
    assert rows[("sonarr", "main")].released == "4.0.20.3014-ls1"  # the develop release is not the default's


def test_runs_page_shows_live_run_then_recent(client: TestClient, state: State) -> None:
    seed(state, *ACTIVE_SEED)
    page = views.RunsPage.model_validate(client.get("/api/runs.json").json())
    assert [v.run.id for v in page.active] == [5001] and page.recent == []
    (view,) = page.active
    assert [j.job.status for j in view.jobs] == ["in_progress"] and view.build and view.build.running
    assert view.elapsed_seconds and view.elapsed_seconds > 0
    html = client.get("/runs").text
    assert "build (amd64)" in html and "in_progress" in html

    seed(state, "workflow_job.completed", "workflow_run.completed")
    page = views.RunsPage.model_validate(client.get("/api/runs.json").json())
    assert page.active == [] and [r.id for r in page.recent] == [5001]


def test_run_page_has_jobs_and_steps(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    page = views.RunPage.model_validate(client.get("/api/runs/5001.json").json())
    assert page.run.conclusion == "success" and page.elapsed_seconds == 540.0  # run start to last job end
    assert [s.name for s in page.jobs[0].job.steps] == ["Set up job", "Build"]
    assert [b.version_tag for b in page.builds] == ["4.0.20.3014-ls1"]
    html = client.get("/runs/5001").text
    assert "Set up job" in html and "8m 48s" in html  # the Build step's duration


def test_queue_page_counts_capacity(client: TestClient, state: State) -> None:
    seed(state, *ACTIVE_SEED)
    queued = fixture("workflow_job.queued")
    queued["workflow_job"].update(id=9002, name="build (arm64)")
    handlers.process(state.model, state.stream, "workflow_job", queued)
    page = views.QueuePage.model_validate(client.get("/api/queue.json").json())
    assert page.limit == 20 and page.in_use == 1 and page.utilisation == 0.05
    assert [j.job.name for j in page.queued] == ["build (arm64)"]
    assert [j.job.name for j in page.in_progress] == ["build (amd64)"]
    # Each job names the branch and event of its run.
    assert [(j.branch, j.event) for j in page.queued + page.in_progress] == [("main", "push"), ("main", "push")]
    html = client.get("/queue").text
    assert '1 <span class="muted">/ 20</span>' in html and "<code>main</code>" in html


def test_pulls_page_shows_build_and_quarantine_tag(client: TestClient, state: State) -> None:
    seed(state, "pull_request.opened")
    run = fixture("workflow_run.completed")
    run["workflow_run"].update(
        id=5005, event="pull_request", head_sha="b" * 40, head_branch="bump", conclusion="failure"
    )
    handlers.process(state.model, state.stream, "workflow_run", run)
    page = views.PullsPage.model_validate(client.get("/api/pulls.json").json())
    (view,) = page.pulls
    assert view.run and view.run.id == 5005 and view.run.conclusion == "failure"
    assert view.quarantine_tag == "ghcr.io/example-org/untrusted-pr/sonarr:pr-7-bbbbbbb"
    assert view.pull.head_fork and view.pull.review_state is None
    html = client.get("/pulls").text
    assert "untrusted-pr/sonarr:pr-7-bbbbbbb" in html and ">fork<" in html


def test_issues_and_releases_pages(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    issues = views.IssuesPage.model_validate(client.get("/api/issues.json").json())
    assert [i.labels for i in issues.issues] == [["bug", "needs-triage"]]
    assert "needs-triage" in client.get("/issues").text
    releases = views.ReleasesPage.model_validate(client.get("/api/releases.json").json())
    (view,) = releases.releases
    assert view.release.tag == "4.0.20.3014-ls1" and view.build and view.build.reason == "upstream"
    assert "upstream" in client.get("/releases").text


def test_build_page_shows_bundle(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    page = views.BuildPage.model_validate(client.get(f"/api/builds/{REPO}/5001.json").json())
    assert page.build.bundle_json is None and page.bundle and page.bundle["version"]["tag"] == "4.0.20.3014-ls1"
    assert page.run and page.run.id == 5001 and len(page.jobs) == 1
    html = client.get(f"/builds/{REPO}/5001").text
    assert "ghcr.io/linuxserver/baseimage-alpine:3.22" in html and "arm64" in html and "passed" in html
    assert client.get("/builds/docker-sonarr/5001").status_code == 404  # three segments, owner included


def test_image_pages_drill_down_by_tag(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    develop = fixture("control.build.summary")
    develop.update(run_id=5002, branch="develop", sent="2026-10-24T13:00:00Z")
    develop["bundle"].update(branch="develop")
    develop["bundle"]["version"].update(tag="develop-5.0.1.1234-ls7", release_tag="develop", upstream="5.0.1.1234")
    handlers.process(state.model, state.stream, "control:build.summary", develop)
    release = fixture("release.published")
    release["release"].update(tag_name="develop-5.0.1.1234-ls7", published_at="2026-10-24T13:00:00Z")
    handlers.process(state.model, state.stream, "release", release)
    dev = fixture("control.build.summary")
    dev.update(run_id=5003, mode="dev", branch="feature", sent="2026-10-24T14:00:00Z")
    handlers.process(state.model, state.stream, "control:build.summary", dev)

    whole = views.ImagePage.model_validate(client.get("/api/images/sonarr.json").json())
    assert whole.tag is None and whole.repo.full_name == REPO and whole.open_pulls == 1 and whole.open_issues == 1
    assert [row.branch for row in whole.rows] == ["main", "develop"]
    assert [b.run_id for b in whole.builds] == [5003, 5002, 5001]
    assert len(whole.releases) == 2 and whole.latest and whole.latest.run_id == 5002

    latest = views.ImagePage.model_validate(client.get("/api/images/sonarr/latest.json").json())
    assert [row.branch for row in latest.rows] == ["main"]
    assert [b.run_id for b in latest.builds] == [5001] and latest.latest and latest.latest.run_id == 5001
    assert [r.tag for r in latest.releases] == ["4.0.20.3014-ls1"]
    assert latest.badge_url == "https://ci.example.test:8443/badge/sonarr/latest.svg"

    develop_page = views.ImagePage.model_validate(client.get("/api/images/sonarr/develop.json").json())
    assert [row.branch for row in develop_page.rows] == ["develop"]
    assert [b.run_id for b in develop_page.builds] == [5002]
    assert [r.tag for r in develop_page.releases] == ["develop-5.0.1.1234-ls7"]

    html = client.get("/images/sonarr/develop").text
    assert "develop-5.0.1.1234-ls7" in html and "/badge/sonarr/develop.svg" in html
    assert client.get("/images/sonarr/nothing").status_code == 200  # a tag nobody publishes: empty sections


def test_status_and_management_pages(client: TestClient, state: State) -> None:
    seed(state, *FINISHED_SEED)
    status = views.StatusPage.model_validate(client.get("/api/status.json").json())
    assert status.events_secret and status.counts["builds"] == 1 and status.app_slug is None
    assert "not configured" in client.get("/status").text
    management = client.get("/management")
    assert "Sign in with GitHub" in management.text and 'action="/manage/' not in management.text
    page = views.ManagementPage.model_validate(client.get("/api/management.json").json())
    assert page.member is None and page.signed_in is None and page.actions == []
    assert page.repos == ["example-org/docker-radarr", REPO]


def test_scheduler_page_reads_decisions(client: TestClient, state: State) -> None:
    state.model.add_decision("sonarr", "main", "dispatch", '{"upstream": "4.0.21", "released": "4.0.20"}')
    state.model.add_decision("radarr", "main", "unchanged", '{"upstream": "5.1", "released": "5.1"}')
    page = views.SchedulerPage.model_validate(client.get("/api/scheduler.json").json())
    assert [(v.decision.image, v.decision.outcome) for v in page.decisions] == [
        ("radarr", "unchanged"),
        ("sonarr", "dispatch"),
    ]
    assert page.images == ["radarr", "sonarr"] and page.upcoming == [] and page.enabled
    html = client.get("/scheduler").text
    assert "4.0.21" in html and "<select" in html
    filtered = views.SchedulerPage.model_validate(client.get("/api/scheduler.json?image=sonarr").json())
    assert [v.decision.image for v in filtered.decisions] == ["sonarr"] and filtered.image == "sonarr"
    by_outcome = views.SchedulerPage.model_validate(client.get("/api/scheduler.json?outcome=unchanged").json())
    assert [v.decision.image for v in by_outcome.decisions] == ["radarr"]
    rows = client.get("/scheduler?image=sonarr&outcome=dispatch").text.split("<tbody>")[-1]
    assert "sonarr" in rows and "radarr" not in rows
    state.model.add_decision("sonarr", "main", "unchanged", '{"head": "b" * 40, "released": "a" * 40}', "release")
    by_kind = views.SchedulerPage.model_validate(client.get("/api/scheduler.json?kind=release").json())
    assert [v.decision.kind for v in by_kind.decisions] == ["release"] and by_kind.kind == "release"
    assert by_kind.kinds == ["upstream", "release", "packages", "manual"]
    assert 'name="kind"' in client.get("/scheduler?kind=release").text


def test_badge_passing_failing_unknown(client: TestClient, state: State) -> None:
    unknown = client.get("/badge/sonarr/latest.svg")
    assert unknown.status_code == 200 and unknown.headers["content-type"].startswith("image/svg+xml")
    assert unknown.headers["cache-control"] == "public, max-age=300"
    assert "CI: unknown" in unknown.text and "#8b949e" in unknown.text

    seed(state, "control.build.summary")
    passing = client.get("/badge/sonarr/latest.svg").text
    assert "CI: passing" in passing and "#3fb950" in passing and passing.startswith("<svg")
    assert "CI: unknown" in client.get("/badge/sonarr/develop.svg").text
    assert "CI: unknown" in client.get("/badge/radarr/latest.svg").text

    failed = fixture("control.build.summary.failed")
    failed["run_id"] = 5002
    failed["sent"] = "2026-10-24T13:00:00Z"
    handlers.process(state.model, state.stream, "control:build.summary", failed)
    failing = client.get("/badge/sonarr/latest.svg").text
    assert "CI: failing" in failing and "#f85149" in failing

    # A dev build never changes the badge.
    dev = fixture("control.build.summary")
    dev.update(run_id=5003, mode="dev", sent="2026-10-24T14:00:00Z")
    handlers.process(state.model, state.stream, "control:build.summary", dev)
    assert "CI: failing" in client.get("/badge/sonarr/latest.svg").text


def test_stuck_detection_math(state: State) -> None:
    assert p95([1.0]) == 1.0
    assert p95(list(map(float, range(1, 101)))) == 95.0
    assert p95(list(map(float, range(1, 21)))) == 19.0
    assert stuck_threshold([5.0] * 9) == DEFAULT_STUCK_SECONDS
    assert stuck_threshold([100.0] * 9 + [1000.0]) == 1000.0

    now = datetime.datetime(2026, 10, 24, 13, 0, tzinfo=datetime.UTC)
    base = fixture("workflow_job.in_progress")["workflow_job"]
    running = Job.from_github(REPO, {**base, "started_at": "2026-10-24T12:40:00Z"})  # 20 minutes in
    assert running.duration_seconds(now) == 1200.0
    assert not is_stuck(running, [], now)  # no history: the one hour default applies
    history = [float(n) for n in range(100, 1100, 100)]  # ten samples, p95 = 1000s
    assert is_stuck(running, history, now)
    done = Job.from_github(REPO, {**base, "status": "completed", "completed_at": "2026-10-24T12:50:00Z"})
    assert not is_stuck(done, history, now)

    # Through the model: ten completed "build (amd64)" jobs of ~9 minutes make a 20 minute one stuck.
    for n in range(10):
        completed = fixture("workflow_job.completed")["workflow_job"]
        completed.update(id=8000 + n, run_id=4000 + n)
        state.model.upsert_job(Job.from_github(REPO, completed))
    state.model.upsert_job(running)
    view = views.job_view(state, running, now)
    assert view.stuck and view.threshold_seconds == 535.0 and view.elapsed_seconds == 1200.0
    other = Job.from_github(REPO, {**base, "id": 9009, "name": "test", "started_at": "2026-10-24T12:40:00Z"})
    state.model.upsert_job(other)
    assert not views.job_view(state, other, now).stuck  # a different job name has no history


@pytest.mark.anyio
async def test_stream_emits_when_a_handler_changes_a_row(state: State) -> None:
    broadcaster = Broadcaster()
    events = views.event_stream(broadcaster, keepalive=0.05)
    assert await anext(events) == ": connected\n\n"
    assert await anext(events) == ": keepalive\n\n"
    handlers.process(state.model, broadcaster, "workflow_run", fixture("workflow_run.requested"))
    received = {await anext(events), await anext(events)}
    assert received == {sse({"type": "repos", "key": REPO}), sse({"type": "runs", "key": 5001})}
    await events.aclose()
    assert broadcaster.subscribers == 0
    broadcaster.publish("runs", 1)  # nobody listening: a no-op, not an error


def test_stream_endpoint_headers(client: TestClient) -> None:
    with client.stream("GET", "/api/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert next(response.iter_lines()) == ": connected"


def test_sse_format() -> None:
    assert sse({"type": "jobs", "key": 9001}) == 'data: {"type":"jobs","key":9001}\n\n'
    assert json.loads(sse({"type": "x", "key": [REPO, 7]})[6:]) == {"type": "x", "key": [REPO, 7]}
