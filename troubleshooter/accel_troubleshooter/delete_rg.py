"""Delete one resource group, only after the user typed its exact name and then confirmed a second time.

This is the only destructive operation in the troubleshooter, and it is deliberately kept out of the approved-fix
path (``actions.EXECUTABLE_ACTIONS`` / ``apply_fix``), so no other flow can reach it. Safeguards, in order:

1. Read-only pre-checks of the target: the group exists, it is in the signed-in subscription (and, when it is this
   azd environment's group, that is the environment's subscription), it has no resource locks. Warnings for resources
   tagged with another azd environment and for an accelerator ``predown`` hook that a plain group delete skips.
2. A fingerprint of what the user was shown: subscription id, group name and every resource id in it.
3. First confirmation: the user types the exact resource group name (case-sensitive, no trimming of inner text).
4. Second confirmation: the user types ``delete <name>``.
5. Right before deleting, the group is read again; if it no longer exists, a lock appeared, the subscription changed or
   any resource was added or removed (the fingerprint differs), nothing is deleted.
6. Exactly one command runs: ``az group delete --name <name> --yes --no-wait``. Nothing else is deleted or purged.
7. It waits until Azure reports the group gone, lists soft-deleted AI accounts that came from that group (purge
   commands are shown, never run) and appends a record to ``.runs/deletions.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .actions import _record, _run_one
from .azcli import AzRunner

WAIT_LIMIT_S = 35 * 60  # big groups (AI accounts, Fabric, App Service plans) can take 20+ minutes
POLL_S = 30
_sleep = time.sleep  # replaced in tests
_NAME = re.compile(r"^[-\w._()]{1,90}$")


def phrase_for(rg: str) -> str:
    return f"delete {rg}"


def snapshot(runner: AzRunner, rg: str) -> Dict[str, Any]:
    """Everything the decision depends on, read-only."""
    snap: Dict[str, Any] = {"resource_group": rg, "exists": False, "errors": []}
    if not _NAME.match(rg or ""):
        snap["errors"].append(f"'{rg}' is not a valid resource group name")
        return snap
    ok, acct, _ = runner.run(["account", "show"])
    if ok and isinstance(acct, dict):
        snap["subscription_id"] = acct.get("id")
        snap["subscription_name"] = acct.get("name")
    else:
        snap["errors"].append("cannot read the signed-in subscription (az login?)")
        return snap
    ok, grp, _ = runner.run(["group", "show", "--name", rg])
    if not ok or not isinstance(grp, dict):
        return snap  # does not exist (or no access): nothing to delete
    snap.update(
        exists=True,
        id=grp.get("id"),
        location=grp.get("location"),
        tags=grp.get("tags") or {},
        state=(grp.get("properties") or {}).get("provisioningState"),
    )
    ok, res, _ = runner.run(["resource", "list", "--resource-group", rg])
    if not ok:
        snap["errors"].append("cannot list the resources in the group")
    snap["resources"] = sorted(
        (
            {
                "id": r.get("id") or f"{r.get('type')}/{r.get('name')}",
                "name": r.get("name"),
                "type": r.get("type"),
                "azd_env": (r.get("tags") or {}).get("azd-env-name"),
            }
            for r in (res if ok and isinstance(res, list) else [])
        ),
        key=lambda r: str(r["id"]).lower(),
    )
    ok, locks, _ = runner.run(["lock", "list", "--resource-group", rg])
    if not ok:
        snap["errors"].append("cannot read resource locks (Reader access to the group is needed)")
    snap["locks"] = [
        {"name": lk.get("name"), "level": lk.get("level")} for lk in (locks if ok and isinstance(locks, list) else [])
    ]
    return snap


def fingerprint(snap: Dict[str, Any]) -> str:
    ids = "\n".join(str(r["id"]).lower() for r in snap.get("resources", []))
    raw = f"{snap.get('subscription_id')}|{str(snap.get('resource_group')).lower()}|{ids}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def assess(
    snap: Dict[str, Any], azd_env: str = "", env_rg: str = "", env_subscription: str = "", predown: str = ""
) -> Dict[str, Any]:
    """Blockers stop the delete; warnings are shown before the user confirms."""
    rg = snap["resource_group"]
    blockers: List[str] = list(snap.get("errors", []))
    warnings: List[str] = []
    if not snap.get("exists") and not snap.get("errors"):
        blockers.append(f"resource group '{rg}' does not exist in subscription {snap.get('subscription_name')}")
    if snap.get("locks"):
        names = ", ".join(f"{lk['name']} ({lk['level']})" for lk in snap["locks"])
        blockers.append(f"resource lock(s) on the group: {names}. Remove them yourself first if you are allowed to")
    if str(snap.get("state") or "").lower() == "deleting":
        blockers.append("the group is already being deleted")
    is_env_rg = bool(env_rg) and env_rg.lower() == rg.lower()
    if is_env_rg and env_subscription and snap.get("subscription_id") and env_subscription != snap["subscription_id"]:
        blockers.append(
            f"the azd environment uses subscription {env_subscription} but you are signed in to "
            f"{snap.get('subscription_id')}: run az account set --subscription {env_subscription} first"
        )
    if env_rg and not is_env_rg:
        warnings.append(f"'{rg}' is not the resource group of the current azd environment ('{env_rg}')")
    other_envs = sorted({r["azd_env"] for r in snap.get("resources", []) if r["azd_env"] and r["azd_env"] != azd_env})
    group_env = (snap.get("tags") or {}).get("azd-env-name")
    if group_env and azd_env and group_env != azd_env:
        warnings.append(f"the group is tagged azd-env-name={group_env}, not the current environment '{azd_env}'")
    if other_envs:
        warnings.append(f"resources tagged for other azd environments are inside it: {', '.join(other_envs)}")
    if predown:
        warnings.append(
            f"this deletes only the resource group: the accelerator's predown hook ({predown}) does NOT run, so items "
            "it would remove outside the group (for example Fabric workspaces) stay; use 'azd down' for that"
        )
    if snap.get("exists") and not snap.get("resources"):
        warnings.append("the group is empty")
    return {"blockers": blockers, "warnings": warnings, "fingerprint": fingerprint(snap) if snap.get("exists") else ""}


def _deleted_ai_accounts(runner: AzRunner, rg: str) -> List[Dict[str, str]]:
    ok, data, _ = runner.run(["cognitiveservices", "account", "list-deleted"])
    out = []
    for a in data if ok and isinstance(data, list) else []:
        rid = str(a.get("id") or "")
        if f"/resourcegroups/{rg.lower()}/" in rid.lower():
            name, loc = a.get("name"), a.get("location")
            out.append(
                {
                    "name": name,
                    "purge": f"az cognitiveservices account purge --location {loc} --resource-group {rg} --name {name}",
                }
            )
    return out


def _log(record: Dict[str, Any]) -> Optional[Path]:
    from .telemetry import RUNS_FILE

    path = Path(RUNS_FILE).parent / "deletions.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return path
    except OSError:
        return None


def delete_resource_group(
    runner: AzRunner,
    rg: str,
    confirm_name: str,
    confirm_phrase: str,
    expected_fingerprint: str,
    repo: Path,
    azd_env: str = "",
    env_rg: str = "",
    env_subscription: str = "",
    predown: str = "",
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    say = progress or (lambda _m: None)
    result: Dict[str, Any] = {"resource_group": rg, "deleted": False, "results": []}

    def refuse(why: str) -> Dict[str, Any]:
        result["refused"] = why
        say(f"  ✖ Not deleted: {why}")
        return result

    if confirm_name != rg:
        return refuse(f"the name you typed ('{confirm_name}') is not exactly '{rg}'")
    if confirm_phrase != phrase_for(rg):
        return refuse(f"the second confirmation must be exactly '{phrase_for(rg)}'")
    if not expected_fingerprint:
        return refuse("no fingerprint of the plan you confirmed: run the cleanup plan first")
    say("Checking the resource group again right before deleting…")
    snap = snapshot(runner, rg)
    verdict = assess(snap, azd_env, env_rg, env_subscription, predown)
    if verdict["blockers"]:
        return refuse("; ".join(verdict["blockers"]))
    if verdict["fingerprint"] != expected_fingerprint:
        return refuse(
            "the group changed since you confirmed (resources added or removed, or another subscription): "
            "run the cleanup plan again and re-check what would be deleted"
        )
    cmd = f"az group delete --name {rg} --yes --no-wait"
    say(f"  ▶ {cmd}")
    start = time.time()
    code, out, _ = _run_one(cmd, repo, 600)
    result["results"].append(_record(cmd, code, out, start))
    if code != 0:
        return refuse(f"Azure refused the delete: {out.strip()[-300:]}")
    result["started"] = True
    waited = 0
    while waited < WAIT_LIMIT_S:
        ok, exists, _ = runner.run(["group", "exists", "--name", rg])
        if ok and exists is False:
            break
        say(f"  ⏳ still deleting ({waited // 60} min)")
        _sleep(POLL_S)
        waited += POLL_S
    else:
        result["still_deleting"] = True
        say(f"  ⏳ Azure is still deleting after {WAIT_LIMIT_S // 60} min; check later: az group exists --name {rg}")
    result["deleted"] = not result.get("still_deleting")
    if result["deleted"]:
        say(f"  ✔ resource group {rg} is gone")
        result["soft_deleted"] = _deleted_ai_accounts(runner, rg)
    result["resources"] = len(snap.get("resources", []))
    result["subscription"] = snap.get("subscription_name")
    result["log"] = str(
        _log(
            {
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "resource_group": rg,
                "subscription_id": snap.get("subscription_id"),
                "resources": [r["id"] for r in snap.get("resources", [])],
                "deleted": result["deleted"],
                "still_deleting": bool(result.get("still_deleting")),
            }
        )
        or ""
    )
    return result


def render_checks(snap: Dict[str, Any], verdict: Dict[str, Any], plain: bool = False) -> str:
    rg = snap["resource_group"]
    x, w, ok = ("!!", "!", "OK") if plain else ("✖", "⚠", "✔")
    out = ["", "DELETE CHECKS (read again from Azure just now)"]
    if snap.get("exists"):
        out.append(
            f"  {ok} {rg} exists in subscription {snap.get('subscription_name')} ({snap.get('location')}), "
            f"{len(snap.get('resources', []))} resource(s), {len(snap.get('locks', []))} lock(s)"
        )
    out += [f"  {x} {b}" for b in verdict["blockers"]]
    out += [f"  {w} {m}" for m in verdict["warnings"]]
    if verdict["blockers"]:
        out.append("  ▸ Deletion is not possible until the items marked above are fixed.")
    else:
        out.append(f"  ▸ Plan fingerprint: {verdict['fingerprint']} (the delete is refused if the group changes)")
    return "\n".join(out)


def render_result(res: Dict[str, Any], plain: bool = False) -> str:
    if res.get("refused"):
        return f"NOT DELETED · {res['resource_group']}\n  • {res['refused']}"
    out = [
        ("DELETED" if res["deleted"] else "DELETE STARTED") + f" · {res['resource_group']} · "
        f"{res.get('resources', 0)} resource(s) · subscription {res.get('subscription')}",
        "  • Only this resource group was deleted. Nothing else was changed or purged.",
    ]
    if res.get("still_deleting"):
        out.append(f"  • Azure is still deleting it: az group exists --name {res['resource_group']}   (expect false)")
    for s in res.get("soft_deleted", []):
        out.append(f"  • Soft-deleted AI account {s['name']} still reserves its name. To free it (you run it):")
        out.append(f"      {s['purge']}")
    out.append("  • Your azd environment still points at this group: create a new one before deploying again.")
    if res.get("log"):
        out.append(f"  • Record kept in {res['log']}")
    return "\n".join(out)
