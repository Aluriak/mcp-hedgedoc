import pytest
import responses

from mcp_hedgedoc.client import (
    HedgeDocClient,
    HedgeDocError,
    NoteAlreadyExistsError,
    NoteNotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
)
from mcp_hedgedoc.config import Settings
from mcp_hedgedoc.realtime import RealtimeUpdateError

BASE = "https://pad.example.org"


def _settings(**overrides):
    env = {
        "HEDGEDOC_URL": BASE,
        "HEDGEDOC_EMAIL": "bot@example.org",
        "HEDGEDOC_PASSWORD": "secret",
    }
    env.update(overrides)
    import os

    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return Settings.from_env()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mock_login_ok():
    responses.add(
        responses.POST,
        f"{BASE}/login",
        status=302,
        headers={"Location": f"{BASE}/"},
    )
    # login_local() uses allow_redirects=True, so requests follows the 302
    # above with a real GET -- has to be mocked too, same as it would hit
    # the real home page on an actual server.
    responses.add(responses.GET, f"{BASE}/", body="<html>home</html>", status=200)
    responses.add(
        responses.GET,
        f"{BASE}/me",
        json={"status": "ok", "id": "bot-id", "name": "librechat-bot", "photo": None},
        status=200,
    )


@responses.activate
def test_login_then_read_note():
    _mock_login_ok()
    responses.add(
        responses.GET,
        f"{BASE}/my-note/download",
        body="# Hello\n\nSome content.",
        status=200,
    )
    client = HedgeDocClient(_settings())
    assert client.read_note("my-note") == "# Hello\n\nSome content."


@responses.activate
def test_read_note_permission_denied():
    _mock_login_ok()
    responses.add(responses.GET, f"{BASE}/private-note/download", status=403)
    client = HedgeDocClient(_settings())
    with pytest.raises(PermissionDeniedError):
        client.read_note("private-note")


@responses.activate
def test_read_note_not_found():
    _mock_login_ok()
    responses.add(responses.GET, f"{BASE}/nope/download", status=404)
    client = HedgeDocClient(_settings())
    with pytest.raises(NoteNotFoundError):
        client.read_note("nope")


@responses.activate
def test_get_note_info():
    _mock_login_ok()
    responses.add(
        responses.GET,
        f"{BASE}/my-note/info",
        json={
            "title": "My note",
            "description": "a description",
            "viewcount": 42,
            "createtime": "2026-01-01T00:00:00.000Z",
            "updatetime": "2026-01-02T00:00:00.000Z",
        },
    )
    client = HedgeDocClient(_settings())
    info = client.get_note_info("my-note")
    assert info["title"] == "My note"
    assert info["viewcount"] == 42


@responses.activate
def test_create_note_without_alias():
    _mock_login_ok()
    responses.add(
        responses.POST,
        f"{BASE}/new",
        status=302,
        headers={"Location": f"{BASE}/aRandomShortId"},
    )
    client = HedgeDocClient(_settings())
    result = client.create_note("# New note")
    assert result == {"note_id": "aRandomShortId", "url": f"{BASE}/aRandomShortId"}


@responses.activate
def test_create_note_with_alias_conflict():
    _mock_login_ok()
    responses.add(responses.POST, f"{BASE}/new/existing-alias", status=409)
    client = HedgeDocClient(_settings())
    with pytest.raises(NoteAlreadyExistsError):
        client.create_note("# content", alias="existing-alias")


@responses.activate
def test_create_note_session_expired_detected():
    _mock_login_ok()
    # redirected straight back to home -- HedgeDoc's tell for "you're not
    # actually logged in (any more)"
    responses.add(
        responses.POST,
        f"{BASE}/new",
        status=302,
        headers={"Location": f"{BASE}/"},
    )
    client = HedgeDocClient(_settings())
    with pytest.raises(SessionExpiredError):
        client.create_note("# content")


@responses.activate
def test_list_history():
    _mock_login_ok()
    responses.add(
        responses.GET,
        f"{BASE}/history",
        json={"history": [{"id": "abc", "text": "My note", "time": 1735689600000}]},
    )
    client = HedgeDocClient(_settings())
    history = client.list_history()
    assert history == [{"id": "abc", "text": "My note", "time": 1735689600000}]


@responses.activate
def test_get_note_revisions():
    _mock_login_ok()
    responses.add(
        responses.GET,
        f"{BASE}/my-note/revision",
        json={"revision": [{"time": 1735689600000, "length": 120}]},
    )
    client = HedgeDocClient(_settings())
    revisions = client.get_note_revisions("my-note")
    assert revisions == [{"time": 1735689600000, "length": 120}]


# --- update_note (realtime path, HTTP side tested with a fake realtime) ---


class _FakeRealtime:
    """Stands in for HedgeDocRealtimeClient: records the call, returns a
    fixed new revision. A raise_on flag simulates the silent-refusal
    timeout (RealtimeUpdateError)."""

    def __init__(self, new_revision=5, raise_on=None):
        self.new_revision = new_revision
        self.raise_on = raise_on
        self.calls = []

    def replace_all(self, note_id, new_content):
        self.calls.append((note_id, new_content))
        if self.raise_on:
            raise self.raise_on
        return self.new_revision


@responses.activate
def test_update_note_replaces_content_and_verifies():
    _mock_login_ok()
    client = HedgeDocClient(_settings())
    fake = _FakeRealtime(new_revision=5)

    # 1 pre-read (old content), then the poll sees old once before the
    # database catches up and returns the new content.
    responses.add(responses.GET, f"{BASE}/my-note/download", body="# old", status=200)
    responses.add(responses.GET, f"{BASE}/my-note/download", body="# old", status=200)
    responses.add(
        responses.GET, f"{BASE}/my-note/download", body="# brand new", status=200
    )

    result = client.update_note("my-note", "# brand new", realtime_factory=lambda: fake)
    assert result == {"note_id": "my-note", "new_revision": 5, "verified": True}
    assert fake.calls == [("my-note", "# brand new")]


@responses.activate
def test_update_note_fails_fast_when_note_missing():
    _mock_login_ok()
    responses.add(responses.GET, f"{BASE}/nope/download", status=404)
    client = HedgeDocClient(_settings())
    fake = _FakeRealtime()
    with pytest.raises(NoteNotFoundError):
        client.update_note("nope", "x", realtime_factory=lambda: fake)
    # the realtime path must never have run
    assert fake.calls == []


@responses.activate
def test_update_note_fails_fast_when_permission_denied():
    _mock_login_ok()
    responses.add(responses.GET, f"{BASE}/locked/download", status=403)
    client = HedgeDocClient(_settings())
    fake = _FakeRealtime()
    with pytest.raises(PermissionDeniedError):
        client.update_note("locked", "x", realtime_factory=lambda: fake)
    assert fake.calls == []


@responses.activate
def test_update_note_propagates_realtime_error():
    _mock_login_ok()
    responses.add(responses.GET, f"{BASE}/my-note/download", body="# old", status=200)
    client = HedgeDocClient(_settings())
    fake = _FakeRealtime(raise_on=RealtimeUpdateError("no ack (permission?)"))
    with pytest.raises(RealtimeUpdateError):
        client.update_note("my-note", "# new", realtime_factory=lambda: fake)


@responses.activate
def test_update_note_errors_when_persist_never_catches_up():
    _mock_login_ok()
    client = HedgeDocClient(_settings())
    fake = _FakeRealtime(new_revision=5)
    # pre-read + every poll returns the old content
    for _ in range(6):
        responses.add(responses.GET, f"{BASE}/my-note/download", body="# old", status=200)
    with pytest.raises(HedgeDocError) as exc:
        client.update_note(
            "my-note", "# new", realtime_factory=lambda: fake, verify_deadline=0.3
        )
    assert "still returns the old content" in str(exc.value)
