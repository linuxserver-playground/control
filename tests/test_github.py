"""The shared REST client asks a read again after a 5xx, and never a write."""

import asyncio
from collections.abc import Callable

import httpx
import pytest

from control import github as github_module
from control.github import GitHub


def _client(
    handler: Callable[[httpx.Request], httpx.Response], monkeypatch: pytest.MonkeyPatch
) -> tuple[GitHub, list[float]]:
    sleeps: list[float] = []

    async def sleep(self: GitHub, seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(GitHub, "sleep", sleep)
    return GitHub(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "ghs_token"), sleeps


def test_a_read_is_asked_again_after_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([502, 503, 200])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(next(answers), json={"ok": True})

    github, sleeps = _client(handler, monkeypatch)
    assert asyncio.run(github.get_json("/repos/example-org/docker-sonarr/actions/runs/1")) == {"ok": True}
    assert len(seen) == 3 and sleeps == list(github_module.RETRY_DELAYS)


def test_a_read_gives_up_after_the_third_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, json={"message": "Bad Gateway"})

    github, sleeps = _client(handler, monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(github.get("/rate_limit"))
    assert calls == 3 and len(sleeps) == 2


def test_a_client_error_and_a_write_are_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404 if request.method == "GET" else 502, json={"message": "no"})

    github, sleeps = _client(handler, monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(github.get("/repos/example-org/gone"))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(github.post("/repos/example-org/docker-sonarr/dispatches", {"event_type": "x"}))
    assert calls == 2 and sleeps == []


def test_a_dropped_connection_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("reset by peer", request=request)
        return httpx.Response(200, text="hello")

    github, sleeps = _client(handler, monkeypatch)
    assert asyncio.run(github.get_text("/repos/example-org/docker-sonarr/contents/image.yml")) == "hello"
    assert calls == 2 and sleeps == [github_module.RETRY_DELAYS[0]]
