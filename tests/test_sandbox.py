"""`command` sources run only through the sandbox, and the scheduler hands the resolver nothing else."""

import subprocess
from typing import Any

import pytest

from control import sandbox, scheduler
from control.github import GitHub
from control.vendor import resolve_version as rv

from .conftest import mock_client
from .test_scheduler import MANIFEST, FakeDispatch, FakeResolver, live


def test_run_refuses_without_the_wrapper(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(sandbox, "WRAPPER", tmp_path / "missing")
    assert not sandbox.available()
    with pytest.raises(sandbox.Unavailable):
        sandbox.run("echo 1")


def test_run_passes_the_script_on_stdin_and_maps_the_kill(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    wrapper = tmp_path / "run-version-command"
    wrapper.write_text('#!/bin/bash\nscript=$(cat)\nbash -c "$script"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(sandbox, "WRAPPER", wrapper)
    monkeypatch.setattr(sandbox, "RUNNER", [str(wrapper)])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/doas")
    done = sandbox.run("echo one; echo 2.0.1")
    assert done.returncode == 0 and done.stdout.split() == ["one", "2.0.1"]
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox.run("exit 137")


def test_resolve_with_token_runs_commands_in_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts: list[str] = []

    def fake(script: str) -> subprocess.CompletedProcess[str]:
        scripts.append(script)
        return subprocess.CompletedProcess(["x"], 0, "4.5.6\n", "")

    monkeypatch.setattr(sandbox, "run", fake)
    manifest = {"upstream": {"version": {"type": "command", "run": "echo 4.5.6"}}}
    assert scheduler.resolve_with_token(manifest, "ghs_token")["upstream"] == "4.5.6"
    assert scripts == ["echo 4.5.6"]
    assert rv.run_command is not fake


@pytest.mark.anyio
async def test_command_source_is_checked_where_the_sandbox_is(state: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: True)
    cmd = live(state, "cmd", {**MANIFEST, "upstream": {"version": {"type": "command", "run": "echo 9.9"}}})
    github = FakeDispatch()
    async with mock_client(github) as http:
        state.discord.client = http
        sched = scheduler.Scheduler(state, GitHub(http, "ghs_token"), resolver=FakeResolver("9.9"))
        decision = await sched.check_upstream(cmd)
    assert decision.outcome == "dispatch", decision.detail
    assert decision.detail["upstream"] == "9.9"
