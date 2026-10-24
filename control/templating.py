"""The Jinja environment for every page. Autoescaping is on for everything we render."""

import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).parent / "templates"


def package_version() -> str:
    try:
        return version("lsio-control")
    except PackageNotFoundError:
        return "dev"


def format_timestamp(iso: str | None) -> str:
    """ "2026-10-24T13:11:46+00:00" -> "24 Oct 2026, 13:11 UTC" """
    if not iso:
        return "never"
    when = datetime.datetime.fromisoformat(iso).astimezone(datetime.UTC)
    return f"{when.day} {when:%b %Y, %H:%M} UTC"


def format_duration(seconds: float | None) -> str:
    """9.4 -> "9s", 545 -> "9m 5s", 7320 -> "2h 2m", 200000 -> "2d 7h"."""
    if seconds is None:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def format_ago(iso: str | None, now: datetime.datetime | None = None) -> str:
    """Relative age for tables: "3m ago", "2d 7h ago"; "never" for None."""
    if not iso:
        return "never"
    when = datetime.datetime.fromisoformat(iso)
    at = now or datetime.datetime.now(datetime.UTC)
    return f"{format_duration((at - when).total_seconds())} ago"


def span(start: str | None, end: str | None, now: datetime.datetime | None = None) -> float | None:
    """Seconds between two ISO timestamps; an open interval runs to now."""
    if not start:
        return None
    begin = datetime.datetime.fromisoformat(start)
    finish = datetime.datetime.fromisoformat(end) if end else (now or datetime.datetime.now(datetime.UTC))
    return max(0.0, (finish - begin).total_seconds())


def kind(status: str | None, conclusion: str | None = None) -> str:
    """Map a GitHub status/conclusion (or a build result) to a pill class: ok, bad, warn, neutral."""
    value = (conclusion or status or "").lower()
    if value in ("success", "passing", "passed", "approved", "open", "merged"):
        return "ok"
    if value in (
        "failure",
        "failing",
        "failed",
        "timed_out",
        "startup_failure",
        "action_required",
        "changes_requested",
    ):
        return "bad"
    if value in ("in_progress", "running", "queued", "waiting", "pending", "requested", "stuck"):
        return "warn"
    return "neutral"


def short_sha(sha: str | None) -> str:
    return (sha or "")[:7]


env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(default=True),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
env.filters.update(timestamp=format_timestamp, duration=format_duration, ago=format_ago, kind=kind, sha=short_sha)
env.globals.update(kind=kind, span=span)


def render(name: str, **context: Any) -> str:
    return env.get_template(name).render(version=package_version(), **context)
