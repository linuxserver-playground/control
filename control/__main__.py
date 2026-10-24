"""
Serve on 0.0.0.0:8080 behind SWAG, which terminates TLS and sets X-Forwarded-*; trusting those from any
proxy is fine because the port is never published outside the compose network.

Usage: python -m control            serve
       python -m control replay     reprocess every stored delivery through the handlers
       python -m control backfill   fill the state tables from the GitHub API, once
"""

import asyncio
import logging
import sys

import httpx
import uvicorn

from . import handlers
from .app import build_state
from .backfill import backfill_once
from .config import Settings
from .db import Database
from .model import Model
from .stream import Broadcaster
from .templating import package_version


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    command = argv[0] if argv else "serve"
    if command == "serve":
        uvicorn.run(
            "control.app:create_app",
            factory=True,
            host="0.0.0.0",
            port=8080,
            proxy_headers=True,
            forwarded_allow_ips="*",
            log_config=None,
        )
        return 0
    settings = Settings.from_env()
    db = Database(settings.db_path)
    db.migrate()
    if command == "replay":
        processed, applied = handlers.replay(Model(db), Broadcaster())
        print(f"replayed {processed} deliveries, {applied} applied")
        return 0
    if command == "backfill":
        record = asyncio.run(_backfill(settings, db))
        print(record)
        return 1 if record.get("error") else 0
    print(__doc__, file=sys.stderr)
    return 2


async def _backfill(settings: Settings, db: Database) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": f"lsio-control/{package_version()}"}) as http:
        return await backfill_once(build_state(settings, db, http))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
