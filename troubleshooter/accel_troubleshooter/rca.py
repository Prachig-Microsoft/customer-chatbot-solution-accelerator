"""Evidence-based root-cause analysis.

Each rule inspects the issue, live facts and check results and may emit a ``Hypothesis``.
Confidence is assigned from explicit criteria (no numeric scores):

* high   - the causal condition was directly observed by a live/repo check AND it is consistent with the
           reported symptom (or the exact error code was observed in Azure).
* medium - the condition was observed but its link to the symptom is indirect, OR an exact documented error
           code was reported by the user but not yet observed live.
* low    - only textual/keyword similarity; no direct evidence.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

from .actions import build_action
from .diagnostics.base import DiagContext
from .models import CONFIRMED, FAIL, LIKELY, POSSIBLE, Action, Hypothesis

Rule = Callable[[DiagContext], List[Tuple[Hypothesis, List[Action]]]]
RULES: List[Tuple[int, Rule]] = []

QUOTA_CODES = {"insufficientquota"}  # Azure OpenAI model quota; other *Quota* codes route to their own doc rows
MODEL_CODES = {"deploymentmodelnotsupported", "servicemodeldeprecated", "invalidresourceproperties"}


def rule(priority: int):
    def deco(fn: Rule) -> Rule:
        RULES.append((priority, fn))
        return fn

    return deco


def _codes(ctx: DiagContext) -> Tuple[set, set]:
    reported = {c.lower() for c in ctx.issue.error_codes}
    observed = {c.lower() for c in ctx.facts.get("deployment_error_codes", [])}
    return reported, observed


def _failed(ctx: DiagContext, cid: str) -> bool:
    r = ctx.results.get(cid)
    return bool(r and r.status == FAIL)


def _ts_ref(ctx: DiagContext, code: str) -> str:
    return ctx.ts_ref(code)


# --------------------------------------------------------------------------- blockers


@rule(1)
def az_not_logged_in(ctx):
    if ctx.facts.get("az_logged_in") is False:
        return [
            (
                Hypothesis(
                    "az-cli-unavailable",
                    "Azure CLI not authenticated/available — live diagnostics blocked",
                    LIKELY,
                    "high",
                    ["az.context failed: no authenticated Azure CLI session"],
                    supporting_checks=["az.context"],
                    missing_evidence=["All live Azure evidence (run 'az login' then re-run)"],
                    component="local tooling",
                ),
                [],
            )
        ]
    return []


@rule(2)
def subscription_mismatch(ctx):
    mm = ctx.facts.get("subscription_mismatch")
    if not mm:
        return []
    a = build_action("select-subscription", ctx.profile, subscription=mm["expected"])
    return [
        (
            Hypothesis(
                "subscription-mismatch",
                "Azure CLI is pointed at a different subscription than the deployment",
                CONFIRMED,
                "high",
                [f"active subscription {mm['active']} != azd env subscription {mm['expected']}"],
                supporting_checks=["az.context"],
                remediation_ids=[a.id],
                component="Azure CLI context",
                doc_refs=[ctx.ref("deployment", "az account set")],
            ),
            [a],
        )
    ]


@rule(3)
def rg_not_found(ctx):
    if ctx.facts.get("rg_exists") is not False:
        return []
    rg = ctx.resource_group
    src = ctx.resource_group_source
    rationale = [
        f"'az group show --name {rg}' returned ResourceGroupNotFound (live)",
        f"resource group name came from {src}",
        "troubleshooting guide: ResourceGroupNotFound is caused by a deleted RG or an old .env file",
    ]
    reported = {c.lower() for c in ctx.issue.error_codes}
    status = (
        CONFIRMED
        if "resourcegroupnotfound" in reported
        or ctx.issue.primary_stage in ("runtime", "startup", "deployment", "post-deployment-script", "unknown")
        else LIKELY
    )
    acts = [
        build_action("new-azd-env", ctx.profile, doc_refs=[_ts_ref(ctx, "ResourceGroupNotFound")]),
        build_action("azd-provision", ctx.profile, doc_refs=[ctx.ref("deployment", "azd up")]),
    ]
    alt = []
    if not ctx.facts.get("subscription_mismatch") and ctx.facts.get("subscription_id"):
        alt.append("RG may exist in another subscription — verify with 'az group list --subscription <id>'")
    return [
        (
            Hypothesis(
                "resource-group-missing",
                f"Resource group '{rg}' no longer exists (deleted or stale azd env)",
                status,
                "high",
                rationale,
                supporting_checks=["rg.state"],
                contradicting=alt,
                doc_refs=[_ts_ref(ctx, "ResourceGroupNotFound"), ctx.ref("deployment", "Recover from Failed")],
                remediation_ids=[a.id for a in acts],
                component="Resource group / azd environment",
            ),
            acts,
        )
    ]


# --------------------------------------------------------------------------- quota & model


@rule(10)
def quota_insufficient(ctx):
    reported, observed = _codes(ctx)
    target = ctx.facts.get("quota_target_region")
    all_short = ctx.facts.get("quota_shortfalls", [])
    if target:
        shortfalls = [s for s in all_short if s["region"] == target]
    else:
        shortfalls = all_short if ctx.facts.get("quota_table") and not ctx.facts.get("quota_candidate_regions") else []
    text_quota = "quota" in ctx.issue.raw_text.lower() or "quota" in ctx.issue.stages
    code_reported = bool(reported & QUOTA_CODES)
    code_observed = bool(observed & QUOTA_CODES)
    if not (shortfalls or code_observed or code_reported):
        if not text_quota or ctx.facts.get("quota_table"):
            return []  # nothing reported, or live quota evidence shows sufficient capacity
    rationale, contra, missing = [], [], []
    if code_observed:
        rationale.append(f"failed ARM operation reported {sorted(observed & QUOTA_CODES)} (live deployment operations)")
    if code_reported:
        rationale.append(
            f"user-reported error code {sorted(reported & QUOTA_CODES)} is documented under Quota & Capacity"
        )
    for s in shortfalls:
        rationale.append(
            f"{s['model']} ({s['sku']}) in {s['region']}: requires {s['capacity']}K TPM "
            f"({s['capacity_source']}), available {s['available']} (used {s['used']}/{s['limit']}, "
            f"scope {s['scope']})"
        )
    if ctx.facts.get("quota_ok") is True:
        contra.append(
            f"live quota in {ctx.facts.get('quota_target_region')} is currently sufficient for all models — "
            "quota may have been freed since the failure or a different region/subscription was used"
        )
    target_rows = (ctx.facts.get("quota_table") or {}).get(target or "", {}).get("models", [])
    for m in ctx.issue.models:
        row = next((x for x in target_rows if x["model"] == m), None)
        if row and row["ok"] and (code_reported or code_observed):
            contra.append(
                f"{m} is named in the reported error, but live {row['sku']} quota in {target} is now "
                f"{row['available']}K available (>= {row['capacity']} required) — usage changed since the failure "
                "or the failing request asked for more capacity than currently configured"
            )
    if "quota.models" not in ctx.results or ctx.results["quota.models"].status in ("skip", "error"):
        missing.append("live quota usage for the target region (az cognitiveservices usage list)")
    if not ctx.facts.get("quota_target_region"):
        missing.append("which AI region was used (AZURE_ENV_AI_SERVICE_LOCATION or --region)")
    if shortfalls and (code_observed or code_reported):
        status, conf = CONFIRMED, "high"
    elif shortfalls:
        status, conf = LIKELY, "high"
    elif code_observed:
        status, conf = (POSSIBLE if contra else LIKELY), ("medium" if contra else "high")
    elif code_reported:
        status, conf = (POSSIBLE if contra else LIKELY), ("low" if contra else "medium")
    else:
        status, conf = POSSIBLE, "low"
        rationale.append("issue text mentions quota but no shortfall or quota error code was observed")
    acts: List[Action] = []
    docs = [ctx.ref("quota", "Check Quota|quota"), _ts_ref(ctx, "InsufficientQuota")]
    candidates = ctx.facts.get("quota_candidate_regions") or []
    reqs = {r["model"]: r for r in ctx.facts.get("model_requirements", [])}
    for s in shortfalls:
        r = reqs.get(s["model"], {})
        floor = r.get("min_value") or 1
        if s["available"] is not None and s["available"] >= floor and r.get("capacity_env"):
            acts.append(
                build_action(
                    "set-model-capacity",
                    ctx.profile,
                    model=s["model"],
                    env=r["capacity_env"],
                    capacity=s["available"],
                    doc_refs=docs,
                )
            )
        if s["scope"] == "Global":
            rationale.append(
                f"{s['model']} {s['sku']} quota is a subscription-wide Global pool — changing region will "
                "NOT free capacity for this model"
            )
    if candidates and any(s["scope"] != "Global" for s in shortfalls):
        acts.append(
            build_action(
                "set-ai-region",
                ctx.profile,
                region=candidates[0],
                env=ctx.profile.get("azd_env_keys", {}).get("ai_location"),
                doc_refs=docs,
            )
        )
    if not shortfalls and not ctx.facts.get("quota_table"):
        scripts = ctx.profile.get("iac", {}).get("quota_scripts", [])
        qp = next((s for s in scripts if "params" in s), None)
        if qp:
            models = ",".join(f"{r['model']}:{r['capacity']}" for r in ctx.facts.get("model_requirements", []))
            acts.append(
                build_action(
                    "quota-check-script",
                    ctx.profile,
                    script=qp,
                    models=models,
                    regions=",".join(ctx.facts.get("allowed_ai_regions", [])),
                    doc_refs=docs,
                )
            )
    acts += [
        build_action("request-quota-increase", ctx.profile, doc_refs=[ctx.ref("quota", "Request More Quota|Quota")]),
        build_action("free-quota-by-deleting-deployments", ctx.profile),
    ]
    acts = [a for a in acts if a]
    return [
        (
            Hypothesis(
                "quota-insufficient",
                "Insufficient Azure OpenAI model quota for the requested capacity",
                status,
                conf,
                rationale,
                supporting_checks=["quota.models", "deploy.operations"],
                contradicting=contra,
                missing_evidence=missing,
                doc_refs=docs,
                source_refs=[
                    f"{ctx.profile['iac']['main']}:{r.get('capacity_param_line')}"
                    for r in reqs.values()
                    if r.get("capacity_param_line")
                ],
                remediation_ids=[a.id for a in acts],
                component="Azure OpenAI model deployment",
            ),
            acts,
        )
    ]


@rule(11)
def model_unsupported(ctx):
    reported, observed = _codes(ctx)
    problems = ctx.facts.get("model_availability_problems") or []
    hit = (reported | observed) & MODEL_CODES
    if not problems and not hit:
        return []
    rationale = list(problems) + ([f"error code(s) {sorted(hit)}"] if hit else [])
    status, conf = (CONFIRMED, "high") if problems and hit else (LIKELY, "high") if problems else (LIKELY, "medium")
    acts = [
        build_action(
            "follow-documented-steps",
            ctx.profile,
            code="DeploymentModelNotSupported",
            doc_refs=[_ts_ref(ctx, "DeploymentModelNotSupported")],
        )
    ]
    cands = ctx.facts.get("quota_candidate_regions") or []
    if cands:
        acts.append(
            build_action(
                "set-ai-region",
                ctx.profile,
                region=cands[0],
                env=ctx.profile.get("azd_env_keys", {}).get("ai_location"),
            )
        )
    return [
        (
            Hypothesis(
                "model-region-unsupported",
                "Model/version/deployment type not available in the selected region",
                status,
                conf,
                rationale,
                supporting_checks=["models.availability"],
                missing_evidence=[] if problems else ["model catalog for the target region (--region)"],
                doc_refs=[_ts_ref(ctx, "DeploymentModelNotSupported")],
                remediation_ids=[a.id for a in acts if a],
                component="Azure OpenAI model deployment",
            ),
            [a for a in acts if a],
        )
    ]


@rule(12)
def providers_unregistered(ctx):
    missing = ctx.facts.get("unregistered_providers") or []
    if not missing:
        return []
    reported, observed = _codes(ctx)
    codes = {"missingsubscriptionregistration", "noregisteredproviderfound", "resourceprovidererror"}
    conf = "high" if (reported | observed) & codes else "medium"
    acts = [
        build_action("register-provider", ctx.profile, namespace=ns, doc_refs=[_ts_ref(ctx, "ResourceProviderError")])
        for ns in missing
    ]
    return [
        (
            Hypothesis(
                "provider-unregistered",
                f"Resource provider(s) not registered: {missing}",
                LIKELY,
                conf,
                [f"provider show reported state != Registered for {missing}"],
                supporting_checks=["providers.registration"],
                doc_refs=[_ts_ref(ctx, "ResourceProviderError")],
                remediation_ids=[a.id for a in acts],
                component="Subscription",
            ),
            acts,
        )
    ]


# --------------------------------------------------------------------------- ARM deployment


@rule(20)
def documented_deployment_errors(ctx):
    reported, observed = _codes(ctx)
    handled = QUOTA_CODES | MODEL_CODES | {"resourcegroupnotfound", "deploymentfailed"}
    out = []
    if ctx.index is None:
        return out
    for code in sorted((observed | reported) - handled):
        hits = ctx.index.search(code, k=1, error_codes=[code], doc_types=["troubleshooting-row"])
        hits = [h for h in hits if any(code in c.lower() for c in h.error_codes)]
        if not hits:
            continue
        row = hits[0]
        live = code in observed
        detail = next((e for e in ctx.facts.get("deployment_errors", []) if (e.get("code") or "").lower() == code), {})
        rationale = [f"error code '{row.title}' matches documented entry {row.ref} ({row.section})"]
        if live:
            rationale.append(
                f"observed live in failed operation on {detail.get('resource_type')}/"
                f"{detail.get('resource_name')}: {detail.get('message', '')[:200]}"
            )
        a = build_action(
            "follow-documented-steps", ctx.profile, code=row.title, commands=row.commands, doc_refs=[row.ref]
        )
        out.append(
            (
                Hypothesis(
                    f"documented-{code}",
                    f"Documented deployment error: {row.title}",
                    CONFIRMED if live else LIKELY,
                    "high" if live else "medium",
                    rationale,
                    supporting_checks=["deploy.operations"] if live else [],
                    missing_evidence=[] if live else ["failed deployment operation details (needs RG / live access)"],
                    doc_refs=[row.ref],
                    remediation_ids=[a.id],
                    component="ARM deployment",
                ),
                [a],
            )
        )
    return out


@rule(21)
def deployment_running(ctx):
    stuck = ctx.facts.get("stuck_deployments") or []
    running = ctx.facts.get("running_deployments") or []
    if not running:
        return []
    return [
        (
            Hypothesis(
                "deployment-in-progress",
                "A deployment is still running" + (" (>1h — likely stuck)" if stuck else ""),
                LIKELY,
                "high" if stuck else "medium",
                [f"running deployments: {[r['name'] for r in running]}"],
                supporting_checks=["deploy.operations"],
                doc_refs=[_ts_ref(ctx, "DeploymentActive")],
                component="ARM deployment",
            ),
            [],
        )
    ]


@rule(22)
def partial_deployment(ctx):
    missing = ctx.facts.get("missing_resource_types") or []
    bad = ctx.facts.get("unhealthy_resources") or []
    if not missing and not bad:
        return []
    acts = [build_action("azd-provision", ctx.profile, doc_refs=[ctx.ref("deployment", "Recover from Failed")])]
    empty = ctx.facts.get("resources") == [] and ctx.facts.get("deployments") == [] and not bad
    if empty:
        # Nothing exists yet: provisioning has not started creating resources (or has only just started).
        return [
            (
                Hypothesis(
                    "resource-group-empty",
                    "The resource group exists but nothing has been deployed into it yet",
                    LIKELY,
                    "medium",
                    [
                        "0 resources and no ARM deployment records in the resource group (live)",
                        "normal in the first minutes of 'azd up'; otherwise azd stopped before creating anything",
                    ],
                    supporting_checks=["resources.state", "deploy.operations"],
                    contradicting=["azd up may simply still be starting — wait a few minutes and re-run this check"],
                    missing_evidence=["the azd up output (use: run -- azd up, or diagnose --from-clipboard)"],
                    remediation_ids=[a.id for a in acts],
                    doc_refs=[ctx.ref("deployment", "Recover from Failed")],
                    component="Infrastructure",
                ),
                acts,
            )
        ]
    rationale = ([f"expected resource types absent: {missing}"] if missing else []) + (
        [f"resources not Succeeded: {[(b['name'], b['state']) for b in bad]}"] if bad else []
    )
    return [
        (
            Hypothesis(
                "partial-deployment",
                "Infrastructure deployment is partial or has failed resources",
                LIKELY,
                "high",
                rationale,
                supporting_checks=["resources.state"],
                remediation_ids=[a.id for a in acts],
                doc_refs=[ctx.ref("deployment", "Recover from Failed")],
                component="Infrastructure",
            ),
            acts,
        )
    ]


# --------------------------------------------------------------------------- app / post-provision


@rule(30)
def post_provision_incomplete(ctx):
    out = []
    ph = ctx.facts.get("placeholder_image_apps") or []
    if ph:
        a = build_action("build-push-images", ctx.profile)
        out.append(
            (
                Hypothesis(
                    "images-not-built",
                    "App Services still run the placeholder image — build_push_images not run " "or failed",
                    CONFIRMED,
                    "high",
                    [
                        f"linuxFxVersion on {ph} contains the template's placeholder image "
                        f"({ctx.profile.get('placeholder_image_ref')})",
                        "DeploymentGuide 5.3: initial deployment configures a placeholder container",
                    ],
                    supporting_checks=["webapp.state"],
                    doc_refs=[a.doc_refs[0]] if a.doc_refs else [],
                    source_refs=[ctx.profile.get("placeholder_image_ref", "")],
                    remediation_ids=[a.id],
                    component="App Service container image",
                ),
                [a],
            )
        )
    pp = ctx.facts.get("post_provision_settings_missing") or {}
    if pp:
        a = build_action("run-post-provision", ctx.profile)
        app = next((x for x in ctx.profile.get("apps", []) if x.get("post_provision_settings")), {})
        out.append(
            (
                Hypothesis(
                    "agents-not-created",
                    "Foundry agent post-provision step not completed (agent settings empty)",
                    CONFIRMED,
                    "high",
                    [
                        f"App settings {pp} are empty/placeholder on the API app",
                        f"these are written by {app.get('post_provision_ref')} after agent creation",
                    ],
                    supporting_checks=["webapp.state"],
                    source_refs=[app.get("post_provision_ref", "")],
                    doc_refs=[a.doc_refs[0]] if a.doc_refs else [],
                    remediation_ids=[a.id],
                    component="Azure AI Foundry agents / API app settings",
                ),
                [a],
            )
        )
    return out


@rule(31)
def app_settings_missing(ctx):
    miss = ctx.facts.get("missing_app_settings") or {}
    if not miss:
        return []
    acts = [build_action("azd-provision", ctx.profile)]
    app = next((x for x in ctx.profile.get("apps", []) if x.get("required_settings_ref")), {})
    return [
        (
            Hypothesis(
                "app-settings-missing",
                "Required App Service settings are missing or empty",
                LIKELY,
                "high",
                [
                    f"{miss} (template declares them in {app.get('required_settings_ref')})",
                    "settings may have been edited manually or the app redeployed outside azd",
                ],
                supporting_checks=["webapp.state"],
                source_refs=[app.get("required_settings_ref", "")],
                remediation_ids=[a.id for a in acts],
                component="App Service configuration",
            ),
            acts,
        )
    ]


@rule(32)
def rbac_missing(ctx):
    miss = ctx.facts.get("rbac_missing") or []
    if not miss:
        return []
    auth_symptom = "authentication" in ctx.issue.stages or any(s in (401, 403) for s in ctx.issue.http_statuses)
    acts = []
    for m in miss:
        pid = (ctx.facts.get("app_principals") or {}).get(m["app"])
        role_def = next(
            (r for a in ctx.profile.get("apps", []) for r in a.get("expected_roles", []) if r["role"] == m["role"]),
            None,
        )
        if pid and role_def and m.get("scope"):
            acts.append(
                build_action(
                    "assign-app-role",
                    ctx.profile,
                    app=m["app"],
                    principal_id=pid,
                    role=m["role"],
                    role_id=role_def["role_id"],
                    scope=m["scope"],
                    source=m.get("source"),
                )
            )
    return [
        (
            Hypothesis(
                "rbac-missing",
                "App managed identity lacks role assignments declared by the template",
                LIKELY,
                "high" if auth_symptom else "medium",
                [f"missing: {[(m['app'], m['role']) for m in miss]}"]
                + (["reported symptom is an auth failure (401/403/unauthorized)"] if auth_symptom else []),
                supporting_checks=["rbac.app_identity"],
                source_refs=[m.get("source", "") for m in miss],
                doc_refs=[ctx.ts_ref("PrincipalNotFound")],
                remediation_ids=[a.id for a in acts],
                component="Managed identity / RBAC",
            ),
            acts,
        )
    ]


@rule(33)
def deployer_data_role(ctx):
    if ctx.facts.get("deployer_cosmos_role") is not False:
        return []
    said_unauth = "unauthori" in ctx.issue.raw_text.lower() or "403" in ctx.issue.raw_text
    ref = ctx.doc_ref("infra/scripts/post-provision/data_scripts/03_write_products_to_cosmos.py", "Cosmos DB Built-in")
    run_ref = ctx.doc_ref(
        "infra/scripts/post-provision/data_scripts/run_upload_data_scripts.ps1", "Built-in Data Contributor role"
    )
    a = build_action("run-post-provision", ctx.profile)
    return [
        (
            Hypothesis(
                "deployer-cosmos-role-missing",
                "Your signed-in identity lacks Cosmos DB data-plane access needed by " "the post-provision data script",
                CONFIRMED if said_unauth else LIKELY,
                "high" if said_unauth else "medium",
                [
                    "cosmosdb sql role assignment list shows no Built-in Data Contributor (…0002) for the signed-in "
                    "user",
                    f"the data script raises 'Unauthorized' in this case ({ref})",
                    f"the wrapper script assigns this role automatically ({run_ref}); failure usually means missing "
                    "User Access Administrator / RBAC Admin rights or the role has not propagated yet (wait ~5 min)",
                ]
                + (["reported symptom contains Unauthorized/403"] if said_unauth else []),
                supporting_checks=["rbac.deployer_data"],
                source_refs=[ref, run_ref],
                remediation_ids=[a.id],
                component="Post-provision data script / Cosmos DB RBAC",
            ),
            [a],
        )
    ]


@rule(34)
def public_access_disabled(ctx):
    dis = ctx.facts.get("public_access_disabled") or []
    if not dis or ctx.facts.get("has_private_endpoints"):
        return []
    a = build_action(
        "enable-public-network-access", ctx.profile, doc_refs=[ctx.ref("deployment", "public network access")]
    )
    return [
        (
            Hypothesis(
                "public-access-disabled",
                "Public network access disabled on dependencies without private "
                "endpoints (often tenant security policy)",
                LIKELY,
                "high",
                [
                    f"publicNetworkAccess=Disabled / ACL Deny on {dis}",
                    "no private endpoints in the resource group",
                    "DeploymentGuide notes tenant restrictions may block public network access",
                ],
                supporting_checks=["network.public_access"],
                doc_refs=a.doc_refs,
                remediation_ids=[a.id],
                component="Networking",
            ),
            [a],
        )
    ]


@rule(35)
def throttling(ctx):
    if "throttling" not in ctx.issue.stages:
        return []
    deps = ctx.facts.get("ai_deployments")
    reqs = ctx.facts.get("model_requirements", [])
    chat = next((r for r in reqs if r["role"] == "chat"), None)
    rationale, missing, acts = ["reported 429/throttling symptom"], [], []
    conf = "low"
    if deps:
        d = next((x for x in deps if chat and (x["model"] == chat["model"] or x["name"] == chat["model"])), None)
        if d:
            rationale.append(
                f"deployment '{d['name']}' has {d['capacity']}K TPM ({d['sku']}) — requests beyond this are "
                "throttled with 429"
            )
            conf = "medium"
    else:
        missing.append("live model deployment capacity (needs RG and az access)")
    table = ctx.facts.get("quota_table") or {}
    region = ctx.facts.get("quota_target_region")
    if chat and region and region in table:
        row = next((m for m in table[region].get("models", []) if m["model"] == chat["model"]), None)
        if row and row["available"] is not None:
            rationale.append(f"remaining {chat['model']} quota in {region}: {row['available']}K TPM")
            if row["available"] > 0 and chat.get("capacity_env"):
                target = (chat["capacity"] or 0) + row["available"]
                acts.append(
                    build_action(
                        "set-model-capacity",
                        ctx.profile,
                        model=chat["model"],
                        env=chat["capacity_env"],
                        capacity=target,
                    )
                )
                acts.append(build_action("azd-provision", ctx.profile))
    missing.append("Azure Monitor metrics for the AI account (ProcessedPromptTokens / 429 count) to confirm peak load")
    acts.append(build_action("request-quota-increase", ctx.profile, doc_refs=[ctx.ref("quota", "Quota")]))
    return [
        (
            Hypothesis(
                "model-throttling",
                "Model deployment capacity (TPM) is too low for the request rate",
                LIKELY if conf != "low" else POSSIBLE,
                conf,
                rationale,
                supporting_checks=["ai.deployments", "quota.models"],
                missing_evidence=missing,
                doc_refs=[ctx.ref("quota", "Quota")],
                remediation_ids=[a.id for a in acts if a],
                component="Azure OpenAI deployment",
            ),
            [a for a in acts if a],
        )
    ]


@rule(36)
def local_vs_cloud(ctx):
    gaps = ctx.facts.get("local_vs_deployed_gaps") or []
    if not gaps:
        return []
    return [
        (
            Hypothesis(
                "local-vs-cloud-config",
                "Settings that exist locally are missing in the deployed App Service",
                LIKELY,
                "high",
                [f"{sorted({g['setting'] for g in gaps})} set in local .env but not in Azure"],
                supporting_checks=["config.compare", "webapp.state"],
                doc_refs=[ctx.ref("local_dev", "Configure Environment Variables")],
                component="App Service configuration",
            ),
            [],
        )
    ]


@rule(40)
def config_drift(ctx):
    findings = [f for f in ctx.facts.get("drift_findings", []) if f["severity"] in ("high", "medium")]
    if not findings:
        return []
    quota_related = any("capacity" in f["id"] or "sku" in f["id"] for f in findings)
    relevant = quota_related and (
        "quota" in ctx.issue.stages or "deployment" in ctx.issue.stages or "pre-deployment" in ctx.issue.stages
    )
    return [
        (
            Hypothesis(
                "config-drift",
                "Configuration is inconsistent across IaC, docs, scripts and azd env",
                LIKELY if relevant else POSSIBLE,
                "medium" if relevant else "low",
                [f["message"] for f in findings],
                supporting_checks=["repo.config_drift"],
                source_refs=[r for f in findings for r in f.get("refs", [])],
                component="Configuration",
            ),
            [],
        )
    ]


@rule(41)
def recent_config_change(ctx):
    files = ctx.facts.get("changed_config_files") or []
    if not files or not (
        {"configuration", "runtime", "startup", "local-vs-cloud", "deployment"} & set(ctx.issue.stages)
    ):
        return []
    said_changed = any(w in ctx.issue.raw_text.lower() for w in ("changed", "modified", "updated", "edited"))
    return [
        (
            Hypothesis(
                "recent-config-change",
                "A recent uncommitted configuration change may have caused the regression",
                LIKELY if said_changed else POSSIBLE,
                "medium" if said_changed else "low",
                [f"{f['path']} ({f['status']}): " + "; ".join(f.get("diff", [])[:4]) for f in files[:5]],
                supporting_checks=["repo.git_changes"],
                missing_evidence=[
                    "whether this change was deployed (compare with live app settings / deployment " "timestamp)"
                ],
                component="Configuration",
            ),
            [],
        )
    ]


@rule(50)
def unhealthy_without_cause(ctx):
    failing = ctx.facts.get("health_failing") or []
    if not failing:
        return []
    rg = ctx.resource_group
    acts = []
    for app in ctx.profile.get("apps", []):
        if app["id"] in failing:
            name = ctx.env.get(app.get("name_env")) or app["id"]
            acts.append(build_action("collect-app-logs", ctx.profile, resource_group=rg, app=name))
            acts.append(build_action("restart-webapp", ctx.profile, resource_group=rg, app=name))
    probes = {p["app"]: p for p in ctx.facts.get("health_results", [])}
    return [
        (
            Hypothesis(
                "app-unhealthy",
                f"Health endpoint failing for {failing}",
                LIKELY,
                "medium",
                [
                    f"{a}: HTTP {probes.get(a, {}).get('status')} {probes.get(a, {}).get('error', '')}".strip()
                    for a in failing
                ],
                supporting_checks=["webapp.health"],
                missing_evidence=[
                    "application/container logs (az webapp log tail) or Application Insights "
                    "exceptions to see the actual stack trace"
                ],
                remediation_ids=[a.id for a in acts],
                component="Application runtime",
            ),
            acts,
        )
    ]


# --------------------------------------------------------------------------- driver

_STATUS_RANK = {CONFIRMED: 0, LIKELY: 1, POSSIBLE: 2}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def analyse(ctx: DiagContext) -> Tuple[List[Hypothesis], Dict[str, Action]]:
    hyps: List[Tuple[int, Hypothesis]] = []
    actions: Dict[str, Action] = {}
    for priority, fn in sorted(RULES, key=lambda x: x[0]):
        for h, acts in fn(ctx) or []:
            keys = []
            for a in acts:
                existing = next(
                    (k for k, v in actions.items() if k.split("#")[0] == a.id and v.params == a.params), None
                )
                if existing:
                    keys.append(existing)
                    continue
                n = sum(1 for k in actions if k.split("#")[0] == a.id)
                a.id = a.id if n == 0 else f"{a.id}#{n + 1}"
                actions[a.id] = a
                keys.append(a.id)
            h.remediation_ids = keys
            hyps.append((priority, h))
    hyps.sort(key=lambda ph: (_STATUS_RANK.get(ph[1].status, 3), _CONF_RANK.get(ph[1].confidence, 3), ph[0]))
    return [h for _, h in hyps], actions


def missing_evidence(ctx: DiagContext, hyps: List[Hypothesis]) -> List[str]:
    out: List[str] = []
    if ctx.runner.mode == "offline":
        out.append("Live Azure state (offline mode) — re-run without --offline after 'az login'")
    if not ctx.resource_group:
        out.append("Resource group name (no azd env found; pass --resource-group)")
    if not ctx.issue.error_codes and {"deployment", "quota"} & set(ctx.issue.stages):
        out.append("Exact error code and message from the azd/ARM output (e.g. 'Code: InsufficientQuota')")
    if not ctx.issue.timestamps and {"runtime", "deployment"} & set(ctx.issue.stages):
        out.append("Approximate time of failure (to correlate with deployment/activity logs)")
    if {"runtime", "startup"} & set(ctx.issue.stages) and not any(h.id == "app-unhealthy" for h in hyps):
        out.append("Application logs (az webapp log tail / Application Insights) for the failing request")
    for h in hyps:
        for m in h.missing_evidence:
            if m not in out:
                out.append(m)
    return out
