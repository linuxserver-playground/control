# Vendored from linuxserver-playground/workflows scripts/resolve_version.py at commit
# f1d031d7764634666c8a1352418ddd2a8b58f074.
# Do not edit here: change it in the workflows repo and copy it back. tests/test_vendor.py fails
# when this file and the source checkout differ, so the build and the scheduler resolve alike.
# End of vendoring header.
#!/usr/bin/env python3
"""
Resolve the upstream version of an image from its manifest's ``upstream.version`` block.

This module is shared between the build (the ``resolve-version`` composite action) and the
control plane's scheduler, which imports it to decide whether a new build is due. It therefore
has no GitHub Actions coupling: inputs are the manifest (a dict, or a path to image.yml /
image.json) and plain environment variables; the output is a dict.

    from resolve_version import resolve
    result = resolve(manifest, manifest_dir=Path("."))
    # {"upstream": "v4.0.20.3014", "upstream_clean": "v4.0.20.3014", "semver": "4.0.20",
    #  "source": "json", "source_url": "https://services.sonarr.tv/v1/releases"}

Every ``version_source.type`` of image.schema.json is implemented. HTTP goes through ``http_get``
so tests (and the control plane, if it wants caching) can replace it.

Only the standard library is required. PyYAML is imported lazily when a ``.yml`` path is given;
``jq`` and ``docker`` are invoked as binaries for the ``json`` and ``deb_repo`` sources.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Characters removed from the upstream version to form the docker tag (CONVENTIONS §3).
SANITIZE_RE = re.compile(r"[~,%@+;:/ ]")

USER_AGENT = "linuxserver-playground-workflows/1 (+https://github.com/linuxserver-playground/workflows)"

# Alpine repo architecture directory per build platform.
ALPINE_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}


class ResolveError(RuntimeError):
    """The version could not be determined."""


class AssetsNotReady(ResolveError):
    """A ``require_assets`` URL is not yet published. The scheduler treats this as 'try later'."""


# --------------------------------------------------------------------------------------------
# HTTP


class HttpResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None, url: str = "") -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.url = url

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _default_http_get(url: str, headers: dict[str, str] | None = None, method: str = "GET", timeout: int = 60) -> HttpResponse:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URLs come from the manifest
            body = b"" if method == "HEAD" else resp.read()
            return HttpResponse(resp.status, body, dict(resp.headers.items()), resp.geturl())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() if method != "HEAD" else b"", dict(exc.headers.items()), url)


# Tests and the control plane may replace this.
http_get: Callable[..., HttpResponse] = _default_http_get


def github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_ok(url: str, headers: dict[str, str] | None = None) -> HttpResponse:
    resp = http_get(url, headers=headers)
    if resp.status != 200:
        raise ResolveError(f"GET {url} returned HTTP {resp.status}")
    return resp


# --------------------------------------------------------------------------------------------
# Sources. Each returns (version, source_url).


def _github_release(src: dict[str, Any]) -> tuple[str, str]:
    repo = src["repo"]
    want_prerelease = bool(src.get("prerelease", False))
    exclude = re.compile(src["exclude"]) if src.get("exclude") else None
    resp = _get_ok(f"https://api.github.com/repos/{repo}/releases?per_page=100", github_headers())
    for rel in resp.json():
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not want_prerelease:
            continue
        tag = rel.get("tag_name") or ""
        if exclude and exclude.search(tag):
            continue
        return tag, rel.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}"
    raise ResolveError(f"no matching release found for github repo {repo}")


def _github_commit(src: dict[str, Any]) -> tuple[str, str]:
    repo, branch = src["repo"], src["branch"]
    resp = _get_ok(f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(branch, safe='')}", github_headers())
    sha = resp.json()["sha"]
    return sha[:8], f"https://github.com/{repo}/commit/{sha}"


def _gitlab_commit(src: dict[str, Any]) -> tuple[str, str]:
    project_id, branch = src["project_id"], src["branch"]
    url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/commits/{urllib.parse.quote(branch, safe='')}"
    data = _get_ok(url).json()
    sha = data["id"]
    web = data.get("web_url") or (f"{src['web_url'].rstrip('/')}/-/commit/{sha}" if src.get("web_url") else url)
    return sha[:8], web


def _gitea_like_release(api_base: str, repo: str, web_base: str) -> tuple[str, str]:
    resp = _get_ok(f"{api_base}/repos/{repo}/releases?limit=50")
    releases = [r for r in resp.json() if not r.get("draft")]
    stable = [r for r in releases if not r.get("prerelease")]
    chosen = (stable or releases or [None])[0]
    if not chosen:
        raise ResolveError(f"no release found for {repo} at {api_base}")
    tag = chosen["tag_name"]
    return tag, chosen.get("html_url") or f"{web_base}/{repo}/releases/tag/{tag}"


def _codeberg_release(src: dict[str, Any]) -> tuple[str, str]:
    return _gitea_like_release("https://codeberg.org/api/v1", src["repo"], "https://codeberg.org")


def _gitea_release(src: dict[str, Any]) -> tuple[str, str]:
    base = src["base_url"].rstrip("/")
    return _gitea_like_release(f"{base}/api/v1", src["repo"], base)


def run_jq(expr: str, body: bytes) -> str:
    """Apply a jq expression with the jq binary; -r so strings come back bare."""
    try:
        proc = subprocess.run(["jq", "-r", expr], input=body, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise ResolveError("jq binary not found; the json source needs it") from exc
    if proc.returncode != 0:
        raise ResolveError(f"jq failed: {proc.stderr.decode(errors='replace').strip()}")
    lines = [line for line in proc.stdout.decode().splitlines() if line.strip()]
    if not lines:
        raise ResolveError(f"jq expression {expr!r} produced no output")
    return lines[0].strip()


def _json(src: dict[str, Any]) -> tuple[str, str]:
    url = src["url"]
    headers = github_headers() if "api.github.com" in url else None
    resp = _get_ok(url, headers)
    return run_jq(src["jq"], resp.body), url


def _npm(src: dict[str, Any]) -> tuple[str, str]:
    package = src["package"]
    dist_tag = src.get("dist_tag", "latest")
    data = _get_ok(f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}").json()
    try:
        version = data["dist-tags"][dist_tag]
    except KeyError as exc:
        raise ResolveError(f"npm package {package} has no dist-tag {dist_tag}") from exc
    return version, f"https://www.npmjs.com/package/{package}"


def _pip(src: dict[str, Any]) -> tuple[str, str]:
    package = src["package"]
    data = _get_ok(f"https://pypi.org/pypi/{package}/json").json()
    return data["info"]["version"], f"https://pypi.org/project/{package}/"


def parse_apkindex(data: bytes) -> dict[str, str]:
    """APKINDEX (the text file inside APKINDEX.tar.gz) -> {package: version}."""
    versions: dict[str, str] = {}
    name = None
    for line in data.decode("utf-8", errors="replace").split("\n"):
        if line.startswith("P:"):
            name = line[2:].strip()
        elif line.startswith("V:") and name:
            versions[name] = line[2:].strip()
        elif not line.strip():
            name = None
    return versions


def _apkindex_from_tgz(body: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith("APKINDEX")), None)
        if member is None:
            raise ResolveError("APKINDEX.tar.gz does not contain an APKINDEX file")
        extracted = tar.extractfile(member)
        return extracted.read() if extracted else b""


def _alpine_apk(src: dict[str, Any], platforms: list[str]) -> tuple[str, str]:
    repo_url = src["repo_url"].rstrip("/")
    package = src["package"]
    found: dict[str, str] = {}
    for platform in platforms or ["amd64"]:
        arch = ALPINE_ARCH.get(platform)
        if not arch:
            raise ResolveError(f"no alpine architecture for platform {platform}")
        url = f"{repo_url}/{arch}/APKINDEX.tar.gz"
        index = parse_apkindex(_apkindex_from_tgz(_get_ok(url).body))
        if package not in index:
            raise ResolveError(f"package {package} not in {url}")
        found[arch] = index[package]
    versions = set(found.values())
    if len(versions) != 1:
        raise AssetsNotReady(f"alpine package {package} disagrees across architectures: {found}")
    return versions.pop(), f"{repo_url}/"


def _decompress_by_url(url: str, body: bytes) -> bytes:
    if url.endswith(".gz"):
        return gzip.decompress(body)
    if url.endswith(".xz"):
        return lzma.decompress(body)
    return body


def _deb_cmp_part(a: str, b: str) -> int:
    """Compare one upstream/revision component the way dpkg does."""

    def order(c: str) -> int:
        if c == "~":
            return -1
        if c.isdigit():
            return 0
        if c.isalpha():
            return ord(c)
        return ord(c) + 256

    while a or b:
        # Non-digit prefix.
        ia = ib = 0
        while ia < len(a) and not a[ia].isdigit():
            ia += 1
        while ib < len(b) and not b[ib].isdigit():
            ib += 1
        pa, pb = a[:ia], b[:ib]
        for x, y in zip(pa.ljust(max(len(pa), len(pb))), pb.ljust(max(len(pa), len(pb))), strict=True):
            ox, oy = (order(x) if x != " " else 0), (order(y) if y != " " else 0)
            if ox != oy:
                return -1 if ox < oy else 1
        a, b = a[ia:], b[ib:]
        # Digit run.
        ia = ib = 0
        while ia < len(a) and a[ia].isdigit():
            ia += 1
        while ib < len(b) and b[ib].isdigit():
            ib += 1
        na, nb = int(a[:ia] or "0"), int(b[:ib] or "0")
        if na != nb:
            return -1 if na < nb else 1
        a, b = a[ia:], b[ib:]
    return 0


def deb_version_key(version: str) -> Any:
    """Sort key wrapper for Debian version strings (epoch:upstream-revision)."""
    import functools

    def cmp(v1: str, v2: str) -> int:
        def split(v: str) -> tuple[int, str, str]:
            epoch, _, rest = v.partition(":") if ":" in v else ("0", "", v)
            upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
            return int(epoch or 0), upstream, revision

        e1, u1, r1 = split(v1)
        e2, u2, r2 = split(v2)
        if e1 != e2:
            return -1 if e1 < e2 else 1
        c = _deb_cmp_part(u1, u2)
        if c:
            return c
        return _deb_cmp_part(r1, r2)

    return functools.cmp_to_key(cmp)(version)


def parse_deb_packages(text: str) -> dict[str, list[str]]:
    """A Debian Packages index -> {package: [versions]}."""
    result: dict[str, list[str]] = {}
    for stanza in re.split(r"\n\s*\n", text):
        name = version = None
        for line in stanza.splitlines():
            if line.startswith("Package:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
        if name and version:
            result.setdefault(name, []).append(version)
    return result


def _deb_packages(src: dict[str, Any]) -> tuple[str, str]:
    url = src["index_url"]
    body = _decompress_by_url(url, _get_ok(url).body)
    packages = parse_deb_packages(body.decode("utf-8", errors="replace"))
    versions = packages.get(src["package"])
    if not versions:
        raise ResolveError(f"package {src['package']} not in {url}")
    return max(versions, key=deb_version_key), url


def _deb_repo(src: dict[str, Any]) -> tuple[str, str]:
    script = (
        f"echo -e {json.dumps(src['sources'])} > /etc/apt/sources.list.d/check.list"
        " && apt-get --allow-unauthenticated update -qq >/dev/null 2>&1"
        f" && apt-cache --no-all-versions show {src['packages']} | md5sum | cut -c1-8"
    )
    proc = subprocess.run(["docker", "run", "--rm", src["image"], "bash", "-c", script], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ResolveError(f"deb_repo resolution failed: {proc.stderr.strip()}")
    version = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{8}", version):
        raise ResolveError(f"deb_repo produced an unexpected value {version!r}")
    return version, f"deb_repo:{src['image']}"


def _blob(src: dict[str, Any]) -> tuple[str, str]:
    url = src["url"]
    return hashlib.md5(_get_ok(url).body).hexdigest()[:8], url  # noqa: S324 - identity, not security


def _os(manifest_dir: Path) -> tuple[str, str]:
    # The manifest handed to the build is the compact image.json written to a temp dir, so the package
    # table is looked up in the repository checkout first (GITHUB_WORKSPACE), then next to the manifest.
    candidates = []
    if workspace := os.environ.get("GITHUB_WORKSPACE"):
        candidates.append(Path(workspace) / "package_versions.txt")
    candidates.append(manifest_dir / "package_versions.txt")
    for path in candidates:
        if path.is_file():
            return hashlib.md5(path.read_bytes()).hexdigest()[:8], "none"  # noqa: S324
    # The first build of a repo has no package list yet; the build writes it.
    return "none", "none"


# A reader such as `head -1` or `grep -m 1` that stops early closes the pipe: most writers then die of SIGPIPE
# (141), and curl, which ignores the signal, exits 23 (write error). The output stands, as it did when Jenkins
# ran these commands without pipefail.
CLOSED_PIPE_EXITS = (141, 23)


def _command(src: dict[str, Any]) -> tuple[str, str]:
    proc = subprocess.run(["bash", "-o", "pipefail", "-c", src["run"]], capture_output=True, text=True, check=False, env=os.environ.copy())
    if proc.returncode not in (0, *CLOSED_PIPE_EXITS):
        raise ResolveError(f"command source failed ({proc.returncode}): {proc.stderr.strip()}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise ResolveError("command source printed nothing")
    return lines[-1].strip(), "command"


def _combined(src: dict[str, Any], platforms: list[str], manifest_dir: Path) -> tuple[str, str]:
    """Several sources resolved independently, each with its own ``transform``, joined with ``separator``: a
    version made of two upstreams (docker and compose), so a change to either is a new version."""
    parts: list[str] = []
    urls: list[str] = []
    for sub in src["sources"]:
        raw, url = resolve_raw(sub, platforms, manifest_dir)
        raw = (raw or "").strip()
        if not raw or raw == "null":
            raise ResolveError(f"combined part {sub.get('type')} returned nothing")
        parts.append(apply_transforms(raw, sub.get("transform")))
        urls.append(url)
    if not parts:
        raise ResolveError("combined source has no sources")
    return str(src.get("separator", "-")).join(parts), urls[0]


# --------------------------------------------------------------------------------------------
# Post-processing.


def apply_transforms(value: str, transforms: list[dict[str, str]] | None) -> str:
    for t in transforms or []:
        value = re.sub(t["regex"], t["replace"], value)
    return value


def sanitize(value: str) -> str:
    return SANITIZE_RE.sub("", value)


def derive_semver(clean: str, today: _dt.date | None = None) -> str | None:
    """
    CONVENTIONS §3: MAJOR.MINOR.PATCH when present anywhere in the string; a bare MAJOR.MINOR gets
    the build date as patch. Anything else has no semver.
    """
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", clean)
    if m:
        return f"{int(m.group(1))}.{int(m.group(2))}.{int(m.group(3))}"
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?(.*)", clean)
    if m and not m.group(3) and not m.group(4):
        day = (today or _dt.datetime.now(tz=_dt.UTC).date()).strftime("%Y%m%d")
        return f"{int(m.group(1))}.{int(m.group(2))}.{day}"
    return None


def check_required_assets(templates: list[str] | None, version: str) -> None:
    for template in templates or []:
        url = template.replace("${VERSION}", version).replace("$VERSION", version)
        resp = http_get(url, method="HEAD")
        if resp.status != 200:
            raise AssetsNotReady(f"required asset {url} returned HTTP {resp.status}")


# --------------------------------------------------------------------------------------------
# Entry points.


def load_manifest(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        return json.loads(text)
    try:
        import yaml  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise ResolveError("PyYAML is needed to read a .yml manifest; pass image.json or pip install pyyaml") from exc

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: str(loader.construct_scalar(node)))
    return yaml.load(text, Loader=_Loader)


def resolve_raw(source: dict[str, Any], platforms: list[str], manifest_dir: Path) -> tuple[str, str]:
    kind = source["type"]
    if kind == "github_release":
        return _github_release(source)
    if kind == "github_commit":
        return _github_commit(source)
    if kind == "gitlab_commit":
        return _gitlab_commit(source)
    if kind == "codeberg_release":
        return _codeberg_release(source)
    if kind == "gitea_release":
        return _gitea_release(source)
    if kind == "json":
        return _json(source)
    if kind == "npm":
        return _npm(source)
    if kind == "pip":
        return _pip(source)
    if kind == "alpine_apk":
        return _alpine_apk(source, platforms)
    if kind == "deb_packages":
        return _deb_packages(source)
    if kind == "deb_repo":
        return _deb_repo(source)
    if kind == "blob":
        return _blob(source)
    if kind == "os":
        return _os(manifest_dir)
    if kind == "command":
        return _command(source)
    if kind == "combined":
        return _combined(source, platforms, manifest_dir)
    raise ResolveError(f"unknown version source type {kind!r}")


def resolve(manifest: dict[str, Any], manifest_dir: str | Path = ".", today: _dt.date | None = None) -> dict[str, Any]:
    """Resolve the manifest's upstream version. Raises ResolveError / AssetsNotReady."""
    source = manifest["upstream"]["version"]
    platforms = list(manifest.get("build", {}).get("platforms") or ["amd64"])
    raw, source_url = resolve_raw(source, platforms, Path(manifest_dir))
    raw = (raw or "").strip()
    if not raw or raw == "null":
        raise ResolveError(f"version source {source['type']} returned nothing")
    upstream = apply_transforms(raw, source.get("transform"))
    clean = sanitize(upstream)
    if not clean:
        raise ResolveError(f"version {upstream!r} is empty after sanitising")
    check_required_assets(source.get("require_assets"), upstream)
    return {
        "upstream": upstream,
        "upstream_clean": clean,
        "semver": derive_semver(clean, today),
        "source": source["type"],
        "source_url": source_url,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in {"-h", "--help"}:
        print("usage: resolve_version.py <image.yml|image.json>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    try:
        result = resolve(load_manifest(path), path.parent)
    except AssetsNotReady as exc:
        print(f"assets not ready: {exc}", file=sys.stderr)
        return 3
    except ResolveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
