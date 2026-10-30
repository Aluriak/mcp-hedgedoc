import pytest

from mcp_hedgedoc.config import ConfigError, Settings


def test_requires_hedgedoc_url(monkeypatch):
    monkeypatch.delenv("HEDGEDOC_URL", raising=False)
    with pytest.raises(ConfigError, match="HEDGEDOC_URL"):
        Settings.from_env()


def test_local_mode_requires_email_and_password(monkeypatch):
    monkeypatch.setenv("HEDGEDOC_URL", "https://pad.example.org")
    monkeypatch.delenv("HEDGEDOC_EMAIL", raising=False)
    monkeypatch.delenv("HEDGEDOC_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="HEDGEDOC_EMAIL"):
        Settings.from_env()


def test_oidc_mode_requires_oidc_credentials(monkeypatch):
    monkeypatch.setenv("HEDGEDOC_URL", "https://pad.example.org")
    monkeypatch.setenv("HEDGEDOC_AUTH_MODE", "oidc")
    monkeypatch.delenv("HEDGEDOC_OIDC_USERNAME", raising=False)
    monkeypatch.delenv("HEDGEDOC_OIDC_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="HEDGEDOC_OIDC_USERNAME"):
        Settings.from_env()


def test_cookie_mode_requires_session_cookie(monkeypatch):
    monkeypatch.setenv("HEDGEDOC_URL", "https://pad.example.org")
    monkeypatch.setenv("HEDGEDOC_AUTH_MODE", "cookie")
    monkeypatch.delenv("HEDGEDOC_SESSION_COOKIE", raising=False)
    with pytest.raises(ConfigError, match="HEDGEDOC_SESSION_COOKIE"):
        Settings.from_env()


def test_rejects_unknown_auth_mode(monkeypatch):
    monkeypatch.setenv("HEDGEDOC_URL", "https://pad.example.org")
    monkeypatch.setenv("HEDGEDOC_AUTH_MODE", "carrier-pigeon")
    with pytest.raises(ConfigError, match="carrier-pigeon"):
        Settings.from_env()


def test_trailing_slash_is_stripped(monkeypatch):
    monkeypatch.setenv("HEDGEDOC_URL", "https://pad.example.org/")
    monkeypatch.setenv("HEDGEDOC_EMAIL", "bot@example.org")
    monkeypatch.setenv("HEDGEDOC_PASSWORD", "secret")
    settings = Settings.from_env()
    assert settings.hedgedoc_url == "https://pad.example.org"


def test_defaults(monkeypatch):
    monkeypatch.setenv("HEDGEDOC_URL", "https://pad.example.org")
    monkeypatch.setenv("HEDGEDOC_EMAIL", "bot@example.org")
    monkeypatch.setenv("HEDGEDOC_PASSWORD", "secret")
    for var in ("HEDGEDOC_READ_ONLY", "MCP_SERVER_PORT", "HEDGEDOC_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.from_env()
    assert settings.read_only is False
    assert settings.port == 8080
    assert settings.timeout_seconds == 15


def test_read_only_true_variants(monkeypatch):
    monkeypatch.setenv("HEDGEDOC_URL", "https://pad.example.org")
    monkeypatch.setenv("HEDGEDOC_EMAIL", "bot@example.org")
    monkeypatch.setenv("HEDGEDOC_PASSWORD", "secret")
    for value in ("true", "True", "1", "yes", "on"):
        monkeypatch.setenv("HEDGEDOC_READ_ONLY", value)
        assert Settings.from_env().read_only is True, value
    for value in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("HEDGEDOC_READ_ONLY", value)
        assert Settings.from_env().read_only is False, value
