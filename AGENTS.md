# AGENTS.md

Guidance for anyone -- human or AI coding agent -- modifying this repo.
Read this before changing `server.py`, `auth.py`, or `client.py`: several
things here look like they could be simplified and aren't, for reasons
verified against HedgeDoc's actual source, not assumed.

## What this is

An MCP server for a HedgeDoc 1.x instance, built to be
dedoubled into `ro`/`rw` container variants the same way the rest of the
LibreChat MCP fleet is (ClickHouse, GitLab, Jira, Grafana) -- one image,
`HEDGEDOC_READ_ONLY` toggles which tools are actually callable.

## The identity model -- read this before "improving" auth.py

HedgeDoc 1.x has **no bearer-token API of any kind**. Every write and
every personalised read needs a real browser-style session cookie,
obtained by an actual login (`auth.py` has three ways to get one: a local
account, scripting HedgeDoc's OAuth2 redirect through Keycloak, or a
pre-obtained cookie). Whichever backend is configured, **this is always
one shared service account** -- there is no way to make HedgeDoc attribute
an action to the real LibreChat user asking for it, short of reimplementing
its entire OAuth2 authorization-code flow *per request* (impractical: it's
a stateful, redirect-driven, one-cookie-per-browser-session dance, not
something you attach a bearer token to). Don't build a feature that
implies otherwise (e.g. a `whoami` that pretends to know who's really
asking) -- `whoami` on purpose always reports the service account.

`HEDGEDOC_AUTH_MODE=oidc` does not change this. It only changes *where*
the service account's own credential is administered (a dedicated
Keycloak account in the central IdP, instead of a local HedgeDoc account
created with `bin/manage_users`) -- not what HedgeDoc believes about who's
acting. See `auth.py`'s module docstring for exactly what the `oidc` mode
does (it scrapes Keycloak's login form) and exactly how it breaks (custom
login theme, 2FA, required actions on the account). If it proves too
fragile in practice, `local` is the boring fallback and switching between
them is one env var, not a code change -- keep it that way.

## Why there is no update_note or delete_note

Checked directly against HedgeDoc's own source, not assumed and not taken
from another project's docs:

```js
// lib/web/note/util.js, newNote() -- both HedgeDoc 1.11.1 and 1.12.0
if (id) {
  return errors.errorConflict(res)   // 409, not an overwrite
}
```

`POST /new/{alias}` on an alias that already has a note returns 409. There
is no other REST-ish endpoint for changing note content -- HedgeDoc's
real-time editor writes through its Socket.IO channel, which this project
does not implement. **Do not** add an `update_note` tool that re-posts to
`/new/{alias}` expecting an overwrite; it will 409 on every call against a
note that already exists, and that's not a bug to route around, it's the
server telling you the truth. If updating notes ever becomes a real
requirement, that's a Socket.IO client to build (operational-transform
protocol, a session/auth handshake over the socket) -- a project of its
own, not a quick addition here.

There is also no `delete_note`: no HTTP endpoint for it exists at all in
HedgeDoc 1.x, only from the web UI.

## Why this doesn't just query HedgeDoc's Postgres database directly

It was considered and rejected for writes specifically, for a concrete,
verified reason -- not generic caution:

```js
// lib/realtime.js -- HedgeDoc's own persistence loop
const notes = {}
setInterval(function () {
  // for every note someone currently has open in the live editor...
  if (note.server.isDirty) {
    note.server.isDirty = false
    updateNote(note, ...)   // flushes the IN-MEMORY copy over the DB row
  }
}, 1000)
```

While a note is open in anyone's browser, its authoritative content lives
in this in-memory map, and this loop overwrites the DB with it every
second. A direct DB write to a "live" note's content would be silently
clobbered within about a second, with no error surfaced anywhere -- worse
than an HTTP 4xx, because nothing fails loudly. There is no way to tell
from outside whether a given note is "live" at the moment you'd write to
it. Reads are a different story (a Postgres read-only role could safely
list notes by real `ownerId`, which the HTTP API can't do at all -- `GET
/history` is a session-scoped activity log, not an ownership listing) --
but that's a separate, not-yet-built capability, and if it's ever added it
must use a role that is structurally incapable of writing, the same
"closed fallback" principle the ClickHouse Vault middleware uses.

## The read-only gate -- how it works and how to extend it

Every tool carries a real `ToolAnnotations(readOnlyHint=...)`. At startup
(`apply_read_only_gate()` in `server.py`, called explicitly from
`__main__.py` -- see its docstring for why NOT at module-import time),
the set of write-tool names is *derived* from those annotations and fed to
a FastMCP `Visibility(False, names=...)` transform. This blocks the tool
at the handler level (`call_tool` raises `NotFoundError: Unknown tool`),
not just at the listing level -- verified in
`tests/test_read_only_gate.py`, which is the single most important test
in this repo. Don't replace it with a hand-maintained name list: the
annotation IS the source of truth, keep it that way so a new tool can't
be added with the wrong readOnlyHint and silently ship into the wrong
variant.

To add a new tool:
1. Write the `HedgeDocClient` method first, with its own test in
   `test_client_smoke.py` (mock the exact HTTP status/body HedgeDoc's
   source actually returns -- check the source, this repo exists because
   another project's undocumented claim about `/new/{alias}` turned out to
   be wrong).
2. Add a `_with_relogin(HedgeDocClient.your_method)` wrapper next to the
   others in `server.py` (module level, wrapped once -- not inline inside
   the tool function).
3. Add the `@mcp.tool(...)` function with the *correct*
   `ToolAnnotations(readOnlyHint=...)` -- this is what determines which
   variant it ends up in, there's no second place to configure that.
4. Update the tool-name sets in `test_read_only_gate.py` and the table in
   `README.md`.

## Dependency pinning -- don't touch without checking both packages

```
fastmcp==3.4.2
fastmcp-slim[client,server]==3.4.2
```

`fastmcp` 3.4.2 is a thin wrapper that requires
`fastmcp-slim[client,server]==3.4.2` specifically; installing `fastmcp`
alone, or letting pip resolve `fastmcp-slim` freely, can silently pull a
newer `fastmcp-slim` that fastmcp itself then reports as incompatible.
Bump both together, deliberately, and rerun the full test suite
(`test_read_only_gate.py` especially -- it exercises real fastmcp
internals, not just this project's own code) before trusting a version
bump.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

All 24 tests run without any network access or real HedgeDoc instance:
`responses` mocks HTTP for the client tests, a hand-written HTML fixture
stands in for Keycloak's login page in the OIDC scraping tests. There is
deliberately no test that spins up a real Keycloak or HedgeDoc -- if you
need to verify against the real instance, use `scripts/smoke_test.sh`
against a built image instead (it talks to a real running container, but
still never touches the actual HedgeDoc URL -- it points at a bogus one
and only checks that the read-only gate holds).

## What was NOT verified in this repo's development sandbox

The `Containerfile` builds a wheel and installs it -- that part was
verified directly. Actually pulling `python:3.12-slim` and running the
resulting image end-to-end inside a container was not: the sandbox this
was built in has no access to any container registry. Do that check once,
for real, before considering this done -- `scripts/smoke_test.sh` is
written for exactly that moment.
