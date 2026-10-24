"""The vendored resolver must equal the workflows repo's copy, so the scheduler and the build agree."""

from pathlib import Path

import pytest

from control.vendor import resolve_version as rv

VENDORED = Path(__file__).resolve().parents[1] / "control" / "vendor" / "resolve_version.py"
SOURCE = Path(__file__).resolve().parents[2] / "workflows" / "scripts" / "resolve_version.py"
HEADER_END = "# End of vendoring header.\n"


def test_vendored_header_names_source_and_commit() -> None:
    text = VENDORED.read_text()
    header, _, _ = text.partition(HEADER_END)
    assert "linuxserver-playground/workflows scripts/resolve_version.py" in header
    assert any(len(word.strip(".")) == 40 for word in header.split()), "header names no 40-hex commit"


@pytest.mark.skipif(not SOURCE.exists(), reason="workflows checkout not beside control")
def test_vendored_file_equals_source() -> None:
    _, found, body = VENDORED.read_text().partition(HEADER_END)
    assert found, "vendoring header marker missing"
    assert body == SOURCE.read_text(), (
        "control/vendor/resolve_version.py drifted from workflows/scripts/resolve_version.py; copy it back "
        "and update the commit in the header"
    )


def test_vendored_module_is_the_resolver() -> None:
    assert rv.sanitize("1.2.3~beta+1") == "1.2.3beta1"
    assert rv.derive_semver("v4.0.20.3014") == "4.0.20"
    assert issubclass(rv.AssetsNotReady, rv.ResolveError)
