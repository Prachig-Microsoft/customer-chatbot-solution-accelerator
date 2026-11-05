"""Microsoft Fabric capacity state for accelerators that deploy one (Active, Paused, Suspended, ...)."""

from __future__ import annotations

import re
from typing import List

from ..models import FAIL, INFO, PASS, SKIP, WARN, CheckResult
from .azure_checks import _error_code, _need_rg
from .base import DiagContext, check, parallel_map

FABRIC_TYPE = "microsoft.fabric/capacities"
CHANGING = {"provisioning", "resuming", "pausing", "scaling", "updating", "preparing", "deleting", "queued"}
BAD = {"suspended", "failed", "deleted", "deprovisioned"}


def _names_from_env(ctx: DiagContext) -> List[str]:
    """Capacity names the azd environment records (AZURE_FABRIC_CAPACITY_NAME and similar), for reused capacities."""
    out = []
    for key, value in ctx.env.values.items():
        if (
            re.search(r"FABRIC.*CAPACITY.*NAME|CAPACITY.*NAME.*FABRIC", key, re.I)
            and value
            and not value.startswith("<")
        ):
            out.append(value)
    return out


@check(
    "azure.fabric",
    "Microsoft Fabric capacity state",
    stages=["runtime", "configuration", "post-deployment-script", "deployment", "startup", "ingestion"],
    order=25,
)
def fabric_capacity(ctx: DiagContext) -> CheckResult:
    title = "Microsoft Fabric capacity state"
    if skip := _need_rg(ctx, "azure.fabric", title):
        return skip
    rg = ctx.resource_group
    names = [r["name"] for r in ctx.facts.get("resources") or [] if (r.get("type") or "").lower() == FABRIC_TYPE]
    targets = [(n, rg) for n in names]
    for name in _names_from_env(ctx):  # a capacity the accelerator reuses may live in another resource group
        if name not in names:
            ok, found, _ = ctx.az("resource", "list", "--name", name, "--resource-type", "Microsoft.Fabric/capacities")
            if ok and isinstance(found, list) and found:
                targets.append((name, found[0].get("resourceGroup") or rg))
    if not targets:
        return CheckResult("azure.fabric", title, SKIP, "No Fabric capacity in this resource group.")

    def look(item):
        name, group = item
        ok, data, _ = ctx.az(
            "resource",
            "show",
            "--resource-group",
            group,
            "--name",
            name,
            "--resource-type",
            "Microsoft.Fabric/capacities",
        )
        if not ok or not isinstance(data, dict):
            return {"name": name, "resource_group": group, "state": None, "error": _error_code(data) or "unreadable"}
        props = data.get("properties") or {}
        return {
            "name": name,
            "resource_group": group,
            "state": props.get("state"),
            "provisioning_state": props.get("provisioningState"),
            "sku": (data.get("sku") or {}).get("name"),
            "location": data.get("location"),
            "admins": len((props.get("administration") or {}).get("members") or []),
        }

    rows = parallel_map(ctx, look, targets)
    ctx.facts["fabric_capacities"] = rows
    lower = lambda r: str(r.get("state") or "").lower()  # noqa: E731
    paused = [r for r in rows if lower(r) in ("paused", "pausing")]
    bad = [r for r in rows if lower(r) in BAD or str(r.get("provisioning_state") or "").lower() in BAD]
    changing = [r for r in rows if lower(r) in CHANGING]
    unread = [r for r in rows if not r.get("state")]
    ctx.facts["fabric_paused"] = paused
    ev = {"capacities": rows}
    detail = ", ".join(f"{r['name']} ({r.get('sku') or '?'}): {r.get('state') or 'unknown'}" for r in rows)
    if bad:
        return CheckResult("azure.fabric", title, FAIL, f"Fabric capacity not usable: {detail}.", evidence=ev)
    if paused:
        return CheckResult(
            "azure.fabric",
            title,
            WARN,
            f"Fabric capacity is paused: {detail}. Fabric items (lakehouse, data agent, dashboards) are unavailable "
            "until it is resumed.",
            evidence=ev,
        )
    if changing:
        return CheckResult("azure.fabric", title, INFO, f"Fabric capacity is changing state: {detail}.", evidence=ev)
    if unread and len(unread) == len(rows):
        return CheckResult(
            "azure.fabric", title, WARN, f"Could not read the Fabric capacity state: {detail}.", evidence=ev
        )
    return CheckResult("azure.fabric", title, PASS, f"Fabric capacity active: {detail}.", evidence=ev)
