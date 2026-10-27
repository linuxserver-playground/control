"""
Serve on 0.0.0.0:8080 behind SWAG, which terminates TLS and sets X-Forwarded-*; trusting those from any
proxy is fine because the port is never published outside the compose network.

Usage: python -m control            serve
       python -m control replay     reprocess every stored delivery through the handlers
       python -m control backfill   fill the state tables from the GitHub API, once
       python -m control discover   read every live branch's image.yml from GitHub, once
       python -m control schedule --once [--image x] [--branch y] [--kind upstream|release|packages] [--dry-run]
                                    run one check now for every (or one) live branch: the upstream check
                                    by default, the release check, or a package check (--image required,
                                    so a typo cannot rebuild the fleet); --dry-run resolves and prints but
                                    records and dispatches nothing
       python -m control batch --once [--project x] [--dry-run]
                                    check every (or one) batch build project now: size it against the jobs
                                    running and dispatch it if it is live (CONTROL_BATCH_LIVE); --dry-run
                                    records and dispatches nothing
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable

import httpx
import uvicorn

from . import batch, handlers, scheduler
from .app import build_state
from .backfill import backfill_once
from .config import Settings
from .db import Database
from .fleet import discover_once
from .model import Model
from .state import State
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
    if command in ("backfill", "discover"):
        once = backfill_once if command == "backfill" else discover_once
        record = asyncio.run(_with_state(settings, db, once))
        print(record)
        return 1 if record.get("error") else 0
    if command == "schedule":
        return _schedule(settings, db, argv[1:])
    if command == "batch":
        return _batch(settings, db, argv[1:])
    print(__doc__, file=sys.stderr)
    return 2


def _schedule(settings: Settings, db: Database, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m control schedule")
    parser.add_argument("--once", action="store_true", required=True, help="run the check now and exit")
    parser.add_argument("--image", help="only this image (name or repo)")
    parser.add_argument("--branch", help="only this branch")
    parser.add_argument(
        "--kind",
        choices=(scheduler.KIND_UPSTREAM, scheduler.KIND_RELEASE, scheduler.KIND_PACKAGES),
        default=scheduler.KIND_UPSTREAM,
        help="which check: upstream (default), release, or packages (forces one and resets the weekly slot)",
    )
    parser.add_argument("--dry-run", action="store_true", help="resolve and print; record and dispatch nothing")
    args = parser.parse_args(argv)
    if args.kind == scheduler.KIND_PACKAGES and not args.image:
        parser.error("--kind packages needs --image: it dispatches a build for every branch matched")

    async def once(state: State) -> list[scheduler.Decision]:
        return await scheduler.run_once(state, args.image, args.branch, args.dry_run, args.kind)

    decisions = asyncio.run(_with_state(settings, db, once))
    for decision in decisions:
        print(("would " if args.dry_run and decision.outcome == "dispatch" else "") + decision.line())
    if not decisions:
        print("no live branches matched; run `python -m control discover` or check --image/--branch")
    return 1 if any(d.outcome == "error" for d in decisions) else 0


def _batch(settings: Settings, db: Database, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m control batch")
    parser.add_argument("--once", action="store_true", required=True, help="run the check now and exit")
    parser.add_argument("--project", choices=[p.repo for p in batch.PROJECTS], help="only this project")
    parser.add_argument("--dry-run", action="store_true", help="size and print; record and dispatch nothing")
    args = parser.parse_args(argv)

    async def once(state: State) -> list[scheduler.Decision]:
        return await scheduler.run_batch_once(state, args.project, args.dry_run)

    decisions = asyncio.run(_with_state(settings, db, once))
    for decision in decisions:
        print(decision.line())
    return 1 if any(d.outcome == "error" for d in decisions) else 0


async def _with_state[T](settings: Settings, db: Database, run: Callable[[State], Awaitable[T]]) -> T:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": f"lsio-control/{package_version()}"}) as http:
        return await run(build_state(settings, db, http))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
