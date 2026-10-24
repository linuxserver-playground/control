import json

import httpx
import pytest

from control.notify import Discord

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
