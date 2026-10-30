"""MCP server exposing the troubleshooter's read-only capabilities to GitHub Copilot (VS Code agent mode).

Run:  python troubleshooter/mcp_launcher.py      (stdio transport)

Every tool runs the real CLI (``python -m accel_troubleshooter ...``) in a **fresh process**. That guarantees chat and
terminal use exactly the same code and give the same findings, and a long-lived server can never serve stale code
after the troubleshooter is updated.

Remediation: read-only tools never change anything. The one state-changing tool, ``apply_fix``, refuses unless the
caller passes ``user_approved=true`` (the agent instructions allow that only after the user said yes to the shown
change) and only runs the small allow-list in ``actions.EXECUTABLE_ACTIONS``; it then verifies and re-checks health.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional

from .actions import EXECUTABLE_ACTIONS
from .repo import TOOL_ROOT

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional dependency
    raise SystemExit("The 'mcp' package is required: pip install -r troubleshooter/requirements-mcp.txt") from exc

mcp = FastMCP("accelerator-troubleshooter")

CLI_TIMEOUT = 480  # a full health check on a large deployment can take a few minutes
FIX_TIMEOUT = 2400  # tag fix waits for Azure to apply tags and the Cosmos update (typically 5-15 min), then re-checks


def _cli(args: List[str], timeout: int = CLI_TIMEOUT) -> str:
    """Run the CLI in a fresh interpreter and return its text. Exit codes 1-3 (findings, clarification) are normal."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    try:
        res = subprocess.run(
            [sys.executable, "-m", "accel_troubleshooter", *args],
            cwd=str(TOOL_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"The troubleshooter did not finish within {timeout} s. Try again, or run it in a terminal."
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    return out or err or f"(no output, exit code {res.returncode})"


def _common(
    accelerator: Optional[str],
    resource_group: Optional[str],
    region: Optional[str],
    env: Optional[str] = None,
    subscription: Optional[str] = None,
    offline: bool = False,
) -> List[str]:
    args = ["--no-prompt", "--format", "full"]
    for flag, value in (
        ("--accelerator", accelerator),
        ("--resource-group", resource_group),
        ("--region", region),
        ("--env", env),
        ("--subscription", subscription),
    ):
        if value:
            args += [flag, value]
    if offline:
        args.append("--offline")
    return args


@mcp.tool()
def identify_accelerator(issue: str, accelerator: Optional[str] = None) -> str:
    """Identify which Azure solution accelerator an issue belongs to. Returns a clarification question when
    identification is ambiguous — ask the user that question instead of guessing."""
    return _cli(["identify", issue] + (["--accelerator", accelerator] if accelerator else []), timeout=120)


@mcp.tool()
def search_docs(query: str, accelerator: Optional[str] = None, k: int = 5) -> str:
    """Search the accelerator's indexed troubleshooting/deployment docs and scripts. Every hit includes file path,
    line range, repo, branch and commit so it can be cited."""
    args = ["search", query, "-k", str(k), "--format", "json"]
    return _cli(args + (["--accelerator", accelerator] if accelerator else []), timeout=180)


@mcp.tool()
def about_accelerator(accelerator: Optional[str] = None) -> str:
    """Tell the user what the accelerator in this workspace is: its name, a 2-3 sentence description taken from its own
    README (with file and line), Azure services, deployment flavors, the docs searched first, and the source repo.
    Use for "what is this?", "about this accelerator", or when the user says hi and has not asked anything yet."""
    return _cli(["about"] + (["--accelerator", accelerator] if accelerator else []), timeout=120)


@mcp.tool()
def health_check(
    resource_group: Optional[str] = None,
    accelerator: Optional[str] = None,
    env: Optional[str] = None,
    region: Optional[str] = None,
    subscription: Optional[str] = None,
) -> str:
    """Full read-only health check of a deployed accelerator. By default it checks the current azd environment's
    resource group. To check a different resource group pass resource_group (and region if it is not discoverable);
    if that group is in another subscription the user must first run 'az account set --subscription <id>'."""
    return _cli(["health", *_common(accelerator, resource_group, region, env, subscription)])


@mcp.tool()
def diagnose(
    issue: str,
    accelerator: Optional[str] = None,
    resource_group: Optional[str] = None,
    region: Optional[str] = None,
    offline: bool = False,
    full: bool = False,
    env: Optional[str] = None,
    subscription: Optional[str] = None,
) -> str:
    """Run the evidence-based diagnosis (read-only Azure CLI checks + docs + repo config) for a problem described in
    plain words (or pasted error text) and return a Markdown report with confirmed findings, likely/alternative
    causes, missing evidence, cited docs and categorized remediation (read-only / approval required / restricted).
    resource_group defaults to the current azd environment's; pass it to investigate a different resource group."""
    args = ["diagnose", issue, *_common(accelerator, resource_group, region, env, subscription, offline)]
    return _cli(args + (["--full"] if full else []))


@mcp.tool()
def quota_check(
    accelerator: Optional[str] = None,
    region: Optional[str] = None,
    capacity_overrides: Optional[dict] = None,
) -> str:
    """End-to-end model quota workflow: derive required models/capacity from the accelerator's Bicep + azd env,
    read live quota for the target (or all allowed) regions, and recommend a region/capacity."""
    args = ["quota", *_common(accelerator, None, region)]
    for param, value in (capacity_overrides or {}).items():
        args += ["--capacity", f"{param}={int(value)}"]
    return _cli(args)


@mcp.tool()
def preflight(accelerator: Optional[str] = None, region: Optional[str] = None) -> str:
    """Pre-deployment check (login, providers, quota in all allowed regions, model availability, config drift).
    Returns a GO / NO-GO verdict with a recommended region."""
    return _cli(["preflight", *_common(accelerator, None, region)])


@mcp.tool()
def apply_fix(
    action_id: str,
    user_approved: bool,
    issue: str = "check my deployment health",
    resource_group: Optional[str] = None,
    accelerator: Optional[str] = None,
    region: Optional[str] = None,
    env: Optional[str] = None,
    subscription: Optional[str] = None,
) -> str:
    """CHANGES AZURE/LOCAL STATE. Apply ONE fix proposed by a previous diagnose/health_check, then verify it and
    re-run the full health check. Call this ONLY after you showed the user the proposed fix (its commands and risk,
    from the report) and the user explicitly answered yes in this conversation. Pass user_approved=true only in that
    case. action_id is the id shown under 'NEXT'/'Recommended remediation' (e.g. apply-exemption-tag). Use the same
    issue/resource_group you used for the diagnosis. Only a small allow-list of fixes can run; anything else is
    refused."""
    base = action_id.split("#")[0]
    if base not in EXECUTABLE_ACTIONS:
        return f"'{base}' is not an action the agent can execute. Show the documented steps to the user instead."
    if not user_approved:
        return (
            "Not applied: the user has not approved this change. Ask them first and call again with user_approved=true."
        )
    args = ["fix", action_id, "--issue", issue, "--approve", "--yes", "--full"]
    return _cli(args + _common(accelerator, resource_group, region, env, subscription), timeout=FIX_TIMEOUT)


@mcp.tool()
def list_checks() -> str:
    """List available diagnostic checks and the issue stages they cover."""
    return _cli(["checks"], timeout=120)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
