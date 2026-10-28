"""
GitHub Pages on every image repository, checked, never changed. Each image's build deploys its heavy files (the
blobs a Release serves without CORS) to its own repository's Pages, so an image repo without a Pages site builds and
releases but its file trees, package lists and test files never show on the website. Enabling Pages and opening the
`github-pages` environment is a maintainer's step of the new-image recipe; control holds no permission for it.

Fleet discovery already lists the org's repositories, and each carries `has_pages` with plain metadata access. For
every image repo (not archived, image.yml on its default branch) without a site, control logs one line and posts
to Discord once, recorded in `repo_pages`; the row goes when the site exists, so a repo that loses it again is
reported again. The discovery record (`last_discovery.pages_missing` in /api/status.json) lists the repos still
without one on every pass.
"""

from __future__ import annotations

import logging

from .state import State

log = logging.getLogger(__name__)

LEGACY_KV = "pages_permissions_missing"
"""kv key the removed Pages setup kept; deleted when found."""


def message(repo: str) -> str:
    return (
        f"{repo} has no GitHub Pages site: its build files will not show on the website. Enable it as the new-image "
        "recipe says (Pages from GitHub Actions, the github-pages environment open to every branch)."
    )


async def check(state: State, has_pages: dict[str, bool]) -> list[str]:
    """Report every image repo of `has_pages` without a site once, clear the ones that have one, and return the
    repos still without one. Repos discovery did not read this pass keep their state. Never raises."""
    if state.db.get(LEGACY_KV) is not None:
        state.db.delete(LEGACY_KV)
    reported = state.model.pages_missing()
    missing: list[str] = []
    for repo, enabled in sorted(has_pages.items()):
        if enabled:
            if repo in reported:
                state.model.clear_pages_missing(repo)
                log.info("%s has a GitHub Pages site now", repo)
            continue
        missing.append(repo)
        if repo in reported:
            continue
        text = message(repo)
        log.warning("%s", text)
        state.model.report_pages_missing(repo, text)
        await state.discord.post(f"⚠️ {text}")
    return missing
