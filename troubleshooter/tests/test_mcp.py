import asyncio
import sys

import pytest

from conftest import TOOL_ROOT

mcp = pytest.importorskip("mcp")


def test_mcp_config_does_not_depend_on_variable_expansion_or_cwd():
    import json

    cfg = json.loads((TOOL_ROOT.parent / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    srv = cfg["servers"]["accelerator-troubleshooter"]
    assert "cwd" not in srv and "${" not in json.dumps(srv)
    assert (TOOL_ROOT.parent / srv["args"][0]).is_file()


def test_mcp_server_lists_read_only_tools():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    result = {}

    async def run():
        # Launch exactly like .vscode/mcp.json does, from the repo root rather than the troubleshooter folder.
        params = StdioServerParameters(
            command=sys.executable, args=["troubleshooter/mcp_launcher.py"], cwd=str(TOOL_ROOT.parent)
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
