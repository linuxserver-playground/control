import pytest

from control.config import Settings
from control.github_app import EVENTS, PERMISSIONS, manifest

from .conftest import PUBLIC_URL


def test_manifest_urls_derive_from_public_url(settings: Settings) -> None:
    m = manifest(settings)
    assert m["url"] == PUBLIC_URL
    assert m["hook_attributes"] == {"url": f"{PUBLIC_URL}/webhooks/github", "active": True}
    assert m["redirect_url"] == f"{PUBLIC_URL}/setup/callback"
    assert m["setup_url"] == f"{PUBLIC_URL}/setup/installed"


def test_manifest_content(settings: Settings) -> None:
    m = manifest(settings)
    assert m["name"] == "LinuxServer CI (playground)"
    assert m["public"] is False
    assert m["setup_on_update"] is True
    assert m["default_permissions"] is PERMISSIONS
    assert m["default_permissions"]["workflows"] == "write"
    assert m["default_permissions"]["metadata"] == "read"
    assert m["default_events"] is EVENTS
    assert {"workflow_run", "installation", "installation_repositories", "push"} <= set(m["default_events"])


def test_manifest_follows_app_name_override(settings: Settings) -> None:
    m = manifest(settings.model_copy(update={"app_name": "Other"}))
    assert m["name"] == "Other"


def test_settings_from_env_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_PUBLIC_URL", "https://ci.example.test/")
    monkeypatch.setenv("CONTROL_DATA_DIR", "/tmp/x")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    s = Settings.from_env()
    assert s.public_url == "https://ci.example.test"
    assert str(s.data_dir) == "/tmp/x"
    assert s.db_path.name == "control.db"
    assert s.credentials_path.name == "github_app.json"
    assert s.discord_webhook_url is None
