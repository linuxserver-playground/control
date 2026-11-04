import json

import httpx
import pytest

from control.notify import Discord, no_previews

from .conftest import mock_client

WEBHOOK = "https://discord.com/api/webhooks/123/token"


@pytest.mark.anyio
async def test_post_waits_and_returns_message_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "9876543210"})

    async with mock_client(handler) as client:
        message_id = await Discord(WEBHOOK, client).post("hello", embeds=[{"title": "t"}])
    assert message_id == "9876543210"
    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{WEBHOOK}?wait=true"
    assert json.loads(seen[0].content) == {"content": "hello", "embeds": [{"title": "t"}]}


@pytest.mark.anyio
async def test_edit_patches_the_message() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "42"})

    async with mock_client(handler) as client:
        assert await Discord(WEBHOOK, client).edit("42", content="edited") is True
    assert seen[0].method == "PATCH"
    assert str(seen[0].url) == f"{WEBHOOK}/messages/42"
    assert json.loads(seen[0].content) == {"content": "edited"}


@pytest.mark.anyio
async def test_failures_are_swallowed() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    async with mock_client(boom) as client:
        discord = Discord(WEBHOOK, client)
        assert await discord.post("x") is None
        assert await discord.edit("1", content="x") is False

    async with mock_client(lambda _: httpx.Response(429, json={"message": "rate limited"})) as client:
        assert await Discord(WEBHOOK, client).post("x") is None


@pytest.mark.anyio
async def test_unconfigured_is_a_noop() -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    async with mock_client(never) as client:
        discord = Discord(None, client)
        assert not discord.configured
        assert await discord.post("x") is None
        assert await discord.edit("1", content="x") is False


def test_links_in_text_never_unfurl() -> None:
    """GitHub links unfurl into large preview boxes; every link in a message's text goes out in brackets."""
    alert = (
        "⚠️ **steam 053af90e-ls4** was released 30 min ago and its website page is still missing: the hub has not "
        "ingested it (hub runs: https://github.com/o/hub/actions/workflows/deploy.yml). "
        "Release: https://github.com/o/docker-steam/releases/tag/053af90e-ls4"
    )
    assert no_previews(alert).endswith(
        "(hub runs: <https://github.com/o/hub/actions/workflows/deploy.yml>). "
        "Release: <https://github.com/o/docker-steam/releases/tag/053af90e-ls4>"
    )
    mixed = "[run](https://x.y/1), [done](<https://x.y/2>), <https://x.y/3>, `https://x.y/4` and https://w.org/A_(b)."
    expected = (
        "[run](<https://x.y/1>), [done](<https://x.y/2>), <https://x.y/3>, `https://x.y/4` and <https://w.org/A_(b)>."
    )
    assert no_previews(mixed) == expected
    assert no_previews(expected) == expected


@pytest.mark.anyio
async def test_posts_and_edits_bracket_links_but_leave_embeds_alone() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "1"})

    embed = {"title": "t", "url": "https://github.com/o/r", "description": "see https://github.com/o/r"}
    async with mock_client(handler) as client:
        await Discord(WEBHOOK, client).post("at https://github.com/o/r", embeds=[embed])
        await Discord(WEBHOOK, client).edit("1", content="at https://github.com/o/r")
    for request in seen:
        assert json.loads(request.content)["content"] == "at <https://github.com/o/r>"
    assert json.loads(seen[0].content)["embeds"] == [embed]
