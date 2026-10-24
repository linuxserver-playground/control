"""
A tiny in-process pub/sub for the dashboard's server-sent events: handlers publish `{"type": <table>,
"key": <id>}` when they change a row, and every open `/api/stream` response holds a queue. One replica
only, by design; a dropped slow subscriber loses events and the page simply refreshes on the next one.
"""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

QUEUE_SIZE = 64


class Broadcaster:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    @property
    def subscribers(self) -> int:
        return len(self._queues)

    def publish(self, type_: str, key: Any) -> None:
        """Safe from the event loop thread, a worker thread, or a plain script with no loop (then a no-op)."""
        event = {"type": type_, "key": key}
        with self._lock:
            loop = self._loop
            queues = list(self._queues)
        if not queues or loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(queues, event)
        else:
            loop.call_soon_threadsafe(self._deliver, queues, event)

    @staticmethod
    def _deliver(queues: list[asyncio.Queue[dict[str, Any]]], event: dict[str, Any]) -> None:
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.debug("dropping event for a slow stream subscriber")

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_SIZE)
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._queues.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                self._queues.discard(queue)


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
