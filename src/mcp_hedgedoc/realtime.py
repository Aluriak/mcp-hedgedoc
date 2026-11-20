"""HedgeDoc 1.x's realtime editor channel, as a client.

This is the ONLY way HedgeDoc 1.x changes an existing note's content --
there is no HTTP update endpoint (see client.py's module docstring). The
protocol below is verified against the HedgeDoc 1.10.8 source:

- The editor server lives on the Socket.IO ROOT namespace
  (lib/app.js:85, `new Server(server)`). The note is selected at
  CONNECT time via the handshake query string ``?noteId=<id>``
  (lib/realtime.js extractNoteIdFromSocket / parseNoteIdFromSocket).
  A noteId that doesn't resolve to a note -> the server 404s the
  connection.
- On connect, the server emits ``doc`` with the current state:
  ``{str, revision, clients}``. ``str`` is the full Markdown, ``revision``
  is ``operations.length`` -- the index the server expects the next
  ``operation`` to be based on (lib/ot/editor-socketio-server.js
  addClient).
- The client sends ``operation (revision, operation, selection)``. The
  wire format of ``operation`` is TextOperation's JSON, which is an
  ARRAY OF PRIMITIVES, not objects: positive int = retain, string =
  insert, NEGATIVE int = delete (lib/ot/text-operation.js
  fromJSON/isRetain/isInsert/isDelete). Sending ``{"delete": n}`` objects
  -- the format some HedgeDoc-OT documentation describes -- makes
  fromJSON throw "unknown operation" and the op is rejected.
- The server replies ``ack (revision)`` once the op is transformed,
  applied and appended to the in-memory operation log. A revision the
  server has never seen throws ("operation revision not in history",
  lib/ot/server.js receiveOperation); concurrent ops are TRANSFORMED, not
  rejected.
- If the op is rejected, the server resyncs by re-emitting ``doc``
  (with ``force: true`` after 3 consecutive failures).
- NOTE the ack is NOT "saved to the database": the note's content is
  persisted by a separate 1-second setInterval loop that flushes
  isDirty notes (lib/realtime.js:79). So ``ack`` means "applied in the
  editor's authoritative in-memory state"; the database -- and therefore
  ``GET /{note}/download`` -- catches up a fraction of a second later.
  Callers must poll the download endpoint, they must not assume it is
  current immediately after the ack.
- Whether the op is even considered is gated by ``ifMayEdit``
  (lib/realtime.js): freely -> anyone; editable/limited -> logged in;
  locked/private/protected -> note owner only. A denied op produces NO
  ack and NO explicit error -- the client just never hears back. That is
  why replace_all() has a timeout and reports it as
  RealtimeUpdateError, indistinguishable in-band from a slow server;
  the error message says both possibilities.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable, Optional, Protocol

import socketio

if TYPE_CHECKING:  # only for the type annotation, not imported at runtime
    import requests


class ReplaceAllProvider(Protocol):
    """Any object that can replace a note's whole content over the
    realtime channel and return the new revision. HedgeDocRealtimeClient
    satisfies this; tests inject fakes that do."""

    def replace_all(self, note_id: str, new_content: str) -> int: ...


class HedgeDocRealtimeError(RuntimeError):
    """Base error for the realtime (Socket.IO) update path."""


class RealtimeConnectError(HedgeDocRealtimeError):
    """The Socket.IO connection could not be established (bad URL, the
    noteId doesn't resolve, transport failure)."""


class RealtimeUpdateError(HedgeDocRealtimeError):
    """The update operation was never acknowledged. Either the note
    permission (locked/private/protected -> owner only, editable/limited
    -> logged in) silently refused it, or the server never answered
    within the timeout. HedgeDoc sends no explicit refusal."""


def build_replace_all_ops(base: str, new_content: str) -> list:
    """Build the TextOperation JSON (a list of PRIMITIVES, see the module
    docstring) that replaces every character of ``base`` with
    ``new_content``. This is the only operation shape update_note needs:
    the tool's contract is full-content replacement, not an incremental
    edit, so a single delete-everything + single insert is always a valid
    op for any base string (baseLength == len(base)).

    A delete op of zero is NOT emitted: fromJSON classifies each element
    as retain (int > 0), insert (string), or delete (int < 0) -- a bare
    0 / -0 is none of those and would be rejected with "unknown
    operation". So an empty base just yields a pure insert (or an empty
    no-op list when both base and new_content are empty).
    """
    ops: list = []
    if base:
        ops.append(-len(base))
    if new_content:
        ops.append(new_content)
    return ops


class HedgeDocRealtimeClient:
    """One short-lived Socket.IO connection, used to replace a note's
    entire content and wait for the server's ack.

    ``session`` is the SAME requests.Session the HTTP client (client.py)
    logged in with: HedgeDoc's realtime middleware reads the raw
    ``Cookie: connect.sid=...`` header of the handshake (lib/realtime.js
    secure()), so passing the session's cookies through is what makes the
    operation attributed to the logged-in service account (and what
    unlocks editing editable/limited notes and any note the service
    account owns). Only ``session.cookies`` is ever read.

    ``client_factory`` is injectable for tests: it must return an object
    with the python-socketio sync Client surface this class uses
    (``.event`` decorator, ``.on``/``.emit``/``.connect``/``.disconnect``).
    Production code always leaves it at the default, the real
    socketio.Client.
    """

    # How long to wait for each of: the initial `doc`, and the `ack`.
    # The defaults match the server-side behaviour this class depends on
    # (the 1-second dirty-flush interval is separate and is handled by
    # the HTTP polling in client.update_note, not here).
    def __init__(
        self,
        socket_url: str,
        session: "requests.Session",
        client_factory: Optional[Callable[[], "socketio.Client"]] = None,
        doc_timeout: int = 10,
        ack_timeout: int = 10,
        max_resyncs: int = 3,
    ):
        self._socket_url = socket_url.rstrip("/")
        self._session = session
        self._client_factory = client_factory or socketio.Client
        self._doc_timeout = doc_timeout
        self._ack_timeout = ack_timeout
        self._max_resyncs = max_resyncs

    def _cookie_header(self) -> str:
        # HedgeDoc's realtime secure() middleware reads the RAW
        # "Cookie: connect.sid=..." header (lib/realtime.js), so the header
        # must be a well-formed Cookie value (name=value; ...), not just the
        # bare session id. Build it from every cookie in the shared
        # session's jar so connect.sid (and anything else) rides through.
        return "; ".join(
            f"{c.name}={c.value}" for c in self._session.cookies
        )

    def replace_all(self, note_id: str, new_content: str) -> int:
        """Replace the ENTIRE content of ``note_id`` with ``new_content``
        over the note's Socket.IO editor channel and return the new
        revision (the server's ack). Raises RealtimeConnectError if the
        connection fails, RealtimeUpdateError if the op is never
        acknowledged within the timeout (silently refused permission, or
        an unresponsive server -- HedgeDoc sends no explicit refusal).

        Concurrency: if another editor writes between the moment we read
        the note's state and the moment our op lands, the server either
        transforms our op against theirs (both changes survive, ours
        applied on top) or resyncs us with a fresh ``doc``. The latter is
        handled here: we re-emit against the fresh state, up to
        ``max_resyncs`` times.
        """
        client = self._client_factory()

        doc_ev = threading.Event()
        ack_ev = threading.Event()
        state: dict = {"doc": None, "resync": False}

        @client.event
        def doc(*args):  # server -> client: initial state, or resync
            # v2.x émet (payload, message_id) — 1.x émet (payload,) → *args
            payload = args[0] if args else None
            if state["doc"] is not None:
                state["resync"] = True
            state["doc"] = payload
            doc_ev.set()

        @client.event
        def ack(*args):  # server -> client: op applied (in memory)
            ack_ev.set()

        url = f"{self._socket_url}/?noteId={self._encode_note_id(note_id)}"
        # The session's cookies (connect.sid) ride along in the handshake
        # headers -- that is the realtime channel's notion of auth.
        client.connect(
            url,
            headers={"Cookie": self._cookie_header()},
            wait_timeout=self._doc_timeout,
        )
        try:
            if not doc_ev.wait(self._doc_timeout):
                raise HedgeDocRealtimeError(
                    f"Connected to {url!r} but the server never sent the "
                    f"initial 'doc' state within {self._doc_timeout}s -- "
                    "is the note id correct?"
                )
            if not isinstance(state["doc"], dict) or "str" not in state["doc"]:
                raise HedgeDocRealtimeError(
                    f"Unexpected 'doc' payload from the server: "
                    f"{state['doc']!r} (expected {{'str', 'revision', ...}})."
                )

            for _ in range(self._max_resyncs + 1):
                state["resync"] = False
                ack_ev.clear()
                base = state["doc"]["str"]
                revision = state["doc"]["revision"]
                ops = build_replace_all_ops(base, new_content)
                # Multiple arguments must be a TUPLE (python-socketio
                # expands it into the server handler's positional args).
                client.emit("operation", (revision, ops, None))
                if ack_ev.wait(self._ack_timeout):
                    return revision + 1
                if not state["resync"]:
                    break
                # The server resynced us (concurrent writer). Loop and
                # re-emit against the fresh state.

            raise RealtimeUpdateError(
                f"The server never acknowledged the content update for "
                f"note {note_id!r} within {self._ack_timeout}s. "
                "HedgeDoc does not send an explicit refusal, so this is "
                "either the note's permission (locked/private/protected "
                "can only be edited by their owner; editable/limited need "
                "a logged-in user -- the session cookie sent was "
                "rejected) or an unresponsive server."
            )
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    @staticmethod
    def _encode_note_id(note_id: str) -> str:
        # HedgeDoc resolves the query noteId through parseNoteId (short
        # id OR alias); we pass it through the same URL-encoding the
        # HTTP client uses, so the two always agree on what "this note"
        # means.
        from urllib.parse import quote

        return quote(note_id, safe="")