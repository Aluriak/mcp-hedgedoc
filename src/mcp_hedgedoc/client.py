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

There is no HTTP update endpoint: POST /new/{alias} on an alias that
already has a note returns 409 Conflict (verified against HedgeDoc 1.11.1
and 1.12.0 source -- this is not a version-drift issue, some other
HedgeDoc-MCP projects describe this as an "update" mechanism and that
description does not match what the code does). The ONE supported way to
change an existing note's content is the realtime editor channel, and
update_note() does exactly that: it speaks the note's Socket.IO
doc/operation/ack protocol (see realtime.py for the full source-verified
description) to replace the entire content, then polls GET /download
until the database catches up -- the server acks once the change is
applied to the editor's in-memory state, and a separate 1-second flush
loop persists it a fraction of a second later.
"""
from __future__ import annotations

import time
from typing import Callable
from urllib.parse import quote

import requests

from .auth import login
from .config import Settings
from .realtime import (
    HedgeDocRealtimeClient,
    RealtimeUpdateError,
    ReplaceAllProvider,
)


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

    def update_note(
        self,
        note_id_or_alias: str,
        new_content: str,
        *,
        realtime_factory: "Callable[[], ReplaceAllProvider] | None" = None,
        verify_deadline: float = 10.0,
    ) -> dict:
        """Replace the ENTIRE content of an existing note.

        This is the only supported content-edit path in HedgeDoc 1.x (see
        the module docstring). It does three things:

          1. Confirms the note exists and is readable via GET /download
             (this also gives the same permission semantics as the read
             tools, and fails fast with the same NoteNotFoundError /
             PermissionDeniedError a plain read would).
          2. Speaks the note's Socket.IO editor channel to replace the
             content and wait for the server's ack.
          3. Polls GET /download until the database has persisted the new
             content (the ack is in-memory-only; a 1-second flush loop
             writes it out). Returns once the read-back matches.

        Raises RealtimeUpdateError if the ack never arrives (silent
        permission refusal or unresponsive server), and PermissionDenied-
        style errors for the up-front read.

        ``realtime_factory`` and ``verify_deadline`` are injectable for
        tests; production callers leave them at the defaults.
        """
        # 1) The note must exist and be readable under this session first.
        # read_note() does exactly that and raises the same typed errors.
        self.read_note(note_id_or_alias)

        # 2) Do the realtime replace-all and get the new revision.
        if realtime_factory is None:
            realtime_factory = lambda: HedgeDocRealtimeClient(
                self._settings.hedgedoc_url, self._session
            )
        new_revision = realtime_factory().replace_all(note_id_or_alias, new_content)

        # 3) Wait for the database to catch up, then confirm by reading
        # back over plain HTTP (the only way to prove persistence).
        expected = new_content
        deadline = time.monotonic() + verify_deadline
        last = None
        while time.monotonic() < deadline:
            last = self.read_note(note_id_or_alias)
            if last == expected:
                return {
                    "note_id": note_id_or_alias,
                    "new_revision": new_revision,
                    "verified": True,
                }
            time.sleep(0.25)
        raise HedgeDocError(
            f"The realtime channel acknowledged the update to note "
            f"{note_id_or_alias!r} (revision {new_revision}), but "
            f"GET /download still returns the old content after "
            f"{verify_deadline:.0f}s. The note was changed in the editor "
            "but the read-back does not yet match -- a concurrent writer "
            "may have transformed the result, or the instance's persist "
            "loop is slow. Read the note again to see the current state."
        )
