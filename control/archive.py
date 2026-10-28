"""
The run archive: every live release keeps the record of the run that made it. GitHub deletes a run's logs after
90 days and the website renders pages only for the newest builds of a branch, so an older build is its GitHub
Release, and control's own run record links logs that will be gone. Once a released run has completed, control
attaches two assets to its Release:

- `run.json`, the run's structured record from the API (trigger, actor, commit, times, conclusion, every job with
  its runner and steps), never log text;
- `logs.zip`, the run's log archive as GitHub serves it. The logs of these public repositories are already public
  and masked by GitHub when written, so archiving them only makes them permanent. Before attaching them control
  still runs a cheap pattern check over the text for credential shapes (GitHub tokens, JWTs, `Authorization`
  headers, Discord webhook URLs) and its own events secret; on any match the logs stay off the Release, run.json
  still goes, and the log line names the kind of match and the file, never the match itself.

This is work for control, not a runner. It runs in the background, a few runs a minute, newest first: woken when a
run completes, and otherwise sweeping released runs not archived yet, which also covers older releases and a
`workflow_run` webhook that never came. Assets already on the Release are left alone, so it is idempotent; a
transient failure is retried with backoff, and after `run_archive_attempts` control gives up and says so.

Discord hears of give-ups and withheld logs grouped (`report`): at most one summary an hour, so a systematic failure
(every upload answering 403) is one message, not one per run. Each run still has its own log line.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import re
import urllib.parse
import zipfile
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx

from .db import utcnow
from .github import GitHub, installation_client
from .model import Build, RunArchive
from .state import State

log = logging.getLogger(__name__)

RUN_JSON = "run.json"
LOGS_ZIP = "logs.zip"
RUN_JSON_SCHEMA = 1
PASS_SECONDS = 60.0
"""How often the archiver sweeps when nothing wakes it."""
WAKE_DELAY_SECONDS = 30.0
"""A run's log archive can lag its `completed` webhook; give it that long before asking."""
SETTLE = datetime.timedelta(hours=1)
"""A released run whose completion webhook never arrived is tried anyway after this."""
BACKOFF_BASE = datetime.timedelta(minutes=5)
BACKOFF_MAX = datetime.timedelta(hours=6)
LOG_RETENTION = datetime.timedelta(days=89)
"""GitHub keeps a public repository's run logs 90 days; a run past that whose logs answer 404 has none left."""
MAX_UNCOMPRESSED = 4 * 1024**3
"""Log text past this is not scanned, and a log archive that is not scanned is not attached."""

PATTERNS: dict[str, re.Pattern[str]] = {
    "a GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"),
    "a JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # A credential in the header; `***` (masked), `$TOKEN` and `${{ … }}` are not one.
    "an Authorization header": re.compile(r"(?i)\bauthorization:\s*(?:bearer|token|basic)\s+[A-Za-z0-9._~+/-]{16,}"),
    "a Discord webhook URL": re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"),
}
SECRET_FINDING = "control's events secret"

REPORT_EVERY = datetime.timedelta(hours=1)
"""Give-ups and withheld logs go to Discord as one summary at most this often."""
REPORTED_AT = "run_archive_reported_at"
"""kv key: when the last summary was posted (or tried)."""
REPORT_SHOWN = 5
"""Runs named in a summary; the rest are counted."""


class Transient(Exception):
    """Worth another try later (GitHub unavailable, logs not ready yet)."""


def release_tag_of(build: Build) -> str | None:
    """The Release's tag: the last part of its URL (`…/releases/tag/<tag>`), else the build's version tag."""
    if build.release_url and "/releases/tag/" in build.release_url:
        return urllib.parse.unquote(build.release_url.rsplit("/releases/tag/", 1)[1].strip("/")) or build.version_tag
    return build.version_tag


# -- run.json -------------------------------------------------------------------------------------------------------


def _login(value: Any) -> str | None:
    return str(value["login"]) if isinstance(value, dict) and value.get("login") else None


def run_record(run: dict[str, Any], jobs: list[dict[str, Any]], build: Build, tag: str, now: str) -> dict[str, Any]:
    """The run's structured record: GitHub's own field names, trimmed to what says how the build ran."""
    commit: dict[str, Any] = run["head_commit"] if isinstance(run.get("head_commit"), dict) else {}
    author: dict[str, Any] = commit["author"] if isinstance(commit.get("author"), dict) else {}
    return {
        "schema": RUN_JSON_SCHEMA,
        "archived_at": now,
        "repository": build.repo,
        "release": {"tag": tag, "url": build.release_url},
        "build": {
            "image": build.image,
            "branch": build.branch,
            "mode": build.mode,
            "version_tag": build.version_tag,
            "release_tag": build.release_tag,
        },
        "run": {
            "id": run.get("id"),
            "name": run.get("name"),
            "path": run.get("path"),
            "run_number": run.get("run_number"),
            "run_attempt": run.get("run_attempt"),
            "html_url": run.get("html_url"),
            "display_title": run.get("display_title"),
            "event": run.get("event"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"),
            "run_started_at": run.get("run_started_at"),
            "updated_at": run.get("updated_at"),
        },
        "trigger": {
            "event": run.get("event"),
            # The build's own reason (upstream, packages, manual…), which the workflow_dispatch input carried.
            "reason": build.reason,
            "actor": _login(run.get("actor")),
            "triggering_actor": _login(run.get("triggering_actor")),
        },
        "head": {
            "branch": run.get("head_branch"),
            "sha": run.get("head_sha"),
            "message": commit.get("message"),
            "timestamp": commit.get("timestamp"),
            "author": author.get("name"),
        },
        "jobs": [
            {
                "id": job.get("id"),
                "name": job.get("name"),
                "html_url": job.get("html_url"),
                "run_attempt": job.get("run_attempt"),
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "created_at": job.get("created_at"),
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
                "runner_name": job.get("runner_name"),
                "runner_group_name": job.get("runner_group_name"),
                "labels": job.get("labels") or [],
                "steps": [
                    {
                        "number": step.get("number"),
                        "name": step.get("name"),
                        "status": step.get("status"),
                        "conclusion": step.get("conclusion"),
                        "started_at": step.get("started_at"),
                        "completed_at": step.get("completed_at"),
                    }
                    for step in job.get("steps") or []
                    if isinstance(step, dict)
                ],
            }
            for job in jobs
            if isinstance(job, dict)
        ],
    }


# -- the pattern check ----------------------------------------------------------------------------------------------


def scan_logs(content: bytes, secrets: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """(kind, file) for every file of a log archive that holds something credential-shaped; never the match itself.
    An archive that cannot be read or is too large to read raises ValueError: it is not attached unread."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("not a zip archive") from exc
    with archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if sum(m.file_size for m in members) > MAX_UNCOMPRESSED:
            raise ValueError("too large to scan")
        findings: list[tuple[str, str]] = []
        for member in members:
            kinds: set[str] = set()
            with archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8", errors="replace") as text:
                for line in text:
                    for kind, pattern in PATTERNS.items():
                        if kind not in kinds and pattern.search(line):
                            kinds.add(kind)
                    if SECRET_FINDING not in kinds and any(s in line for s in secrets):
                        kinds.add(SECRET_FINDING)
            findings.extend((kind, member.filename) for kind in sorted(kinds))
        return findings


def own_secrets(state: State) -> tuple[str, ...]:
    """The exact values control holds that a workflow could print: the events secret, which every build signs with."""
    secret = state.settings.events_secret
    return (secret,) if secret and len(secret) >= 8 else ()


# -- one run --------------------------------------------------------------------------------------------------------


async def _release(github: GitHub, repo: str, tag: str) -> dict[str, Any] | None:
    try:
        body = await github.get_json(f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return body if isinstance(body, dict) else None


async def _upload(github: GitHub, release: dict[str, Any], name: str, content: bytes, content_type: str) -> str:
    """`uploaded`, or `present` when another writer attached it first (GitHub answers 422 already_exists)."""
    try:
        await github.upload_asset(str(release["upload_url"]), name, content, content_type)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 422 and "already_exists" in exc.response.text:
            return "present"
        raise
    return "uploaded"


def _older_than_retention(run: dict[str, Any] | None, now: str) -> bool:
    finished = (run or {}).get("updated_at")
    if not isinstance(finished, str):
        return False
    try:
        at = datetime.datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.datetime.fromisoformat(now) - at > LOG_RETENTION


async def _logs(
    state: State, github: GitHub, repo: str, run: dict[str, Any] | None, now: str
) -> tuple[str, bytes | None]:
    """The run's log archive, or why there is none to attach (`expired`, `too-large`)."""
    run_id = (run or {}).get("id")
    response = await github.download(f"/repos/{repo}/actions/runs/{run_id}/logs")
    if response.status_code == 410 or (response.status_code == 404 and _older_than_retention(run, now)):
        return "expired", None
    if response.status_code == 404:
        raise Transient("logs not available yet")
    if response.status_code >= 400:
        raise Transient(f"logs answered {response.status_code}")
    content = response.content
    if len(content) > state.settings.run_archive_max_logs_mb * 1024 * 1024:
        log.warning("run archive: the logs of %s run %s are %s bytes, too large to attach", repo, run_id, len(content))
        return "too-large", None
    return "fetched", content


def _new_row(repo: str, run_id: int, tag: str | None, now: str) -> RunArchive:
    return RunArchive(
        repo=repo,
        run_id=run_id,
        release_tag=tag,
        state="retry",
        run_json=None,
        logs=None,
        attempts=0,
        next_attempt_at=None,
        note=None,
        created_at=now,
        updated_at=now,
    )


async def archive_run(state: State, github: GitHub, repo: str, run_id: int, now: Callable[[], str] = utcnow) -> None:
    """Archive one released run, or record why not. Raises on a transient failure, which the caller backs off."""
    build = state.model.build(repo, run_id)
    previous = state.model.run_archive(repo, run_id)
    if build is None or (tag := release_tag_of(build)) is None:
        return
    row = previous or _new_row(repo, run_id, tag, now())
    release = await _release(github, repo, tag)
    if release is None:
        state.model.upsert_run_archive(row.model_copy(update={"state": "skipped", "note": "no such release"}))
        log.info("run archive: %s has no release %s, skipping run %s", repo, tag, run_id)
        return
    names = {str(a.get("name")) for a in release.get("assets") or [] if isinstance(a, dict)}
    run_json = "present" if RUN_JSON in names else row.run_json
    logs = "present" if LOGS_ZIP in names else row.logs
    run: dict[str, Any] | None = None
    if run_json is None or logs is None:
        run = await github.get_json(f"/repos/{repo}/actions/runs/{run_id}")
        if not isinstance(run, dict) or run.get("status") != "completed":
            raise Transient("run not completed")
    if run_json is None and run is not None:
        jobs = await github.paginate(f"/repos/{repo}/actions/runs/{run_id}/jobs", {"per_page": 100}, key="jobs")
        record = run_record(run, jobs, build, tag, now())
        body = json.dumps(record, indent=2).encode() + b"\n"
        run_json = await _upload(github, release, RUN_JSON, body, "application/json")
        # Saved now, so a log failure that follows never uploads run.json twice.
        row = row.model_copy(update={"run_json": run_json, "updated_at": now()})
        state.model.upsert_run_archive(row)
    note = None
    if logs is None:
        fetched, content = await _logs(state, github, repo, run, now())
        logs = fetched
        if content is not None:
            try:
                findings = await asyncio.to_thread(scan_logs, content, own_secrets(state))
            except ValueError as exc:
                logs, note = "unreadable", str(exc)
                log.warning("run archive: the logs of %s run %s were not attached: %s", repo, run_id, exc)
            else:
                if findings:
                    logs, note = "withheld", describe(findings)
                    log.warning("run archive: the logs of %s run %s %s; logs.zip not attached", repo, run_id, note)
                else:
                    logs = await _upload(github, release, LOGS_ZIP, content, "application/zip")
    state.model.upsert_run_archive(
        row.model_copy(
            update={
                "release_tag": tag,
                "state": "done",
                "run_json": run_json,
                "logs": logs,
                "next_attempt_at": None,
                "note": note,
                "updated_at": now(),
            }
        )
    )
    log.info("run archive: %s %s run %s, run.json %s, logs.zip %s", repo, tag, run_id, run_json, logs)


def describe(findings: list[tuple[str, str]]) -> str:
    """`matched <kind>, <kind> in <file>, <file>`; `matched_kinds` reads the kinds back."""
    kinds = sorted({kind for kind, _ in findings})
    files = sorted({name for _, name in findings})
    shown = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
    return f"matched {', '.join(kinds)} in {shown}"


def matched_kinds(note: str | None) -> list[str]:
    """The kinds of match in a `describe` note (no kind holds `, ` or ` in `)."""
    if not note or not note.startswith("matched "):
        return []
    return [kind for kind in note.removeprefix("matched ").split(" in ", 1)[0].split(", ") if kind]


async def failed(state: State, repo: str, run_id: int, error: str, now: Callable[[], str] = utcnow) -> None:
    """Count a failed attempt; back off, or give up and say so after the configured number of tries."""
    row = state.model.run_archive(repo, run_id)
    build = state.model.build(repo, run_id)
    tag = release_tag_of(build) if build else None
    attempts = (row.attempts if row else 0) + 1
    at = datetime.datetime.fromisoformat(now())
    delay = min(BACKOFF_BASE * 2 ** (attempts - 1), BACKOFF_MAX)
    final = attempts >= state.settings.run_archive_attempts
    base = row or _new_row(repo, run_id, tag, now())
    state.model.upsert_run_archive(
        base.model_copy(
            update={
                "state": "failed" if final else "retry",
                "attempts": attempts,
                "next_attempt_at": None if final else (at + delay).isoformat(timespec="seconds"),
                "note": error,
                "updated_at": now(),
            }
        )
    )
    if final:
        log.error("run archive: gave up on %s run %s after %s attempts: %s", repo, run_id, attempts, error)
    else:
        log.warning("run archive: %s run %s attempt %s failed (%s), next in %s", repo, run_id, attempts, error, delay)


# -- the Discord summary --------------------------------------------------------------------------------------------


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _runs(rows: list[RunArchive]) -> str:
    named = [
        f"[{row.repo.rsplit('/', 1)[-1]} {row.release_tag or f'run {row.run_id}'}]"
        f"(<https://github.com/{row.repo}/actions/runs/{row.run_id}>)"
        for row in rows[:REPORT_SHOWN]
    ]
    more = len(rows) - REPORT_SHOWN
    return ", ".join(named) + (f" and {more} more" if more > 0 else "")


def _most_common(values: list[str]) -> str:
    return Counter(values).most_common(1)[0][0] if values else "unknown"


def summary(rows: list[RunArchive], attempts: int) -> str:
    """The grouped Discord post. Reasons are control's own notes (an HTTP status, an exception's class name, the
    kinds of credential matched), never log text or a match."""
    gave_up = [row for row in rows if row.state == "failed"]
    withheld = [row for row in rows if row.state != "failed" and row.logs == "withheld"]
    parts: list[str] = []
    if gave_up:
        parts.append(
            f"⚠️ **Run archive**: control gave up archiving {_plural(len(gave_up), 'run')} on "
            f"{'its release' if len(gave_up) == 1 else 'their releases'} after "
            f"{attempts} attempts; most common reason: {_most_common([row.note or 'unknown' for row in gave_up])}. "
            f"{_runs(gave_up)}"
        )
    if withheld:
        kinds = [kind for row in withheld for kind in matched_kinds(row.note)]
        parts.append(
            f"⚠️ **Run archive**: the logs of {_plural(len(withheld), 'run')} matched a credential pattern, so "
            f"`logs.zip` was not attached (`run.json` was); most common match: {_most_common(kinds)}. Check the runs "
            f"before their logs expire: {_runs(withheld)}"
        )
    return "\n\n".join(parts) + "\n\nEach run is in control's log (`run archive:`) and `run_archives`."


async def report(state: State, now: Callable[[], str] = utcnow) -> bool:
    """Post one summary of the runs given up on and the logs withheld since the last one, at most once per
    REPORT_EVERY: the first goes out at once, the rest wait for the hour. True when a summary went out. A post
    Discord refuses leaves the runs for the next summary."""
    at = now()
    last = state.db.get(REPORTED_AT)
    if last and datetime.datetime.fromisoformat(at) - datetime.datetime.fromisoformat(last) < REPORT_EVERY:
        return False
    rows = state.model.unreported_run_archives()
    if not rows:
        return False
    state.db.set(REPORTED_AT, at)
    posted = await state.discord.post(summary(rows, state.settings.run_archive_attempts))
    if posted is None and state.discord.configured:
        log.warning("run archive: the summary of %s runs did not post; it is tried again in an hour", len(rows))
        return False
    state.model.mark_run_archives_reported([(row.repo, row.run_id) for row in rows], at)
    return True


async def archive_once(state: State, now: Callable[[], str] = utcnow) -> int:
    """One sweep: archive up to `run_archive_per_pass` due runs. Returns how many were tried. Never raises."""
    github = await installation_client(state)
    if github is None:
        return 0
    at = now()
    settled = (datetime.datetime.fromisoformat(at) - SETTLE).isoformat(timespec="seconds")
    due = state.model.archive_candidates(at, settled, state.settings.run_archive_per_pass)
    for repo, run_id in due:
        try:
            await archive_run(state, github, repo, run_id, now)
        except asyncio.CancelledError:
            raise
        except Transient as exc:
            await failed(state, repo, run_id, str(exc), now)
        except httpx.HTTPStatusError as exc:
            await failed(state, repo, run_id, f"GitHub answered {exc.response.status_code}", now)
        except httpx.HTTPError as exc:
            await failed(state, repo, run_id, type(exc).__name__, now)
        except Exception as exc:  # one run's failure never stops the sweep
            log.exception("run archive of %s run %s failed", repo, run_id)
            await failed(state, repo, run_id, type(exc).__name__, now)
    try:
        await report(state, now)
    except Exception:
        log.exception("run archive summary failed")
    return len(due)


def wake(state: State, payload: dict[str, Any] | None = None) -> None:
    """From the webhook handler: a run completed, so archive it soon rather than at the next sweep."""
    if payload is None or payload.get("action") == "completed":
        state.archive_wakeup.set()


async def run(state: State) -> None:
    """The background task the app starts beside the server."""
    while True:
        try:
            await asyncio.wait_for(state.archive_wakeup.wait(), PASS_SECONDS)
            state.archive_wakeup.clear()
            await asyncio.sleep(WAKE_DELAY_SECONDS)
        except TimeoutError:
            pass
        try:
            await archive_once(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("run archive sweep failed")
