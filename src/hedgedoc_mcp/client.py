"""A thin client for the handful of HTTP endpoints HedgeDoc 1.x actually
has, verified directly against its source (routers/controllers), not
against any third-party description of them:

  GET  /{note}/download          -- raw Markdown content (public, no auth
                                     needed unless the note's permission is
                                     private/limited/protected)
  GET  /{note}/info              -- {title, description, viewcount,
                                     createtime, updatetime} (same auth rule)
  GET  /{note}/revision          -- {revision: [...]} list of revisions
  GET  /{note}/revision/{time}   -- raw content as of that revision
  GET  /me                       -- {status, id, name, photo} for the
                                     CURRENTLY AUTHENTICATED SESSION (i.e.
                                     always the service account, never the
                                     real LibreChat user -- see README.md)
  GET  /history                  -- {history: [...]} notes the current
                                     session has recently viewed/pinned;
                                     requires auth
  POST /new[/{alias}]            -- create a note; requires
                                     CMD_ALLOW_FREEURL=true server-side to
                                     use a custom alias

There is deliberately no update/delete here: POST /new/{alias} on an
alias that already has a note returns 409 Conflict (verified against
HedgeDoc 1.11.1 and 1.12.0 source -- this is not a version-drift issue,
some other HedgeDoc-MCP projects describe this as an "update" mechanism
and that description does not match what the code does). Content changes
only ever happen over HedgeDoc's Socket.IO realtime channel, which this
client does not implement.
"""
from __future__ import annotations

from urllib.parse import quote

import requests

from .auth import login
from .config import Settings


class HedgeDocError(RuntimeError):
    """Raised for any HedgeDoc API failure (network, unexpected status, etc.)."""


class NoteNotFoundError(HedgeDocError):
    pass


class PermissionDeniedError(HedgeDocError):
    """The note exists but its permission (private/limited/protected)
    forbids the current session from reading it."""


class SessionExpiredError(HedgeDocError):
    """The session cookie was rejected; call re_login() and retry once."""


class NoteAlreadyExistsError(HedgeDocError):
    """POST /new/{alias} returned 409 -- see this module's docstring."""


class HedgeDocClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._session: requests.Session = login(settings)

    def re_login(self) -> None:
        self._session = login(self._settings)

    def _get(self, path: str) -> requests.Response:
        resp = self._session.get(
            f"{self._settings.hedgedoc_url}{path}",
            timeout=self._settings.timeout_seconds,
        )
        if resp.status_code == 403:
            raise PermissionDeniedError(
                f"GET {path}: permission denied (note is private/limited/"
                "protected and the current session can't see it)."
            )
        if resp.status_code == 404:
            raise NoteNotFoundError(f"GET {path}: note not found.")
        if resp.status_code >= 400:
            raise HedgeDocError(f"GET {path}: unexpected HTTP {resp.status_code}")
        return resp

    @staticmethod
    def _encode(note_id_or_alias: str) -> str:
        return quote(note_id_or_alias, safe="")

    def read_note(self, note_id_or_alias: str) -> str:
        resp = self._get(f"/{self._encode(note_id_or_alias)}/download")
        return resp.text

    def get_note_info(self, note_id_or_alias: str) -> dict:
        resp = self._get(f"/{self._encode(note_id_or_alias)}/info")
        return resp.json()

    def get_note_revisions(self, note_id_or_alias: str) -> list[dict]:
        resp = self._get(f"/{self._encode(note_id_or_alias)}/revision")
        return resp.json().get("revision", [])

    def get_note_revision_content(self, note_id_or_alias: str, revision_time_ms: int) -> str:
        resp = self._get(f"/{self._encode(note_id_or_alias)}/revision/{int(revision_time_ms)}")
        return resp.text

    def whoami(self) -> dict:
        resp = self._session.get(
            f"{self._settings.hedgedoc_url}/me",
            timeout=self._settings.timeout_seconds,
        )
        data = resp.json()
        if data.get("status") != "ok":
            raise SessionExpiredError("GET /me: session is no longer authenticated.")
        return data

    def list_history(self) -> list[dict]:
        resp = self._session.get(
            f"{self._settings.hedgedoc_url}/history",
            timeout=self._settings.timeout_seconds,
        )
        if resp.status_code == 403:
            raise SessionExpiredError("GET /history: session is no longer authenticated.")
        if resp.status_code >= 400:
            raise HedgeDocError(f"GET /history: unexpected HTTP {resp.status_code}")
        return resp.json().get("history", [])

    def create_note(self, content: str, alias: str | None = None) -> dict:
        path = f"/new/{self._encode(alias)}" if alias else "/new"
        resp = self._session.post(
            f"{self._settings.hedgedoc_url}{path}",
            data=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown"},
            allow_redirects=False,
            timeout=self._settings.timeout_seconds,
        )
        if resp.status_code == 409:
            raise NoteAlreadyExistsError(
                f"A note already exists at alias {alias!r}. HedgeDoc has no "
                "HTTP endpoint to update existing note content -- see this "
                "module's docstring."
            )
        if resp.status_code != 302:
            raise HedgeDocError(f"POST {path}: unexpected HTTP {resp.status_code}")

        location = resp.headers.get("Location", "")
        base = self._settings.hedgedoc_url
        if location in ("", "/", base, f"{base}/"):
            raise SessionExpiredError(
                "Note creation redirected to the home page instead of a new "
                "note -- the session is no longer authenticated, or "
                "anonymous note creation is disabled on this instance. "
                "Call re_login() and retry."
            )
        note_url = location if location.startswith("http") else f"{base}{location}"
        note_id = note_url.rstrip("/").rsplit("/", 1)[-1]
        return {"note_id": note_id, "url": note_url}
