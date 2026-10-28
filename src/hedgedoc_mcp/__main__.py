from __future__ import annotations

import asyncio

from .server import apply_read_only_gate, mcp, settings


def main() -> None:
    # Two separate, sequential event-loop lifetimes -- not nested: this
    # one runs to completion and closes before mcp.run() starts its own.
    asyncio.run(apply_read_only_gate())
    mcp.run(transport="http", host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
