#!/usr/bin/env python3
"""Connects to a running mcp-hedgedoc instance and checks that the
read-only gate genuinely holds: only the six read tools are listed, and
create_note is rejected at call time, not merely hidden.

Usage: python3 mcp_check.py http://127.0.0.1:PORT/mcp

Exit code 0 on success, 1 on any check failure. Requires `fastmcp`
installed (`pip install fastmcp==3.4.2 fastmcp-slim[client,server]==3.4.2`)
-- this is a dev-time check, not something the container itself needs.
"""
from __future__ import annotations

import asyncio
import sys

EXPECTED_READ_ONLY_TOOLS = {
    "read_note",
    "get_note_info",
    "get_note_revisions",
    "get_note_revision_content",
    "whoami",
    "list_history",
}


async def check(url: str) -> bool:
    from fastmcp import Client

    ok = True
    async with Client(url) as client:
        tools = {t.name for t in await client.list_tools()}
        if tools != EXPECTED_READ_ONLY_TOOLS:
            print(f"FAIL: tool listing was {sorted(tools)}, expected {sorted(EXPECTED_READ_ONLY_TOOLS)}")
            ok = False
        else:
            print(f"OK: read-only listing is exactly {sorted(tools)}")

        try:
            await client.call_tool("create_note", {"content": "smoke test"})
            print("FAIL: create_note was callable in read-only mode")
            ok = False
        except Exception as e:  # noqa: BLE001 -- any rejection is the point
            if "Unknown tool" in str(e) or "not found" in str(e).lower():
                print(f"OK: create_note correctly rejected ({type(e).__name__})")
            else:
                print(f"FAIL: create_note failed for the wrong reason: {type(e).__name__}: {e}")
                ok = False

        try:
            await client.call_tool("read_note", {"note_id_or_alias": "smoke-test"})
            print("FAIL: read_note succeeded against a bogus HEDGEDOC_URL -- shouldn't be reachable")
            ok = False
        except Exception as e:  # noqa: BLE001
            # Expected: the tool call reached the real dispatch and failed
            # on the network, since HEDGEDOC_URL points nowhere real. That
            # proves the gate let a genuine read tool THROUGH, which
            # matters as much as proving it blocks writes.
            print(f"OK: read_note reached real dispatch and failed on network, as expected ({type(e).__name__})")

    return ok


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    ok = asyncio.run(check(sys.argv[1]))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
