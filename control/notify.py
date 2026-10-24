"""
Discord is the human-facing side of control for now: a message per notable event, edited in place as the
event progresses. A notification failing must never fail the request that triggered it, so every call
logs and returns None instead of raising.
"""

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class Discord:
    def __init__(self, webhook_url: str | None, client: httpx.AsyncClient) -> None:
        self.webhook_url = webhook_url
        self.client = client

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    async def post(self, content: str | None = None, embeds: list[dict[str, Any]] | None = None) -> str | None:
        """Send a message; `wait=true` makes Discord return it so we get an id to edit later."""
        if not self.webhook_url:
            log.info("discord not configured, dropping: %s", content or embeds)
            return None
        try:
            response = await self.client.post(
                self.webhook_url, params={"wait": "true"}, json=_body(content, embeds), timeout=10
            )
            response.raise_for_status()
            return str(response.json()["id"])
        except Exception:
            log.exception("discord post failed")
            return None

    async def edit(
        self, message_id: str, content: str | None = None, embeds: list[dict[str, Any]] | None = None
    ) -> bool:
        if not self.webhook_url:
            return False
        try:
            response = await self.client.patch(
                f"{self.webhook_url}/messages/{message_id}", json=_body(content, embeds), timeout=10
            )
            response.raise_for_status()
            return True
        except Exception:
            log.exception("discord edit failed")
            return False


def _body(content: str | None, embeds: list[dict[str, Any]] | None) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if content is not None:
        body["content"] = content
    if embeds is not None:
        body["embeds"] = embeds
    return body
