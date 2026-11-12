"""MCP server exposing the troubleshooter's read-only capabilities to GitHub Copilot (VS Code agent mode).

Run:  python troubleshooter/mcp_launcher.py      (stdio transport)

Every tool runs the real CLI (``python -m accel_troubleshooter ...``) in a **fresh process**. That guarantees chat and
terminal use exactly the same code and give the same findings, and a long-lived server can never serve stale code
after the troubleshooter is updated.

Remediation: read-only tools never change anything. Two tools act on the user's behalf and both refuse unless the caller
passes ``user_approved=true`` (the agent instructions allow that only after the user said yes): ``apply_fix`` (runs one
allow-listed fix, then verifies and re-checks health) and ``chat_test`` (sends one real chat message and deletes the
test session). ``draft_guide_entry`` only prints a draft; it never edits the guide. ``delete_resource_group`` is the
only destructive tool: it deletes one resource group, and only after the server itself asked the user, in two
confirmation forms the model cannot see or fill, to type the exact name and then ``delete <name>``; the group is
re-checked before and right before deleting.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from typing import List, Optional

from .actions import EXECUTABLE_ACTIONS
from .repo import TOOL_ROOT

try:
    from mcp.server.fastmcp import Context, FastMCP
    from pydantic import BaseModel, Field
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
    window: Optional[str] = None,
) -> List[str]:
    args = ["--no-prompt", "--format", "full"]
    for flag, value in (
        ("--accelerator", accelerator),
        ("--resource-group", resource_group),
        ("--region", region),
        ("--env", env),
        ("--subscription", subscription),
        ("--window", window),
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
def resume_plan(
    resource_group: Optional[str] = None, error: Optional[str] = None, accelerator: Optional[str] = None
) -> str:
    """A deployment stopped halfway: runs a FULL health check (plus analysis of the azd error, if the user pasted one in
    'error') and decides where it stopped and whether re-running is safe: NOTHING TO RESUME, WAIT, FIX FIRST, SAFE TO
    RESUME (azd provision) or SAFE TO RESUME THE LAST STEP (post-provision hook). Infrastructure counts as done only
    when azd's own provision deployment succeeded. Changes nothing. Ask the user for the error first if they have it."""
    args = ["resume"]
    if error:
        args.append(error)
    return _cli(args + _common(accelerator, resource_group, None))


@mcp.tool()
def cleanup_plan(resource_group: Optional[str] = None, accelerator: Optional[str] = None) -> str:
    """Read-only plan for cleaning up a deployment (azd down): what would be deleted, locks and soft-deleted resources
    that need a purge, what the accelerator's own hooks and docs say, and the exact commands for the user to run.
    Deletes nothing and never will: show the commands, never run them."""
    return _cli(["cleanup", *_common(accelerator, resource_group, None)])


class ConfirmName(BaseModel):
    resource_group: str = Field(description="Type the exact name of the resource group to delete (case-sensitive)")


class ConfirmPhrase(BaseModel):
    confirmation: str = Field(description="Type exactly: delete <resource-group-name>")


_FP = re.compile(r"plan fingerprint is:\s*([0-9a-f]{6,})")
_SUMMARY = re.compile(r"^\s*✔ (.+resource\(s\).+lock\(s\))\s*$", re.M)
TERMINAL_ROUTE = "python -m accel_troubleshooter cleanup --delete --resource-group {rg}"


@mcp.tool()
async def delete_resource_group(resource_group: str, ctx: Context, accelerator: Optional[str] = None) -> str:
    """DESTRUCTIVE: deletes exactly one resource group and everything in it (az group delete). Call it once with the
    group the user asked to delete. The tool re-checks the group in Azure (exists, subscription, locks, contents), then
    asks the USER directly in two VS Code input forms to type the exact name and then 'delete <name>'; you never see
    or supply those answers. Anything other than an exact match, a cancel, or a change to the group deletes nothing.
    If the client cannot show forms, it deletes nothing and gives the terminal command instead."""
    rg = resource_group
    base = ["cleanup", "--delete", *_common(accelerator, rg, None)]
    checks = await asyncio.to_thread(_cli, base)
    found = _FP.search(checks)
    if not found:
        return checks + "\n\nNOT DELETED: the checks above block it (or the group could not be read)."
    fp = found.group(1)
    what = _SUMMARY.search(checks)
    what_text = what.group(1) if what else f"{rg} and everything in it"
    try:
        first = await ctx.elicit(
            message=(
                f"⚠ Permanently delete the resource group '{rg}'? ({what_text}). This cannot be undone.\n"
                "Type the exact name of the resource group to continue."
            ),
            schema=ConfirmName,
        )
    except Exception:  # noqa: BLE001 - client without elicitation support
        return (
            checks + "\n\nNOT DELETED: this chat client cannot show the confirmation form. Run it in your own terminal "
            "(it asks both questions there): " + TERMINAL_ROUTE.format(rg=rg)
        )
    if first.action != "accept" or first.data is None:
        return "NOT DELETED: you cancelled at the first confirmation."
    name = first.data.resource_group
    if name != rg:
        return f"NOT DELETED: the name typed was not exactly '{rg}'. Nothing was changed."
    second = await ctx.elicit(message=f"Last check. Type exactly:  delete {rg}", schema=ConfirmPhrase)
    if second.action != "accept" or second.data is None:
        return "NOT DELETED: you cancelled at the second confirmation."
    args = base + ["--confirm-name", name, "--confirm", second.data.confirmation, "--fingerprint", fp]
    return await asyncio.to_thread(_cli, args, FIX_TIMEOUT)


@mcp.tool()
def doc_drift(accelerator: Optional[str] = None) -> str:
    """Offline report of where the accelerator's docs and scripts disagree with its Bicep and parameter files (for
    example a documented default capacity that differs from the template, regions or models the template does not
    use, undocumented variables, broken links). Each finding names the file and line to edit and how. Nothing in the
    repository is changed; the full list of document gaps (drift, tool versions that documents disagree on, missing
    docs, broken links) is also saved to the local, git-ignored .runs/document-gaps.md. Use for 'is the documentation
    up to date?' or before a release."""
    return _cli(["drift", "--no-color"] + (["--accelerator", accelerator] if accelerator else []), timeout=180)


@mcp.tool()
def draft_guide_entry(
    issue: str,
    resource_group: Optional[str] = None,
    note: Optional[str] = None,
    accelerator: Optional[str] = None,
    force: bool = False,
) -> str:
    """Draft one new row for the accelerator's troubleshooting guide from a diagnosed incident: generalised (no
    resource names, ids or secrets), in the guide's table format, with the section and line it belongs to. It only
    PRINTS the draft: nothing is written. If the guide already documents the problem it says so. Pass 'note' with
    what fixed it. Show the row to the user; only if they ask you to add it, insert it at the stated line."""
    args = ["draft-guide", issue, *_common(accelerator, resource_group, None)]
    if note:
        args += ["--note", note]
    return _cli(args + (["--force"] if force else []))


@mcp.tool()
def chat_test(
    user_approved: bool,
    resource_group: Optional[str] = None,
    client_id: Optional[str] = None,
    accelerator: Optional[str] = None,
    env: Optional[str] = None,
    region: Optional[str] = None,
    subscription: Optional[str] = None,
) -> str:
    """SENDS ONE REAL CHAT MESSAGE as the signed-in user to the deployed chat API (uses a little model quota and
    writes one chat session, which it deletes afterwards), together with the full health check. It proves whether a
    user can really get a model reply, and names where it breaks (sign-in, Cosmos DB, agents, model). Call this ONLY
    after the user explicitly said yes in this conversation, then pass user_approved=true. client_id is the API app
    registration's client id if it is not in the azd environment."""
    if not user_approved:
        return (
            "Not sent: the user has not approved a real chat message. Ask them first (it uses their sign-in, one "
            "model call, and the test session is deleted afterwards), then call again with user_approved=true."
        )
    args = ["chat-test", "--yes", *_common(accelerator, resource_group, region, env, subscription)]
    return _cli(args + (["--client-id", client_id] if client_id else []))


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
    window: Optional[str] = None,
) -> str:
    """Full read-only health check of a deployed accelerator, including the errors the apps logged to Application
    Insights (last hour; pass window such as PT15M, PT6H or P1D to change it). By default it checks the current azd
    environment's resource group. To check a different resource group pass resource_group (and region if it is not
    discoverable); if that group is in another subscription the user must first run
    'az account set --subscription <id>'. It cannot prove a real chat reply: for that offer chat_test."""
    return _cli(["health", *_common(accelerator, resource_group, region, env, subscription, window=window)])


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
    window: Optional[str] = None,
) -> str:
    """Run the evidence-based diagnosis (read-only Azure CLI checks + docs + repo config) for a problem described in
    plain words (or pasted error text) and return a Markdown report with confirmed findings, likely/alternative
    causes, missing evidence, cited docs and categorized remediation (read-only / approval required / restricted).
    resource_group defaults to the current azd environment's; pass it to investigate a different resource group.
    Runtime problems also read the apps' logged errors from Application Insights (window, default PT1H)."""
    args = ["diagnose", issue, *_common(accelerator, resource_group, region, env, subscription, offline, window)]
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
def preflight(accelerator: Optional[str] = None, region: Optional[str] = None, location: Optional[str] = None) -> str:
    """Pre-deployment check (login, providers, model quota in all allowed AI regions, model availability, App Service
    plan quota in the primary location, tools, config drift). Returns a GO / NO-GO verdict with a recommended AI region
    (`region`, model quota) and, separately, a recommended primary location (`location`, App Service quota).
    "GO (models only; App Service quota not checked)" is not a plain GO: report it as written."""
    args = ["preflight", *_common(accelerator, None, region)]
    if location:
        args += ["--location", location]
    return _cli(args)


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
