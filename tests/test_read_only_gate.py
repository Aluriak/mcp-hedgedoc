"""The one test that must never be allowed to regress: in read-only mode,
a write tool must be genuinely unreachable -- not merely absent from the
tool listing. This is the entire safety property the ro/rw variant split
in the wider LibreChat fleet depends on.
"""
import importlib
import os

import pytest
from fastmcp.exceptions import NotFoundError


async def _fresh_server_module(read_only: bool):
    os.environ["HEDGEDOC_URL"] = "http://localhost:1"
    os.environ["HEDGEDOC_EMAIL"] = "bot@example.org"
    os.environ["HEDGEDOC_PASSWORD"] = "secret"
    os.environ["HEDGEDOC_READ_ONLY"] = "true" if read_only else "false"

    import hedgedoc_mcp.server as server_module

    importlib.reload(server_module)
    # Mirrors exactly what __main__.main() does before mcp.run() --
    # see server.py's apply_read_only_gate() docstring for why this
    # isn't done automatically at import time.
    await server_module.apply_read_only_gate()
    return server_module


@pytest.mark.asyncio
async def test_read_only_mode_lists_only_read_tools():
    server_module = await _fresh_server_module(read_only=True)
    tools = await server_module.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "read_note",
        "get_note_info",
        "get_note_revisions",
        "get_note_revision_content",
        "whoami",
        "list_history",
    }
    assert "create_note" not in names


@pytest.mark.asyncio
async def test_read_only_mode_blocks_create_note_at_call_time():
    server_module = await _fresh_server_module(read_only=True)
    with pytest.raises(NotFoundError):
        await server_module.mcp.call_tool("create_note", {"content": "x"})


@pytest.mark.asyncio
async def test_rw_mode_lists_all_seven_tools():
    server_module = await _fresh_server_module(read_only=False)
    tools = await server_module.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "read_note",
        "get_note_info",
        "get_note_revisions",
        "get_note_revision_content",
        "whoami",
        "list_history",
        "create_note",
    }
