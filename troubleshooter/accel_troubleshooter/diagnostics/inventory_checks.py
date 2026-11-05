"""Read-only inventory of a resource group for the guided cleanup (what would be deleted, what blocks it)."""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import INFO, PASS, WARN, CheckResult
from .azure_checks import _error_code, _need_rg
from .base import DiagContext, check

# Services that keep a deleted resource around (soft delete) until it is purged; a redeploy with the same name fails.
SOFT_DELETE = {
    "microsoft.cognitiveservices/accounts": (
        "az cognitiveservices account purge --location {location} --resource-group {rg} --name {name}"
    ),
    "microsoft.keyvault/vaults": "az keyvault purge --name {name} --location {location}",
    "microsoft.apimanagement/service": "az apim deletedservice purge --service-name {name} --location {location}",
    "microsoft.appconfiguration/configurationstores": "az appconfig purge --name {name} --location {location}",
}


@check(
    "cleanup.inventory",
    "What a cleanup would delete (resources, locks, soft-delete)",
    stages=["cleanup"],
    order=60,
    opt_in=True,  # only the cleanup command asks for it
)
def cleanup_inventory(ctx: DiagContext) -> CheckResult:
    title = "What a cleanup would delete (resources, locks, soft-delete)"
    if skip := _need_rg(ctx, "cleanup.inventory", title):
        return skip
    rg = ctx.resource_group
    ok, res, _ = ctx.az("resource", "list", "--resource-group", rg)
    if not ok:
        return CheckResult("cleanup.inventory", title, WARN, f"Cannot list resources: {_error_code(res) or res}")
    env_name = ctx.env.name or ""
    resources: List[Dict[str, Any]] = []
    for r in res or []:
        tags = r.get("tags") or {}
        resources.append(
            {
                "name": r.get("name"),
                "type": r.get("type"),
                "location": r.get("location"),
                "azd_env_tag": tags.get("azd-env-name"),
            }
        )
    # azd tags what it creates with azd-env-name; a mismatch only means something when the resources use that tag at all
    uses_tag = any(r["azd_env_tag"] for r in resources)
    foreign = [r for r in resources if env_name and uses_tag and r["azd_env_tag"] != env_name]
    soft = [
        {**r, "purge": SOFT_DELETE[(r["type"] or "").lower()].format(rg=rg, name=r["name"], location=r["location"])}
        for r in resources
        if (r["type"] or "").lower() in SOFT_DELETE
    ]
    ok, locks, _ = ctx.az("lock", "list", "--resource-group", rg)
    lock_rows = [
        {"name": lk.get("name"), "level": lk.get("level"), "notes": lk.get("notes")}
        for lk in (locks if ok and isinstance(locks, list) else [])
    ]
    ev = {
        "resource_group": rg,
        "azd_env": env_name,
        "resources": resources,
        "foreign": foreign,
        "soft_delete": soft,
        "locks": lock_rows,
        "locks_readable": ok,
    }
    if lock_rows:
        return CheckResult(
            "cleanup.inventory",
            title,
            WARN,
            f"{len(resources)} resources; {len(lock_rows)} resource lock(s) will block deletion until removed.",
            evidence=ev,
        )
    if foreign:
        return CheckResult(
            "cleanup.inventory",
            title,
            INFO,
            f"{len(resources)} resources, {len(foreign)} not tagged azd-env-name={env_name} "
            "(deleting the group deletes them too).",
            evidence=ev,
        )
    return CheckResult(
        "cleanup.inventory", title, PASS, f"{len(resources)} resources, {len(soft)} need a purge.", evidence=ev
    )
