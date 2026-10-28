"""Authentication backends for HedgeDoc 1.x.

HedgeDoc 1.x has no bearer-token API at all -- every write and every
personalised read goes through a browser-style session cookie, obtained by
an actual login. There are exactly two ways to get one:

  1. POST /login with an email + password, for a *local* HedgeDoc account
     (config.email / CMD_EMAIL enabled server-side). This is the `local`
     backend below -- a single HTTP call, nothing fragile about it.

  2. Drive HedgeDoc's own /auth/oauth2 redirect dance: HedgeDoc redirects
     the browser to Keycloak's login page, the human types their password
     there, Keycloak redirects back to /auth/oauth2/callback with a code,
     and HedgeDoc's server exchanges that code for a token and *only then*
     sets its own session cookie. There is no shortcut into this from
     outside -- HedgeDoc does not accept a Keycloak-issued token directly,
     it only accepts the result of a callback it initiated itself.

The `oidc` backend below automates step 2 by scripting an HTTP client
through it: fetch the Keycloak login page, scrape the <form> the page
renders, POST credentials to it, and let `requests` follow the resulting
redirect chain back into HedgeDoc's callback. This works against a vanilla
Keycloak login theme (the default one) and breaks if:

  - the realm's login theme is customised in a way that changes the login
    form's structure or requires JavaScript to submit it;
  - the account has 2FA / WebAuthn / an "update password" or "review
    profile" required action attached -- there is no interactive step here
    to satisfy any of those;
  - Keycloak throttles/blocks the login as suspicious (unusual for a
    stable server-side service, but worth knowing).

None of this makes the resulting session "belong" to the Keycloak account
in any way HedgeDoc understands -- HedgeDoc still just sees one more of
its own local user rows (provider=oauth2), the same shared service account
model as the `local` backend. Choosing `oidc` only moves where the
credential is administered (your central IdP, with its own rotation and
audit policy) instead of HedgeDoc's own user table. See config.py's module
docstring for the same point made from the settings side.

If this ever proves too fragile in practice, HEDGEDOC_AUTH_MODE=local
(bin/manage_users) remains the boring, reliable fallback -- switching
between them is a config change, not a code change.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urljoin

import requests

from .config import Settings

_LOGIN_FORM_RE = re.compile(
    r'<form[^>]+id=["\']kc-form-login["\'][^>]*action=["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)
_ANY_FORM_ACTION_RE = re.compile(
    r'<form[^>]+action=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


class AuthenticationError(RuntimeError):
    """Raised when login fails, by any backend, for any reason."""


def extract_login_form_action(page_html: str, page_url: str) -> str | None:
    """Pull the login <form>'s action URL out of a Keycloak login page,
    HTML-unescaped and resolved against the page's own URL. Returns None
    if no form could be found at all. Pure and network-free on purpose --
    see tests/test_auth_oidc_scraping.py.
    """
    match = _LOGIN_FORM_RE.search(page_html) or _ANY_FORM_ACTION_RE.search(page_html)
    if not match:
        return None
    return urljoin(page_url, html.unescape(match.group(1)))


def _verify_session(session: requests.Session, settings: Settings) -> None:
    """Confirm the session cookie we just obtained is actually valid, by
    hitting the one endpoint that requires auth and returns JSON either
    way: GET /me.
    """
    resp = session.get(
        f"{settings.hedgedoc_url}/me",
        timeout=settings.timeout_seconds,
    )
    if resp.status_code != 200:
        raise AuthenticationError(
            f"Login appeared to succeed but GET /me returned {resp.status_code} "
            "-- the session cookie is not valid. Check the credentials and, "
            "for oidc mode, whether the Keycloak login form still looks like "
            "the default theme (see auth.py's module docstring)."
        )
    try:
        data = resp.json()
    except ValueError:
        raise AuthenticationError("GET /me did not return JSON -- unexpected response body.") from None
    if not data.get("status") == "ok":
        raise AuthenticationError(f"GET /me reports not logged in: {data!r}")


def login_local(settings: Settings) -> requests.Session:
    """POST /login with email + password (Passport local strategy)."""
    session = requests.Session()
    resp = session.post(
        f"{settings.hedgedoc_url}/login",
        data={"email": settings.email, "password": settings.password},
        allow_redirects=True,
        timeout=settings.timeout_seconds,
    )
    if resp.status_code >= 400:
        raise AuthenticationError(
            f"POST /login failed with HTTP {resp.status_code}. Check "
            "HEDGEDOC_EMAIL/HEDGEDOC_PASSWORD, and that CMD_EMAIL is "
            "enabled on the HedgeDoc server."
        )
    _verify_session(session, settings)
    return session


def login_oidc(settings: Settings) -> requests.Session:
    """Drive HedgeDoc's /auth/oauth2 -> Keycloak -> callback dance.

    See this module's docstring for exactly what this does and does not
    protect against.
    """
    session = requests.Session()

    start_url = f"{settings.hedgedoc_url}{settings.oidc_login_path}"
    resp = session.get(start_url, timeout=settings.timeout_seconds)
    if resp.status_code >= 400:
        raise AuthenticationError(
            f"GET {settings.oidc_login_path} failed with HTTP {resp.status_code} "
            "-- is HEDGEDOC_OIDC_LOGIN_PATH correct, and is OAuth2 login "
            "(CMD_OAUTH2_*) actually enabled on this HedgeDoc instance?"
        )

    action_url = extract_login_form_action(resp.text, resp.url)
    if not action_url:
        raise AuthenticationError(
            "Could not find a login <form> in the page HedgeDoc's "
            f"{settings.oidc_login_path} redirected to (landed on "
            f"{resp.url}). Either this isn't Keycloak's default login "
            "theme, or the session was already authenticated and got "
            "redirected straight back to HedgeDoc -- in which case check "
            "HEDGEDOC_OIDC_USERNAME/PASSWORD are for the right account."
        )

    resp2 = session.post(
        action_url,
        data={"username": settings.oidc_username, "password": settings.oidc_password},
        allow_redirects=True,
        timeout=settings.timeout_seconds,
    )
    if resp2.status_code >= 400:
        raise AuthenticationError(
            f"POST to the Keycloak login form failed with HTTP {resp2.status_code}. "
            "Check HEDGEDOC_OIDC_USERNAME/HEDGEDOC_OIDC_PASSWORD, and that this "
            "account has no 2FA / required action attached -- neither can be "
            "satisfied by this scripted flow (see auth.py's module docstring)."
        )

    _verify_session(session, settings)
    return session


def login_cookie(settings: Settings) -> requests.Session:
    """Skip login entirely; trust an already-valid session cookie."""
    session = requests.Session()
    session.cookies.set("connect.sid", settings.session_cookie)
    _verify_session(session, settings)
    return session


def login(settings: Settings) -> requests.Session:
    """Dispatch to the configured backend."""
    if settings.auth_mode == "local":
        return login_local(settings)
    if settings.auth_mode == "oidc":
        return login_oidc(settings)
    if settings.auth_mode == "cookie":
        return login_cookie(settings)
    raise AssertionError(f"unreachable: unknown auth_mode {settings.auth_mode!r}")
