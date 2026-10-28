"""
`command` version sources, run for the scheduler. A command is shell from an image repository's live branch,
which only the org can push, but it still never runs as control's own user: that user owns /config (the GitHub
App key, the database) and control's environment holds its secrets. `run-version-command` (in root/) runs it as
nobody through doas, with an empty environment, its own empty directory, resource limits and a hard kill after
120 seconds. Where the wrapper is missing (a checkout, the tests) commands are not run at all.
"""

import shutil
import subprocess
from pathlib import Path

WRAPPER = Path("/usr/local/bin/run-version-command")
RUNNER = ["doas", "-u", "nobody", str(WRAPPER)]
WAIT = 150
"""Seconds to wait for the wrapper, whose own kill comes at 120: control cannot signal nobody's processes."""


class Unavailable(RuntimeError):
    """No sandbox here, so the command is not run."""


def available() -> bool:
    return WRAPPER.is_file() and shutil.which("doas") is not None


def run(script: str) -> subprocess.CompletedProcess[str]:
    """The resolver's `run_command`: the command's exit code and output, as if bash had run it."""
    if not available():
        raise Unavailable(f"{WRAPPER} or doas is missing; command sources only run in the control image")
    proc = subprocess.Popen(RUNNER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(script, timeout=WAIT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    # timeout -s KILL exits 137 when it kills the command.
    if proc.returncode == 137:
        raise subprocess.TimeoutExpired(RUNNER, 120)
    return subprocess.CompletedProcess(RUNNER, proc.returncode, out, err)
