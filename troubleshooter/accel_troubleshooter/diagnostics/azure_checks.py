"""Read-only Azure subscription / deployment / AI checks."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..models import ERROR, FAIL, INFO, PASS, SKIP, WARN, CheckResult
from .base import DiagContext, check
from .repo_checks import effective_model_requirements

_ERR_CODE = re.compile(r"\(([A-Za-z]+)\)|Code:\s*([A-Za-z]+)")


def _error_code(err: Any) -> str:
    text = err.get("error", "") if isinstance(err, dict) else str(err)
    m = _ERR_CODE.search(text or "")
    return (m.group(1) or m.group(2)) if m else ""


def _need_rg(ctx: DiagContext, cid: str, title: str) -> Optional[CheckResult]:
    if not ctx.resource_group:
        return CheckResult(
            cid, title, SKIP, "Resource group unknown (no azd env, no --resource-group, none in issue text)."
        )
    if ctx.rg_known_missing():
        return CheckResult(cid, title, SKIP, f"Resource group '{ctx.resource_group}' does not exist.")
    return None


# --------------------------------------------------------------------------- subscription


@check("az.context", "Azure CLI identity and subscription", stages=["*"], order=5)
def az_context(ctx: DiagContext) -> CheckResult:
    title = "Azure CLI identity and subscription"
    if not ctx.runner.available:
        ctx.facts["az_logged_in"] = False
        return CheckResult("az.context", title, ERROR, "Azure CLI (az) is not installed or not on PATH.")
    ok, data, _ = ctx.az("account", "show")
    if not ok:
        ctx.facts["az_logged_in"] = False
        return CheckResult("az.context", title, FAIL, "Not logged in to Azure CLI (run 'az login').", evidence=data)
    ctx.facts.update(az_logged_in=True, subscription_id=data.get("id"), tenant_id=data.get("tenantId"))
    ev = {
        "subscription_name": data.get("name"),
        "subscription_id": data.get("id"),
        "tenant_id": data.get("tenantId"),
        "user_type": (data.get("user") or {}).get("type"),
        "state": data.get("state"),
    }
    expected = ctx.overrides.get("subscription") or ctx.env.get(ctx.env_key("subscription"))
    if data.get("state") and data["state"] != "Enabled":
        return CheckResult(
            "az.context",
            title,
            FAIL,
            f"Subscription state is '{data['state']}' (not Enabled).",
            evidence=ev,
            doc_refs=[ctx.ts_ref("ReadOnlyDisabledSubscription")],
        )
    if expected and expected != data.get("id"):
        ctx.facts["subscription_mismatch"] = {"expected": expected, "active": data.get("id")}
        return CheckResult(
            "az.context",
            title,
            FAIL,
            f"Active az subscription {data.get('id')} differs from the deployment's subscription {expected}.",
            evidence={**ev, "expected_subscription": expected},
        )
    return CheckResult(
        "az.context",
        title,
        PASS,
        f"Logged in as a {ev['user_type']} to subscription '{ev['subscription_name']}'.",
        evidence=ev,
    )


@check(
    "providers.registration",
    "Resource provider registration",
    stages=["deployment", "pre-deployment", "quota"],
    order=8,
)
def providers(ctx: DiagContext) -> CheckResult:
    title = "Resource provider registration"
    namespaces = ctx.profile.get("required_providers", [])
    with ThreadPoolExecutor(max_workers=6) as pool:
        outs = list(pool.map(lambda ns: (ns, ctx.az("provider", "show", "--namespace", ns)), namespaces))
    states = {ns: (d.get("registrationState") if ok else None) for ns, (ok, d, _) in outs}
    missing = [ns for ns, s in states.items() if s in ("NotRegistered", "Unregistered", "Registering")]
    unknown = [ns for ns, s in states.items() if s is None]
    ctx.facts["unregistered_providers"] = missing
    if missing:
        status, summary = FAIL, f"Not registered: {missing}"
    elif unknown and len(unknown) == len(namespaces):
        status, summary = ERROR, "Could not read provider registration state."
    elif unknown:
        status, summary = WARN, f"Registered except unknown state for {unknown}."
    else:
        status, summary = PASS, f"All {len(namespaces)} required providers registered."
    return CheckResult(
        "providers.registration",
        title,
        status,
        summary,
        evidence={"states": {k: v or "unknown" for k, v in states.items()}},
    )


# --------------------------------------------------------------------------- resource group & ARM


@check(
    "rg.state",
    "Resource group exists and provisioning state",
    stages=[
        "deployment",
        "post-deployment-script",
        "startup",
        "runtime",
        "authentication",
        "throttling",
        "configuration",
        "local-vs-cloud",
        "networking",
        "ingestion",
        "document-processing",
    ],
    order=10,
)
def rg_state(ctx: DiagContext) -> CheckResult:
    title = "Resource group exists and provisioning state"
    rg = ctx.resource_group
    if not rg:
        return CheckResult("rg.state", title, SKIP, "Resource group unknown — provide --resource-group or an azd env.")
    ok, data, _ = ctx.az("group", "show", "--name", rg)
    if not ok:
        code = _error_code(data)
        if code == "ResourceGroupNotFound":
            ctx.facts["rg_exists"] = False
            return CheckResult(
                "rg.state",
                title,
                FAIL,
                f"Resource group '{rg}' (from {ctx.resource_group_source}) does not exist in the active subscription.",
                evidence={
                    "resource_group": rg,
                    "source": ctx.resource_group_source,
                    "error_code": code,
                    "azd_env": ctx.env.name or None,
                },
                doc_refs=[ctx.ts_ref("ResourceGroupNotFound")],
            )
        return CheckResult("rg.state", title, ERROR, f"Could not read resource group: {code or data}", evidence=data)
    ctx.facts["rg_exists"] = True
    state = (data.get("properties") or {}).get("provisioningState")
    ctx.facts["rg_location"] = data.get("location")
    ev = {
        "resource_group": rg,
        "location": data.get("location"),
        "provisioning_state": state,
        "azd_env_tag": (data.get("tags") or {}).get("azd-env-name"),
    }
    if state == "Deleting":
        ctx.facts["rg_deleting"] = True
        return CheckResult("rg.state", title, FAIL, f"Resource group '{rg}' is being deleted.", evidence=ev)
    return CheckResult(
        "rg.state", title, PASS, f"Resource group '{rg}' exists ({state}, {data.get('location')}).", evidence=ev
    )


def _op_errors(ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for op in ops or []:
        p = op.get("properties") or {}
        if p.get("provisioningState") not in ("Failed",):
            continue
        sm = p.get("statusMessage") or {}
        err = sm.get("error") if isinstance(sm, dict) else None
        err = err or (sm if isinstance(sm, dict) and "code" in sm else {})
        details = err.get("details") or []
        inner = details[0] if details else {}
        tr = p.get("targetResource") or {}
        out.append(
            {
                "resource_type": tr.get("resourceType"),
                "resource_name": tr.get("resourceName"),
                "code": inner.get("code") or err.get("code"),
                "outer_code": err.get("code"),
                "message": (inner.get("message") or err.get("message") or "")[:600],
                "timestamp": p.get("timestamp"),
            }
        )
    return out


@check(
    "deploy.operations",
    "ARM/Bicep deployment history and failed operations",
    stages=["deployment", "quota", "post-deployment-script"],
    order=12,
)
def deploy_operations(ctx: DiagContext) -> CheckResult:
    title = "ARM/Bicep deployment history and failed operations"
    if skip := _need_rg(ctx, "deploy.operations", title):
        return skip
    rg = ctx.resource_group
    ok, deps, _ = ctx.az("deployment", "group", "list", "--resource-group", rg)
    if not ok:
        return CheckResult("deploy.operations", title, ERROR, f"Cannot list deployments: {_error_code(deps) or deps}")
    deps = sorted(deps or [], key=lambda d: (d.get("properties") or {}).get("timestamp") or "", reverse=True)
    summary = [
        {
            "name": d.get("name"),
            "state": (d.get("properties") or {}).get("provisioningState"),
            "timestamp": (d.get("properties") or {}).get("timestamp"),
        }
        for d in deps[:10]
    ]
    running = [d for d in summary if d["state"] in ("Running", "Accepted")]
    failed = [d for d in deps if (d.get("properties") or {}).get("provisioningState") == "Failed"]
    errors: List[Dict[str, Any]] = []
    for d in failed[:3]:
        ok, ops, _ = ctx.az("deployment", "operation", "group", "list", "--resource-group", rg, "--name", d["name"])
        if ok:
            for e in _op_errors(ops):
                e["deployment"] = d["name"]
                errors.append(e)
    codes = sorted({e["code"] for e in errors if e.get("code") and e["code"] != "DeploymentFailed"})
    # ARM leaves brief gaps between phases where nothing is "Running"; very recent activity still means "in progress".
    recent_min = None
    if summary and not failed:
        try:
            newest = datetime.fromisoformat(summary[0]["timestamp"].replace("Z", "+00:00"))
            recent_min = (datetime.now(timezone.utc) - newest).total_seconds() / 60
        except (AttributeError, ValueError, KeyError):
            pass
    ctx.facts.update(
        deployment_error_codes=codes,
        deployment_errors=errors,
        running_deployments=running,
        deployments=summary,
        deployment_recent_minutes=recent_min,
    )
    ev = {"recent_deployments": summary, "failed_operations": errors[:15]}
    if running:
        now = datetime.now(timezone.utc)
        stuck = []
        for r in running:
            try:
                ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                if (now - ts).total_seconds() > 3600:
                    stuck.append(r["name"])
            except (AttributeError, ValueError):
                pass
        ctx.facts["stuck_deployments"] = stuck
        return CheckResult(
            "deploy.operations",
            title,
            WARN if not stuck else FAIL,
            f"{len(running)} deployment(s) still running" + (f"; {stuck} running > 1h" if stuck else "."),
            evidence=ev,
        )
    if not deps:
        return CheckResult(
            "deploy.operations", title, INFO, "No ARM deployments recorded in this resource group.", evidence=ev
        )
    if failed and not codes:
        return CheckResult(
            "deploy.operations",
            title,
            FAIL,
            f"{len(failed)} failed deployment(s); no operation-level error code.",
            evidence=ev,
        )
    if codes:
        return CheckResult(
            "deploy.operations",
            title,
            FAIL,
            f"Failed deployment operations with error code(s): {', '.join(codes)}.",
            evidence=ev,
        )
    return CheckResult(
        "deploy.operations",
        title,
        PASS,
        f"Latest deployment '{summary[0]['name']}' is {summary[0]['state']}.",
        evidence=ev,
    )


@check(
    "resources.state",
    "Resource inventory and provisioning states",
    stages=["deployment", "post-deployment-script", "startup", "runtime", "ingestion", "document-processing"],
    order=14,
)
def resources_state(ctx: DiagContext) -> CheckResult:
    title = "Resource inventory and provisioning states"
    if skip := _need_rg(ctx, "resources.state", title):
        return skip
    ok, res, _ = ctx.az("resource", "list", "--resource-group", ctx.resource_group)
    if not ok:
        return CheckResult("resources.state", title, ERROR, f"Cannot list resources: {_error_code(res) or res}")
    by_type: Dict[str, int] = {}
    bad = []
    for r in res or []:
        by_type[r.get("type")] = by_type.get(r.get("type"), 0) + 1
        state = r.get("provisioningState")
        if state and state not in ("Succeeded",):
            bad.append({"name": r.get("name"), "type": r.get("type"), "state": state})
    ctx.facts["resources"] = [{"name": r.get("name"), "type": r.get("type")} for r in res or []]
    expected = ctx.profile.get("expected_resource_types", [])
    missing = [t for t in expected if not any(k.lower() == t.lower() for k in by_type)]
    ctx.facts["missing_resource_types"] = missing
    ctx.facts["unhealthy_resources"] = bad
    status = FAIL if (bad or missing) else PASS
    summary = f"{len(res or [])} resources."
    if missing:
        summary += f" Missing expected types: {missing} (partial deployment?)."
    if bad:
        summary += f" {len(bad)} not in Succeeded state."
    return CheckResult(
        "resources.state",
        title,
        status,
        summary,
        evidence={"counts_by_type": by_type, "not_succeeded": bad, "missing_types": missing},
    )


@check(
    "activity.failures",
    "Failed operations in the activity log (72h)",
    stages=["deployment", "runtime", "configuration", "post-deployment-script"],
    order=16,
)
def activity_failures(ctx: DiagContext) -> CheckResult:
    title = "Failed operations in the activity log (72h)"
    if skip := _need_rg(ctx, "activity.failures", title):
        return skip
    ok, events, _ = ctx.az(
        "monitor",
        "activity-log",
        "list",
        "--resource-group",
        ctx.resource_group,
        "--offset",
        "72h",
        "--status",
        "Failed",
        "--max-events",
        "50",
    )
    if not ok:
        return CheckResult(
            "activity.failures", title, ERROR, f"Cannot read activity log: {_error_code(events) or events}"
        )
    rows = []
    for e in events or []:
        rows.append(
            {
                "time": e.get("eventTimestamp"),
                "operation": (e.get("operationName") or {}).get("value"),
                "status": (e.get("status") or {}).get("value"),
                "sub_status": (e.get("subStatus") or {}).get("value"),
                "resource": (e.get("resourceId") or "").split("/")[-1],
                "caller_type": "user" if "@" in (e.get("caller") or "") else "principal",
            }
        )
    ctx.facts["activity_failures"] = rows
    return CheckResult(
        "activity.failures",
        title,
        WARN if rows else PASS,
        f"{len(rows)} failed operation(s) in the last 72h." if rows else "No failed operations in the last 72h.",
        evidence={"events": rows[:20]},
    )


# --------------------------------------------------------------------------- quota & models


def _usage_for_region(ctx: DiagContext, region: str):
    ok, data, rec = ctx.az("cognitiveservices", "usage", "list", "--location", region)
    return region, ok, data, rec


def quota_table(ctx: DiagContext, regions: List[str]) -> Dict[str, Any]:
    reqs = effective_model_requirements(ctx)
    with ThreadPoolExecutor(max_workers=6) as pool:
        outs = list(pool.map(lambda r: _usage_for_region(ctx, r), regions))
    table: Dict[str, Any] = {}
    for region, ok, data, _ in outs:
        if not ok:
            table[region] = {"error": _error_code(data) or str(data)[:200]}
            continue
        usage = {u["name"]["value"]: u for u in data or [] if isinstance(u, dict) and u.get("name")}
        rows = []
        for r in reqs:
            u = usage.get(r["usage_name"])
            if u is None:
                rows.append(
                    {
                        **_slim(r),
                        "used": None,
                        "limit": None,
                        "available": None,
                        "scope": None,
                        "ok": False,
                        "note": "no quota entry for this model/SKU in region (model or SKU not offered)",
                    }
                )
                continue
            used, limit = int(u.get("currentValue") or 0), int(u.get("limit") or 0)
            avail = limit - used
            need = r["capacity"] if isinstance(r["capacity"], int) else 0
            rows.append(
                {
                    **_slim(r),
                    "used": used,
                    "limit": limit,
                    "available": avail,
                    "scope": u.get("scopeType"),
                    "ok": avail >= need,
                }
            )
        table[region] = {"models": rows, "all_ok": all(x["ok"] for x in rows)}
    return table


def _slim(r: Dict[str, Any]) -> Dict[str, Any]:
    return {k: r[k] for k in ("role", "model", "sku", "capacity", "usage_name", "capacity_source", "capacity_env")}


@check(
    "quota.models",
    "Model quota vs required capacity",
    stages=["quota", "pre-deployment", "deployment", "throttling"],
    order=20,
)
def quota_models(ctx: DiagContext) -> CheckResult:
    title = "Model quota vs required capacity"
    reqs = effective_model_requirements(ctx)
    if not reqs:
        return CheckResult("quota.models", title, SKIP, "Profile defines no model deployments.")
    allowed = ctx.facts.get("allowed_ai_regions") or []
    target = ctx.ai_region
    regions = list(
        dict.fromkeys(
            ([target] if target else []) + (allowed if (ctx.overrides.get("all_regions") or not target) else [])
        )
    )
    if not regions:
        return CheckResult("quota.models", title, SKIP, "No target AI region and no allowed-region list in IaC.")
    table = quota_table(ctx, regions)
    if target and target in table and not table[target].get("all_ok") and not ctx.overrides.get("all_regions"):
        # Target region is short: evaluate the other template-allowed regions so we can recommend one.
        table.update(quota_table(ctx, [r for r in allowed if r != target]))
    ctx.facts["quota_table"] = table
    ctx.facts["quota_target_region"] = target or None

    def _chat_available(region: str) -> int:
        return next((m["available"] or 0 for m in table[region]["models"] if m["role"] == "chat"), 0)

    candidates = sorted([r for r, v in table.items() if v.get("all_ok")], key=_chat_available, reverse=True)
    ctx.facts["quota_candidate_regions"] = candidates
    shortfalls = []
    for region, v in table.items():
        for m in v.get("models", []):
            if not m["ok"]:
                shortfalls.append({"region": region, **m})
    ctx.facts["quota_shortfalls"] = shortfalls
    ref = [ctx.doc_ref(ctx.profile["iac"]["quota_doc"], "Check Quota|Quota"), ctx.ts_ref("InsufficientQuota")]
    ev = {
        "target_region": target or None,
        "requirements": [_slim(r) for r in reqs],
        "regions": table,
        "regions_with_sufficient_quota": candidates,
    }
    if target:
        t = table.get(target, {})
        if "error" in t:
            return CheckResult(
                "quota.models", title, ERROR, f"Could not read quota in {target}: {t['error']}", evidence=ev
            )
        if t.get("all_ok"):
            ctx.facts["quota_ok"] = True
            return CheckResult(
                "quota.models", title, PASS, f"Sufficient quota for all models in {target}.", evidence=ev, doc_refs=ref
            )
        ctx.facts["quota_ok"] = False
        short = [
            f"{m['model']} ({m['sku']}): need {m['capacity']}, available {m['available']}"
            for m in t.get("models", [])
            if not m["ok"]
        ]
        return CheckResult(
            "quota.models",
            title,
            FAIL,
            f"Insufficient quota in {target}: " + "; ".join(short),
            evidence=ev,
            doc_refs=ref,
        )
    return CheckResult(
        "quota.models",
        title,
        PASS if candidates else FAIL,
        (
            f"Regions with sufficient quota for all models: {candidates}"
            if candidates
            else "No allowed region has sufficient quota for all models."
        ),
        evidence=ev,
        doc_refs=ref,
    )


@check(
    "models.availability",
    "Model/version/deployment-type availability in region",
    stages=["quota", "pre-deployment", "deployment"],
    order=22,
)
def models_availability(ctx: DiagContext) -> CheckResult:
    title = "Model/version/deployment-type availability in region"
    region = ctx.ai_region
    reqs = effective_model_requirements(ctx)
    if not region and ctx.facts.get("quota_candidate_regions"):
        # Preflight without a chosen region: validate the top quota-recommended region.
        region = ctx.facts["quota_candidate_regions"][0]
    if not region:
        return CheckResult("models.availability", title, SKIP, "No target AI region known (use --region).")
    ok, data, _ = ctx.az("cognitiveservices", "model", "list", "--location", region)
    if not ok:
        return CheckResult("models.availability", title, ERROR, f"Cannot list models: {_error_code(data) or data}")
    now = datetime.now(timezone.utc)
    rows, problems = [], []
    for r in reqs:
        matches = [m["model"] for m in data or [] if (m.get("model") or {}).get("name") == r["model"]]
        versions = sorted({m.get("version") for m in matches})
        m = next((x for x in matches if not r["version"] or x.get("version") == r["version"]), None)
        skus = [s.get("name") for s in (m or {}).get("skus", [])]
        dep = ((m or {}).get("deprecation") or {}).get("inference")
        deprecated = False
        if dep:
            try:
                deprecated = datetime.fromisoformat(dep.replace("Z", "+00:00")) < now
            except ValueError:
                pass
        row = {
            "model": r["model"],
            "version": r["version"],
            "sku": r["sku"],
            "available_versions": versions,
            "version_found": m is not None,
            "sku_supported": r["sku"] in skus,
            "skus": skus,
            "inference_deprecation": dep,
            "deprecated": deprecated,
            "lifecycle": (m or {}).get("lifecycleStatus"),
        }
        rows.append(row)
        if not matches:
            problems.append(f"{r['model']} not offered in {region}")
        elif m is None:
            problems.append(f"{r['model']} version {r['version']} not offered in {region} (available: {versions})")
        elif r["sku"] not in skus:
            problems.append(f"{r['model']} does not support deployment type {r['sku']} in {region} (supports {skus})")
        elif deprecated:
            problems.append(f"{r['model']} {r['version']} inference retired on {dep}")
    ctx.facts["model_availability_problems"] = problems
    ref = [ctx.ts_ref("DeploymentModelNotSupported")]
    return CheckResult(
        "models.availability",
        title,
        FAIL if problems else PASS,
        "; ".join(problems) if problems else f"All models/versions/deployment types available in {region}.",
        evidence={"region": region, "models": rows},
        doc_refs=ref,
    )


def _ai_account(ctx: DiagContext) -> str:
    name = ctx.env.get(ctx.env_key("ai_account"))
    if name:
        return name
    for r in ctx.facts.get("resources", []):
        if (r.get("type") or "").lower() == "microsoft.cognitiveservices/accounts":
            return r["name"]
    if not ctx.facts.get("resources") and ctx.resource_group and not ctx.rg_known_missing():
        ok, data, _ = ctx.az("cognitiveservices", "account", "list", "--resource-group", ctx.resource_group)
        if ok and data:
            return data[0].get("name", "")
    return ""


@check(
    "ai.deployments",
    "AI model deployments (names, SKU, capacity)",
    stages=["throttling", "runtime", "authentication", "post-deployment-script", "deployment", "configuration"],
    order=24,
)
def ai_deployments(ctx: DiagContext) -> CheckResult:
    title = "AI model deployments (names, SKU, capacity)"
    if skip := _need_rg(ctx, "ai.deployments", title):
        return skip
    acct = _ai_account(ctx)
    if not acct:
        return CheckResult("ai.deployments", title, SKIP, "AI Services account not found in resource group.")
    ok, data, _ = ctx.az(
        "cognitiveservices", "account", "deployment", "list", "--resource-group", ctx.resource_group, "--name", acct
    )
    if not ok:
        return CheckResult("ai.deployments", title, ERROR, f"Cannot list deployments: {_error_code(data) or data}")
    deps = [
        {
            "name": d.get("name"),
            "model": ((d.get("properties") or {}).get("model") or {}).get("name"),
            "version": ((d.get("properties") or {}).get("model") or {}).get("version"),
            "sku": (d.get("sku") or {}).get("name"),
            "capacity": (d.get("sku") or {}).get("capacity"),
            "state": (d.get("properties") or {}).get("provisioningState"),
            "rate_limits": (d.get("properties") or {}).get("rateLimits"),
        }
        for d in data or []
    ]
    ctx.facts["ai_deployments"] = deps
    reqs = effective_model_requirements(ctx)
    missing = [r["model"] for r in reqs if not any(d["name"] == r["model"] or d["model"] == r["model"] for d in deps)]
    bad = [d for d in deps if d["state"] != "Succeeded"]
    ctx.facts["missing_ai_deployments"] = missing
    status = FAIL if (missing or bad) else PASS
    summary = f"{len(deps)} deployment(s) on {acct}."
    if missing:
        summary += f" Missing: {missing}."
    if bad:
        summary += f" Not Succeeded: {[d['name'] for d in bad]}."
    return CheckResult("ai.deployments", title, status, summary, evidence={"account": acct, "deployments": deps})


@check(
    "network.public_access",
    "Public network access on data/AI services",
    stages=[
        "networking",
        "runtime",
        "startup",
        "authentication",
        "ingestion",
        "post-deployment-script",
        "local-vs-cloud",
    ],
    order=30,
)
def network_access(ctx: DiagContext) -> CheckResult:
    title = "Public network access on data/AI services"
    if skip := _need_rg(ctx, "network.public_access", title):
        return skip
    resources = ctx.facts.get("resources")
    if resources is None:
        ok, res, _ = ctx.az("resource", "list", "--resource-group", ctx.resource_group)
        resources = [{"name": r.get("name"), "type": r.get("type")} for r in (res or [])] if ok else []
    checks = {
        "microsoft.cognitiveservices/accounts": ("cognitiveservices", "account", "show"),
        "microsoft.search/searchservices": ("search", "service", "show"),
        "microsoft.documentdb/databaseaccounts": ("cosmosdb", "show"),
    }
    rows, disabled = [], []
    has_pe = any((r.get("type") or "").lower() == "microsoft.network/privateendpoints" for r in resources)
    for r in resources:
        cmd = checks.get((r.get("type") or "").lower())
        if not cmd:
            continue
        ok, data, _ = ctx.az(*cmd, "--resource-group", ctx.resource_group, "--name", r["name"])
        if not ok:
            rows.append({"name": r["name"], "error": _error_code(data)})
            continue
        props = data.get("properties", data)
        pna = props.get("publicNetworkAccess") or data.get("publicNetworkAccess")
        acl = (props.get("networkAcls") or {}).get("defaultAction")
        rows.append(
            {
                "name": r["name"],
                "type": r["type"],
                "publicNetworkAccess": pna,
                "networkAclsDefault": acl,
                "disableLocalAuth": props.get("disableLocalAuth"),
            }
        )
        if (pna or "").lower() == "disabled" or (acl or "").lower() == "deny":
            disabled.append(r["name"])
    ctx.facts["public_access_disabled"] = disabled
    ctx.facts["has_private_endpoints"] = has_pe
    ref = [ctx.ref("deployment", "blocking public network access")]
    if disabled and not has_pe:
        return CheckResult(
            "network.public_access",
            title,
            FAIL,
            f"Public access disabled on {disabled} and no private endpoints exist in the RG — apps "
            "cannot reach these services.",
            evidence={"services": rows},
            doc_refs=ref,
        )
    if disabled:
        return CheckResult(
            "network.public_access",
            title,
            INFO,
            f"Public access disabled on {disabled}; private endpoints "
            "present (expected for WAF/private networking).",
            evidence={"services": rows},
            doc_refs=ref,
        )
    return CheckResult(
        "network.public_access",
        title,
        PASS,
        "Public network access enabled on data/AI services.",
        evidence={"services": rows},
    )
