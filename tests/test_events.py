import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from control import handlers
from control.app import create_app
from control.config import Settings
from control.events import (
    AMBER,
    GREEN,
    GREY,
    RED,
    build_embed,
    embed_color,
    embed_length,
    validate_event,
    within_limits,
)
from control.github_app import AppCredentials
from control.model import Build
from control.state import State

from .conftest import REPO, fixture, mock_discord, post_event, seed

SITE = "https://site.example/site"
SETTINGS = Settings(public_url="https://x", site_url=SITE)


def field(embed: dict[str, Any], name: str) -> str:
    """The value of the named field; the blank column fillers are skipped."""
    return str(next(f["value"] for f in embed["fields"] if f["name"] == name))


def test_events_disabled_without_secret(settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"events_secret": None}))) as client:
        response = post_event(client, fixture("control.build.started"), secret="anything")
    assert response.status_code == 503


def test_bad_or_missing_signature_is_401(client: TestClient, state: State) -> None:
    assert post_event(client, fixture("control.build.started"), secret="wrong").status_code == 401
    assert post_event(client, fixture("control.build.started"), secret=None).status_code == 401
    assert state.db.count_deliveries() == 0


def test_schema_violations_are_400_with_the_error_list(client: TestClient) -> None:
    payload = fixture("control.build.summary")
    payload["result"] = "exploded"
    del payload["bundle"]["version"]["tag"]
    payload["bundle"]["platforms"][0]["arch"] = "riscv"
    response = post_event(client, payload)
    assert response.status_code == 400
    detail = response.json()["detail"]
    errors = detail["errors"]
    assert any(e.startswith("result:") for e in errors)
    # `bundle` is a oneOf(null, bundle schema), so nested problems surface at `bundle` itself.
    assert any(e.startswith("bundle:") for e in errors)
    assert client.post("/events", content=b"not json", headers={"X-Control-Signature": "sha256=00"}).status_code == 401


def test_validate_event_accepts_the_fixtures() -> None:
    for name in ("control.build.started", "control.build.summary", "control.build.summary.failed"):
        assert validate_event(fixture(name)) == [], name
    assert validate_event({"schema": 2}) != []
    assert validate_event({"schema": 1, "type": "build.summary", "sent": "x", "repository": "a/b", "run_id": 1})


def test_started_posts_and_summary_edits_one_live_message(
    client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"id": "msg-1"})

    mock_discord(state, monkeypatch, handler)
    assert post_event(client, fixture("control.build.started")).status_code == 202
    build = state.model.build(REPO, 5001)
    assert build and build.running and build.discord_message_id == "msg-1"
    assert len(calls) == 1 and calls[0][0] == "POST"
    assert len(calls[0][2]["embeds"]) == 1
    embed = calls[0][2]["embeds"][0]
    site = state.settings.site_url
    assert embed["title"] == "sonarr main \u00b7 \U0001f7e1 running" and embed["color"] == AMBER
    assert embed["description"] == "live \u00b7 upstream"
    # Nothing is released yet, so the provisional message points at the control run page.
    assert embed["url"] == f"https://ci.example.test:8443/builds/{REPO}/5001"
    assert embed["author"] == {"name": "sonarr", "url": f"{site}/images/sonarr/"}
    assert field(embed, "Platforms") == "pending" and field(embed, "Timing") == "started 12:00 UTC"
    assert "[run](https://github.com/example-org/docker-sonarr/actions/runs/5001)" in field(embed, "Links")
    assert "CI report" not in field(embed, "Links")

    assert post_event(client, fixture("control.build.summary")).status_code == 202
    assert len(calls) == 2 and calls[1][0] == "PATCH" and calls[1][1].endswith("/messages/msg-1")
    # Released, but its website page is minutes away: held at "publishing", pointing at control.
    held = calls[1][2]["embeds"][0]
    assert held["title"] == "sonarr 4.0.20.3014-ls1 \u00b7 \U0001f7e1 passed, publishing" and held["color"] == AMBER
    assert held["description"] == "live \u00b7 upstream \u00b7 publishing to the website"
    assert held["url"] == f"https://ci.example.test:8443/builds/{REPO}/5001"
    assert field(held, "Links").startswith("CI report (publishing\u2026)")
    build = state.model.build(REPO, 5001)
    assert build and build.publish_state == "pending" and build.publish_started_at

    # The final form, once publish.py has seen the page live.
    state.model.set_publication(REPO, 5001, "live", published_at="2026-10-24T12:15:00+00:00")
    build = state.model.build(REPO, 5001)
    assert build is not None
    embed = build_embed(build, state.settings)
    assert embed["title"] == "sonarr 4.0.20.3014-ls1 \u00b7 \U0001f7e2 passed" and embed["color"] == GREEN
    assert embed["description"] == "live \u00b7 upstream"
    report = f"{site}/images/sonarr/builds/4.0.20.3014-ls1/"
    assert embed["url"] == report
    assert embed["author"]["url"] == f"{site}/images/sonarr/"
    assert [f["name"] for f in embed["fields"]] == ["Platforms", "Upstream", "\u200b", "Timing", "Links"]
    assert all(f["inline"] for f in embed["fields"])
    assert field(embed, "Platforms") == (
        "**amd64** `dddddddddddd` 114.4 MiB tests passed\n**arm64** `eeeeeeeeeeee` 112.5 MiB tests passed"
    )
    assert field(embed, "Upstream") == (
        "[4.0.20.3014](https://github.com/Sonarr/Sonarr/releases/tag/v4.0.20.3014) (github_release)"
    )
    # The started time comes from the first message's row, the finish from the summary.
    assert field(embed, "Timing") == "amd64 8m00s \u00b7 arm64 8m30s\n12:00 \u2192 12:09 UTC (9m37s)"
    links = field(embed, "Links")
    assert links.startswith(f"[CI report]({report})")
    assert "[release](https://github.com/example-org/docker-sonarr/releases/tag/4.0.20.3014-ls1)" in links
    assert links.endswith(f"[control](https://ci.example.test:8443/builds/{REPO}/5001)")
    assert embed["footer"] == {"text": f"{REPO} \u00b7 run 5001"} and "thumbnail" not in embed
    build = state.model.build(REPO, 5001)
    assert build and build.result == "success" and build.discord_message_id == "msg-1"

    # The same body again (a workflow retry) is a no-op: no row change, no third message.
    response = post_event(client, fixture("control.build.summary"))
    assert response.status_code == 202 and response.json()["status"] == "duplicate"
    assert len(calls) == 2
    assert state.db.count_deliveries() == 2


def test_dev_and_pr_builds_only_get_a_summary(
    client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "m"})

    mock_discord(state, monkeypatch, handler)
    started = fixture("control.build.started")
    started.update(mode="pr", pull_request=7, reason="pull_request", run_id=6001)
    assert post_event(client, started).status_code == 202
    assert calls == []

    summary = fixture("control.build.summary")
    summary.update(mode="pr", pull_request=7, reason="pull_request", run_id=6001)
    summary.pop("release_url")
    summary["artifact_url"] = "https://github.com/example-org/docker-sonarr/actions/runs/6001/artifacts/1"
    summary["bundle"].update(mode="pr", pull_request=7, reason="pull_request")
    summary["bundle"]["version"].update(tag="pr-7-ccccccc", ls_build=0)
    del summary["bundle"]["version"]["release_tag"]
    assert post_event(client, summary).status_code == 202
    assert len(calls) == 1
    embed = calls[0]["embeds"][0]
    assert embed["color"] == GREY and embed["title"] == "sonarr pr-7-ccccccc \u00b7 \u26aa passed"
    assert embed["description"] == "pr \u00b7 pull_request \u00b7 PR #7"
    # A pr build has no page on the website: the control run page and the pull request are the links.
    assert embed["url"] == f"https://ci.example.test:8443/builds/{REPO}/6001"
    links = field(embed, "Links")
    assert "[artifact](" in links and f"[PR #7](https://github.com/{REPO}/pull/7)" in links
    assert "CI report" not in links and "site.example" not in links


def test_failure_is_red_in_any_mode(client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "m"})

    mock_discord(state, monkeypatch, handler)
    failed = fixture("control.build.summary.failed")
    failed["mode"] = "dev"
    assert post_event(client, failed).status_code == 202
    embed = calls[0]["embeds"][0]
    assert embed["color"] == RED and embed["title"] == "sonarr main \u00b7 \U0001f534 failed"
    assert embed["description"] == "dev \u00b7 upstream \u00b7 failed job: build (arm64)"
    assert field(embed, "Platforms") == "\u2014" and field(embed, "Upstream") == "\u2014"
    assert field(embed, "Timing") == "finished 12:05 UTC"
    assert embed["url"] == f"https://ci.example.test:8443/builds/{REPO}/5001"


def test_package_check_embeds(client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "m"})

    mock_discord(state, monkeypatch, handler)
    # A package check whose table did not change releases nothing and reports `skipped`: a grey embed.
    unchanged = fixture("control.build.summary")
    unchanged.update(reason="packages", result="skipped", run_id=7001)
    unchanged.pop("release_url")
    unchanged["bundle"].update(reason="packages")
    assert post_event(client, unchanged).status_code == 202
    embed = calls[-1]["embeds"][0]
    assert embed["color"] == GREY and embed["title"] == "sonarr 4.0.20.3014-ls1 \u00b7 \u26aa skipped"
    assert embed["description"] == "live \u00b7 packages \u00b7 packages unchanged, nothing released"
    assert embed["url"] == f"https://ci.example.test:8443/builds/{REPO}/7001" and "CI report" not in field(
        embed, "Links"
    )

    # One that committed a new table dispatched the release build; the embed links that run once its webhook
    # arrived. The check itself was a `workflow_dispatch` run too, so the lookup must skip its own run.
    started = fixture("control.build.started")
    started.update(reason="packages", run_id=7002, sent="2026-10-24T12:00:08Z")
    assert post_event(client, started).status_code == 202
    for run_id, created in ((7002, "2026-10-24T12:00:08Z"), (7003, "2026-10-24T12:08:00Z")):
        run = fixture("workflow_run.requested")
        run["workflow_run"].update(id=run_id, event="workflow_dispatch", created_at=created)
        run["workflow_run"]["html_url"] = f"https://github.com/{REPO}/actions/runs/{run_id}"
        handlers.process(state.model, state.stream, "workflow_run", run)
    committed = fixture("control.build.summary")
    committed.update(reason="packages", run_id=7002)
    committed.pop("release_url")
    committed["bundle"].update(reason="packages")
    assert post_event(client, committed).status_code == 202
    embed = calls[-1]["embeds"][0]
    assert embed["color"] == GREEN
    assert embed["description"] == (
        "live \u00b7 packages \u00b7 new package table committed \u00b7 "
        f"release build [run 7003](https://github.com/{REPO}/actions/runs/7003)"
    )
    assert field(embed, "Timing").endswith("12:00 \u2192 12:09 UTC (9m37s)")

    # Without the follow-up run yet, the embed still says a release build follows.
    build = state.model.build(REPO, 7002)
    assert build is not None
    assert build_embed(build, SETTINGS)["description"].endswith(
        "new package table committed \u00b7 release build follows"
    )


def test_packages_compared_with_the_previous_release(client: TestClient, state: State) -> None:
    previous = fixture("control.build.summary")
    previous["bundle"]["packages"] = {
        "amd64": {"count": 120, "distro": "Alpine Linux v3.22", "hash": "aaaaaaaa"},
        "arm64": {"count": 119, "distro": "Alpine Linux v3.22", "hash": "bbbbbbbb"},
    }
    assert post_event(client, previous).status_code == 202
    current = fixture("control.build.summary")
    current.update(run_id=5002, sent="2026-10-24T14:00:00Z")
    current["release_url"] = f"https://github.com/{REPO}/releases/tag/4.0.20.3015-ls1"
    current["bundle"]["version"].update(upstream="4.0.20.3015", upstream_clean="4.0.20.3015", tag="4.0.20.3015-ls1")
    current["bundle"]["previous"] = {"tag": "4.0.20.3014-ls1", "release_url": previous["release_url"]}
    current["bundle"]["packages"] = {
        "amd64": {"count": 123, "distro": "Alpine Linux v3.22", "hash": "cccccccc"},
        "arm64": {"count": 119, "distro": "Alpine Linux v3.22", "hash": "bbbbbbbb"},
    }
    assert post_event(client, current).status_code == 202
    state.model.set_publication(REPO, 5002, "live")  # on the website, so the line is the final one
    build = state.model.build(REPO, 5002)
    assert build is not None
    before = state.model.build_for_tag(REPO, "4.0.20.3014-ls1", with_bundle=True)
    assert before and before.bundle
    embed = build_embed(build, SETTINGS, previous=before)
    assert embed["description"] == "live \u00b7 upstream \u00b7 packages changed (amd64 +3)"
    same = before.model_copy(update={"bundle_json": build.bundle_json})
    assert (
        build_embed(build, SETTINGS, previous=same)["description"] == "live \u00b7 upstream \u00b7 packages unchanged"
    )
    # Without the previous bundle there is nothing to compare, so the line stays short.
    assert build_embed(build, SETTINGS)["description"] == "live \u00b7 upstream"
    # The event flow finds the previous build itself from `bundle.previous.tag`.
    from control.events import previous_build

    assert (found := previous_build(state.model, build)) and found.run_id == 5001


def test_embeds_stay_within_discord_limits() -> None:
    event = fixture("control.build.summary.failed")
    event.update(image="i" * 300, branch="b" * 300, failed_job="j" * 5000, reason="r" * 100)
    embed = build_embed(Build.from_event(event), SETTINGS)
    assert len(embed["title"]) == 256 and embed["title"].endswith("\u2026")
    assert len(embed["description"]) == 4096 and embed["description"].endswith("\u2026")
    assert embed_length(embed) <= 6000
    long_version = fixture("control.build.summary")
    long_version["bundle"]["version"]["upstream"] = "v" * 2000
    embed = build_embed(Build.from_event(long_version), SETTINGS)
    assert len(field(embed, "Upstream")) == 1024 and field(embed, "Upstream").endswith("\u2026")
    assert all(len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in embed["fields"])
    crowded = within_limits(
        {
            "title": "t",
            "description": "d" * 7000,
            "fields": [{"name": "n", "value": "v" * 1000, "inline": True} for _ in range(30)],
            "footer": {"text": "f" * 3000},
        }
    )
    assert len(crowded["fields"]) == 25 and len(crowded["footer"]["text"]) == 2048
    assert embed_length(crowded) <= 6000 and crowded["description"].endswith("\u2026")


def test_summary_with_a_failed_platform_has_no_digest(client: TestClient, state: State) -> None:
    summary = fixture("control.build.summary")
    summary["result"] = "failure"
    summary["failed_job"] = "build (arm64)"
    summary["bundle"]["platforms"][1] = {"arch": "arm64", "result": "failure", "runner": "ubuntu-24.04-arm"}
    summary["bundle"]["test"]["success"] = False
    assert validate_event(summary) == []
    assert post_event(client, summary).status_code == 202
    build = state.model.build(REPO, 5001)
    assert build and [(p.arch, p.result) for p in build.platforms] == [("amd64", "success"), ("arm64", "failure")]
    assert build.archs == ["amd64", "arm64"]
    embed = build_embed(build, SETTINGS)
    assert embed["color"] == RED and embed["description"] == "live \u00b7 upstream \u00b7 failed job: build (arm64)"
    assert field(embed, "Platforms") == "**amd64** `dddddddddddd` 114.4 MiB tests passed\n**arm64** failure"
    page = client.get(f"/builds/{REPO}/5001")
    assert page.status_code == 200 and ">failure<" in page.text


def test_discord_failure_does_not_fail_the_event(
    client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    mock_discord(state, monkeypatch, boom)
    assert post_event(client, fixture("control.build.started")).status_code == 202
    build = state.model.build(REPO, 5001)
    assert build and build.discord_message_id is None


def test_embed_colors() -> None:
    seed_build = Build.from_event(fixture("control.build.summary"))
    assert embed_color(seed_build) == GREEN
    assert embed_color(seed_build.model_copy(update={"mode": "dev"})) == GREY
    assert embed_color(seed_build.model_copy(update={"result": "failure"})) == RED
    assert embed_color(seed_build.model_copy(update={"result": "cancelled"})) == GREY
    assert embed_color(seed_build.model_copy(update={"result": None})) == AMBER
    assert build_embed(seed_build, SETTINGS)["footer"] == {"text": f"{REPO} \u00b7 run 5001"}


def test_events_are_replayable_deliveries(client: TestClient, state: State) -> None:
    seed(state)  # nothing seeded; the endpoint stores the event under control:<type>
    assert post_event(client, fixture("control.build.summary")).status_code == 202
    (stored,) = state.db.recent_deliveries()
    assert stored.event == "control:build.summary" and stored.repo == REPO and stored.delivery_id.startswith("control-")


def test_embed_links_the_website_for_released_builds() -> None:
    build = Build.from_event(fixture("control.build.summary"))
    assert build.mode == "live" and build.version_tag
    page = f"{SITE}/images/{build.image}/builds/{build.version_tag}/"
    embed = build_embed(build, SETTINGS)
    assert embed["url"] == page and f"[CI report]({page})" in field(embed, "Links")
    assert embed["author"]["url"] == f"{SITE}/images/{build.image}/"
    # Dev builds and runs that released nothing have no page there: the control run page is the primary link.
    dev = build.model_copy(update={"mode": "dev", "release_url": None})
    embed = build_embed(dev, SETTINGS)
    assert embed["url"] == f"https://x/builds/{REPO}/5001" and "builds/4.0.20" not in field(embed, "Links")
    assert embed["author"]["url"] == f"{SITE}/images/{build.image}/"


def test_live_success_summary_dispatches_the_hub(
    client: TestClient, state: State, monkeypatch: pytest.MonkeyPatch, credentials: AppCredentials
) -> None:
    from .conftest import install, mock_github, token_response

    install(state, credentials)
    dispatches: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (token := token_response(request)) is not None:
            return token
        if request.url.path == f"/repos/{state.settings.github_org}/{state.settings.hub_repo}/dispatches":
            dispatches.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    mock_github(state, monkeypatch, handler)
    payload = fixture("control.build.summary")
    assert post_event(client, payload).status_code == 202
    assert dispatches == [
        {
            "event_type": "image-released",
            "client_payload": {"repository": payload["repository"], "tag": payload["bundle"]["version"]["tag"]},
        }
    ]
    # A failed summary never dispatches.
    dispatches.clear()
    assert post_event(client, fixture("control.build.summary.failed")).status_code == 202
    assert dispatches == []
    # Neither does a successful run that released nothing (a package check): no release_url.
    unreleased = json.loads(json.dumps(payload))
    unreleased.pop("release_url", None)
    unreleased["run_id"] = payload["run_id"] + 1
    assert post_event(client, unreleased).status_code == 202
    assert dispatches == []
