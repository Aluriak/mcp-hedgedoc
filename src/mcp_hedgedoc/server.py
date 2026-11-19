"""The MCP server itself.

Read-only gating works the same way as this fleet's ClickHouse middleware:
every tool carries a real `ToolAnnotations(readOnlyHint=...)`, and when
HEDGEDOC_READ_ONLY=true we derive the set of write tools *from those
annotations* and hide them with a FastMCP Visibility transform -- enforced
at the server/handler level (an unknown-tool error), not just hidden from
the tool listing. See AGENTS.md for why this indirection (deriving the
name set at startup) is preferred over hand-maintaining a list.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from fastmcp import FastMCP
from fastmcp.server.transforms.visibility import Visibility
from mcp.types import ToolAnnotations

from .client import HedgeDocClient, SessionExpiredError
from .config import Settings

settings = Settings.from_env()
mcp = FastMCP(
    "mcp-hedgedoc",
    instructions=(
        "Reads, creates, and updates notes on a HedgeDoc instance, through "
        "a single shared service account -- every action appears under that "
        "account in HedgeDoc's own history, never under the real person's "
        "identity. update_note replaces a note's ENTIRE content over "
        "HedgeDoc's realtime editor channel (there is no HTTP update "
        "endpoint); there is no tool for partial/in-place edits and none to "
        "delete a note."
    ),
)

_client: HedgeDocClient | None = None


def _get_client() -> HedgeDocClient:
    global _client
    if _client is None:
        _client = HedgeDocClient(settings)
    return _client


F = TypeVar("F", bound=Callable[..., Any])


def _with_relogin(fn: F) -> F:
    """Run fn(client, *a, **kw); on SessionExpiredError, log in again once
    and retry exactly once more. A second failure is a real error, not
    silently retried forever.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        client = _get_client()
        try:
            return fn(client, *args, **kwargs)
        except SessionExpiredError:
            client.re_login()
            return fn(client, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


# Wrapped once, at import time -- not re-wrapped on every call. Add a new
# entry here for every new HedgeDocClient method a tool needs.
_read_note = _with_relogin(HedgeDocClient.read_note)
_get_note_info = _with_relogin(HedgeDocClient.get_note_info)
_get_note_revisions = _with_relogin(HedgeDocClient.get_note_revisions)
_get_note_revision_content = _with_relogin(HedgeDocClient.get_note_revision_content)
_whoami = _with_relogin(HedgeDocClient.whoami)
_list_history = _with_relogin(HedgeDocClient.list_history)
_create_note = _with_relogin(HedgeDocClient.create_note)
_update_note = _with_relogin(HedgeDocClient.update_note)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def read_note(note_id_or_alias: str) -> str:
    """Return the raw Markdown content of a note. Works without any login
    at all for notes whose permission is freely/editable/locked (the
    HedgeDoc default) -- fails with a permission error for private/limited/
    protected notes the service account can't see."""
    return _read_note(note_id_or_alias)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def get_note_info(note_id_or_alias: str) -> dict:
    """Metadata for a note: title, description, view count, creation and
    last-update time. Same permission rules as read_note."""
    return _get_note_info(note_id_or_alias)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def get_note_revisions(note_id_or_alias: str) -> list[dict]:
    """List the saved revisions of a note (timestamp + length of each)."""
    return _get_note_revisions(note_id_or_alias)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def get_note_revision_content(note_id_or_alias: str, revision_time_ms: int) -> str:
    """Raw Markdown content of a note as of a specific revision timestamp
    (milliseconds since epoch, as returned by get_note_revisions)."""
    return _get_note_revision_content(note_id_or_alias, revision_time_ms)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def whoami() -> dict:
    """Identity of the currently authenticated session -- always the
    shared service account configured for this server, never the real
    LibreChat user asking the question. Useful mainly to confirm the
    server is actually logged in."""
    return _whoami()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def list_history() -> list[dict]:
    """Notes the service account's session has recently viewed or pinned.
    This is an activity log for the shared account, not a list of notes
    owned by any particular real person."""
    return _list_history()


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    )
)
def create_note(content: str, alias: str | None = None) -> dict:
    """Create a NEW note and return its id and URL. If `alias` is given,
    the note is reachable at that custom slug (requires
    CMD_ALLOW_FREEURL=true server-side) -- but if a note already exists at
    that alias, this fails rather than overwriting it: HedgeDoc has no HTTP
    endpoint to modify existing note content, only its realtime editor
    does -- that is what update_note drives. There is no delete_note tool
    at all."""
    return _create_note(content, alias)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
    )
)
def update_note(note_id_or_alias: str, new_content: str) -> dict:
    """Replace the ENTIRE Markdown content of an EXISTING note.

    HedgeDoc 1.x has no HTTP endpoint for content changes, so this drives
    the note's Socket.IO realtime editor channel (doc -> operation ->
    ack) and then reads the note back over HTTP until the new content is
    confirmed persisted. Use this to update a note; use create_note to
    make a new one.

    Behaviour and limits, read before calling:

    - FULL replacement only. Whatever is in the note now is replaced by
      ``new_content`` in its entirety; there is no in-place/partial edit.
    - Identity: the change is attributed to this server's shared service
      account in HedgeDoc's history, never to the person asking in
      LibreChat.
    - Permissions are enforced by HedgeDoc, not here: freely notes can be
      edited by anyone; editable/limited need a logged-in user; locked /
      private / protected can only be edited by their OWNER. If the
      service account lacks the right, HedgeDoc sends no explicit
      refusal -- the tool fails after a timeout with a message saying so.
    - Concurrency: if someone else is editing at the same time, HedgeDoc
      transforms the changes together (both survive); if the server
      forces a resync, this retries automatically.
    - Returns {note_id, new_revision, verified: true} once the read-back
      matches, so a verified result is genuinely in the database."""
    return _update_note(note_id_or_alias, new_content)


async def apply_read_only_gate() -> None:
    """Hide every write tool (readOnlyHint is False) when HEDGEDOC_READ_ONLY
    is set. Async, and deliberately NOT called at module-import time:
    fastmcp's list_tools() needs a running event loop (middleware chain,
    context managers), and importing this module from inside one already
    running -- e.g. from a test, or from any other async caller -- would
    otherwise crash with "asyncio.run() cannot be called from a running
    event loop". __main__.py calls this once, synchronously, before
    mcp.run() starts the server's own loop; tests call it directly with
    `await` since they already run inside one.
    """
    if not settings.read_only:
        return
    tools = await mcp.list_tools()
    write_names = {t.name for t in tools if t.annotations and t.annotations.readOnlyHint is False}
    mcp.add_transform(Visibility(False, names=write_names))
