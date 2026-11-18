"""Integration test: the REAL python-socketio client (no fake) talks to a
REAL socketio.Server emulating one HedgeDoc 1.x note.

This is the test that would catch a regression in the actual client
wiring -- handler registration, the tuple multi-arg emit, the noteId
handshake query, the cookie header -- none of which the fake in
test_realtime.py exercises (the fake only checks our own logic). It needs
werkzeug, which python-socketio[client] does not pull in on its own, so it
is skipped if werkzeug is absent.

The emulated server faithfully reproduces the two server-side behaviours
we depend on (lib/ot/editor-socketio-server.js onOperation + the OT
primitive validation in text-operation.js fromJSON): it rejects
malformed ops (objects, zeros) and only acks a well-formed
delete-all/insert, replying ack(revision+1).
"""
import threading
import time

import pytest

werkzeug = pytest.importorskip("werkzeug")
from werkzeug.serving import make_server  # noqa: E402

import socketio  # noqa: E402

from mcp_hedgedoc.realtime import (  # noqa: E402
    HedgeDocRealtimeClient,
    RealtimeUpdateError,
)

BASE = "http://127.0.0.1:8917"


def _valid_op(op):
    """Mirror text-operation.js fromJSON classification: positive int =
    retain, string = insert, negative int = delete. Anything else is a
    server-side 'unknown operation' rejection."""
    for o in op:
        if isinstance(o, bool):
            return False
        if isinstance(o, int):
            if o == 0:
                return False
            continue
        if isinstance(o, str):
            continue
        return False
    return True


class _NoteServer:
    def __init__(self, initial_str, revision, editable=True, port=8917):
        self.initial = initial_str
        self.revision = revision
        self.editable = editable
        self.port = port
        self.received = []
        self._srv = socketio.Server()
        self._app = socketio.WSGIApp(self._srv)

        @self._srv.on("connect")
        def _on_connect(sid, environ):
            # capture the handshake query so the test can assert noteId
            self.last_note_id = environ.get("QUERY_STRING", "")
            self._srv.emit(
                "doc",
                {"str": self.initial, "revision": self.revision, "clients": {}},
                to=sid,
            )
            return True

        @self._srv.on("operation")
        def _on_operation(sid, revision, op, selection):
            self.received.append((revision, op, selection))
            if not self.editable:
                return  # ifMayEdit silent refusal
            if not _valid_op(op):
                return  # fromJSON would throw -> no ack
            self._srv.emit("ack", revision + 1, to=sid)

    def start(self):
        self.httpd = make_server("127.0.0.1", self.port, self._app)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        time.sleep(0.4)

    def stop(self):
        self.httpd.shutdown()


class _Sess:
    def __init__(self, sid):
        from requests.cookies import RequestsCookieJar

        self.cookies = RequestsCookieJar()
        self.cookies.set("connect.sid", sid, domain="127.0.0.1")


def test_real_client_replace_all_end_to_end():
    note = _NoteServer("hello world", revision=4, editable=True)
    note.start()
    try:
        client = HedgeDocRealtimeClient(
            BASE, _Sess("s3cr3t"), doc_timeout=5, ack_timeout=5
        )
        new_rev = client.replace_all("my-note", "NEW")
        assert new_rev == 5
        # the server saw exactly one well-formed delete-all + insert
        (revision, op, selection) = note.received[0]
        assert revision == 4
        assert op == [-11, "NEW"]
        assert selection is None
        # noteId rode in the handshake query string
        assert "noteId=my-note" in note.last_note_id
    finally:
        note.stop()


def test_real_client_empty_base_inserts():
    note = _NoteServer("", revision=0, editable=True, port=8918)
    note.start()
    try:
        client = HedgeDocRealtimeClient(BASE.replace("8917", "8918"), _Sess("s"))
        new_rev = client.replace_all("n", "first")
        assert new_rev == 1
        assert note.received[0][1] == ["first"]
    finally:
        note.stop()


def test_real_client_silent_refusal_times_out():
    note = _NoteServer("locked", revision=2, editable=False, port=8919)
    note.start()
    try:
        client = HedgeDocRealtimeClient(
            BASE.replace("8917", "8919"), _Sess("s"), ack_timeout=1
        )
        with pytest.raises(RealtimeUpdateError):
            client.replace_all("n", "x")
    finally:
        note.stop()