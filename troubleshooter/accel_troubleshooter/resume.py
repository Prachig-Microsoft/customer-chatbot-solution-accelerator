"""Can a halted deployment be resumed from where it stopped? Read-only assessment, nothing is run.

Azure cannot continue a half-finished ARM deployment, but ``azd provision`` re-applies the same template and only
creates or updates what is missing, so re-running is usually safe *once the cause of the stop is gone*. This module
reads the checks (deployment history, resource states, post-provision state, Fabric capacity, confirmed causes) and
says one of: nothing to resume, wait, fix first, safe to resume infrastructure, or safe to resume the last step.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RESUME_CHECKS = [
    "repo.azd_env",
    "repo.config_drift",
    "az.context",
    "providers.registration",
    "rg.state",
    "deploy.operations",
    "resources.state",
    "activity.failures",
    "quota.models",
    "models.availability",
    "azure.fabric",
    "network.public_access",
    "webapp.state",
]

# Causes a plain re-run would hit again, so they must be fixed first.
DETERMINISTIC = {
    "quota-insufficient",
    "model-unsupported",
    "parameter-not-allowed",
    "providers-unregistered",
    "subscription-mismatch",
    "documented-deployment-errors",
    "public-access-disabled",
    "az-not-logged-in",
}
PURGE_CODES = {"flagmustbesetforrestore", "softdeletedresourcesexist", "conflict"}


def _check(d: Dict[str, Any], cid: str) -> Optional[Dict[str, Any]]:
    return next((c for c in d.get("checks", []) if c["check_id"] == cid), None)


def _hooks(repo: Optional[Path]) -> Dict[str, str]:
    """azure.yaml hooks by name (postprovision, predown, ...) with the script each one runs."""
    out: Dict[str, str] = {}
    path = (repo / "azure.yaml") if repo else None
    if not path or not path.exists():
        return out
    current = ""
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^  (\w+):\s*$", line)
        if m:
            current = m.group(1)
            continue
        r = re.match(r"^\s+run:\s*(.+)$", line)
        if r and current and current not in out:
            out[current] = r.group(1).strip().strip("\"'")
    return out


def script_of(run: str) -> str:
    """The script an azure.yaml hook runs, as a short name (the last file path in the command)."""
    found = re.findall(r"[\w./-]+\.(?:ps1|sh|py)", run)
    return found[-1].rsplit("/", 1)[-1] if found else run[:60]


def _resume_capacity_cmd(r: Dict[str, Any]) -> str:
    return (
        f"az fabric capacity resume --resource-group {r['resource_group']} --capacity-name {r['name']}"
        "   (needs: az extension add --name microsoft-fabric; or use Resume in the portal; billing restarts)"
    )


def _older_than(timestamp: Optional[str], minutes: int) -> bool:
    try:
        then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - then).total_seconds() > minutes * 60
    except ValueError:
        return False


def build_resume_plan(d: Dict[str, Any], repo: Optional[Path] = None) -> Dict[str, Any]:
    t = d.get("targets", {})
    rg = t.get("resource_group") or ""
    rgc, ops, res = _check(d, "rg.state"), _check(d, "deploy.operations"), _check(d, "resources.state")
    web, fab = _check(d, "webapp.state"), _check(d, "azure.fabric")
    hooks = _hooks(repo)
    steps: List[Dict[str, str]] = []
    why: List[str] = []
    next_cmds: List[str] = []
    fixes: List[Dict[str, Any]] = []

    ops_ev = (ops or {}).get("evidence") or {}
    recent = ops_ev.get("recent_deployments") or []
    running = [r for r in recent if r.get("state") in ("Running", "Accepted")]
    failed_ops = ops_ev.get("failed_operations") or []
    res_ev = (res or {}).get("evidence") or {}
    not_ok = res_ev.get("not_succeeded") or []
    failed_state = [r for r in not_ok if str(r.get("state")).lower() == "failed"]
    missing_types = res_ev.get("missing_types") or []
    n_resources = sum((res_ev.get("counts_by_type") or {}).values())
    web_ev = (web or {}).get("evidence") or {}
    pp_missing = web_ev.get("post_provision_missing") or {}
    placeholder = [a.get("name") for a in web_ev.get("apps", []) if a.get("placeholder_image")]
    causes = [h for h in d.get("hypotheses", []) if h["status"] in ("confirmed", "likely") and h["id"] in DETERMINISTIC]
    codes = {str(e.get("code") or "").lower() for e in failed_ops}
    paused = [
        r
        for r in ((fab or {}).get("evidence") or {}).get("capacities", [])
        if str(r.get("state")).lower() in ("paused", "pausing")
    ]

    def step(name: str, ok: Optional[bool], detail: str) -> None:
        steps.append(
            {"name": name, "state": "done" if ok else "failed" if ok is False else "pending", "detail": detail}
        )

    # ---- where it stopped
    rg_gone = bool(rgc and rgc["status"] in ("fail", "error") and not n_resources)
    if rg_gone:
        step("resource group", False, rgc["summary"])
    elif not rgc or rgc["status"] == "skip":
        step("resource group", None, "no resource group found for this environment")
    else:
        step("resource group", True, f"{rg} exists")
    if running:
        step("provision (infrastructure)", None, f"{len(running)} deployment(s) still running")
    elif failed_ops or failed_state or (ops and ops["status"] == "fail"):
        bits = []
        if failed_ops:
            bits.append(
                f"{len(failed_ops)} failed operation(s): "
                + ", ".join(sorted({f"{e.get('resource_name')} ({e.get('code')})" for e in failed_ops})[:3])
            )
        if failed_state:
            bits.append("resources in Failed state: " + ", ".join(r["name"] for r in failed_state[:3]))
        step("provision (infrastructure)", False, "; ".join(bits) or (ops or {}).get("summary", "failed"))
    elif missing_types:
        step("provision (infrastructure)", False, f"expected resource types missing: {missing_types}")
    elif n_resources:
        step("provision (infrastructure)", True, f"{n_resources} resources created")
    else:
        step("provision (infrastructure)", None, "nothing created yet")
    post_name = "post-provision step"
    if hooks.get("postprovision"):
        post_name += f" ({script_of(hooks['postprovision'])})"
    if pp_missing or placeholder:
        bits = []
        if placeholder:
            bits.append(f"placeholder image on {placeholder}")
        if pp_missing:
            bits.append(f"settings not populated: {pp_missing}")
        step(post_name, False, "; ".join(bits))
    elif web and web["status"] in ("pass", "fail") and n_resources:
        step(post_name, True, "settings and images are in place")
    else:
        step(post_name, None, "not checked (no app-level checks for this accelerator)")
    if paused:
        step("Fabric capacity", False, ", ".join(f"{r['name']} is {r['state']}" for r in paused))
    elif fab and fab["status"] == "pass":
        rows = (fab.get("evidence") or {}).get("capacities", [])
        step("Fabric capacity", True, ", ".join(f"{r['name']} ({r.get('sku') or '?'}) {r.get('state')}" for r in rows))

    # ---- verdict
    infra_state = steps[1]["state"]
    if steps[0]["state"] == "failed" or (steps[0]["state"] == "pending" and not n_resources):
        verdict, label = "nothing", "NOTHING TO RESUME"
        why.append("the resource group does not exist or is empty: there is no half-finished deployment to continue")
        next_cmds += ["azd up   (create a new azd environment first if this one is stale: azd env new <name>)"]
    elif running:
        stuck = [r for r in running if _older_than(r.get("timestamp"), 60)]
        verdict, label = "wait", "WAIT"
        why.append("a deployment is still running; starting another one fails with DeploymentActive")
        if stuck:
            why.append("it has been running for over an hour and looks stuck")
            next_cmds += [
                f"az deployment group cancel --resource-group {rg} --name {stuck[0]['name']}"
                "   (then re-run azd provision)"
            ]
        else:
            next_cmds += ["wait for the running 'azd up' to finish, then run this again"]
    elif causes or (infra_state == "failed" and failed_state):
        verdict, label = "fix-first", "FIX FIRST"
        for h in causes[:3]:
            why.append(f"{h['title']} ({h['status']}): a re-run would stop at the same place")
        for r in failed_state[:3]:
            why.append(f"{r['name']} is in a Failed state; delete it (or the resource group) before re-running")
        if codes & PURGE_CODES:
            why.append("a soft-deleted resource with the same name is blocking creation: purge it, then re-run")
        for h in causes[:2]:
            for rid in h.get("remediation_ids", [])[:2]:
                act = d.get("actions", {}).get(rid)
                if act:
                    fixes.append(
                        {
                            "id": act["id"],
                            "title": act["title"],
                            "category": act["category"],
                            "commands": act["commands"][:2],
                        }
                    )
        next_cmds += ["apply the fix above, then run: azd provision"]
    elif infra_state == "failed":
        verdict, label = "resume-infra", "SAFE TO RESUME"
        why += [
            "nothing failed for a repeatable reason (no quota, region, policy or parameter cause was found)",
            "azd provision re-applies the same template: it keeps what exists and creates what is missing",
        ]
        if failed_ops:
            why.append(
                "the failed operation(s) may have been transient; if it fails again at the same resource, run diagnose"
            )
        next_cmds += [
            "azd provision   (infrastructure only)",
            "azd up   (also runs the post-provision step and deploys)",
        ]
    elif steps[2]["state"] == "failed":
        verdict, label = "resume-last", "SAFE TO RESUME THE LAST STEP"
        why.append("infrastructure is complete; only the post-provision step is unfinished")
        if paused:
            why.append("resume the Fabric capacity first: items cannot be created on a paused capacity")
            next_cmds += [_resume_capacity_cmd(paused[0])]
        if hooks.get("postprovision"):
            next_cmds += ["azd hooks run postprovision   (re-runs the post-provision hook from azure.yaml only)"]
        else:
            next_cmds += ["run the post-provision scripts from the deployment guide (see the health check)"]
    elif paused:
        verdict, label = "fix-first", "RESUME THE FABRIC CAPACITY FIRST"
        why += [
            f"Fabric capacity {paused[0]['name']} is {paused[0]['state']}: its workspace and items are unavailable",
            "a post-provision step that creates or loads Fabric items would fail until it is resumed",
        ]
        next_cmds += [_resume_capacity_cmd(paused[0])]
        if hooks.get("postprovision"):
            next_cmds += ["then, if the post-provision step did not finish: azd hooks run postprovision"]
    elif steps[2]["state"] == "pending":
        verdict, label = "nothing", "NOTHING HALTED (infrastructure complete)"
        why.append("all infrastructure was created and nothing failed")
        why.append("the post-provision step cannot be verified for this accelerator (no app-level checks)")
        next_cmds += (
            ["if you saw it stop: azd hooks run postprovision   (re-runs only that hook from azure.yaml)"]
            if hooks.get("postprovision")
            else ["if a documented post-deployment script did not finish, run it again from the deployment guide"]
        )
        next_cmds += ["python -m accel_troubleshooter health   (full check)"]
    else:
        verdict, label = "nothing", "NOTHING TO RESUME"
        why.append("infrastructure and post-provision look complete; no halted deployment was found")
        next_cmds += ["python -m accel_troubleshooter health   (full check)"]
    return {
        "verdict": verdict,
        "label": label,
        "resource_group": rg,
        "azd_env": t.get("azd_env"),
        "accelerator": (d.get("accelerator") or {}).get("name") or (d.get("accelerator") or {}).get("id"),
        "steps": steps,
        "why": why,
        "fixes": fixes,
        "next": next_cmds,
        "paused_capacity": [r["name"] for r in paused],
    }


def render_resume(plan: Dict[str, Any], plain: bool = False) -> str:
    icon = {"done": "OK" if plain else "✅", "failed": "!!" if plain else "❌", "pending": "--" if plain else "⏳"}
    head = {"nothing": "➖", "wait": "⏳", "fix-first": "⛔", "resume-infra": "✅", "resume-last": "✅"}
    bar = "#" if plain else "▌"
    out = [
        f"{bar}RESUME · {plan['accelerator']} · RG {plan['resource_group'] or '-'} · "
        f"azd env {plan.get('azd_env') or '-'}",
        f"VERDICT  {'' if plain else head[plan['verdict']] + ' '}{plan['label']}",
        "",
        "WHERE IT STOPPED",
    ]
    out += [f"  {icon[s['state']]} {s['name']}: {s['detail']}" for s in plan["steps"]]
    out += ["", "WHY"] + [f"  • {w}" for w in plan["why"]]
    if plan["fixes"]:
        out += ["", "FIX FIRST WITH"]
        for f in plan["fixes"]:
            out.append(f"  • {f['id']} ({f['category']}): {f['title']}")
            out += [f"      {c}" for c in f["commands"]]
    out += ["", "NEXT"] + [f"  ▶ {n}" for n in plan["next"]]
    out += [
        "",
        "  ▸ Nothing was run. Azure cannot continue a half-finished deployment: azd re-applies the template and",
        "    only creates what is missing. Run the command yourself in a terminal you can watch.",
    ]
    return "\n".join(out).replace("▶", ">") if plain else "\n".join(out)
