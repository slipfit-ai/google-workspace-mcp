"""Manual smoke test: start the server over stdio like Claude Code does and call it."""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command="uv", args=["run", "google-workspace-mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print("instructions present:", bool(init.instructions))
        print("tools:", [t.name for t in (await session.list_tools()).tools])
        result = await session.call_tool("list_accounts", {})
        print("list_accounts:", result.structured_content or result.content)


asyncio.run(main())
