"""
The authenticated GitHub REST client the scheduler, fleet discovery and member actions share: one
installation token, the standard headers, `raise_for_status` everywhere, and pagination over `Link`
headers. The backfill keeps its own copy with rate-limit sleeping because it is the only caller that
makes thousands of requests in a row.
"""

import logging
from typing import Any

import httpx

from .github_app import API
from .state import State

log = logging.getLogger(__name__)


def headers_for(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class GitHub:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self.http = http
        self.token = token
        self.headers = headers_for(token)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = path if path.startswith("https://") else f"{API}{path}"
        response = await self.http.get(url, params=params, headers=self.headers)
        response.raise_for_status()
        return response

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self.get(path, params)).json()

    async def post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        response = await self.http.post(f"{API}{path}", json=json, headers=self.headers)
        response.raise_for_status()
        return response

    async def paginate(
        self, path: str, params: dict[str, Any], key: str | None = None, limit: int | None = None
    ) -> list[Any]:
        """Follow `Link: rel="next"` until `limit` items (or the end). `key` picks the list out of an envelope."""
        items: list[Any] = []
        next_url: str | None = path
        next_params: dict[str, Any] | None = params
        while next_url and (limit is None or len(items) < limit):
            response = await self.get(next_url, next_params)
            body = response.json()
            page = body.get(key, []) if key else body
            if not isinstance(page, list):
                break
            items.extend(page)
            next_url = response.links.get("next", {}).get("url")
            next_params = None
        return items[:limit] if limit is not None else items


async def installation_client(state: State) -> GitHub | None:
    """A client with the installation token, or None while the app is not configured or not installed."""
    credentials = state.credentials.load()
    installation_id = state.installation_id
    if credentials is None or installation_id is None:
        return None
    token = await state.tokens.get(credentials, installation_id)
    return GitHub(state.http, token)
