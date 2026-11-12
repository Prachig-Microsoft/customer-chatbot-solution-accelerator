"""Compute quota for the App Service plans the template creates, in the **primary** location (not the AI region).

Model quota and compute quota are different things in different regions: the AI services region holds the model
deployments, while App Service plans are created in the primary location (``AZURE_LOCATION`` / ``location``). A
subscription can have plenty of model quota and still have ``B3 VMs: 0`` in the primary location, which makes ``azd up``
fail validation with ``InternalSubscriptionIsOverQuotaForSku``.

Evidence: the Microsoft.Quota provider for Microsoft.Web (read-only GETs, verified on a real subscription to report the
same "B3 VMs" limit the ARM error names). This check never reports PASS without a limit read from Azure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..bicepeval import find_app_service_plans
from ..models import FAIL, PASS, SKIP, WARN, CheckResult
from ..repo import parse_bicep_params, parse_parameters_env_map
from .azure_checks import _error_code
from .base import DiagContext, check, parallel_map
from .repo_checks import effective_model_requirements, resolve_param

QUOTA_API = "2023-02-01"


def compute_requirements(ctx: DiagContext) -> Dict[str, Any]:
    """App Service plans in the template (or declared in the profile), with SKU, instances and which region decides
    where they go. Cached in facts."""
    if "compute_requirements" in ctx.facts:
        return ctx.facts["compute_requirements"]
    iac = ctx.profile.get("iac", {})
    main = iac.get("main", "infra/main.bicep")
    loc_param, ai_param = iac.get("location_param") or "location", iac.get("ai_location_param") or ""
    params = parse_bicep_params(ctx.repo / main)
    env_map = parse_parameters_env_map(ctx.repo / iac.get("parameters", "infra/main.parameters.json"))
    values = {name: resolve_param(ctx, name, params, env_map) for name in params}
    flavor = next(((n, values[n]) for n in params if n.lower() == "deploymentflavor"), None)
    found = (
        find_app_service_plans(ctx.repo, main, values, tuple(p for p in (loc_param, ai_param) if p))
        if (ctx.repo / main).exists()
        else {"plans": [], "notes": [f"{main} not found"]}
    )
    plans, source = found["plans"], "bicep"
    if not plans and ctx.profile.get("compute_skus"):
        source = "profile"
        plans = [
            {
                "name": s.get("name", "app service plan"),
                "sku": s.get("sku"),
                "capacity": int(s.get("capacity", 1)),
                "zone_redundant": bool(s.get("zone_redundant", False)),
                "location_param": s.get("location_param", loc_param),
                "location_value": None,
                "where": s.get("where") or f"{ctx.profile.get('_profile_file', 'profile')} (compute_skus)",
                "conditional": False,
                "unresolved": None,
            }
            for s in ctx.profile["compute_skus"]
            if (s.get("kind") or s.get("type") or "appservice").lower() in ("appservice", "app_service", "serverfarm")
        ]
    for p in plans:
        role = "primary"
        if p.get("location_param") and ai_param and p["location_param"] == ai_param:
            role = "ai"
        elif p.get("location_value"):
            role = "fixed"
        elif not p.get("location_param"):
            role = "assumed-primary"
        p["location_role"] = role
    out = {
        "plans": plans,
        "notes": found["notes"],
        "source": source if plans else "none",
        "flavor": {"param": flavor[0], "value": flavor[1][0], "source": flavor[1][1]} if flavor else None,
        "location_param": loc_param,
    }
    ctx.facts["compute_requirements"] = out
    return out


def _region_quota(ctx: DiagContext, region: str) -> Dict[str, Any]:
    sub = ctx.facts.get("subscription_id")
    if not sub:
        ok, acct, _ = ctx.az("account", "show")
        sub = acct.get("id") if ok and isinstance(acct, dict) else None
        if not sub:
            return {"error": "cannot read the signed-in subscription"}
    base = (
        f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Web/locations/{region}"
        "/providers/Microsoft.Quota"
    )
    out: Dict[str, Any] = {"limits": {}, "usages": {}}
    ok, q, _ = ctx.az(
        "rest", "--method", "get", "--url", f"{base}/quotas", "--uri-parameters", f"api-version={QUOTA_API}"
    )
    if not ok:
        return {"error": _error_code(q) or "quota read failed"}
    for item in (q or {}).get("value", []) if isinstance(q, dict) else []:
        props = item.get("properties") or {}
        limit = (props.get("limit") or {}).get("value")
        out["limits"][item.get("name")] = {"limit": limit, "applicable": bool(props.get("isQuotaApplicable"))}
    ok, u, _ = ctx.az(
        "rest", "--method", "get", "--url", f"{base}/usages", "--uri-parameters", f"api-version={QUOTA_API}"
    )
    if ok and isinstance(u, dict):
        for item in u.get("value", []):
            out["usages"][item.get("name")] = ((item.get("properties") or {}).get("usages") or {}).get("value")
    else:
        out["usage_error"] = _error_code(u) or "usage read failed"
    return out


def assess_region(data: Dict[str, Any], needs: Dict[Tuple[str, bool], int]) -> Dict[str, Any]:
    """{status: ok|short|unknown, rows: [...]} for one region's quota data against the needed SKUs."""
    if "error" in data:
        return {"status": "unknown", "rows": [], "reason": data["error"]}
    limits, usages = data.get("limits", {}), data.get("usages", {})
    rows = []
    for (sku, zone), need in sorted(needs.items()):
        keys = [f"{sku}_AZ", sku] if zone else [sku]
        key = next((k for k in keys if k in limits and limits[k]["applicable"]), None)
        basis = "per SKU"
        if key is None and "*" in limits and limits["*"].get("limit") is not None:
            key, basis = "*", "regional total (no per-SKU limit listed)"
        if key is None:
            rows.append({"sku": sku, "zone": zone, "need": need, "ok": None, "basis": "not reported for this SKU"})
            continue
        limit = limits[key]["limit"]
        used = usages.get(key)
        known = isinstance(used, (int, float)) and used >= 0
        free = (limit or 0) - (int(used) if known else 0)
        rows.append(
            {
                "sku": key if key != "*" else sku,
                "zone": zone,
                "need": need,
                "limit": limit,
                "used": int(used) if known else None,
                "free": free,
                "ok": free >= need,
                "basis": basis + ("" if known else "; current use not reported"),
            }
        )
    states = [r["ok"] for r in rows]
    status = "short" if False in states else "unknown" if (None in states or not rows) else "ok"
    return {"status": status, "rows": rows}


def _short(rows: List[Dict[str, Any]]) -> str:
    return "; ".join(
        f"{r['sku']} needs {r['need']}, "
        + (
            f"limit {r['limit']}" + (f" ({r['used']} in use)" if r.get("used") is not None else "")
            if "limit" in r
            else "limit not reported"
        )
        for r in rows
        if r["ok"] is not True
    )


@check(
    "quota.compute",
    "App Service plan quota in the primary location",
    stages=["quota", "pre-deployment", "deployment"],
    order=21,
    opt_in=True,  # before deploying only: once deployed, the plan's own instances are counted as used
)
def compute_quota(ctx: DiagContext) -> CheckResult:
    title = "App Service plan quota in the primary location"
    effective_model_requirements(ctx)  # also reads the allowed primary locations from the template
    req = compute_requirements(ctx)
    plans = req["plans"]
    loc_key = ctx.env_key("location") or "AZURE_LOCATION"
    ev: Dict[str, Any] = {
        "plans": plans,
        "source": req["source"],
        "flavor": req["flavor"],
        "notes": req["notes"][:5],
        "location_env": loc_key,
        "location_param": req["location_param"],
    }
    if not plans:
        ev["not_applicable"] = True
        return CheckResult(
            "quota.compute", title, SKIP, "No App Service plan in the template: nothing to check.", evidence=ev
        )
    readable = [p for p in plans if p.get("sku") and p.get("capacity")]
    unread = [p for p in plans if p not in readable]
    if not readable:
        why = unread[0].get("unresolved") or "not a literal"
        return CheckResult(
            "quota.compute",
            title,
            SKIP,
            f"App Service plan quota was not checked: the SKU set at {unread[0]['where']} could not be read ({why}).",
            evidence=ev,
        )
    ai_plans = [p for p in readable if p["location_role"] == "ai"]
    fixed = [p for p in readable if p["location_role"] == "fixed"]
    primary = [p for p in readable if p["location_role"] in ("primary", "assumed-primary")]
    needs: Dict[Tuple[str, bool], int] = {}
    for p in primary:
        k = (p["sku"], bool(p["zone_redundant"]))
        needs[k] = needs.get(k, 0) + int(p["capacity"])
    target = (
        ctx.overrides.get("location")
        or ctx.env.get(loc_key)
        or (str(ctx.env.params.get(req["location_param"])) if ctx.env.params.get(req["location_param"]) else "")
    )
    allowed = list(ctx.facts.get("allowed_locations") or [])
    regions = list(
        dict.fromkeys(
            ([target] if target else []) + (allowed if (ctx.overrides.get("all_regions") or not target) else [])
        )
    )
    ev.update(target_location=target or None, allowed_locations=allowed)
    label = ", ".join(f"{s}{' (zone redundant)' if z else ''} x{n}" for (s, z), n in sorted(needs.items()))
    if not needs:
        where = ", ".join(sorted({p["location_role"] for p in readable}))
        return CheckResult(
            "quota.compute",
            title,
            SKIP,
            f"The App Service plans are not in the primary location ({where}); not checked.",
            evidence=ev,
        )
    if not regions:
        return CheckResult(
            "quota.compute",
            title,
            SKIP,
            f"App Service plan quota ({label}) was not checked: no primary location is chosen and the template has no "
            f"allowed list (set {loc_key}).",
            evidence=ev,
        )
    data = dict(zip(regions, parallel_map(ctx, lambda r: _region_quota(ctx, r), regions)))
    table = {r: assess_region(data[r], needs) for r in regions}
    ev["regions"] = table
    ok_regions = [r for r in regions if table[r]["status"] == "ok" and (not allowed or r in allowed)]
    ok_regions.sort(key=lambda r: -min((row.get("free") or 0) for row in table[r]["rows"]))
    ev["regions_with_quota"] = ok_regions
    ctx.facts.update(
        compute_table=table, compute_candidates=ok_regions, compute_target=target or None, compute_needs=label
    )
    extra = []
    if unread:
        extra.append(f"SKU of {len(unread)} other plan(s) could not be read ({unread[0]['where']})")
    if ai_plans or fixed:
        extra.append(f"{len(ai_plans) + len(fixed)} plan(s) in another region were not checked")
    tail = (" " + "; ".join(extra) + ".") if extra else ""
    if target:
        t = table[target]
        if t["status"] == "ok":
            row = t["rows"][0]
            used = f", {row['used']} in use" if row.get("used") is not None else ""
            status = WARN if extra else PASS
            return CheckResult(
                "quota.compute",
                title,
                status,
                f"{label} fits in {target} (limit {row['limit']}{used}, {row['basis']}).{tail}",
                evidence=ev,
            )
        if t["status"] == "short":
            others = [r for r in ok_regions if r != target]
            hint = f" Locations with quota: {others[:5]}." if others else " No allowed location has enough quota."
            return CheckResult(
                "quota.compute",
                title,
                FAIL,
                f"Not enough App Service quota in {target}: {_short(t['rows'])}.{hint}{tail}",
                evidence=ev,
            )
        reason = t.get("reason") or _short(t["rows"]) or "not reported"
        return CheckResult(
            "quota.compute",
            title,
            SKIP,
            f"App Service plan quota ({label}) in {target} was not checked: {reason}.{tail}",
            evidence=ev,
        )
    if ok_regions:
        return CheckResult(
            "quota.compute",
            title,
            WARN if extra else PASS,
            f"Primary locations with App Service quota for {label}: {ok_regions}.{tail}",
            evidence=ev,
        )
    if any(table[r]["status"] == "short" for r in regions):
        return CheckResult(
            "quota.compute",
            title,
            FAIL,
            f"No allowed primary location has App Service quota for {label}.{tail}",
            evidence=ev,
        )
    reason = next((table[r].get("reason") for r in regions if table[r].get("reason")), "not reported")
    return CheckResult(
        "quota.compute", title, SKIP, f"App Service plan quota ({label}) was not checked: {reason}.{tail}", evidence=ev
    )


def short_rows(table: Dict[str, Any], region: Optional[str]) -> List[Dict[str, Any]]:
    return [r for r in (table.get(region or "", {}) or {}).get("rows", []) if r.get("ok") is False]
