import datetime
import json
import stat
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from control.github_app import API, AppCredentials, CredentialStore, InstallationTokens, app_jwt, convert

from .conftest import mock_client

# The documented response of POST /app-manifests/{code}/conversions, trimmed.
CONVERSION = {
    "id": 1,
    "slug": "octoapp",
    "node_id": "MDxOkludGVncmF0aW9uMQ==",
    "owner": {"login": "octocat", "id": 1},
    "name": "Octocat App",
    "description": "",
    "external_url": "https://example.com",
    "html_url": "https://github.com/apps/octoapp",
    "created_at": "2018-09-13T12:28:37Z",
    "updated_at": "2018-09-13T12:28:37Z",
    "permissions": {"metadata": "read", "contents": "read", "issues": "write", "single_file": "write"},
    "events": ["push", "pull_request"],
    "client_id": "Iv1.8a61f9b3a7aba766",
    "client_secret": "1726be1638095a19edd134c77bde3aa2ece1e5d8",
    "webhook_secret": "e340154128314309424b7c8e90325147d99fdafa",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----\n",
}


@pytest.mark.anyio
async def test_convert_posts_code_and_returns_credentials() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=CONVERSION)

    async with mock_client(handler) as client:
        creds = await convert("the-code", client)
    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{API}/app-manifests/the-code/conversions"
    assert "Authorization" not in seen[0].headers
    assert creds.id == 1
    assert creds.slug == "octoapp"
    assert creds.client_id == CONVERSION["client_id"]
    assert creds.client_secret == CONVERSION["client_secret"]
    assert creds.webhook_secret == CONVERSION["webhook_secret"]
    assert creds.pem == CONVERSION["pem"]
    assert creds.html_url == "https://github.com/apps/octoapp"
    assert creds.install_url == "https://github.com/apps/octoapp/installations/new"


@pytest.mark.anyio
async def test_convert_raises_on_error() -> None:
    async with mock_client(lambda _: httpx.Response(404, json={"message": "Not Found"})) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await convert("stale", client)


def test_credentials_file_is_private_and_round_trips(tmp_path: Path, credentials: AppCredentials) -> None:
    path = tmp_path / "data" / "github_app.json"
    store = CredentialStore(path)
    assert store.load() is None
    store.save(credentials)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["slug"] == credentials.slug
    assert CredentialStore(path).load() == credentials
    assert not path.with_suffix(".json.tmp").exists()


def test_app_jwt_is_signed_by_the_app_key(credentials: AppCredentials) -> None:
    now = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.UTC)
    token = app_jwt(credentials, now=now)
    private = serialization.load_pem_private_key(credentials.pem.encode(), password=None)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    claims = jwt.decode(token, public, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(now.timestamp()) - 60
    assert claims["exp"] == int(now.timestamp()) + 9 * 60
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


@pytest.mark.anyio
async def test_installation_token_is_cached_until_near_expiry(credentials: AppCredentials) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == f"{API}/app/installations/99/access_tokens"
        assert request.headers["Authorization"].startswith("Bearer ")
        expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        return httpx.Response(201, json={"token": f"ghs_{calls}", "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")})

    async with mock_client(handler) as client:
        tokens = InstallationTokens(client)
        assert await tokens.get(credentials, 99) == "ghs_1"
        assert await tokens.get(credentials, 99) == "ghs_1"
        # Fast-forward the cached expiry to within five minutes: a fresh token is fetched.
        token, _ = tokens._cache[99]
        tokens._cache[99] = (token, datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=4))
        assert await tokens.get(credentials, 99) == "ghs_2"
    assert calls == 2
