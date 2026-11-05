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

import re
from typing import Any, Callable, Dict, List, Tuple

from .actions import build_action
from .diagnostics.app_checks import _app_name
from .diagnostics.base import DiagContext
from .models import CONFIRMED, FAIL, LIKELY, POSSIBLE, Action, Hypothesis
from .repo import parse_bicep_params, parse_parameters_env_map, template_sets_tag

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
    reported = {c.lower() for c in ctx.issue.error_codes}
    if ctx.facts.get("rg_exists") is not False:
        if "resourcegroupnotfound" not in reported or ctx.facts.get("rg_exists") is True:
            return []
        # Reported by the user but not verified live (offline, or no resource group known): cite the guide only.
        ref = _ts_ref(ctx, "ResourceGroupNotFound")
        a = build_action("new-azd-env", ctx.profile, doc_refs=[ref])
        return [
            (
                Hypothesis(
                    "resource-group-missing",
                    "Resource group does not exist (deleted, or a stale azd environment / old .env file)",
                    LIKELY,
                    "medium",
                    [
                        "the error code ResourceGroupNotFound was reported",
                        f"troubleshooting guide: caused by a deleted resource group or an old .env file ({ref})",
                    ],
                    missing_evidence=["live check: 'az group show' for the resource group (needs Azure access)"],
                    doc_refs=[ref, ctx.ref("deployment", "Recover from Failed")],
                    remediation_ids=[a.id],
                    component="Resource group / azd environment",
                ),
                [a],
            )
        ]
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


_QUOTA_NUMBERS = re.compile(
    r"requires?\s+(\d+)\D{0,80}?(?:only\s+(\d+)|available capacity\s+(\d+))", re.IGNORECASE | re.DOTALL
)


def _arm_quota_failures(ctx) -> List[Dict[str, Any]]:
    """Quota failures from live ARM deployment operations, with the requested/available numbers Azure reported."""
    out = []
    for e in ctx.facts.get("deployment_errors", []):
        if (e.get("code") or "").lower() not in QUOTA_CODES:
            continue
        m = _QUOTA_NUMBERS.search(e.get("message") or "")
        need, avail = (int(m.group(1)), int(m.group(2) or m.group(3))) if m else (None, None)
        out.append({**e, "need": need, "avail": avail})
    return out


@rule(10)
def quota_insufficient(ctx):
    raw_lower = ctx.issue.raw_text.lower()
    if re.search(r"\b(vms?|vcpus?|cores?)\b", raw_lower) and not re.search(
        r"tokens|tpm|openai|gpt|embedding|foundry|capacity", raw_lower
    ):
        return []  # VM/core quota (documented separately), not model quota
    reported, observed = _codes(ctx)
    arm = _arm_quota_failures(ctx)
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
    arm_confirmed = False
    chat_cap = next((r["capacity"] for r in ctx.facts.get("model_requirements", []) if r["role"] == "chat"), None)
    for f in arm:
        if f["need"] is None:
            continue
        arm_confirmed = arm_confirmed or f["need"] > f["avail"]
        where = f"{f.get('resource_type')} {f.get('resource_name')}, {f.get('timestamp')}"
        rationale.append(
            f"deployment '{f.get('deployment')}' ({where}) was rejected: "
            f"requested {f['need']}, available {f['avail']} (from the ARM error message)"
        )
        if isinstance(chat_cap, int) and f["need"] != chat_cap:
            rationale.append(
                f"that request ({f['need']}) differs from the configured capacity ({chat_cap}K): it came from "
                "an override, another template or a manual deployment — the configured value is not what failed"
            )
        rationale.append(f"to fit, request at most {f['avail']} (the free quota when it failed)")
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
    if ctx.facts.get("quota_ok") is True and not arm_confirmed:
        contra.append(
            f"live quota in {ctx.facts.get('quota_target_region')} is currently sufficient for all models — "
            "quota may have been freed since the failure or a different region/subscription was used"
        )
    target_rows = (ctx.facts.get("quota_table") or {}).get(target or "", {}).get("models", [])
    for m in ctx.issue.models:
        row = next((x for x in target_rows if x["model"] == m), None)
        if row and row["ok"] and (code_reported or code_observed) and not arm_confirmed:
            contra.append(
                f"{m} is named in the reported error, but live {row['sku']} quota in {target} is now "
                f"{row['available']}K available (>= {row['capacity']} required) — usage changed since the failure "
                "or the failing request asked for more capacity than currently configured"
            )
    if "quota.models" not in ctx.results or ctx.results["quota.models"].status in ("skip", "error"):
        missing.append("live quota usage for the target region (az cognitiveservices usage list)")
    if not ctx.facts.get("quota_target_region"):
        missing.append("which AI region was used (AZURE_ENV_AI_SERVICE_LOCATION or --region)")
    if (shortfalls and (code_observed or code_reported)) or arm_confirmed:
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


_ALLOWED_VALUE = [
    re.compile(r"provided value '([^']*)' for the template parameter '(\w+)'", re.I),
    re.compile(r"template parameter '(\w+)'[^\n]{0,200}?'([\w.-]+)' is not in the allowed", re.I),
]


@rule(12)
def parameter_not_allowed(ctx):
    """'The provided value X for the template parameter P is not valid': compare with the Bicep @allowed list."""
    pname = value = None
    for i, rx in enumerate(_ALLOWED_VALUE):
        m = rx.search(ctx.issue.raw_text)
        if m:
            value, pname = (m.group(1), m.group(2)) if i == 0 else (m.group(2), m.group(1))
            break
    if not pname:
        return []
    iac = ctx.profile.get("iac", {})
    params = parse_bicep_params(ctx.repo / iac.get("main", "infra/main.bicep"))
    p = params.get(pname)
    if p is None or not p.allowed or value in p.allowed:
        return []
    env_map = parse_parameters_env_map(ctx.repo / iac.get("parameters", "infra/main.parameters.json"))
    envvar = (env_map.get(pname) or {}).get("env")
    ref = f"{iac.get('main', 'infra/main.bicep')}:{p.line}"
    cmd = (
        f"azd env set {envvar} <one of: {', '.join(p.allowed)}>"
        if envvar
        else f"change {pname} to one of: {', '.join(p.allowed)}"
    )
    a = build_action(
        "follow-documented-steps",
        ctx.profile,
        code="InvalidTemplate",
        commands=[cmd],
        doc_refs=[ref],
    )
    return [
        (
            Hypothesis(
                "parameter-not-allowed",
                f"'{value}' is not an allowed value for template parameter '{pname}'",
                CONFIRMED,
                "high",
                [
                    f"the error names parameter '{pname}' with value '{value}'",
                    f"{ref} restricts it with @allowed to: {', '.join(p.allowed)}",
                    f"'{value}' is not in that list"
                    + (f"; the value comes from the azd environment variable {envvar}" if envvar else ""),
                ],
                doc_refs=[ref, ctx.ref("troubleshooting", "InvalidResourceLocation")],
                source_refs=[ref],
                remediation_ids=[a.id],
                component="Template parameters / azd environment",
            ),
            [a],
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


def _words(text: str) -> set:
    return set(re.findall(r"[a-z]{4,}", text.lower()))


def _best_row(ctx: DiagContext, code: str):
    """The documented row for an error code. Generic codes (BadRequest, Unauthorized) have many rows: prefer the one
    whose title/section shares the most words with what the user pasted."""
    hits = ctx.index.search(code, k=10, error_codes=[code], doc_types=["troubleshooting-row"])
    hits = [h for h in hits if any(code in c.lower() for c in h.error_codes)]
    if len(hits) <= 1:
        return hits[0] if hits else None
    pasted = _words(ctx.issue.raw_text)
    return max(hits, key=lambda h: (len(pasted & _words(h.title + " " + h.section)), h.score))


def _phrase_rows(ctx: DiagContext, skip: set):
    """Rows whose long, plain-language title appears in the pasted text (no machine error code needed)."""
    text = _words(ctx.issue.raw_text)
    out = []
    for p in ctx.index.passages:
        if p.doc_type != "troubleshooting-row" or p.category == "summary" or p.ref in skip:
            continue
        title = _words(p.title)
        if len(title) >= 5 and len(title & text) >= max(5, int(0.85 * len(title))):
            out.append(p)
    return out


@rule(20)
def documented_deployment_errors(ctx):
    reported, observed = _codes(ctx)
    handled = QUOTA_CODES | MODEL_CODES | {"resourcegroupnotfound", "deploymentfailed"}
    out = []
    if ctx.index is None:
        return out
    rows = []
    for code in sorted((observed | reported) - handled):
        row = _best_row(ctx, code)
        if row is not None and all(row.ref != r[1].ref for r in rows):
            rows.append((code, row))
    seen = {r.ref for _, r in rows}
    rows += [(p.title.lower(), p) for p in _phrase_rows(ctx, seen)]
    for code, row in rows:
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
    if ctx.facts.get("failed_while_running"):
        return []  # an operation already failed: the failure rules explain it, "not a failure yet" would be wrong
    stuck = ctx.facts.get("stuck_deployments") or []
    running = ctx.facts.get("running_deployments") or []
    recent = ctx.facts.get("deployment_recent_minutes")
    just_active = recent is not None and recent < 10
    if not running and not just_active:
        return []
    n = len(ctx.facts.get("resources") or [])
    evidence = (
        f"{len(running)} ARM deployment(s) still running: {[r['name'] for r in running]}"
        if running
        else f"latest ARM deployment activity was {recent:.0f} min ago (between phases, nothing 'Running' right now)"
    )
    return [
        (
            Hypothesis(
                "deployment-in-progress",
                "Deployment is still in progress"
                + (" (running > 1h — likely stuck)" if stuck else " — not a failure yet"),
                LIKELY,
                "high" if stuck else "medium",
                [evidence, f"{n} resources created so far; the rest (e.g. web apps) are created later in the same run"],
                supporting_checks=["deploy.operations"],
                contradicting=[] if stuck else ["no failed operations recorded; wait for azd up to finish"],
                missing_evidence=["azd up final output (if it later fails, use: diagnose --from-clipboard)"],
                doc_refs=[_ts_ref(ctx, "DeploymentActive")],
                component="ARM deployment",
            ),
            [],
        )
    ]


@rule(22)
def partial_deployment(ctx):
    recent = ctx.facts.get("deployment_recent_minutes")
    if ctx.facts.get("running_deployments") or (recent is not None and recent < 10):
        return []  # still active: missing resources are expected, not a partial failure
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


@rule(28)
def fabric_capacity_inactive(ctx):
    rows = ctx.facts.get("fabric_capacities") or []
    paused = [r for r in rows if str(r.get("state") or "").lower() in ("paused", "pausing")]
    if not paused:
        return []
    acts = [
        build_action("resume-fabric-capacity", ctx.profile, name=r["name"], resource_group=r["resource_group"])
        for r in paused
    ]
    return [
        (
            Hypothesis(
                "fabric-capacity-paused",
                f"Fabric capacity is paused: {', '.join(r['name'] for r in paused)}",
                CONFIRMED,
                "high",
                [
                    f"'az resource show' reports state {paused[0].get('state')} for {paused[0]['name']} "
                    f"({paused[0].get('sku') or 'unknown SKU'}) (live)",
                    "a paused capacity makes its Fabric workspaces and items unavailable (general Fabric behaviour); "
                    "anything that reads them, and post-provision steps that create items, fail until it is resumed",
                ],
                supporting_checks=["azure.fabric"],
                missing_evidence=["who paused it and when (activity log: Microsoft.Fabric/capacities/suspend/action)"],
                remediation_ids=[a.id for a in acts],
                component="Microsoft Fabric capacity",
            ),
            acts,
        )
    ]


@rule(29)
def app_stopped(ctx):
    stopped = ctx.facts.get("stopped_apps") or []
    if not stopped:
        return []
    rg = ctx.resource_group
    acts = [build_action("start-webapp", ctx.profile, resource_group=rg, app=name) for name in stopped]
    probes = ctx.facts.get("health_results") or []
    symptom = [f"{p['app']}: HTTP {p.get('status')}" for p in probes if p["app"] in ("chat-api", "chat-web")]
    return [
        (
            Hypothesis(
                "app-stopped",
                f"App Service is stopped: {', '.join(stopped)}",
                CONFIRMED,
                "high",
                [f"'az webapp show' reports state != Running for {stopped} (live)"]
                + ([f"health probes: {', '.join(symptom)}"] if symptom else []),
                supporting_checks=["webapp.state"],
                missing_evidence=["who stopped it and why (activity log: Microsoft.Web/sites/stop/action)"],
                doc_refs=[ctx.ref("deployment", "Verify Deployment")],
                remediation_ids=[a.id for a in acts],
                component="App Service",
            ),
            acts,
        )
    ]


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


def _template_sets_tag(ctx, name: str) -> bool:
    """True when infra/main.bicep already defaults `param tags` to something containing this tag."""
    return template_sets_tag(ctx.repo, "infra/main.bicep", name)


@rule(34)
def public_access_disabled(ctx):
    dis = ctx.facts.get("public_access_disabled") or []
    if not dis or ctx.facts.get("has_private_endpoints"):
        return []
    enforcers = ctx.facts.get("policy_enforcers") or {}
    env_map = (ctx.facts.get("param_env_map") or {}).get("deploymentFlavor") or {}
    flavor = ctx.env.get(env_map.get("env")) or env_map.get("default") or "unknown"
    waf = flavor == "avm-waf"
    policies = [(res, p) for res, plist in enforcers.items() for p in plist if p.get("public_access", True)]
    guide = ctx.ref("deployment", "Choose Deployment Type")
    base = [f"publicNetworkAccess=Disabled / ACL Deny on {dis}; no private endpoints in the resource group"]
    contra: List[str] = []
    rationale: List[str] = []
    for res, p in policies:
        rationale.append(
            f"Azure Policy '{p['definition']}' ({p['action']}, assignment '{p['assignment']}', set at "
            f"{p['scope_level']} level) is enforced on {res} — live 'az policy state list'"
        )
    if not waf:
        rationale.append(
            f"deployment flavor is '{flavor}' (non-WAF): cosmos-db-nosql.bicep sets no network restriction, so "
            "Disabled did not come from this template"
        )
    if any(p["action"] == "modify" for _, p in policies):
        rationale.append(
            "a 'modify' policy rewrites the setting every time it changes — re-enabling public access (including the "
            "post-provision script's temporary firewall rule) is reverted by the policy"
        )
    rationale += base
    if not policies:
        contra.append(
            "no enforcing policy was found (needs Reader on policy states) — a manual change is also possible"
        )
    # Does the policy skip tagged resources, and are we missing that tag? (read from the policy's own rule)
    exempt = next((p["exempt_tag"] for _, p in policies if p.get("exempt_tag")), None)
    tag_missing = False
    if exempt and exempt.get("name"):
        rg_tags = ctx.facts.get("rg_tags") or {}
        res_tags = ctx.facts.get("resource_tags") or {}
        want = (exempt["name"], exempt.get("value", ""))
        has = lambda tags: str(tags.get(want[0], "")).lower() == want[1].lower()  # noqa: E731
        tag_missing = not has(rg_tags) and not any(has(t) for t in res_tags.values())
        tag_txt = f"{want[0]}={want[1]}" if want[1] else want[0]
        if tag_missing:
            rationale.insert(
                0,
                f"the policy skips {exempt['scope']} when it is tagged {tag_txt} ({exempt['source']}); neither the "
                f"resource group (tags: {sorted(rg_tags)}) nor {sorted(res_tags)} carries it, so the policy applied",
            )
    acts: List[Action] = []
    if tag_missing:
        rg = ctx.resource_group
        sub = ctx.facts.get("subscription_id")
        ids = ctx.facts.get("resource_ids", {})
        bicep_ref = ctx.doc_ref("infra/main.bicep", r"^param tags ")
        acts.append(
            build_action(
                "apply-exemption-tag",
                ctx.profile,
                tag=exempt["name"],
                value=exempt.get("value", ""),
                resource_group=rg,
                rg_id=ctx.facts.get("rg_id") or (f"/subscriptions/{sub}/resourceGroups/{rg}" if sub else None),
                resources={res: ids[res] for res, _ in policies if res in ids},
                bicep_ref=bicep_ref,
            )
        )
        if bicep_ref and not _template_sets_tag(ctx, exempt["name"]):
            acts.append(
                build_action(
                    "add-tag-to-template",
                    ctx.profile,
                    tag=exempt["name"],
                    value=exempt.get("value", ""),
                    bicep_ref=bicep_ref,
                )
            )
    if policies:
        acts.append(build_action("request-policy-exemption", ctx.profile, policies=[p for _, p in policies]))
    else:
        acts.append(
            build_action(
                "enable-public-network-access", ctx.profile, doc_refs=[ctx.ref("deployment", "public network")]
            )
        )
    acts.append(build_action("use-waf-deployment", ctx.profile, doc_refs=[guide]))
    acts = [a for a in acts if a]
    if tag_missing:
        title = f"Tenant policy turned Cosmos public access OFF because the '{exempt['name']}' exemption tag is missing"
    elif policies and not waf:
        title = "Tenant policy forces public network access OFF on a non-WAF deployment"
    else:
        title = "Public network access is disabled on dependencies and there are no private endpoints"
    return [
        (
            Hypothesis(
                "public-access-disabled",
                title,
                CONFIRMED if policies else LIKELY,
                "high",
                rationale,
                supporting_checks=["network.public_access"],
                contradicting=contra,
                doc_refs=[guide, ctx.ref("deployment", "tenants may have additional security")],
                source_refs=["infra/bicep/modules/data/cosmos-db-nosql.bicep"],
                remediation_ids=[a.id for a in acts],
                component="Networking / Azure Policy",
            ),
            acts,
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


# --------------------------------------------------------------------------- what the apps logged / a real chat test

# A logged or observed error category already explained by one of these findings is added to it as evidence
# instead of being reported a second time.
_COVERED_BY = {
    "cosmos-firewall": {"public-access-disabled"},
    "rbac-denied": {"rbac-missing", "deployer-cosmos-role-missing"},
    "throttled": {"model-throttling", "quota-insufficient"},
    "config-missing": {"app-settings-missing", "agents-not-created"},
    "model-not-found": {"model-region-unsupported"},
}
_CERTAIN_FROM_STATUS = {"cosmos-firewall", "auth-token", "config-missing"}  # the text or HTTP status names the cause


def _category_hypothesis(ctx, cat: str, lines: List[str], source_check: str, from_chat: bool, extra_missing=None):
    hyp_ids = ctx.facts.get("hyp_ids", set())
    covered = (_COVERED_BY.get(cat, set()) | {f"observed-{cat}"}) & hyp_ids
    if covered:
        for hid in sorted(covered):
            ctx.facts.setdefault("hyp_notes", {}).setdefault(hid, []).extend(lines)
            ctx.facts.setdefault("hyp_support", {}).setdefault(hid, set()).add(source_check)
        return []
    info = {
        "cosmos-firewall": (
            "Cosmos DB is refusing the app: its firewall blocks the request",
            ctx.ref("deployment", "blocking public network access"),
            "Networking / Cosmos DB",
        ),
        "model-not-found": (
            "The app asks for a model deployment that does not exist in the AI account",
            ctx.ts_ref("DeploymentNotFound"),
            "AI model deployments",
        ),
        "throttled": ("Model requests are being throttled (HTTP 429)", ctx.ref("quota", "Quota"), "Model capacity"),
        "rbac-denied": (
            "An identity is being denied access (403 / AuthorizationFailed)",
            ctx.ref("deployment", "role"),
            "RBAC",
        ),
        "auth-token": (
            "Sign-in tokens are being rejected (HTTP 401)",
            ctx.ref("auth", "user_impersonation"),
            "Authentication",
        ),
        "config-missing": (
            "The app reports missing configuration",
            ctx.ref("deployment", "Build Container Images"),
            "App configuration",
        ),
        "timeout-network": (
            "Calls from the app time out or cannot connect",
            ctx.ref("deployment", "network"),
            "Networking",
        ),
        "other": ("The apps are logging errors that match no documented cause", "", "Application runtime"),
    }[cat]
    certain = cat in _CERTAIN_FROM_STATUS or (cat == "cosmos-firewall")
    if cat == "other" or cat == "timeout-network":
        status, conf = POSSIBLE, "low"
    elif certain:
        status, conf = CONFIRMED, "high"
    else:
        status, conf = LIKELY, "medium"
    rg = ctx.resource_group
    api = next((_app_name(ctx, a) for a in ctx.profile.get("apps", []) if a.get("role") == "backend"), "")
    acts = [build_action("collect-app-logs", ctx.profile, resource_group=rg, app=api)] if api and rg else []
    docs = [d for d in [info[1]] if d]
    if cat == "other":
        res = ctx.results.get("appinsights.errors")
        docs += [d["ref"] for d in (res.evidence.get("closest_docs", []) if res else [])]
    missing = list(extra_missing or [])
    if cat == "other":
        missing.append(
            "the stack trace of the top error (az webapp log tail, or the operation id in Application Insights)"
        )
    return [
        (
            Hypothesis(
                f"observed-{cat}",
                info[0],
                status,
                conf,
                lines,
                supporting_checks=[source_check],
                missing_evidence=missing,
                doc_refs=docs,
                remediation_ids=[a.id for a in acts],
                component=info[2],
            ),
            acts,
        )
    ]


@rule(37)
def app_logged_errors(ctx):
    data = ctx.facts.get("appinsights_errors")
    if not data or not data.get("groups"):
        return []
    from .diagnostics.insights_checks import _minutes_ago

    out = []
    window = data["window"]
    known = [c for c in data["by_category"] if c != "other"]
    for cat, count in sorted(data["by_category"].items(), key=lambda kv: -kv[1]):
        groups = [g for g in data["groups"] if g["category"] == cat and g["source"] != "request"]
        if not groups or (cat == "other" and known):
            continue
        ago = _minutes_ago(max(g["last"] for g in groups))
        lines = [
            f"Application Insights (last {window}): {count} logged error(s) of this kind"
            + (f", latest {ago} min ago" if ago is not None else "")
        ] + [f"{g['role'] or 'app'}: {g['what']} (x{g['count']})" for g in groups[:2]]
        if cat == "other" and data.get("requests_failed"):
            lines.append(f"{data['requests_failed']} request(s) returned HTTP 5xx in the same window")
        out += _category_hypothesis(
            ctx,
            cat,
            lines,
            "appinsights.errors",
            False,
            ["errors older than the fix stay in the window: use --window PT15M to look only at the last 15 minutes"],
        )
    return out


@rule(38)
def chat_e2e_failed(ctx):
    res = ctx.facts.get("chat_e2e")
    if not res or res.get("ok"):
        return []
    lines = [
        f"a real signed-in chat message failed (HTTP {res.get('status') or 'no response'}): "
        f"{str(res.get('detail') or '')[:160]}".rstrip(": ")
    ]
    return _category_hypothesis(ctx, res.get("category", "other"), lines, "chat.e2e", True)


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
        ctx.facts["hyp_ids"] = {h.id for _, h in hyps}
    notes, support = ctx.facts.get("hyp_notes", {}), ctx.facts.get("hyp_support", {})
    for _, h in hyps:  # evidence from app logs / the chat test that backs an existing finding
        h.rationale.extend(n for n in notes.get(h.id, []) if n not in h.rationale)
        h.supporting_checks.extend(c for c in sorted(support.get(h.id, ())) if c not in h.supporting_checks)
    hyps.sort(key=lambda ph: (_STATUS_RANK.get(ph[1].status, 3), _CONF_RANK.get(ph[1].confidence, 3), ph[0]))
    return [h for _, h in hyps], actions


def missing_evidence(ctx: DiagContext, hyps: List[Hypothesis]) -> List[str]:
    out: List[str] = []
    if ctx.runner.mode == "offline":
        out.append("Live Azure state (offline mode) — re-run without --offline after 'az login'")
    if not ctx.resource_group:
        out.append("Resource group name (no azd env found; pass --resource-group)")
    observed_codes = ctx.facts.get("deployment_error_codes") or []
    if not ctx.issue.error_codes and not observed_codes and {"deployment", "quota"} & set(ctx.issue.stages):
        out.append("Exact error code and message from the azd/ARM output (e.g. 'Code: InsufficientQuota')")
    when = [e.get("timestamp") for e in ctx.facts.get("deployment_errors", []) if e.get("timestamp")]
    if not ctx.issue.timestamps and not when and {"runtime", "deployment"} & set(ctx.issue.stages):
        out.append("Approximate time of failure (to correlate with deployment/activity logs)")
    if {"runtime", "startup"} & set(ctx.issue.stages) and not (
        any(h.id == "app-unhealthy" for h in hyps) or ctx.facts.get("appinsights_errors")
    ):
        out.append("Application logs (az webapp log tail / Application Insights) for the failing request")
    for h in hyps:
        for m in h.missing_evidence:
            if m not in out:
                out.append(m)
    return out
