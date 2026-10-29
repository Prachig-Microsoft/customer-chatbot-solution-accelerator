"""MCP server exposing the troubleshooter's read-only capabilities to GitHub Copilot (VS Code agent mode).

Run:  python -m accel_troubleshooter.mcp_server      (stdio transport)

Remediation execution is intentionally NOT exposed here: state-changing actions are executed only through the
CLI ``fix <action-id> --approve`` with an interactive confirmation typed by a human.
"""

from __future__ import annotations

import json
from typing import Optional

from .azcli import AzRunner, OfflineRunner
from .diagnostics import CHECKS
from .engine import Troubleshooter
from .report import render_markdown

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional dependency
    raise SystemExit("The 'mcp' package is required: pip install -r troubleshooter/requirements-mcp.txt") from exc

mcp = FastMCP("accelerator-troubleshooter")


def _ts(offline: bool) -> Troubleshooter:
    return Troubleshooter(runner=OfflineRunner() if offline else AzRunner())


def _overrides(resource_group: Optional[str], region: Optional[str], all_regions: bool = False) -> dict:
    o = {}
    if resource_group:
        o["resource_group"] = resource_group
    if region:
        o["region"] = region
    if all_regions:
        o["all_regions"] = True
    return o


@mcp.tool()
def identify_accelerator(issue: str, accelerator: Optional[str] = None) -> str:
    """Identify which Azure solution accelerator an issue belongs to. Returns a clarification question when
    identification is ambiguous — ask the user that question instead of guessing."""
    return json.dumps(_ts(True).identify(issue, accelerator), indent=2)


@mcp.tool()
def search_docs(query: str, accelerator: Optional[str] = None, k: int = 5) -> str:
    """Search the accelerator's indexed troubleshooting/deployment docs and scripts. Every hit includes file path,
    line range, repo, branch and commit so it can be cited."""
    ts = _ts(True)
    accel = accelerator or ts.identify(query)["accelerator"]
    if not accel:
        return ts.identify(query)["question"]
    idx = ts.index_for(accel)
    if idx is None:
        return f"Repository for {accel} not available locally."
    hits = idx.search(query, k=k, error_codes=[w for w in query.split() if w[:1].isupper()])
    return json.dumps([h.to_dict() | {"text": h.text[:1200]} for h in hits], indent=2)


@mcp.tool()
def diagnose(
    issue: str,
    accelerator: Optional[str] = None,
    resource_group: Optional[str] = None,
    region: Optional[str] = None,
    offline: bool = False,
    full: bool = False,
) -> str:
    """Run the full evidence-based diagnosis (read-only Azure CLI checks + docs + repo config) and return a
    Markdown incident report with confirmed findings, likely/alternative causes, missing evidence, cited docs and
    categorized remediation (read-only / approval required / restricted)."""
    ts = _ts(offline)
    diag = ts.diagnose(issue, accelerator=accelerator, overrides=_overrides(resource_group, region), full=full)
    return render_markdown(diag.to_dict())


@mcp.tool()
def quota_check(
    accelerator: Optional[str] = None, region: Optional[str] = None, capacity_overrides: Optional[dict] = None
) -> str:
    """End-to-end model quota workflow: derive required models/capacity from the accelerator's Bicep + azd env,
    read live quota for the target (or all allowed) regions, and recommend a region/capacity."""
    ts = _ts(False)
    o = _overrides(None, region, all_regions=not region)
    if capacity_overrides:
        o["capacity_overrides"] = {k: int(v) for k, v in capacity_overrides.items()}
    diag = ts.diagnose(
        "quota check: is there enough model quota to deploy?",
        accelerator=accelerator,
        overrides=o,
        only_checks=["repo.azd_env", "repo.config_drift", "az.context", "quota.models", "models.availability"],
    )
    return render_markdown(diag.to_dict(), verbose=True)


@mcp.tool()
def preflight(accelerator: Optional[str] = None) -> str:
    """Pre-deployment health check (auth, providers, quota in all allowed regions, model availability, config
    drift). Returns GO/NO-GO evidence."""
    ts = _ts(False)
    diag = ts.diagnose(
        "pre-deployment preflight check before azd up",
        accelerator=accelerator,
        overrides={"all_regions": True},
        only_checks=[
            "repo.azd_env",
            "repo.config_drift",
            "repo.git_changes",
            "az.context",
            "providers.registration",
            "quota.models",
            "models.availability",
        ],
    )
    return render_markdown(diag.to_dict())


@mcp.tool()
def list_checks() -> str:
    """List available diagnostic checks and the issue stages they cover."""
    return json.dumps(
        {cid: {"title": s.title, "stages": s.stages, "needs_azure": s.needs_azure} for cid, s in CHECKS.items()},
        indent=2,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
