"""The Jinja environment for the status and setup pages. Autoescaping is on for everything we render."""

import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).parent / "templates"


def format_timestamp(iso: str | None) -> str:
    """ "2026-10-24T13:11:46+00:00" -> "24 Oct 2026, 13:11 UTC" """
    if not iso:
        return "never"
    when = datetime.datetime.fromisoformat(iso).astimezone(datetime.UTC)
    return f"{when.day} {when:%b %Y, %H:%M} UTC"


env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(default=True),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
env.filters.update(timestamp=format_timestamp)


def render(name: str, **context: Any) -> str:
    return env.get_template(name).render(**context)
