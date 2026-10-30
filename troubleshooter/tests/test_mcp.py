import asyncio
import os
import re
import subprocess
import sys

import pytest

from conftest import TOOL_ROOT

mcp = pytest.importorskip("mcp")


def test_mcp_tools_run_the_cli_in_a_fresh_process_so_chat_equals_terminal(tmp_path):
    """The root cause of 'chat and terminal disagree' was a long-lived server running old code."""
    src = (TOOL_ROOT / "accel_troubleshooter" / "mcp_server.py").read_text(encoding="utf-8")
    assert "subprocess.run" in src and '"-m", "accel_troubleshooter"' in src
    assert "from .engine" not in src and "Troubleshooter(" not in src  # no in-process engine that could go stale
    assert "health_check" in src and "resource_group" in src


def test_mcp_health_tool_matches_the_cli_output_exactly(synthetic_env_file):
    from accel_troubleshooter import mcp_server

    args = [
        "diagnose",
        "chat returns 500 errors",
        "--no-prompt",
        "--format",
        "full",
        "--env-file",
        synthetic_env_file,
        "--replay",
        str(TOOL_ROOT / "evals" / "fixtures" / "synthetic-base.json"),
    ]
    via_tool = mcp_server._cli(args)
    direct = subprocess.run(
        [sys.executable, "-m", "accel_troubleshooter", *args],
        cwd=TOOL_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
    ).stdout.strip()

    def strip(text):  # run ids and timings legitimately differ between two runs
        return re.sub(r"`[0-9a-f]{12}`|Generated [^\n]*|\| \d+ ms|\d{4}-\d\d-\d\dT[\d:]+Z", "", text)

    assert strip(via_tool) == strip(direct) and "Troubleshooting report" in via_tool


def test_mcp_common_args_pass_resource_group_and_never_prompt():
    from accel_troubleshooter.mcp_server import _common

    a = _common(None, "rg-other", "swedencentral", None, None, False)
    assert "--no-prompt" in a and a[a.index("--resource-group") + 1] == "rg-other"
    assert a[a.index("--region") + 1] == "swedencentral" and "--offline" not in a


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
