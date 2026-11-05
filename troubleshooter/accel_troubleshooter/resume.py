"""Can a halted deployment be resumed from where it stopped?

It runs on a **full health check** (every read-only check, including app settings, images, RBAC, network, Fabric and
the errors the apps logged) plus, when given, the error the user pasted from ``azd up``. It never trusts "some
resources exist" as "infrastructure is done": the deciding evidence is the state of **azd's own provision deployment**
(the ARM deployment azd tags with ``azd-env-name``). From that it says one of: nothing to resume, wait, fix first, safe
to resume the infrastructure, or safe to resume the last step, with the command to run.

Azure cannot continue a half-finished ARM deployment, but ``azd provision`` re-applies the same template and only
creates or updates what is missing, so re-running is safe *once the cause of the stop is gone*. This module only
decides; the CLI offers to run the fix or the command after the user types yes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Causes a plain re-run would hit again, so they must be fixed first.
DETERMINISTIC_PREFIXES = (
    "quota-insufficient",
    "model-region-unsupported",
    "parameter-not-allowed",
    "provider-unregistered",
    "subscription-mismatch",
    "public-access-disabled",
    "az-cli-unavailable",
    "fabric-capacity-paused",
    "deployer-cosmos-role-missing",
    "documented-",  # an error code the accelerator's troubleshooting guide documents
)
# Findings that mean the post-provision step (images, data, agents, settings) is not finished.
POST_PROVISION_IDS = {"images-not-built", "agents-not-created", "app-settings-missing"}
PURGE_CODES = {"flagmustbesetforrestore", "softdeletedresourcesexist", "conflict"}
_POST_TEXT = re.compile(r"post-?provision|postdeploy|hook failed|hooks? .*failed|failed running .*hook", re.I)


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


def _is_deterministic(h: Dict[str, Any]) -> bool:
    return any(h["id"] == p or (p.endswith("-") and h["id"].startswith(p)) for p in DETERMINISTIC_PREFIXES)


def _fixes_for(hyps: List[Dict[str, Any]], actions: Dict[str, Any]) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for h in hyps:
        for rid in h.get("remediation_ids", [])[:2]:
            act = actions.get(rid)
            if act and rid not in seen:
                seen.add(rid)
                out.append(
                    {
                        "id": act["id"],
                        "title": act["title"],
                        "category": act["category"],
                        "commands": act["commands"][:2],
                        "for": h["title"],
                    }
                )
    return out


def build_resume_plan(d: Dict[str, Any], repo: Optional[Path] = None, pasted: str = "") -> Dict[str, Any]:
    t = d.get("targets", {})
    rg = t.get("resource_group") or ""
    rgc, ops, res = _check(d, "rg.state"), _check(d, "deploy.operations"), _check(d, "resources.state")
    web, fab = _check(d, "webapp.state"), _check(d, "azure.fabric")
    hooks = _hooks(repo)
    issue = d.get("issue") or {}
    pasted_codes = [c for c in issue.get("error_codes", []) if c] if pasted else []
    steps: List[Dict[str, str]] = []
    why: List[str] = []
    next_cmds: List[str] = []
    run: List[str] = []  # the one command the CLI may offer to run for the user (after typed yes)

    ops_ev = (ops or {}).get("evidence") or {}
    recent = ops_ev.get("recent_deployments") or []
    running = [r for r in recent if r.get("state") in ("Running", "Accepted")]
    failed_ops = ops_ev.get("failed_operations") or []
    azd_dep = ops_ev.get("azd_deployment")
    res_ev = (res or {}).get("evidence") or {}
    not_ok = res_ev.get("not_succeeded") or []
    failed_state = [r for r in not_ok if str(r.get("state")).lower() == "failed"]
    missing_types = res_ev.get("missing_types") or []
    n_resources = sum((res_ev.get("counts_by_type") or {}).values())
    web_ev = (web or {}).get("evidence") or {}
    pp_missing = web_ev.get("post_provision_missing") or {}
    placeholder = [a.get("name") for a in web_ev.get("apps", []) if a.get("placeholder_image")]
    real = [h for h in d.get("hypotheses", []) if h["status"] in ("confirmed", "likely")]
    causes = [h for h in real if _is_deterministic(h)]
    other = [h for h in real if not _is_deterministic(h) and h["id"] not in ("deployment-in-progress",)]
    codes = {str(e.get("code") or "").lower() for e in failed_ops}
    paused = [
        r
        for r in ((fab or {}).get("evidence") or {}).get("capacities", [])
        if str(r.get("state")).lower() in ("paused", "pausing")
    ]
    pasted_post = bool(pasted) and bool(_POST_TEXT.search(pasted))

    def step(name: str, state: str, detail: str) -> None:
        steps.append({"name": name, "state": state, "detail": detail})

    # ---- 1. resource group
    rg_gone = bool(rgc and rgc["status"] in ("fail", "error") and not n_resources)
    if rg_gone:
        step("resource group", "failed", rgc["summary"])
    elif not rgc or rgc["status"] == "skip":
        step("resource group", "pending", "no resource group found for this environment")
    else:
        step("resource group", "done", f"{rg} exists")

    # ---- 2. infrastructure: azd's own deployment decides, not "some resources exist"
    infra = "pending"
    if running:
        infra, detail = (
            "pending",
            f"{len(running)} deployment(s) still running: {', '.join(r['name'] for r in running[:3])}",
        )
    elif azd_dep and azd_dep.get("state") == "Succeeded" and not failed_state and not missing_types:
        infra, detail = (
            "done",
            f"azd provision '{azd_dep['name']}' Succeeded ({azd_dep.get('timestamp')}), {n_resources} resources",
        )
    elif azd_dep and azd_dep.get("state") in ("Failed", "Canceled"):
        infra = "failed"
        detail = f"azd provision '{azd_dep['name']}' {azd_dep['state']}"
        if failed_ops:
            detail += ": " + ", ".join(sorted({f"{e.get('resource_name')} ({e.get('code')})" for e in failed_ops})[:3])
    elif failed_ops or failed_state or (ops and ops["status"] == "fail"):
        infra = "failed"
        bits = []
        if failed_ops:
            bits.append(
                f"{len(failed_ops)} failed operation(s): "
                + ", ".join(sorted({f"{e.get('resource_name')} ({e.get('code')})" for e in failed_ops})[:3])
            )
        if failed_state:
            bits.append("resources in Failed state: " + ", ".join(r["name"] for r in failed_state[:3]))
        detail = "; ".join(bits) or (ops or {}).get("summary", "failed")
    elif missing_types:
        infra, detail = "failed", f"expected resource types missing: {missing_types}"
    elif n_resources:
        infra = "failed"
        detail = (
            f"{n_resources} resources exist, but no successful azd provision is recorded in this group: "
            "it did not finish (stopped, cancelled or still being set up)"
        )
    else:
        detail = "nothing created yet"
    step("provision (infrastructure)", infra, detail)

    # ---- 3. post-provision step
    post_name = "post-provision step" + (
        f" ({script_of(hooks['postprovision'])})" if hooks.get("postprovision") else ""
    )
    pp_hyps = [h for h in real if h["id"] in POST_PROVISION_IDS]
    if pp_missing or placeholder or pp_hyps:
        bits = []
        if placeholder:
            bits.append(f"placeholder image on {placeholder}")
        if pp_missing:
            bits.append(f"settings not populated: {pp_missing}")
        bits += [h["title"] for h in pp_hyps if h["id"] not in ("images-not-built", "agents-not-created")]
        step(post_name, "failed", "; ".join(bits) or pp_hyps[0]["title"])
    elif pasted_post:
        step(post_name, "failed", "the error you pasted comes from the post-provision step")
    elif web and web["status"] in ("pass", "fail") and infra == "done":
        step(post_name, "done", "settings and images are in place")
    elif infra != "done":
        step(post_name, "pending", "not reached yet (infrastructure first)")
    else:
        step(post_name, "pending", "cannot be verified for this accelerator; paste its error if it failed")
    if paused:
        step("Fabric capacity", "failed", ", ".join(f"{r['name']} is {r['state']}" for r in paused))
    elif fab and fab["status"] == "pass":
        rows = (fab.get("evidence") or {}).get("capacities", [])
        step(
            "Fabric capacity", "done", ", ".join(f"{r['name']} ({r.get('sku') or '?'}) {r.get('state')}" for r in rows)
        )

    # ---- what the pasted error says
    notes: List[str] = []
    if pasted_codes:
        explained = [h for h in real if any(c.lower() in (h["id"] + " " + h["title"]).lower() for c in pasted_codes)]
        if infra == "done" and azd_dep and not explained and not pasted_post:
            notes.append(
                f"the error you pasted ({', '.join(pasted_codes[:3])}) is not in the current state: the latest azd "
                f"provision ({azd_dep['name']}) succeeded after it, so it came from an earlier run"
            )
        elif explained:
            notes.append(f"the error you pasted ({', '.join(pasted_codes[:3])}) matches: {explained[0]['title']}")

    # ---- verdict
    post = steps[2]["state"]
    if steps[0]["state"] == "failed" or (steps[0]["state"] == "pending" and not n_resources):
        verdict, label = "nothing", "NOTHING TO RESUME"
        why.append("the resource group does not exist or is empty: there is no half-finished deployment to continue")
        next_cmds += ["azd up   (starts a fresh deployment; use azd env new <name> first if this environment is stale)"]
        run = ["azd", "up"]
    elif running:
        stuck = [r for r in running if _older_than(r.get("timestamp"), 60)]
        verdict, label = "wait", "WAIT"
        why.append("a deployment is still running; starting another one fails with DeploymentActive")
        if stuck:
            why.append("it has been running for over an hour and looks stuck")
            next_cmds += [f"az deployment group cancel --resource-group {rg} --name {stuck[0]['name']}   (then resume)"]
        else:
            next_cmds += ["wait for the running deployment to finish, then run this again"]
    elif causes or (infra == "failed" and failed_state) or paused:
        verdict, label = "fix-first", "FIX FIRST"
        for h in causes[:3]:
            why.append(f"{h['title']} ({h['status']}): a re-run would stop at the same place")
        for r in failed_state[:3]:
            why.append(f"{r['name']} is in a Failed state; delete it (or the resource group) before re-running")
        if paused and not any(h["id"] == "fabric-capacity-paused" for h in causes):
            why.append(f"Fabric capacity {paused[0]['name']} is {paused[0]['state']}: Fabric items cannot be created")
            next_cmds.append(_resume_capacity_cmd(paused[0]))
        if codes & PURGE_CODES:
            why.append("a soft-deleted resource with the same name is blocking creation: purge it, then re-run")
        then = (
            "azd provision"
            if infra != "done"
            else ("azd hooks run postprovision" if hooks.get("postprovision") else "the post-provision scripts")
        )
        next_cmds += [f"apply the fix below, then resume with: {then}"]
    elif infra == "failed":
        verdict, label = "resume-infra", "SAFE TO RESUME"
        why += [
            "nothing failed for a repeatable reason (no quota, region, policy, parameter or permission cause was found "
            "by the full health check" + (" or in the error you pasted" if pasted else "") + ")",
            "azd provision re-applies the same template: it keeps what exists and creates what is missing",
        ]
        if failed_ops:
            why.append("the failed operation(s) look transient; if it fails again at the same resource, run diagnose")
        next_cmds += [
            "azd provision   (infrastructure only; then the post-provision step)",
            "azd up   (infrastructure, post-provision hooks and app deployment in one go)",
        ]
        run = ["azd", "provision"]
    elif post == "failed":
        verdict, label = "resume-last", "SAFE TO RESUME THE LAST STEP"
        why.append("infrastructure is complete (azd's provision succeeded); only the post-provision step is unfinished")
        if hooks.get("postprovision"):
            next_cmds += ["azd hooks run postprovision   (re-runs only the post-provision hook from azure.yaml)"]
            run = ["azd", "hooks", "run", "postprovision"]
        else:
            next_cmds += ["run the post-provision scripts from the deployment guide (the health check names them)"]
    elif post == "pending":
        verdict, label = "nothing", "NOTHING HALTED (infrastructure complete)"
        why.append("azd's provision succeeded and nothing failed")
        why.append("the post-provision step cannot be verified for this accelerator")
        if hooks.get("postprovision"):
            next_cmds += ["if you saw it stop: azd hooks run postprovision   (re-runs only that hook)"]
        next_cmds += ['or paste the error it showed: resume "<error>"']
    else:
        verdict, label = "nothing", "NOTHING TO RESUME"
        why.append("infrastructure and post-provision are complete; no halted deployment was found")
    if verdict == "fix-first":
        fixes = _fixes_for(causes, d.get("actions", {}))
    else:
        fixes = _fixes_for([h for h in real if h.get("remediation_ids")], d.get("actions", {}))
    also = [h["title"] for h in other if h["id"] not in POST_PROVISION_IDS][:4]
    return {
        "verdict": verdict,
        "label": label,
        "resource_group": rg,
        "azd_env": t.get("azd_env"),
        "accelerator": (d.get("accelerator") or {}).get("name") or (d.get("accelerator") or {}).get("id"),
        "azd_deployment": azd_dep,
        "steps": steps,
        "why": why,
        "notes": notes,
        "also_found": also,
        "fixes": fixes,
        "next": next_cmds,
        "run": run,
        "pasted_error": bool(pasted_codes or pasted),
        "paused_capacity": [r["name"] for r in paused],
    }


def render_resume(plan: Dict[str, Any], plain: bool = False) -> str:
    icon = {"done": "OK" if plain else "✅", "failed": "!!" if plain else "❌", "pending": "--" if plain else "⏳"}
    head = {"nothing": "➖", "wait": "⏳", "fix-first": "⛔", "resume-infra": "✅", "resume-last": "✅"}
    bar = "#" if plain else "▌"
    out = [
        f"{bar}RESUME · {plan['accelerator']} · RG {plan['resource_group'] or '-'} · "
        f"azd env {plan.get('azd_env') or '-'}",
        "  (full health check" + (" + the error you pasted" if plan.get("pasted_error") else "") + ")",
        f"VERDICT  {'' if plain else head[plan['verdict']] + ' '}{plan['label']}",
        "",
        "WHERE IT STOPPED",
    ]
    out += [f"  {icon[s['state']]} {s['name']}: {s['detail']}" for s in plan["steps"]]
    out += ["", "WHY"] + [f"  • {w}" for w in plan["why"]]
    out += [f"  • {n}" for n in plan.get("notes", [])]
    if plan.get("also_found"):
        out += ["", "ALSO FOUND BY THE HEALTH CHECK"] + [f"  • {a}" for a in plan["also_found"]]
    if plan["fixes"]:
        out += ["", "FIX FIRST WITH" if plan["verdict"] == "fix-first" else "FIXES"]
        for f in plan["fixes"]:
            out.append(f"  • {f['id']} ({f['category']}): {f['title']}")
            out += [f"      {c}" for c in f["commands"]]
    if plan["next"]:
        out += ["", "NEXT"] + [f"  ▶ {n}" for n in plan["next"]]
    out += [
        "",
        "  ▸ Azure cannot continue a half-finished deployment: azd re-applies the template and only creates what is",
        "    missing. Nothing has been run yet.",
    ]
    text = "\n".join(out)
    return text.replace("▶", ">").replace("▸", ">") if plain else text
