import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from control.app import create_app
from control.config import Settings
from control.events import AMBER, GREEN, GREY, RED, build_embed, embed_color, validate_event
from control.github_app import AppCredentials
from control.model import Build
from control.state import State

from .conftest import REPO, fixture, mock_discord, post_event, seed


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
    embed = calls[0][2]["embeds"][0]
    assert embed["title"] == "sonarr main" and embed["color"] == AMBER
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["result"] == "running" and fields["mode"] == "live" and fields["reason"] == "upstream"
    assert "[run](https://github.com/example-org/docker-sonarr/actions/runs/5001)" in fields["links"]

    assert post_event(client, fixture("control.build.summary")).status_code == 202
    assert len(calls) == 2 and calls[1][0] == "PATCH" and calls[1][1].endswith("/messages/msg-1")
    embed = calls[1][2]["embeds"][0]
    assert embed["title"] == "sonarr 4.0.20.3014-ls1" and embed["color"] == GREEN
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {
        "mode": "live",
        "reason": "upstream",
        "result": "success",
        "platforms": "amd64, arm64",
        "test": "passed",
        "links": fields["links"],
    }
    assert "[release](https://github.com/example-org/docker-sonarr/releases/tag/4.0.20.3014-ls1)" in fields["links"]
    assert "[control](https://ci.example.test:8443/builds/example-org/docker-sonarr/5001)" in fields["links"]
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
    assert embed["color"] == GREY and embed["title"] == "sonarr pr-7-ccccccc"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["pull request"] == "#7" and "[artifact](" in fields["links"]


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
    assert embed["color"] == RED
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["result"] == "failure (build (arm64))" and fields["test"] == "not run" and fields["platforms"] == "—"


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
    fields = {f["name"]: f["value"] for f in build_embed(build, "https://x")["fields"]}
    assert fields["platforms"] == "amd64 (success), arm64 (failure)" and fields["test"] == "failed"
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
    assert build_embed(seed_build, "https://x")["footer"] == {"text": REPO}


def test_events_are_replayable_deliveries(client: TestClient, state: State) -> None:
    seed(state)  # nothing seeded; the endpoint stores the event under control:<type>
    assert post_event(client, fixture("control.build.summary")).status_code == 202
    (stored,) = state.db.recent_deliveries()
    assert stored.event == "control:build.summary" and stored.repo == REPO and stored.delivery_id.startswith("control-")


def test_embed_links_hub_results_for_live_builds() -> None:
    build = Build.from_event(fixture("control.build.summary"))
    assert build.mode == "live" and build.version_tag
    hub_link = f"https://hub.example/hub/builds/{build.image}/{build.version_tag}/"
    assert hub_link in json.dumps(build_embed(build, "https://x", "https://hub.example/hub"))
    assert "https://hub.example" not in json.dumps(build_embed(build, "https://x"))
    dev = build.model_copy(update={"mode": "dev"})
    assert "https://hub.example" not in json.dumps(build_embed(dev, "https://x", "https://hub.example/hub"))


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
