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


class GraphQLError(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__("; ".join(str(e.get("message", e)) for e in errors))


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

    async def get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        """A file's raw bytes from the contents API (`/repos/{repo}/contents/{path}`), as text."""
        headers = {**self.headers, "Accept": "application/vnd.github.raw+json"}
        response = await self.http.get(f"{API}{path}", params=params, headers=headers)
        response.raise_for_status()
        return response.text

    async def post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        response = await self.http.post(f"{API}{path}", json=json, headers=self.headers)
        response.raise_for_status()
        return response

    async def patch(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        response = await self.http.patch(f"{API}{path}", json=json, headers=self.headers)
        response.raise_for_status()
        return response

    async def put(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        response = await self.http.put(f"{API}{path}", json=json, headers=self.headers)
        response.raise_for_status()
        return response

    async def delete(self, path: str) -> httpx.Response:
        response = await self.http.delete(f"{API}{path}", headers=self.headers)
        response.raise_for_status()
        return response

    async def upload_asset(self, upload_url: str, name: str, content: bytes, content_type: str) -> httpx.Response:
        """Attach a file to a Release; `upload_url` is the release's own template (`…/assets{?name,label}`)."""
        url = upload_url.split("{", 1)[0]
        headers = {**self.headers, "Content-Type": content_type}
        response = await self.http.post(url, params={"name": name}, content=content, headers=headers, timeout=300)
        response.raise_for_status()
        return response

    async def download(self, path: str) -> httpx.Response:
        """An API path that answers with a redirect to signed storage (a run's logs). The redirect is followed by hand
        and without our token, which the signed URL does not need and must never see. The caller judges the status."""
        response = await self.http.get(f"{API}{path}", headers=self.headers)
        if response.is_redirect and (location := response.headers.get("location")):
            response = await self.http.get(location, timeout=300)
        return response

    async def graphql(self, query: str, variables: dict[str, Any]) -> Any:
        """The `data` of a GraphQL answer; GitHub reports failures with 200 and `errors`, which raise here."""
        body = (await self.post("/graphql", {"query": query, "variables": variables})).json()
        if body.get("errors"):
            raise GraphQLError(body["errors"])
        return body.get("data")

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
