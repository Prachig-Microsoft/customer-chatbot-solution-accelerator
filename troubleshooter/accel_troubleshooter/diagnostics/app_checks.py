"""App-level checks driven by the profile's ``apps`` section (App Service based accelerators)."""

from __future__ import annotations

import re
from typing import Any, Dict

from ..models import ERROR, FAIL, INFO, PASS, SKIP, CheckResult
from ..repo import _parse_dotenv
from .azure_checks import _error_code, _need_rg
from .base import DiagContext, check

PLACEHOLDER = re.compile(r"populate manually", re.I)  # values arrive pre-redacted; "<redacted>" means set


def _app_name(ctx: DiagContext, app: Dict[str, Any]) -> str:
    name = ctx.env.get(app.get("name_env"))
    if name:
        return name
    prefix = {
        "chat-api": "api-chat-",
        "chat-web": "app-chat-",
        "scenario-api": "api-scenario-",
        "scenario-web": "app-scenario-",
    }.get(app["id"], "")
    for r in ctx.facts.get("resources", []):
        if (r.get("type") or "").lower() == "microsoft.web/sites" and prefix and r["name"].startswith(prefix):
            return r["name"]
    return ""


def _discover_sites(ctx: DiagContext) -> None:
    if ctx.facts.get("resources") is None and ctx.resource_group and not ctx.rg_known_missing():
        ok, data, _ = ctx.az("webapp", "list", "--resource-group", ctx.resource_group)
        if ok:
            ctx.facts["resources"] = [{"name": s.get("name"), "type": "Microsoft.Web/sites"} for s in data or []]


@check(
    "webapp.state",
    "App Services: state, container image, identity, app settings",
    needs_apps=True,
    stages=[
        "startup",
        "runtime",
        "post-deployment-script",
        "authentication",
        "configuration",
        "local-vs-cloud",
        "throttling",
        "networking",
        "ingestion",
    ],
    order=40,
)
def webapp_state(ctx: DiagContext) -> CheckResult:
    title = "App Services: state, container image, identity, app settings"
    if skip := _need_rg(ctx, "webapp.state", title):
        return skip
    _discover_sites(ctx)
    markers = ctx.profile.get("placeholder_image_markers", [])
    apps_out, placeholder_apps, stopped, missing_settings, pp_missing = [], [], [], {}, {}
    ctx.facts["app_principals"] = {}
    ctx.facts["app_settings_names"] = {}
    for app in ctx.profile.get("apps", []):
        name = _app_name(ctx, app)
        if not name:
            apps_out.append({"id": app["id"], "error": "app name unknown"})
            continue
        ok, data, _ = ctx.az("webapp", "show", "--resource-group", ctx.resource_group, "--name", name)
        if not ok:
            apps_out.append({"id": app["id"], "name": name, "error": _error_code(data) or "not found"})
            continue
        fx = (data.get("siteConfig") or {}).get("linuxFxVersion") or ""
        image = fx.split("|", 1)[-1] if fx else ""
        is_placeholder = any(m in image for m in markers)
        row = {
            "id": app["id"],
            "name": name,
            "state": data.get("state"),
            "host": data.get("defaultHostName"),
            "image": image,
            "placeholder_image": is_placeholder,
            "identity": (data.get("identity") or {}).get("type"),
            "https_only": data.get("httpsOnly"),
        }
        pid = (data.get("identity") or {}).get("principalId")
        if pid:
            ctx.facts["app_principals"][app["id"]] = pid
        if is_placeholder:
            placeholder_apps.append(name)
        if data.get("state") != "Running":
            stopped.append(name)
        if app.get("required_settings") or app.get("post_provision_settings"):
            ok, settings, _ = ctx.az(
                "webapp", "config", "appsettings", "list", "--resource-group", ctx.resource_group, "--name", name
            )
            if ok:
                # Only names and set/empty/placeholder flags leave this function, never values.
                flags = {
                    s["name"]: (
                        "placeholder"
                        if PLACEHOLDER.search(str(s.get("value") or ""))
                        else "set" if str(s.get("value") or "").strip() else "empty"
                    )
                    for s in settings or []
                    if isinstance(s, dict) and "name" in s
                }
                ctx.facts["app_settings_names"][app["id"]] = flags
                miss = [k for k in app.get("required_settings", []) if flags.get(k) != "set"]
                pp = [k for k in app.get("post_provision_settings", []) if flags.get(k) != "set"]
                if miss:
                    missing_settings[name] = miss
                if pp:
                    pp_missing[name] = pp
                row["settings_checked"] = len(flags)
        apps_out.append(row)
    ctx.facts.update(
        placeholder_image_apps=placeholder_apps,
        stopped_apps=stopped,
        missing_app_settings=missing_settings,
        post_provision_settings_missing=pp_missing,
    )
    problems = []
    if placeholder_apps:
        problems.append(f"placeholder image on {placeholder_apps}")
    if stopped:
        problems.append(f"not running: {stopped}")
    if missing_settings:
        problems.append(f"missing/empty required settings: {missing_settings}")
    if pp_missing:
        problems.append(f"post-provision settings not populated: {pp_missing}")
    errors = [a for a in apps_out if "error" in a]
    if errors and not problems and len(errors) == len(apps_out):
        return CheckResult(
            "webapp.state", title, ERROR, f"Could not inspect apps: {errors}", evidence={"apps": apps_out}
        )
    refs = [ctx.profile.get("placeholder_image_ref", ""), ctx.ref("deployment", "Build Container Images")]
    return CheckResult(
        "webapp.state",
        title,
        FAIL if problems else PASS,
        "; ".join(problems) if problems else f"{len(apps_out)} app(s) running with real images and settings.",
        evidence={"apps": apps_out, "missing_settings": missing_settings, "post_provision_missing": pp_missing},
        doc_refs=[r for r in refs if r],
    )


@check(
    "webapp.health",
    "HTTP health probes",
    needs_apps=True,
    stages=["startup", "runtime", "local-vs-cloud", "configuration", "authentication"],
    order=42,
)
def webapp_health(ctx: DiagContext) -> CheckResult:
    title = "HTTP health probes"
    if ctx.rg_known_missing():
        return CheckResult("webapp.health", title, SKIP, "Resource group does not exist.")
    rows, failing = [], []
    for app in ctx.profile.get("apps", []):
        url = ctx.env.get(app.get("url_env"))
        if not url:
            host = (
                next(
                    (
                        a.get("host")
                        for a in ctx.results.get("webapp.state").evidence.get("apps", [])
                        if a.get("id") == app["id"] and a.get("host")
                    ),
                    None,
                )
                if ctx.results.get("webapp.state")
                else None
            )
            url = f"https://{host}" if host else ""
        if not url:
            continue
        probe = url.rstrip("/") + app.get("health_path", "/")
        ok, res, _ = ctx.runner.http_get(probe)
        rows.append({"app": app["id"], "url": probe, **res})
        if not ok:
            failing.append(app["id"])
    ctx.facts["health_failing"] = failing
    ctx.facts["health_results"] = rows
    if not rows:
        return CheckResult("webapp.health", title, SKIP, "No app URLs known.")
    return CheckResult(
        "webapp.health",
        title,
        FAIL if failing else PASS,
        (
            f"Failing: {failing}"
            if failing
            else f"{len(rows)} endpoint(s) respond"
            + (" (config-only check)" if ctx.profile.get("health_is_shallow") else "")
            + "."
        ),
        evidence={"probes": rows},
    )


@check(
    "rbac.app_identity",
    "Managed identity RBAC for backend apps",
    needs_apps=True,
    stages=["authentication", "runtime", "local-vs-cloud", "ingestion"],
    order=44,
)
def rbac_app_identity(ctx: DiagContext) -> CheckResult:
    title = "Managed identity RBAC for backend apps"
    if skip := _need_rg(ctx, "rbac.app_identity", title):
        return skip
    principals = ctx.facts.get("app_principals")
    if principals is None:
        return CheckResult("rbac.app_identity", title, SKIP, "Run webapp.state first (no principal IDs collected).")
    rows, missing = [], []
    for app in ctx.profile.get("apps", []):
        pid = principals.get(app["id"])
        roles = app.get("expected_roles", [])
        if not roles and not app.get("cosmos_data_role"):
            continue
        if not pid:
            missing.append({"app": app["id"], "role": "system-assigned managed identity", "source": "profile"})
            continue
        ok, assigns, _ = ctx.az("role", "assignment", "list", "--assignee", pid, "--all")
        assigns = assigns if ok else []
        for role in roles:
            scope = ctx.env.get(role.get("scope_env"))
            found = any(
                a.get("roleDefinitionId", "").lower().endswith(role["role_id"])
                and (not scope or scope.lower().startswith((a.get("scope") or "").lower()))
                for a in assigns or []
            )
            rows.append(
                {
                    "app": app["id"],
                    "role": role["role"],
                    "scope": scope or "(any)",
                    "assigned": found,
                    "source": role.get("source"),
                }
            )
            if not found:
                missing.append({"app": app["id"], "role": role["role"], "scope": scope, "source": role.get("source")})
        cdr = app.get("cosmos_data_role")
        acct = ctx.env.get(ctx.env_key("cosmos_account"))
        if cdr and acct:
            ok, cas, _ = ctx.az(
                "cosmosdb",
                "sql",
                "role",
                "assignment",
                "list",
                "--resource-group",
                ctx.resource_group,
                "--account-name",
                acct,
            )
            found = ok and any(
                c.get("principalId") == pid and (c.get("roleDefinitionId") or "").endswith(cdr["role_id_suffix"])
                for c in cas or []
            )
            rows.append(
                {
                    "app": app["id"],
                    "role": "Cosmos DB Built-in Data Contributor",
                    "scope": acct,
                    "assigned": bool(found),
                    "source": cdr.get("source"),
                }
            )
            if not found:
                missing.append(
                    {
                        "app": app["id"],
                        "role": "Cosmos DB Built-in Data Contributor",
                        "scope": acct,
                        "source": cdr.get("source"),
                    }
                )
    ctx.facts["rbac_missing"] = missing
    return CheckResult(
        "rbac.app_identity",
        title,
        FAIL if missing else PASS,
        (
            f"Missing role assignment(s): {[m['role'] + ' for ' + m['app'] for m in missing]}"
            if missing
            else "All expected role assignments present."
        ),
        evidence={"assignments": rows},
    )


@check(
    "rbac.deployer_data",
    "Deploying identity data-plane access (post-provision scripts)",
    needs_apps=True,
    stages=["post-deployment-script", "ingestion"],
    order=45,
)
def rbac_deployer_data(ctx: DiagContext) -> CheckResult:
    title = "Deploying identity data-plane access (post-provision scripts)"
    if skip := _need_rg(ctx, "rbac.deployer_data", title):
        return skip
    acct = ctx.env.get(ctx.env_key("cosmos_account")) or next(
        (
            r["name"]
            for r in ctx.facts.get("resources", [])
            if (r.get("type") or "").lower() == "microsoft.documentdb/databaseaccounts"
        ),
        "",
    )
    if not acct:
        return CheckResult("rbac.deployer_data", title, SKIP, "No Cosmos DB account known.")
    ok, me, _ = ctx.az("ad", "signed-in-user", "show")
    if not ok or not isinstance(me, dict) or not me.get("id"):
        return CheckResult(
            "rbac.deployer_data",
            title,
            SKIP,
            "Signed-in identity is not a user (service principal / managed identity) or Graph read failed.",
        )
    ok, cas, _ = ctx.az(
        "cosmosdb", "sql", "role", "assignment", "list", "--resource-group", ctx.resource_group, "--account-name", acct
    )
    if not ok:
        return CheckResult(
            "rbac.deployer_data", title, ERROR, f"Cannot list Cosmos role assignments: {_error_code(cas)}"
        )
    has = any(
        c.get("principalId") == me["id"]
        and (c.get("roleDefinitionId") or "").endswith("00000000-0000-0000-0000-000000000002")
        for c in cas or []
    )
    ctx.facts["deployer_cosmos_role"] = has
    ref = ctx.doc_ref("infra/scripts/post-provision/data_scripts/03_write_products_to_cosmos.py", "Cosmos DB Built-in")
    return CheckResult(
        "rbac.deployer_data",
        title,
        PASS if has else FAIL,
        (
            "Signed-in user has Cosmos DB Built-in Data Contributor."
            if has
            else f"Signed-in user lacks Cosmos DB Built-in Data Contributor on '{acct}' — the data upload script "
            "writes to Cosmos with your identity."
        ),
        evidence={"cosmos_account": acct, "has_role": has},
        doc_refs=[ref],
    )


@check(
    "config.compare",
    "Local config vs IaC vs deployed app settings",
    needs_apps=True,
    needs_azure=False,
    stages=["local-vs-cloud", "configuration", "runtime", "startup"],
    order=46,
)
def config_compare(ctx: DiagContext) -> CheckResult:
    title = "Local config vs IaC vs deployed app settings"
    rows = []
    gaps = []
    for app in ctx.profile.get("apps", []):
        required = app.get("required_settings", []) + app.get("post_provision_settings", [])
        if not required:
            continue
        local_env = ctx.repo / ("chat-app/backend/.env" if app["id"] == "chat-api" else "scenario-app/backend/.env")
        local = _parse_dotenv(local_env.read_text(encoding="utf-8", errors="ignore")) if local_env.exists() else None
        deployed = (ctx.facts.get("app_settings_names") or {}).get(app["id"])
        for key in required:
            row = {
                "app": app["id"],
                "setting": key,
                "local": None if local is None else ("set" if local.get(key) else "missing"),
                "deployed": None if deployed is None else deployed.get(key, "missing"),
            }
            rows.append(row)
            if row["local"] == "set" and row["deployed"] not in (None, "set"):
                gaps.append(row)
    ctx.facts["local_vs_deployed_gaps"] = gaps
    if not rows:
        return CheckResult("config.compare", title, SKIP, "No settings defined in profile.")
    have_deployed = any(r["deployed"] is not None for r in rows)
    have_local = any(r["local"] is not None for r in rows)
    if gaps:
        return CheckResult(
            "config.compare",
            title,
            FAIL,
            f"{len(gaps)} setting(s) present locally but missing/empty in Azure: "
            f"{sorted({g['setting'] for g in gaps})}",
            evidence={"settings": rows},
            doc_refs=[ctx.ref("local_dev", "Configure Environment Variables")],
        )
    status = PASS if (have_deployed and have_local) else INFO
    note = "" if have_deployed else " Deployed settings not inspected (no live data)."
    note += "" if have_local else " No local backend .env found."
    return CheckResult(
        "config.compare",
        title,
        status,
        "No local-vs-deployed setting gaps detected." + note,
        evidence={"settings": rows},
    )
