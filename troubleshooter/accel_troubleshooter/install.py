"""Install the troubleshooter into another accelerator repository.

Copies the tool, the Copilot custom agent, the starter-question skills and the MCP server entry, so the other
repository gets the same chat experience without any manual file shuffling. Nothing outside the target is changed
and existing files are only overwritten with ``force``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from .repo import TOOL_ROOT

SERVER_NAME = "accelerator-troubleshooter"
SERVER_ENTRY = {"type": "stdio", "command": "python", "args": ["troubleshooter/mcp_launcher.py"]}
AGENT_REL = Path(".github") / "agents" / "accelerator-troubleshooter.agent.md"
SKILL_NAMES = ["about-accelerator", "preflight", "health-check", "why-is-this-failing"]
RUNTIME = ["accel_troubleshooter", "mcp_launcher.py", "requirements-mcp.txt", "README.md", ".gitignore"]
GENERATED = {".venv", ".cache", ".runs", "__pycache__", ".pytest_cache", ".mypy_cache", "demo-output"}


def _copy_tree(src: Path, dst: Path, force: bool, done: List[str], dry: bool) -> None:
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        if item.is_dir() or any(part in GENERATED for part in rel.parts) or item.suffix == ".pyc":
            continue
        if rel.parts[0] == "knowledge" and item.suffix == ".json":
            continue
        target = dst / rel
        if target.exists() and not force:
            done.append(f"kept existing {target}")
            continue
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def install(target: Path, force: bool = False, full: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    source_repo = TOOL_ROOT.parent
    target = target.resolve()
    if not target.is_dir():
        raise ValueError(f"target folder does not exist: {target}")
    if target == source_repo or target == TOOL_ROOT:
        raise ValueError("target is the repository this tool already lives in")
    steps: List[str] = []
    tool_dst = target / "troubleshooter"
    if tool_dst.exists() and not force:
        raise ValueError(f"{tool_dst} already exists. Use --force to update it (your .runs/ and .cache/ are kept).")

    # 1. the tool itself
    entries = [p.name for p in TOOL_ROOT.iterdir() if p.name not in GENERATED] if full else RUNTIME
    for name in entries:
        src = TOOL_ROOT / name
        if not src.exists():
            continue
        if src.is_dir():
            _copy_tree(src, tool_dst / name, True, steps, dry_run)
        elif not dry_run:
            tool_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tool_dst / name)
    steps.append(f"{'would copy' if dry_run else 'copied'} the tool to {tool_dst}")

    # 2. the custom agent and the starter-question skills
    for rel in [AGENT_REL] + [Path(".github") / "skills" / n / "SKILL.md" for n in SKILL_NAMES]:
        src, dst = source_repo / rel, target / rel
        if not src.exists():
            steps.append(f"skipped {rel} (not found in the source repository)")
        elif dst.exists() and not force:
            steps.append(f"kept existing {rel}")
        else:
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            steps.append(f"{'would write' if dry_run else 'wrote'} {rel}")

    # 3. the MCP server entry (merged into an existing file, never replacing other servers)
    mcp = target / ".vscode" / "mcp.json"
    snippet = {"servers": {SERVER_NAME: SERVER_ENTRY}}
    manual_mcp = False
    if mcp.exists():
        try:
            data = json.loads(mcp.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        if not isinstance(data, dict):
            manual_mcp = True
            steps.append(f"could not parse {mcp} (comments?): add the server entry by hand")
        elif SERVER_NAME in (data.get("servers") or {}) and not force:
            steps.append(f"kept existing MCP server entry in {mcp.name}")
        else:
            data.setdefault("servers", {})[SERVER_NAME] = SERVER_ENTRY
            if not dry_run:
                mcp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            steps.append(f"{'would add' if dry_run else 'added'} the MCP server to {mcp}")
    else:
        if not dry_run:
            mcp.parent.mkdir(parents=True, exist_ok=True)
            mcp.write_text(json.dumps(snippet, indent=2) + "\n", encoding="utf-8")
        steps.append(f"{'would create' if dry_run else 'created'} {mcp}")

    return {"target": str(target), "steps": steps, "manual_mcp": manual_mcp, "snippet": snippet, "dry_run": dry_run}


def detect(target: Path) -> Dict[str, Any]:
    """Which accelerator profile matches the target repository (offline, read-only)."""
    from .azcli import OfflineRunner
    from .engine import Troubleshooter

    ts = Troubleshooter(target, OfflineRunner())
    return ts.identify("azd up failed", None)
