"""Environment-variable configuration for hedgedoc-mcp.

Every setting is read once, at process startup, into a frozen `Settings`
instance. There is no hot-reload: changing an env var means restarting the
container, same as every other MCP server in this fleet.

Supported HEDGEDOC_AUTH_MODE values:

  local  (default) -- POST /login with HEDGEDOC_EMAIL / HEDGEDOC_PASSWORD.
                      Needs a local HedgeDoc account (CMD_EMAIL=true server
                      side), e.g. one created with bin/manage_users.

  oidc   -- Drives HedgeDoc's own /auth/oauth2 browser-redirect flow by
            scraping the Keycloak login form and submitting
            HEDGEDOC_OIDC_USERNAME / HEDGEDOC_OIDC_PASSWORD to it. Needs a
            dedicated Keycloak account (so the service account's identity
            lives in the central IdP rather than in HedgeDoc's own user
            table). See auth.py's module docstring for exactly how fragile
            this is and why.

  cookie -- Skip login entirely; use an already-valid session cookie
            supplied via HEDGEDOC_SESSION_COOKIE. Mostly useful for
            debugging against a real instance without touching either
            login path.

Either way, this is always a SINGLE SHARED SERVICE ACCOUNT. HedgeDoc 1.x
has no per-request bearer token of any kind (see the "identity" note in
README.md) -- choosing HEDGEDOC_AUTH_MODE=oidc changes *where the service
account's identity is administered*, not *who HedgeDoc thinks made the
request*. Every action still lands in HedgeDoc's history under this one
account, never under the real LibreChat user's identity.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised for missing or contradictory configuration at startup."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got: {value!r}") from None


@dataclass(frozen=True)
class Settings:
    hedgedoc_url: str
    auth_mode: str  # "local" | "oidc" | "cookie"
    read_only: bool
    port: int
    timeout_seconds: int

    # local
    email: str | None
    password: str | None

    # oidc
    oidc_username: str | None
    oidc_password: str | None
    oidc_login_path: str

    # cookie
    session_cookie: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        hedgedoc_url = _env("HEDGEDOC_URL")
        if not hedgedoc_url:
            raise ConfigError("HEDGEDOC_URL is required (e.g. https://hedgedoc.example.org)")
        hedgedoc_url = hedgedoc_url.rstrip("/")

        auth_mode = (_env("HEDGEDOC_AUTH_MODE", "local") or "local").lower()
        if auth_mode not in ("local", "oidc", "cookie"):
            raise ConfigError(
                f"HEDGEDOC_AUTH_MODE must be 'local', 'oidc', or 'cookie', got: {auth_mode!r}"
            )

        email = _env("HEDGEDOC_EMAIL")
        password = _env("HEDGEDOC_PASSWORD")
        if auth_mode == "local" and not (email and password):
            raise ConfigError(
                "HEDGEDOC_AUTH_MODE=local requires both HEDGEDOC_EMAIL and HEDGEDOC_PASSWORD"
            )

        oidc_username = _env("HEDGEDOC_OIDC_USERNAME")
        oidc_password = _env("HEDGEDOC_OIDC_PASSWORD")
        if auth_mode == "oidc" and not (oidc_username and oidc_password):
            raise ConfigError(
                "HEDGEDOC_AUTH_MODE=oidc requires both HEDGEDOC_OIDC_USERNAME "
                "and HEDGEDOC_OIDC_PASSWORD (credentials for a dedicated "
                "Keycloak service account, not a real person's own login)"
            )

        session_cookie = _env("HEDGEDOC_SESSION_COOKIE")
        if auth_mode == "cookie" and not session_cookie:
            raise ConfigError("HEDGEDOC_AUTH_MODE=cookie requires HEDGEDOC_SESSION_COOKIE")

        return cls(
            hedgedoc_url=hedgedoc_url,
            auth_mode=auth_mode,
            read_only=_env_bool("HEDGEDOC_READ_ONLY", False),
            port=_env_int("MCP_SERVER_PORT", 8000),
            timeout_seconds=_env_int("HEDGEDOC_TIMEOUT_SECONDS", 15),
            email=email,
            password=password,
            oidc_username=oidc_username,
            oidc_password=oidc_password,
            oidc_login_path=_env("HEDGEDOC_OIDC_LOGIN_PATH", "/auth/oauth2") or "/auth/oauth2",
            session_cookie=session_cookie,
        )
