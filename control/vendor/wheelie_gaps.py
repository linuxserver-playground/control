# Vendored from linuxserver-playground/wheelie scripts/gaps.py at commit
# 8019270ccaf2582021d51244d08b497bfa751bac.
# Do not edit here: change it in the wheelie repo and copy it back. tests/test_vendor.py fails
# when this file and the source checkout differ, so control and the Build workflow find the same gaps.
# End of vendoring header.
#!/usr/bin/env python3
"""Decide which wheels wheelie has to build.

A package needs a build for a distro and arch when the version pip would install there (the newest release on
PyPI, or the pinned one) has no compatible wheel on PyPI and none in our index either. Compatible means:

- platform: `any`; or the distro's libc family (musllinux on Alpine, manylinux on Ubuntu) for the arch, at or
  below the distro's libc version; or, in our own index only, `linux_<arch>` (built on that distro).
- interpreter: the distro's CPython exactly (`cp314-cp314`, `cp314-none`), the stable ABI at or below it
  (`cp311-abi3`), or a pure `py3`/`py3X` tag with no ABI.

The distros' CPython and libc versions come from `distros.json`, so this needs nothing but HTTP: no Docker and
no runner. The control plane can import `find_gaps` and pass its own fetchers.

    gaps.py                         # JSON list of {distro, arch, packages}, checking the Pages index
    gaps.py --index-dir docs        # read the index pages from a checkout instead
    gaps.py --packages bluepy --rebuild --matrix   # ignore our index; emit a GitHub Actions matrix
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parent.parent
PYPI = "https://pypi.org/pypi"
INDEX_URL = "https://linuxserver-playground.github.io/wheelie"
USER_AGENT = "linuxserver-playground-wheelie (+https://github.com/linuxserver-playground/wheelie)"

LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
LIBC_FAMILY = {"glibc": "manylinux", "musl": "musllinux"}

Fetch = Callable[[str], "bytes | None"]


def canonical(name: str) -> str:
    """PEP 503 project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def version_key(version: str) -> str:
    """Wheel filenames write a version's `-` as `_`; compare on one spelling."""
    return version.replace("_", "-").lower()


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def parse_wheel(filename: str) -> dict | None:
    """Split a wheel filename into name, version and its expanded (python, abi, platform) tags."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return None
    py, abi, plat = parts[-3:]
    return {
        "name": canonical(parts[0]),
        "version": version_key(parts[1]),
        "tags": {(p, a, pl) for p in py.split(".") for a in abi.split(".") for pl in plat.split(".")},
    }


def python_ok(py: str, abi: str, python: tuple[int, int]) -> bool:
    match = re.fullmatch(r"(cp|py)(\d)(\d*)", py)
    if not match or int(match.group(2)) != python[0]:
        return False
    kind, minor = match.group(1), match.group(3)
    if kind == "py":
        return abi == "none" and (not minor or int(minor) <= python[1])
    if not minor:
        return False
    if abi == "abi3":
        return int(minor) <= python[1]
    return int(minor) == python[1] and abi in (f"cp{python[0]}{python[1]}", "none")


def platform_ok(plat: str, arch: str, libc: str, libc_version: tuple[int, ...], allow_linux: bool) -> bool:
    if plat == "any":
        return True
    if plat == f"linux_{arch}":
        return allow_linux
    match = re.fullmatch(r"(manylinux|musllinux)_(\d+)_(\d+)_(.+)", plat)
    if match:
        return (
            match.group(1) == LIBC_FAMILY[libc]
            and match.group(4) == arch
            and (int(match.group(2)), int(match.group(3))) <= libc_version
        )
    match = re.fullmatch(r"(manylinux1|manylinux2010|manylinux2014)_(.+)", plat)
    if match:
        return libc == "glibc" and match.group(2) == arch and LEGACY_MANYLINUX[match.group(1)] <= libc_version
    return False


def compatible(filename: str, distro: dict, arch: str, allow_linux: bool = False) -> bool:
    """Whether pip on this distro and arch (`x86_64`, `aarch64`) would install this wheel."""
    wheel = parse_wheel(filename)
    if wheel is None:
        return False
    python = as_tuple(distro["python"])
    libc_version = as_tuple(distro["libc_version"])
    return any(
        python_ok(py, abi, python) and platform_ok(plat, arch, distro["libc"], libc_version, allow_linux)
        for py, abi, plat in wheel["tags"]
    )


def python_satisfies(requires: str | None, python: str) -> bool:
    """Evaluate Requires-Python for CPython `X.Y` (taken as X.Y.0, enough for the specifiers in use)."""
    if not requires:
        return True
    have = as_tuple(python) + (0,)
    for clause in requires.split(","):
        clause = clause.strip()
        match = re.fullmatch(r"(~=|===|==|!=|<=|>=|<|>)\s*([0-9.*]+)", clause)
        if not match:
            continue
        op, value = match.groups()
        if value.endswith(".*"):
            prefix = as_tuple(value[:-2])
            same = have[: len(prefix)] == prefix
            if (op == "==" and not same) or (op == "!=" and same):
                return False
            continue
        want = as_tuple(value)
        width = max(len(want), len(have))
        a = have + (0,) * (width - len(have))
        b = want + (0,) * (width - len(want))
        ok = {
            "==": a == b, "===": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
            "~=": a >= b and a[: len(want) - 1] == b[: len(want) - 1],
        }[op]
        if not ok:
            return False
    return True


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def index_links(page: str) -> dict[str, str]:
    """Map each wheel filename on an index page to its href."""
    parser = _Anchors()
    parser.feed(page)
    links: dict[str, str] = {}
    for href in parser.hrefs:
        filename = urllib.parse.unquote(urllib.parse.urlparse(href).path.rsplit("/", 1)[-1])
        if filename.endswith(".whl"):
            links.setdefault(filename, href)
    return links


def http_fetch(url: str) -> bytes | None:
    """GET a URL; None on 404."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def split_spec(spec: str) -> tuple[str, str | None]:
    """`name` or `name==version`."""
    name, _, version = spec.partition("==")
    return name.strip(), (version.strip() or None)


def pypi_release(spec: str, fetch: Fetch = http_fetch) -> dict:
    """The release pip would pick (newest, or pinned): its version, Requires-Python and wheel filenames."""
    name, version = split_spec(spec)
    url = f"{PYPI}/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json" if version else f"{PYPI}/{urllib.parse.quote(name)}/json"
    raw = fetch(url)
    if raw is None:
        raise LookupError(f"{spec} is not on PyPI")
    data = json.loads(raw)
    files = [f for f in data.get("urls", []) if not f.get("yanked")]
    return {
        "name": name,
        "version": data["info"]["version"],
        "requires_python": data["info"].get("requires_python"),
        "files": [f["filename"] for f in files],
    }


def load_config(path: Path = ROOT / "distros.json") -> dict:
    return json.loads(path.read_text())


def read_packages(path: Path = ROOT / "packages.txt") -> list[str]:
    return [line.split("#", 1)[0].strip() for line in path.read_text().splitlines() if line.split("#", 1)[0].strip()]


def find_gaps(
    config: dict,
    packages: Iterable[str],
    index_fetch: Callable[[str], "str | None"] | None,
    pypi_fetch: Fetch = http_fetch,
    distros: Iterable[str] | None = None,
    log: Callable[[str], None] = lambda message: None,
) -> list[dict]:
    """Return [{distro, arch, packages}] for every distro and arch that needs at least one build.

    `index_fetch(index)` returns the HTML of our index page for that index name (None when it does not exist);
    pass None to ignore our index (a rebuild). Packages come back pinned (`name==version`)."""
    wanted = list(distros) if distros else list(config["distros"])
    unknown = [d for d in wanted if d not in config["distros"]]
    if unknown:
        raise ValueError(f"unknown distro(s): {', '.join(unknown)}")
    releases = {spec: pypi_release(spec, pypi_fetch) for spec in packages}
    pages: dict[str, dict[str, str]] = {}
    result = []
    for distro_name in wanted:
        distro = config["distros"][distro_name]
        if index_fetch is not None and distro["index"] not in pages:
            pages[distro["index"]] = index_links(index_fetch(distro["index"]) or "")
        for arch_name, arch in config["arches"].items():
            needed = []
            for release in releases.values():
                where = f"{release['name']} {release['version']} on {distro_name} {arch_name}"
                if not python_satisfies(release["requires_python"], distro["python"]):
                    log(f"{where}: requires Python {release['requires_python']}, skipped")
                    continue
                if any(compatible(f, distro, arch["platform"]) for f in release["files"]):
                    log(f"{where}: PyPI has a wheel")
                    continue
                if index_fetch is not None:
                    ours = [
                        f for f in pages[distro["index"]]
                        if (w := parse_wheel(f)) and w["name"] == canonical(release["name"])
                        and w["version"] == version_key(release["version"])
                        and compatible(f, distro, arch["platform"], allow_linux=True)
                    ]
                    if ours:
                        log(f"{where}: in our index ({ours[0]})")
                        continue
                log(f"{where}: needs a build")
                needed.append(f"{release['name']}=={release['version']}")
            if needed:
                result.append({"distro": distro_name, "arch": arch_name, "packages": needed})
    return result


def as_matrix(config: dict, gaps: list[dict]) -> dict:
    """The build job's matrix: one leg per distro and arch."""
    include = []
    for gap in gaps:
        distro = config["distros"][gap["distro"]]
        include.append({
            "distro": gap["distro"],
            "arch": gap["arch"],
            "runner": config["arches"][gap["arch"]]["runner"],
            "image": distro["image"],
            "index": distro["index"],
            "packages": " ".join(gap["packages"]),
        })
    return {"include": include}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--packages", help="space or comma separated packages (name or name==version); default packages.txt")
    parser.add_argument("--distros", help="space or comma separated distros to check; default all in distros.json")
    parser.add_argument("--index-dir", type=Path, help="read <dir>/<index>/index.html instead of the Pages index")
    parser.add_argument("--index-url", default=INDEX_URL, help=f"index base URL (default {INDEX_URL})")
    parser.add_argument("--rebuild", action="store_true", help="ignore our index: build wherever PyPI has no wheel")
    parser.add_argument("--matrix", action="store_true", help="print a GitHub Actions matrix instead of the list")
    parser.add_argument("--config", type=Path, default=ROOT / "distros.json")
    parser.add_argument("-q", "--quiet", action="store_true", help="no reasoning on stderr")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    packages = re.split(r"[\s,]+", args.packages.strip()) if args.packages and args.packages.strip() else read_packages()
    distros = re.split(r"[\s,]+", args.distros.strip()) if args.distros and args.distros.strip() else None

    if args.rebuild:
        index_fetch = None
    elif args.index_dir:
        def index_fetch(index: str) -> str | None:
            page = args.index_dir / index / "index.html"
            return page.read_text() if page.exists() else None
    else:
        def index_fetch(index: str) -> str | None:
            raw = http_fetch(f"{args.index_url.rstrip('/')}/{index}/")
            return raw.decode() if raw is not None else None

    log = (lambda message: None) if args.quiet else (lambda message: print(message, file=sys.stderr))
    gaps = find_gaps(config, packages, index_fetch, http_fetch, distros=distros, log=log)
    print(json.dumps(as_matrix(config, gaps) if args.matrix else gaps, separators=(",", ":") if args.matrix else None, indent=None if args.matrix else 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
