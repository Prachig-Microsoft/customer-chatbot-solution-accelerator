"""Derive a profile for an accelerator that has no hand-written one, straight from its repository.

Any azd-based accelerator has the same anatomy: ``azure.yaml``, Bicep under ``infra/``, a parameters file and markdown
docs. This reads those (never Azure) and builds the same profile structure the shipped JSON profiles use, so docs
search, preflight (quota and model availability) and the generic Azure checks work without anyone writing JSON first.
It is deliberately conservative: what it cannot read from the repository it leaves out instead of guessing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .docscan import SKIP_DIRS, scan_docs
from .repo import git_info, parse_bicep_params, parse_parameters_env_map, parse_usage_names

MAX_BICEP_FILES = 300

# AVM module folder (avm/res/<name>/...) -> resource provider, and provider -> friendly service name.
AVM_PROVIDERS = {
    "cognitive-services": "Microsoft.CognitiveServices",
    "document-db": "Microsoft.DocumentDB",
    "web": "Microsoft.Web",
    "app": "Microsoft.App",
    "search": "Microsoft.Search",
    "storage": "Microsoft.Storage",
    "container-registry": "Microsoft.ContainerRegistry",
    "operational-insights": "Microsoft.OperationalInsights",
    "insights": "Microsoft.Insights",
    "key-vault": "Microsoft.KeyVault",
    "network": "Microsoft.Network",
    "managed-identity": "Microsoft.ManagedIdentity",
    "app-configuration": "Microsoft.AppConfiguration",
    "sql": "Microsoft.Sql",
    "db-for-postgre-sql": "Microsoft.DBforPostgreSQL",
    "service-bus": "Microsoft.ServiceBus",
    "event-hub": "Microsoft.EventHub",
    "compute": "Microsoft.Compute",
    "container-service": "Microsoft.ContainerService",
    "machine-learning-services": "Microsoft.MachineLearningServices",
    "signal-r-service": "Microsoft.SignalRService",
    "fabric": "Microsoft.Fabric",
    "data-factory": "Microsoft.DataFactory",
    "databricks": "Microsoft.Databricks",
    "api-management": "Microsoft.ApiManagement",
    "cache": "Microsoft.Cache",
}
SERVICE_NAMES = {
    "Microsoft.CognitiveServices": "Azure AI services / OpenAI",
    "Microsoft.DocumentDB": "Azure Cosmos DB",
    "Microsoft.Web": "Azure App Service",
    "Microsoft.App": "Azure Container Apps",
    "Microsoft.Search": "Azure AI Search",
    "Microsoft.Storage": "Azure Storage",
    "Microsoft.ContainerRegistry": "Azure Container Registry",
    "Microsoft.OperationalInsights": "Log Analytics",
    "Microsoft.Insights": "Application Insights",
    "Microsoft.KeyVault": "Azure Key Vault",
    "Microsoft.AppConfiguration": "Azure App Configuration",
    "Microsoft.Sql": "Azure SQL",
    "Microsoft.DBforPostgreSQL": "Azure Database for PostgreSQL",
    "Microsoft.ServiceBus": "Azure Service Bus",
    "Microsoft.EventHub": "Azure Event Hubs",
    "Microsoft.ContainerService": "Azure Kubernetes Service",
    "Microsoft.MachineLearningServices": "Azure Machine Learning",
    "Microsoft.Fabric": "Microsoft Fabric",
    "Microsoft.ApiManagement": "Azure API Management",
    "Microsoft.Cache": "Azure Cache for Redis",
}
NOT_WORTH_CHECKING = {"Microsoft.Authorization", "Microsoft.Resources", "Microsoft.Management"}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _azure_yaml_name(repo: Path) -> Optional[str]:
    m = re.search(r"^name:\s*(\S+)", _read(repo / "azure.yaml"), re.M)
    return m.group(1).strip("'\"") if m else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _pretty(name: str) -> str:
    words = [w for w in re.split(r"[-_\s]+", name) if w]
    return " ".join(w.upper() if len(w) <= 2 else w.capitalize() if w.islower() else w for w in words)


def discover_docs(repo: Path) -> Dict[str, List[str]]:
    """Every document in the repository, grouped by what it is about (see docscan.py)."""
    return scan_docs(repo)["docs"]


def _bicep_files(repo: Path) -> List[Path]:
    out: List[Path] = []
    for p in sorted(repo.glob("infra/**/*.bicep")) + sorted(repo.glob("*.bicep")):
        if len(out) >= MAX_BICEP_FILES:
            break
        if not ({x.lower() for x in p.relative_to(repo).parts} & SKIP_DIRS):
            out.append(p)
    return out


def _providers(repo: Path) -> List[str]:
    found = set()
    for p in _bicep_files(repo):
        text = _read(p)
        found.update(f"Microsoft.{m}" for m in re.findall(r"\bMicrosoft\.([A-Za-z]+)/[A-Za-z]+(?:/[A-Za-z]+)*@", text))
        for avm in re.findall(r"avm/res/([a-z-]+)/", text):
            if avm in AVM_PROVIDERS:
                found.add(AVM_PROVIDERS[avm])
    return sorted(found - NOT_WORTH_CHECKING)


def _tokens(model: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", model.lower()) if len(t) >= 3 and not t.isdigit()]


def _pick(params: Dict[str, Any], model: str, hint: str, want_type: str, default: Any) -> Optional[str]:
    """The one Bicep parameter that looks like this model's <hint> (name/version/capacity); None when unclear."""
    cands = [
        p.name
        for p in params.values()
        if hint in p.name.lower() and p.type.rstrip("?") == want_type and (default is None or p.default == default)
    ]
    if len(cands) > 1 or hint == "version":  # a version parameter is only trusted when it names this model
        cands = [n for n in cands if any(t in n.lower() for t in _tokens(model))]
    return cands[0] if len(cands) == 1 else None


def _models(params: Dict[str, Any], usage: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    models: List[Dict[str, Any]] = []
    chat_taken = False
    for u in usage:
        model, sku, cap = u["model"], u["sku"], u["capacity"]
        lowered = model.lower()
        if "embedding" in lowered:
            role = "embedding"
        elif "realtime" in lowered:
            role = "realtime"
        elif not chat_taken:
            role, chat_taken = "chat", True
        else:
            role = f"model-{len(models) + 1}"
        entry: Dict[str, Any] = {"role": role, "name_fixed": model, "sku_fixed": sku, "capacity_fixed": cap}
        for key, hint, kind, default in (
            ("name_param", "model", "string", model),
            ("version_param", "version", "string", None),
            ("capacity_param", "capacity", "int", cap),
        ):
            picked = _pick(params, model, hint, kind, default)
            if picked:
                entry[key] = picked
        models.append(entry)
    return models


def derive_profile(repo: Path) -> Optional[Dict[str, Any]]:
    """A profile built from the repository, or None when it does not look like an azd/Bicep accelerator."""
    repo = repo.resolve()
    azd_name = _azure_yaml_name(repo)
    main = next((r for r in ("infra/main.bicep", "main.bicep") if (repo / r).exists()), None)
    if not azd_name and not main:
        return None
    name = azd_name or repo.name
    pid = _slug(name)
    if not pid:
        return None
    git = git_info(repo)
    remote = (git.get("remote") or "").removesuffix(".git")
    remote_name = remote.rstrip("/").rsplit("/", 1)[-1] if remote else ""

    docs = discover_docs(repo)
    params = parse_bicep_params(repo / main) if main else {}
    ai_param = next((p for p in params.values() if p.usage_names), None) or next(
        (p for p in params.values() if re.search(r"(ai|openai|foundry|cognitive).*(location|region)", p.name, re.I)),
        None,
    )
    usage = parse_usage_names(ai_param.usage_names) if ai_param else []
    parameters_rel = next(
        (r for r in ("infra/main.parameters.json", "main.parameters.json") if (repo / r).exists()), None
    )
    env_map = parse_parameters_env_map(repo / parameters_rel) if parameters_rel else {}

    keys = {"subscription": "AZURE_SUBSCRIPTION_ID", "resource_group": "AZURE_RESOURCE_GROUP"}
    if "location" in params:
        keys["location"] = "AZURE_LOCATION"
    ai_env = (env_map.get(ai_param.name) or {}).get("env") if ai_param else None
    if ai_env:
        keys["ai_location"] = ai_env

    providers = _providers(repo)
    quota_doc = (docs.get("quota") or ["README.md"])[0]
    scripts = [
        p.relative_to(repo).as_posix()
        for p in sorted(repo.glob("infra/scripts/**/*quota*"))
        if p.is_file() and p.suffix in (".sh", ".ps1", ".py")
    ]
    iac: Dict[str, Any] = {
        "provider": "bicep" if main else "unknown",
        "main": main or "infra/main.bicep",
        "parameters": parameters_rel or "infra/main.parameters.json",
        "models": _models(params, usage),
        "quota_scripts": scripts,
        "quota_doc": quota_doc,
    }
    if ai_param:
        iac["ai_location_param"] = ai_param.name
    if "location" in params:
        iac["location_param"] = "location"
    param_doc = next((d for d in docs.get("deployment", []) if re.search(r"customi|parameter", d, re.I)), None)
    if param_doc:
        iac["param_doc"] = param_doc

    profile: Dict[str, Any] = {
        "id": pid,
        "name": _pretty(name) + (" Solution Accelerator" if "accelerator" not in name.lower() else ""),
        "upstream_repo": remote or None,
        "aliases": sorted({name, name.replace("-", " "), pid}),
        "repo_candidates": ["."],
        "fingerprint": {
            "azure_yaml_names": [azd_name] if azd_name else [],
            "remote_patterns": [remote_name] if remote_name else [],
            "marker_files": ["azure.yaml"] if azd_name else [],
        },
        "resource_name_patterns": [],
        "services": [SERVICE_NAMES[p] for p in providers if p in SERVICE_NAMES],
        "required_providers": providers,
        "docs": docs,
        "source_globs": [
            "infra/scripts/**/*.ps1",
            "infra/scripts/**/*.sh",
            "infra/scripts/**/*.py",
            "scripts/**/*.ps1",
            "scripts/**/*.sh",
            "azure.yaml",
        ],
        "iac": iac,
        "azd_env_keys": keys,
        "apps": [],
        "diagnostics": "generic",
        "auto_derived": True,
        "_profile_file": "(derived from the repository; run init-profile to save it)",
        "notes": (
            "No hand-written profile exists for this accelerator, so this one was derived from its repository "
            "(azure.yaml, Bicep, parameters and docs). Docs search, quota/model checks and the generic Azure checks "
            "apply; app-level checks are not available. Run `init-profile` to save the profile and review it."
        ),
    }
    return profile
