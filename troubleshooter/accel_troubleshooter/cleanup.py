"""Guided cleanup: what `azd down` / a resource-group delete would remove, what blocks it, and what the docs say.

Read-only and advisory: every delete command is printed for the user to run; this tool never deletes anything. The
plan comes from the accelerator's own files (``azure.yaml`` ``predown`` hook, teardown scripts, teardown lines in the
documents) and from a live inventory of the resource group (resources, locks, soft-delete services).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .docscan import scan_docs
from .resume import _check, _hooks, script_of

CLEANUP_CHECKS = ["repo.azd_env", "az.context", "rg.state", "resources.state", "azure.fabric", "cleanup.inventory"]
_TEARDOWN_LINE = re.compile(
    r"\bazd down\b|az group delete|delete (?:the |your )?resource group|\bpurge\b|soft.?delet|clean ?up|tear ?down",
    re.I,
)
_SCRIPT_NAME = re.compile(r"remove|clean_?up|teardown|uninstall|destroy|delete", re.I)


def _doc_refs(repo: Path, limit: int = 6) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    docs = scan_docs(repo)["docs"]
    files = [f for kind in ("reuse", "deployment", "readme", "local_dev", "other") for f in docs.get(kind, [])]
    for rel in files:
        for n, line in enumerate((repo / rel).read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            if _TEARDOWN_LINE.search(line) and len(line.strip()) > 8:
                out.append({"where": f"{rel}:{n}", "text": re.sub(r"\s+", " ", line.strip())[:140]})
                if len(out) >= limit:
                    return out
    return out


def _scripts(repo: Path) -> List[str]:
    found = []
    for pattern in ("infra/scripts/**/*", "scripts/**/*"):
        for p in sorted(repo.glob(pattern)):
            if p.is_file() and p.suffix.lower() in (".py", ".sh", ".ps1") and _SCRIPT_NAME.search(p.stem):
                found.append(p.relative_to(repo).as_posix())
    return found[:6]


def build_cleanup_plan(d: Dict[str, Any], repo: Optional[Path]) -> Dict[str, Any]:
    t = d.get("targets", {})
    rg, env = t.get("resource_group") or "", t.get("azd_env") or ""
    inv = _check(d, "cleanup.inventory")
    ev = (inv or {}).get("evidence") or {}
    resources = ev.get("resources") or []
    hooks = _hooks(repo)
    scripts = _scripts(repo) if repo else []
    docs = _doc_refs(repo) if repo else []
    fabric = [r for r in resources if (r.get("type") or "").lower() == "microsoft.fabric/capacities"]
    by_type: Dict[str, int] = {}
    for r in resources:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    steps: List[Dict[str, Any]] = []
    warnings: List[str] = []
    if not rg or not inv or inv["status"] == "skip":
        warnings.append("no resource group was found for this environment: nothing to clean up in Azure")
        # The group is already gone: deleting again is pointless, but Azure may still hold soft-deleted resources
        # whose names block the next deployment.
        steps.append(
            {
                "title": "Check for leftovers Azure keeps after deletion (soft-deleted, they block reusing the names)",
                "commands": [
                    "az cognitiveservices account list-deleted",
                    "az keyvault list-deleted",
                ],
                "notes": ["purge any of yours that are listed, or deploy with a new azd environment name"],
            }
        )
        return {
            "accelerator": (d.get("accelerator") or {}).get("name") or (d.get("accelerator") or {}).get("id"),
            "resource_group": rg,
            "azd_env": env,
            "resources": 0,
            "by_type": {},
            "soft_delete": [],
            "locks": [],
            "predown": hooks.get("predown"),
            "scripts": scripts,
            "doc_refs": docs,
            "warnings": warnings,
            "steps": steps,
            "has_fabric": False,
        }
    if ev.get("locks"):
        names = ", ".join(f"{lk['name']} ({lk['level']})" for lk in ev["locks"])
        warnings.append(f"resource lock(s) block deletion: {names}")
        steps.append(
            {
                "title": "Remove the lock(s) first (only if you are allowed to and sure)",
                "commands": [f"az lock delete --name {lk['name']} --resource-group {rg}" for lk in ev["locks"]],
            }
        )
    if ev.get("foreign"):
        names = ", ".join(r["name"] for r in ev["foreign"][:5])
        warnings.append(
            f"{len(ev['foreign'])} resource(s) are not tagged azd-env-name={env} (for example {names}): they were not "
            "created by this azd environment, but deleting the resource group deletes them too"
        )
    if fabric:
        warnings.append(
            "a Fabric capacity bills while it is Active even when idle: pause it to stop the billing but keep the "
            "data, "
            "or delete it with the group"
        )
    down = "azd down --purge --force" + (f" --environment {env}" if env else "")
    why_down = [
        "deletes the resource group and everything in it, then purges soft-deleted resources so the names are free"
    ]
    if hooks.get("predown"):
        why_down.insert(0, f"first runs the accelerator's predown hook (azure.yaml): {script_of(hooks['predown'])}")
    steps.append(
        {
            "title": "Option A (recommended): let azd do it, following the accelerator's own teardown",
            "commands": [down],
            "notes": why_down,
        }
    )
    manual = [f"az group delete --name {rg} --yes --no-wait"] if rg else []
    manual += [s["purge"] for s in ev.get("soft_delete", [])]
    steps.append(
        {
            "title": "Option B: delete the group yourself, then purge what Azure keeps for a while",
            "commands": manual,
            "notes": (
                [
                    "skips the predown hook, so anything created outside the resource group (for example Fabric "
                    "items) stays"
                ]
                if hooks.get("predown")
                else []
            ),
        }
    )
    steps.append(
        {
            "title": "Check it is gone",
            "commands": ([f"az group exists --name {rg}   (expect false)"] if rg else [])
            + (
                ["az cognitiveservices account list-deleted   (nothing of yours should be listed)"]
                if any(s["type"].lower() == "microsoft.cognitiveservices/accounts" for s in ev.get("soft_delete", []))
                else []
            ),
        }
    )
    return {
        "accelerator": (d.get("accelerator") or {}).get("name") or (d.get("accelerator") or {}).get("id"),
        "resource_group": rg,
        "azd_env": env,
        "resources": len(resources),
        "by_type": by_type,
        "soft_delete": ev.get("soft_delete", []),
        "locks": ev.get("locks", []),
        "predown": hooks.get("predown"),
        "scripts": scripts,
        "doc_refs": docs,
        "warnings": warnings,
        "steps": steps,
        "has_fabric": bool(fabric),
    }


def render_cleanup(plan: Dict[str, Any], plain: bool = False) -> str:
    bar, dot = ("#", "*") if plain else ("▌", "•")
    out = [
        f"{bar}CLEANUP PLAN · {plan['accelerator']} · RG {plan['resource_group'] or '-'} · "
        f"azd env {plan['azd_env'] or '-'}",
        "  Nothing is deleted by this tool: the commands below are for you to run.",
        "",
        "WHAT WOULD BE DELETED",
        f"  {dot} {plan['resources']} resource(s)"
        + (
            ": " + ", ".join(f"{n} x {t.replace('Microsoft.', '')}" for t, n in sorted(plan["by_type"].items())[:8])
            if plan["by_type"]
            else ""
        ),
    ]
    if plan["soft_delete"]:
        out.append(
            f"  {dot} {len(plan['soft_delete'])} are soft-deleted by Azure and need a purge to free the name: "
            + ", ".join(s["name"] for s in plan["soft_delete"][:4])
        )
    if plan["warnings"]:
        out += ["", "WATCH OUT"] + [f"  ⚠ {w}" if not plain else f"  ! {w}" for w in plan["warnings"]]
    out += ["", "WHAT THE ACCELERATOR SAYS"]
    if plan["predown"]:
        out.append(f"  {dot} azure.yaml predown hook runs before azd down: {script_of(plan['predown'])}")
    for s in plan["scripts"][:3]:
        out.append(f"  {dot} teardown script: {s}")
    for r in plan["doc_refs"][:4]:
        out.append(f"  {dot} {r['where']}  {r['text'][:110]}")
    if not (plan["predown"] or plan["scripts"] or plan["doc_refs"]):
        out.append(f"  {dot} no teardown steps found in the documents: azd down is the standard route")
    out += ["", "STEPS (run them yourself, in this order)"]
    for i, s in enumerate(plan["steps"], start=1):
        out.append(f"  {i}. {s['title']}")
        out += [f"       {c}" for c in s["commands"]]
        out += [f"       ({n})" for n in s.get("notes", [])]
    out += ["", "  ▸ Deleting cannot be undone. Make sure this is the right resource group and subscription first."]
    text = "\n".join(out)
    return text.replace("▸", ">") if plain else text
