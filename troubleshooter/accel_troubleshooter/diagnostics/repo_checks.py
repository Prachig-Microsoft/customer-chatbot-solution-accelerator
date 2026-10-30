"""Repository / configuration checks (no Azure access required)."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..models import FAIL, INFO, PASS, WARN, CheckResult
from ..redact import is_secret_name, redact_text
from ..repo import git_changed_files, git_diff_lines, parse_bicep_params, parse_parameters_env_map, parse_usage_names
from .base import DiagContext, check

PLACEHOLDER = re.compile(r"^<.*>$|populate manually", re.I)


def effective_model_requirements(ctx: DiagContext) -> List[Dict[str, Any]]:
    """Resolve each model's name/sku/capacity: azd env override > Bicep default. Cached in facts."""
    if "model_requirements" in ctx.facts:
        return ctx.facts["model_requirements"]
    iac = ctx.profile.get("iac", {})
    params = parse_bicep_params(ctx.repo / iac.get("main", "infra/main.bicep"))
    env_map = parse_parameters_env_map(ctx.repo / iac.get("parameters", "infra/main.parameters.json"))
    ai_param = params.get(iac.get("ai_location_param", ""))
    loc_param = params.get(iac.get("location_param", ""))
    ctx.facts["allowed_ai_regions"] = ai_param.allowed if ai_param else []
    ctx.facts["allowed_locations"] = loc_param.allowed if loc_param else []
    usage = parse_usage_names(ai_param.usage_names if ai_param else [])
    ctx.facts["usage_name_metadata"] = usage

    def resolve(pname: str):
        if not pname:
            return None, "n/a"
        overrides = ctx.overrides.get("capacity_overrides", {})
        if pname in overrides:
            return overrides[pname], "what-if override"
        envvar = (env_map.get(pname) or {}).get("env")
        if envvar and ctx.env.get(envvar):
            return ctx.env.get(envvar), f"azd env {envvar}"
        if pname in ctx.env.params:
            return ctx.env.params[pname], f"azd env config.json infra.parameters.{pname}"
        p = params.get(pname)
        if p is not None and p.default is not None:
            return p.default, f"{iac['main']}:{p.line} default"
        envdef = (env_map.get(pname) or {}).get("default")
        return envdef, f"{iac['parameters']} default" if envdef else "unset"

    reqs = []
    for m in iac.get("models", []):
        name, name_src = resolve(m.get("name_param", ""))
        version, _ = resolve(m.get("version_param", ""))
        cap, cap_src = resolve(m.get("capacity_param", ""))
        sku = m.get("sku_fixed")
        sku_src = "profile (fixed)"
        if m.get("sku_param"):
            sku, sku_src = resolve(m["sku_param"])
        try:
            cap = int(cap) if cap is not None else None
        except (TypeError, ValueError):
            pass
        meta = next((u for u in usage if u["model"] == name), None)
        cap_param = params.get(m.get("capacity_param", ""))
        reqs.append(
            {
                "role": m["role"],
                "model": name,
                "version": version,
                "sku": sku,
                "capacity": cap,
                "usage_name": f"OpenAI.{sku}.{name}",
                "capacity_source": cap_src,
                "name_source": name_src,
                "sku_source": sku_src,
                "capacity_param": m.get("capacity_param"),
                "capacity_env": (env_map.get(m.get("capacity_param", "")) or {}).get("env"),
                "capacity_param_line": cap_param.line if cap_param else None,
                "min_value": cap_param.min_value if cap_param else None,
                "usage_metadata_capacity": meta["capacity"] if meta else None,
                "usage_metadata_sku": meta["sku"] if meta else None,
            }
        )
    ctx.facts["model_requirements"] = reqs
    ctx.facts["param_env_map"] = env_map
    return reqs


@check("repo.azd_env", "azd environment present and complete", stages=["*"], needs_azure=False, order=1)
def azd_env(ctx: DiagContext) -> CheckResult:
    keys = ctx.profile.get("azd_env_keys", {})
    if ctx.overrides.get("foreign_env"):
        return CheckResult(
            "repo.azd_env",
            "azd environment present and complete",
            INFO,
            f"Checking resource group '{ctx.resource_group}', which is not the current azd environment "
            f"('{ctx.overrides['foreign_env']}'). Its settings were ignored; everything below comes from Azure.",
            evidence={"foreign_env": ctx.overrides["foreign_env"], "resource_group": ctx.resource_group},
        )
    iac_cfg = ctx.profile.get("iac", {})
    param_for = {"ai_location": iac_cfg.get("ai_location_param"), "location": iac_cfg.get("location_param")}

    def known(logical: str, envvar: str) -> bool:  # azd keeps the prompted answers in config.json as well as .env
        return bool(ctx.env.get(envvar) or (param_for.get(logical) and ctx.env.params.get(param_for[logical])))

    if not ctx.env.found:
        return CheckResult(
            "repo.azd_env",
            "azd environment present and complete",
            WARN,
            "No azd environment found under .azure/ (deleted, never created, or a different machine). "
            "Live resource checks need --resource-group / --region.",
            evidence={
                "searched": str(ctx.repo / ".azure"),
                "resource_group_used": ctx.resource_group or None,
                "resource_group_source": ctx.resource_group_source,
            },
        )
    missing = [v for k, v in keys.items() if not known(k, v)]
    placeholders = sorted(k for k, v in ctx.env.values.items() if PLACEHOLDER.search(v or ""))
    empty = sorted(k for k, v in ctx.env.values.items() if v == "" and not k.endswith(("KEY", "SECRET")))
    # Most keys (resource names, URLs) only exist after provisioning. Only these are needed beforehand, and azd
    # also keeps the answers it prompted for in .azure/<env>/config.json (infra.parameters).
    ai_param = ctx.profile.get("iac", {}).get("ai_location_param", "")
    iac_loc_param = ctx.profile.get("iac", {}).get("location_param", "")
    needed_now = {"subscription": keys.get("subscription"), "location": keys.get("location")}
    missing_now = [k for k, v in needed_now.items() if v and not ctx.env.get(v)]
    has_ai_region = bool(ctx.env.get(keys.get("ai_location")) or ctx.env.params.get(ai_param))
    deployed = bool(ctx.env.get(keys.get("resource_group")))
    finished = deployed and any(ctx.env.get(a.get("url_env")) for a in ctx.profile.get("apps", []))
    status = WARN if missing_now else PASS
    state = (
        "deployed (app URLs recorded)."
        if finished
        else "provisioning started (resource group recorded)." if deployed else "not deployed yet."
    )
    summary = f"azd env '{ctx.env.name}': {state}"
    if missing_now:
        summary += f" Not set: {missing_now}."
    if not has_ai_region and not deployed:
        summary += f" AI region not chosen yet (azd will ask, or: azd env set {keys.get('ai_location')} <region>)."
    if deployed and missing and not finished:
        summary += f" {len(missing)} output(s) not recorded yet (normal until provisioning finishes)."
        status = INFO if status == PASS else status
    elif finished and missing:
        summary += f" Missing recorded value(s): {missing[:3]}."
        status = INFO if status == PASS else status
    if placeholders:
        summary += (
            f" {len(placeholders)} placeholder value(s) — expected for post-provision outputs that are "
            "written to App Service settings rather than the azd env."
        )
    return CheckResult(
        "repo.azd_env",
        "azd environment present and complete",
        status,
        summary,
        evidence={
            "env_name": ctx.env.name,
            "env_file": ctx.env.path,
            "deployed": deployed,
            "ai_region_chosen": has_ai_region,
            "missing_keys": missing,
            "placeholder_keys": placeholders,
            "empty_keys": empty,
            "subscription": ctx.env.get(keys.get("subscription")),
            "resource_group": ctx.env.get(keys.get("resource_group")),
            "ai_location": ctx.env.get(keys.get("ai_location")) or ctx.env.params.get(ai_param) or None,
            "location": ctx.env.get(keys.get("location")) or ctx.env.params.get(iac_loc_param) or None,
        },
    )


def _doc_default_models(text: str) -> Dict[str, int]:
    m = re.search(r"Default Models[^\n]*\n+```[^\n]*\n([^`]+)```", text, re.I)
    if not m:
        return {}
    return {k.strip(): int(v) for k, v in re.findall(r"([A-Za-z0-9.\-]+):(\d+)", m.group(1))}


def _doc_default_regions(text: str) -> List[str]:
    m = re.search(r"Default Regions[^\n]*\n+```[^\n]*\n([^`]+)```", text, re.I)
    return [r.strip() for r in m.group(1).split(",") if r.strip()] if m else []


@check("repo.config_drift", "IaC vs docs vs scripts vs azd env consistency", stages=["*"], needs_azure=False, order=2)
def config_drift(ctx: DiagContext) -> CheckResult:
    reqs = effective_model_requirements(ctx)
    iac = ctx.profile.get("iac", {})
    findings: List[Dict[str, Any]] = []
    allowed_ai = ctx.facts.get("allowed_ai_regions", [])

    for r in reqs:
        meta_cap = r["usage_metadata_capacity"]
        if meta_cap is not None and isinstance(r["capacity"], int) and r["capacity"] > meta_cap:
            findings.append(
                {
                    "severity": "medium",
                    "id": "capacity-exceeds-azd-usage-metadata",
                    "message": f"{r['model']} capacity {r['capacity']} ({r['capacity_source']}) is higher than the "
                    f"azd region-picker quota filter ({meta_cap}). azd may offer regions without enough "
                    "quota, leading to InsufficientQuota at deploy time.",
                    "refs": [ctx.doc_ref(iac["main"], r["usage_name"].replace(".", r"\.") + ",")],
                }
            )
        if r["usage_metadata_sku"] and r["sku"] and r["usage_metadata_sku"] != r["sku"]:
            findings.append(
                {
                    "severity": "high",
                    "id": "sku-differs-from-usage-metadata",
                    "message": f"{r['model']} deployment type is {r['sku']} ({r['sku_source']}) but the azd quota "
                    f"filter checks {r['usage_metadata_sku']} quota.",
                    "refs": [ctx.doc_ref(iac["main"], "usageName")],
                }
            )
        if r["min_value"] and isinstance(r["capacity"], int) and r["capacity"] < r["min_value"]:
            findings.append(
                {
                    "severity": "high",
                    "id": "capacity-below-min",
                    "message": f"{r['model']} capacity {r['capacity']} is below @minValue({r['min_value']}).",
                    "refs": [f"{iac['main']}:{r['capacity_param_line']}"],
                }
            )

    quota_doc = ctx.repo / iac.get("quota_doc", "")
    if quota_doc.is_file():
        text = quota_doc.read_text(encoding="utf-8", errors="ignore")
        doc_models = _doc_default_models(text)
        for r in reqs:
            if r["model"] in doc_models and isinstance(r["capacity"], int) and doc_models[r["model"]] != r["capacity"]:
                findings.append(
                    {
                        "severity": "medium",
                        "id": "doc-capacity-mismatch",
                        "message": f"{iac['quota_doc']} documents {r['model']}:{doc_models[r['model']]} but the "
                        f"effective capacity is {r['capacity']} ({r['capacity_source']}).",
                        "refs": [ctx.doc_ref(iac["quota_doc"], "Default Models")],
                    }
                )
        doc_regions = _doc_default_regions(text)
        not_allowed = [x for x in doc_regions if allowed_ai and x not in allowed_ai]
        if not_allowed:
            findings.append(
                {
                    "severity": "low",
                    "id": "doc-regions-not-allowed",
                    "short": f"{iac['quota_doc']} lists {len(not_allowed)} region(s) this template can't use "
                    f"(e.g. {', '.join(not_allowed[:3])}).",
                    "message": f"{iac['quota_doc']} lists default regions {not_allowed} that are not in the "
                    f"@allowed list for {iac.get('ai_location_param')} {allowed_ai}; quota found there "
                    "cannot be used by this template.",
                    "refs": [
                        ctx.doc_ref(iac["quota_doc"], "Default Regions"),
                        ctx.doc_ref(iac["main"], f"param {iac.get('ai_location_param')} "),
                    ],
                }
            )
    for script in iac.get("quota_scripts", []):
        sp = ctx.repo / script
        if not sp.is_file():
            continue
        stext = sp.read_text(encoding="utf-8", errors="ignore")
        dm = re.search(r'DEFAULT_MODEL_CAPACITY="([^"]+)"', stext)
        script_models = {k: int(v) for k, v in re.findall(r"([A-Za-z0-9.\-]+):(\d+)", dm.group(1))} if dm else {}
        role_by_prefix = (("GPT_REALTIME", "realtime"), ("EMBEDDING", "embedding"), ("GPT", "chat"))
        for var, val in re.findall(r'(\w+)_MIN_CAPACITY="\$\{\w+:-(\d+)\}"', stext):
            role = next((rl for pre, rl in role_by_prefix if var.startswith(pre)), None)
            model = next((r["model"] for r in reqs if r["role"] == role), None)
            if model:
                script_models.setdefault(model, int(val))
        for r in reqs:
            if (
                r["model"] in script_models
                and isinstance(r["capacity"], int)
                and script_models[r["model"]] != r["capacity"]
            ):
                findings.append(
                    {
                        "severity": "medium",
                        "id": "script-capacity-mismatch",
                        "message": f"{script} checks {r['model']} >= {script_models[r['model']]} but the effective "
                        f"capacity is {r['capacity']} ({r['capacity_source']}).",
                        "refs": [ctx.doc_ref(script, re.escape(r["model"]) + ":|MIN_CAPACITY")],
                    }
                )
        rm = re.search(r'DEFAULT_REGIONS="([^"]+)"', stext)
        if rm and allowed_ai:
            bad = [x for x in rm.group(1).split(",") if x not in allowed_ai]
            if bad:
                findings.append(
                    {
                        "severity": "low",
                        "id": "script-regions-not-allowed",
                        "short": f"{script.rsplit('/', 1)[-1]} default regions include {len(bad)} "
                        f"the template can't use (e.g. {', '.join(bad[:3])}).",
                        "message": f"{script} default regions include {bad}, which are not allowed for "
                        f"{iac.get('ai_location_param')}.",
                        "refs": [ctx.doc_ref(script, "DEFAULT_REGIONS=")],
                    }
                )

    ai_loc = ctx.env.get(ctx.env_key("ai_location")) or ctx.env.params.get(iac.get("ai_location_param", ""))
    if ai_loc and allowed_ai and ai_loc not in allowed_ai:
        findings.append(
            {
                "severity": "high",
                "id": "env-ai-region-not-allowed",
                "message": f"azd env AI location '{ai_loc}' is not in the template's allowed list {allowed_ai}.",
                "refs": [ctx.doc_ref(iac["main"], f"param {iac.get('ai_location_param')} ")],
            }
        )

    ctx.facts["drift_findings"] = findings
    sev = {f["severity"] for f in findings}
    status = FAIL if "high" in sev else WARN if sev & {"medium"} else INFO if findings else PASS
    summary = (
        f"{len(findings)} consistency finding(s)."
        if findings
        else "IaC, docs, quota scripts and azd env are consistent."
    )
    return CheckResult(
        "repo.config_drift",
        "IaC vs docs vs scripts vs azd env consistency",
        status,
        summary,
        evidence={
            "findings": findings,
            "effective_models": reqs,
            "allowed_ai_regions": allowed_ai,
            "allowed_locations": ctx.facts.get("allowed_locations", []),
        },
    )


CONFIG_PATTERNS = (
    r"^infra/",
    r"\.env",
    r"azure\.yaml$",
    r"config\.py$",
    r"\.bicepparam$",
    r"parameters.*\.json$",
    r"^scenarios/.*manifest\.json$",
)

_DIFF_KV = re.compile(r"^([+-]\s*)([\"']?)([A-Za-z_][\w.\-]*)(\2\s*[:=]\s*)(.*)$")


def mask_diff_line(path: str, line: str) -> str:
    """Hide values in diff lines: all values for .env-style files, secret-named keys everywhere."""
    m = _DIFF_KV.match(line)
    if m:
        name = m.group(3)
        if ".env" in path.rsplit("/", 1)[-1] or is_secret_name(name):
            return f"{m.group(1)}{m.group(2)}{name}{m.group(4)}<value hidden>"
    return redact_text(line)


@check(
    "repo.git_changes",
    "Uncommitted configuration changes",
    stages=["configuration", "local-vs-cloud", "deployment", "pre-deployment", "runtime", "startup"],
    needs_azure=False,
    order=3,
)
def git_changes(ctx: DiagContext) -> CheckResult:
    changed = git_changed_files(ctx.repo)
    cfg = [
        c
        for c in changed
        if any(re.search(p, c["path"]) for p in CONFIG_PATTERNS) and not c["path"].startswith("troubleshooter/")
    ]
    details = []
    for c in cfg[:10]:
        lines = git_diff_lines(ctx.repo, c["path"]) if c["status"] in ("M", "MM", "AM") else []
        lines = [mask_diff_line(c["path"], ln) for ln in lines]
        details.append({**c, "diff": lines[:20]})
    ctx.facts["changed_config_files"] = details
    if not cfg:
        return CheckResult(
            "repo.git_changes",
            "Uncommitted configuration changes",
            PASS,
            "No uncommitted configuration/IaC changes in the working tree.",
        )
    return CheckResult(
        "repo.git_changes",
        "Uncommitted configuration changes",
        WARN,
        f"{len(cfg)} configuration/IaC file(s) differ from the last commit — compare with what was deployed.",
        evidence={"files": details},
    )
