"""Holding a released build's message until its website page is live, and reporting the ones that never get there."""

import datetime
import json
from typing import Any

import httpx
import pytest

from control import publish
from control.state import State

from .conftest import REPO, fixture, mock_discord, post_event

TAG = "4.0.20.3014-ls1"
RELEASED_AT = "2026-10-24T12:10:00+00:00"


def at(minutes: float) -> str:
    start = datetime.datetime.fromisoformat(RELEASED_AT)
    return (start + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")


class World:
    """Discord, the website and the hub, as publish.py sees them."""

    def __init__(self) -> None:
        self.page_live = False
        self.hub_tags: list[str] = []
        self.discord: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "discord.com" in url:
            self.discord.append((request.method, json.loads(request.content)))
            return httpx.Response(200, json={"id": "msg-1"})
        if f"/images/sonarr/builds/{TAG}/" in url:
            return httpx.Response(200 if self.page_live else 404, text="page")
        if url.endswith("/api/v1/catalog/sonarr/builds.json"):
            return httpx.Response(200, json={"data": {"builds": [{"tag": t} for t in self.hub_tags]}})
        return httpx.Response(404)

    def posts(self) -> list[str]:
        return [body.get("content", "") for method, body in self.discord if method == "POST" and "content" in body]

    def last_embed(self) -> dict[str, Any]:
        embed: dict[str, Any] = [body for method, body in self.discord if "embeds" in body][-1]["embeds"][0]
        return embed


@pytest.fixture
def world(client: Any, state: State, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World()
    mock_discord(state, monkeypatch, world)
    assert post_event(client, fixture("control.build.started")).status_code == 202
    assert post_event(client, fixture("control.build.summary")).status_code == 202
    state.model.set_publication(REPO, 5001, "pending")
    with state.db.connect() as conn:
        conn.execute("UPDATE builds SET publish_started_at = ? WHERE run_id = 5001", (RELEASED_AT,))
    return world


@pytest.mark.anyio
async def test_page_going_live_finishes_the_message(state: State, world: World) -> None:
    await publish.check_once(state, now=lambda: at(3))
    build = state.model.build(REPO, 5001)
    assert build and build.publish_state == "pending" and world.posts() == []

    world.page_live = True
    await publish.check_once(state, now=lambda: at(6))
    build = state.model.build(REPO, 5001)
    assert build and build.publish_state == "live" and build.published_at == at(6)
    embed = world.last_embed()
    assert embed["title"].endswith("passed") and embed["url"].endswith(f"/images/sonarr/builds/{TAG}/")
    assert world.posts() == []  # an on-time page needs no extra post
    assert state.model.publications() == []


@pytest.mark.anyio
async def test_overdue_page_is_reported_with_where_it_is_stuck(state: State, world: World) -> None:
    await publish.check_once(state, now=lambda: at(31))
    build = state.model.build(REPO, 5001)
    assert build and build.publish_state == "overdue"
    assert build.publish_note and "the hub has not ingested it" in build.publish_note
    assert len(world.posts()) == 1 and "sonarr 4.0.20.3014-ls1** was released 31 min ago" in world.posts()[0]
    assert "not on the website yet: the hub has not ingested it" in world.last_embed()["description"]

    # Still overdue on the next check: no second alert.
    world.hub_tags = [TAG]
    await publish.check_once(state, now=lambda: at(40))
    assert len(world.posts()) == 1

    # It turns up after all: the message is finished and a short follow-up clears the alert.
    world.page_live = True
    await publish.check_once(state, now=lambda: at(45))
    build = state.model.build(REPO, 5001)
    assert build and build.publish_state == "live"
    assert len(world.posts()) == 2 and "reached the website after 45 min" in world.posts()[1]
    assert "not on the website" not in world.last_embed()["description"]


@pytest.mark.anyio
async def test_hub_has_it_but_the_site_does_not(state: State, world: World) -> None:
    world.hub_tags = [TAG]
    await publish.check_once(state, now=lambda: at(31))
    build = state.model.build(REPO, 5001)
    assert build and build.publish_note and build.publish_note.startswith("the hub has it, the site has not published")


@pytest.mark.anyio
async def test_watching_stops_after_the_expiry(state: State, world: World) -> None:
    await publish.check_once(state, now=lambda: at(31))
    await publish.check_once(state, now=lambda: at(24 * 60 + 1))
    build = state.model.build(REPO, 5001)
    assert build and build.publish_state == "expired"
    assert len(world.posts()) == 2 and "control has stopped watching" in world.posts()[1]
    assert "never reached the website" in world.last_embed()["description"]
    assert state.model.publications() == []


def test_replayed_summary_keeps_the_publication_state(client: Any, state: State, world: World) -> None:
    state.model.set_publication(REPO, 5001, "live", published_at=at(5))
    replay = fixture("control.build.summary")
    replay["sent"] = "2026-10-24T12:30:00Z"  # a different body, so not a duplicate delivery
    assert post_event(client, replay).status_code == 202
    build = state.model.build(REPO, 5001)
    assert build and build.publish_state == "live" and build.published_at == at(5)
