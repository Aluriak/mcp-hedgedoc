"""Tests for the realtime (Socket.IO) update path, using a fake socket
client injected through HedgeDocRealtimeClient(client_factory=...).

The fake emulates the server side of the protocol as implemented in
HedgeDoc 1.10.8 (lib/ot/editor-socketio-server.js, lib/realtime.js):
  - connect() delivers the initial `doc` state
  - emit('operation', (revision, ops, selection)) applies the op and
    replies with `ack(revision+1)`, or re-emits a fresh `doc` to
    simulate a concurrent-writer resync, or stays silent (the
    ifMayEdit-silent-refusal case)
The same event-then-handler dispatch shape (handlers registered with
`.event` are invoked when the 'server' delivers) mirrors the real
python-socketio sync client, so what is exercised here is the client's
own logic: URL/handshake building, doc parsing, op building, ack
handling, and resync retry -- not the transport.
"""
import pytest

from mcp_hedgedoc.realtime import (
    HedgeDocRealtimeClient,
    RealtimeUpdateError,
    build_replace_all_ops,
)


class FakeSession:
    """Just enough of requests.Session: a cookies jar with a connect.sid."""

    def __init__(self, sid="abc123"):
        from requests.cookies import RequestsCookieJar

        self.cookies = RequestsCookieJar()
        self.cookies.set("connect.sid", sid, domain="example.org")


class FakeSocketClient:
    """Emulates one note's editor server + the python-socketio sync
    Client surface HedgeDocRealtimeClient uses.

    scenario:
      'ok'      -- doc, then ack for every op
      'resync'  -- first op gets a fresh `doc` (resync) instead of an
                   ack; second op is acked
      'silent'  -- doc, then no ack ever (permission refused silently)
    """

    def __init__(self, doc_state, scenario="ok"):
        self._doc_state = doc_state
        self._scenario = scenario
        self._handlers = {}
        self.emitted = []  # (event, data) tuples, in order
        self.connected_url = None
        self.connect_headers = None
        self.disconnected = False
        self._ops_seen = 0

    # --- python-socketio sync Client surface ---

    def event(self, fn):
        self._handlers[fn.__name__] = fn
        return fn

    def on(self, name, fn):
        self._handlers[name] = fn
        return fn

    def connect(self, url, **kwargs):
        self.connected_url = url
        self.connect_headers = kwargs.get("headers")
        # The real client returns once the server has delivered the
        # connect ack; the editor server then immediately emits the
        # initial `doc`. Emulate that by dispatching synchronously.
        self._handlers["doc"](self._doc_state)

    def emit(self, event, data=None, **kwargs):
        self.emitted.append((event, data))
        if event != "operation":
            return
        assert data is not None
        revision, ops, selection = data
        assert selection is None
        # Validate the op is legal TextOperation JSON (the same
        # classification fromJSON uses): positive int / string /
        # negative int only. A malformed op (e.g. {"delete": 3}) would
        # raise here, exactly like the server would reject it.
        for op in ops:
            assert isinstance(op, int) and op != 0 or isinstance(op, str), (
                f"malformed op {op!r} -- fromJSON would throw 'unknown operation'"
            )
        self._ops_seen += 1
        if self._scenario == "resync" and self._ops_seen == 1:
            # concurrent writer forced a resync: fresh state, no ack
            self._doc_state = {"str": self._doc_state["str"] + "X", "revision": 1}
            self._handlers["doc"](self._doc_state)
        elif self._scenario == "silent":
            pass  # ifMayEdit refusal: no ack, no error
        else:
            self._handlers["ack"](revision + 1)

    def disconnect(self):
        self.disconnected = True


def _client(fake, doc_timeout=5, ack_timeout=5):
    return HedgeDocRealtimeClient(
        "https://pad.example.org",
        FakeSession(),  # type: ignore[arg-type]
        client_factory=lambda: fake,
        doc_timeout=doc_timeout,
        ack_timeout=ack_timeout,
    )


class TestBuildReplaceAllOps:
    def test_replaces_whole_content(self):
        assert build_replace_all_ops("hello", "world") == [-5, "world"]

    def test_empties_the_note(self):
        assert build_replace_all_ops("hello", "") == [-5]

    def test_fills_an_empty_note(self):
        # no zero-length delete op: 0 is not a valid TextOperation element
        assert build_replace_all_ops("", "world") == ["world"]

    def test_empty_to_empty_is_a_noop(self):
        assert build_replace_all_ops("", "") == []

    def test_ops_are_primitives_only(self):
        # fromJSON only accepts: positive int (retain), string (insert),
        # negative int (delete). No objects, ever.
        for base, new in [("a", "b"), ("abc", ""), ("", "xyz"), ("x", "y")]:
            for op in build_replace_all_ops(base, new):
                assert isinstance(op, str) or (isinstance(op, int) and op != 0)


class TestReplaceAll:
    def test_success_ack_and_return_value(self):
        fake = FakeSocketClient({"str": "hello world", "revision": 4}, scenario="ok")
        new_rev = _client(fake).replace_all("my-note", "NEW content")
        assert new_rev == 5
        assert fake.disconnected
        # one operation, delete-all + insert
        event, (revision, ops, selection) = fake.emitted[0]
        assert event == "operation"
        assert revision == 4
        assert ops == [-11, "NEW content"]
        assert selection is None

    def test_handshake_carries_note_id_and_cookie(self):
        fake = FakeSocketClient({"str": "a", "revision": 0}, scenario="ok")
        _client(fake).replace_all("my note", "b")
        # note id is URL-encoded in the handshake query string
        assert fake.connected_url == "https://pad.example.org/?noteId=my%20note"
        assert fake.connect_headers == {"Cookie": "connect.sid=abc123"}

    def test_empty_base_sends_pure_insert(self):
        fake = FakeSocketClient({"str": "", "revision": 0}, scenario="ok")
        _client(fake).replace_all("n", "first")
        _, (_, ops, _) = fake.emitted[0]
        assert ops == ["first"]

    def test_no_ack_raises_realtime_update_error(self):
        fake = FakeSocketClient({"str": "a", "revision": 0}, scenario="silent")
        client = _client(fake, ack_timeout=1)
        with pytest.raises(RealtimeUpdateError):
            client.replace_all("n", "b")

    def test_resync_then_ack_retries_against_fresh_state(self):
        fake = FakeSocketClient({"str": "ab", "revision": 0}, scenario="resync")
        new_rev = _client(fake).replace_all("n", "NEW")
        assert new_rev == 2  # fresh revision was 1
        events = [e for e, _ in fake.emitted]
        assert events == ["operation", "operation"]
        # second op was built against the post-resync state
        _, (rev2, ops2, _) = fake.emitted[1]
        assert rev2 == 1
        assert ops2 == [-3, "NEW"]  # len("abX") == 3

    def test_bad_doc_payload_rejected(self):
        fake = FakeSocketClient({"bogus": True}, scenario="ok")
        # connect() delivers the doc; it lacks 'str' -> clean error
        with pytest.raises(Exception) as exc:
            _client(fake).replace_all("n", "x")
        assert "Unexpected 'doc' payload" in str(exc.value)