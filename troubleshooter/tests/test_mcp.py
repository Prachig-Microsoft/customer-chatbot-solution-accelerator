import asyncio
import sys

import pytest

from conftest import TOOL_ROOT

mcp = pytest.importorskip("mcp")


def test_mcp_server_lists_read_only_tools():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    result = {}

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "accel_troubleshooter.mcp_server"], cwd=str(TOOL_ROOT)
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result["tools"] = {t.name for t in (await session.list_tools()).tools}
                res = await session.call_tool("identify_accelerator", {"issue": "chat with your data fails"})
                result["text"] = res.content[0].text

    try:
        asyncio.run(asyncio.wait_for(run(), timeout=90))
    except BaseException as exc:  # noqa: BLE001 - tolerate the known stdio teardown race only
        if "text" not in result or "BrokenResource" not in repr(exc):
            raise
    tools, text = result["tools"], result["text"]
    assert {"identify_accelerator", "search_docs", "diagnose", "quota_check", "preflight", "list_checks"} <= tools
    assert not any("fix" in t or "execute" in t for t in tools)
    assert "chat-with-your-data" in text
