"""App Service plan quota in the primary location (quota.compute): Bicep SKU extraction, live quota, verdicts."""

import copy
import json

from accel_troubleshooter.azcli import ReplayRunner, is_read_only
from accel_troubleshooter.bicepeval import find_app_service_plans
from accel_troubleshooter.classify import extract
from accel_troubleshooter.compact import recommend_primary, render_preflight
from accel_troubleshooter.diagnostics.compute_checks import assess_region
from accel_troubleshooter.engine import Troubleshooter

SUB = "00000000-0000-0000-0000-00000000aaaa"
WEB = f"https://management.azure.com/subscriptions/{SUB}/providers/Microsoft.Web/locations"
REPORTED_ERROR = (
    "ERROR: error executing step command 'provision': deployment failed: InvalidTemplateDeployment: The template "
    "deployment 'pgmacae2' is not valid according to the validation procedure. 'Microsoft.Web/serverFarms "
    "(2025-05-01)' reported preflight validation errors. InternalSubscriptionIsOverQuotaForSku: Operation cannot be "
    "completed without additional quota. Location: East US 2. Current Limit (B3 VMs): 0, Current Usage: 0, Amount "
    "required (B3 VMs): 1"
)

MAIN = """targetScope = 'resourceGroup'
@allowed(['bicep', 'avm', 'avm-waf'])
param deploymentFlavor string
@allowed([
  'eastus2'
  'westus3'
])
param location string
@allowed(['westus3', 'swedencentral'])
@metadata({
  azd: {
    type: 'location'
    usageName: [
      'OpenAI.GlobalStandard.gpt-4o, 10'
    ]
  }
})
param azureAiServiceLocation string
param enableScalability bool = deploymentFlavor == 'avm-waf'
param enableRedundancy bool = deploymentFlavor == 'avm-waf'
var isAvm = deploymentFlavor == 'avm' || deploymentFlavor == 'avm-waf'
var isBicep = deploymentFlavor == 'bicep'

module avmDeployment './avm/main.bicep' = if (isAvm) {
  name: 'avm'
  params: {
    location: location
    enableScalability: enableScalability
    enableRedundancy: enableRedundancy
  }
}

module bicepDeployment './bicep/main.bicep' = if (isBicep) {
  name: 'bicep'
  params: {
    location: location
    aiLocation: azureAiServiceLocation
  }
}
"""

BICEP_MAIN = """param location string
param aiLocation string
var solutionLocation = location
// a comment with braces { and a 'quote
module app_service_plan './modules/app-service-plan.bicep' = {
  name: take('module.app-service-plan.${location}', 64)
  params: {
    name: 'asp-${uniqueString(resourceGroup().id)}'
    location: solutionLocation
    skuName: 'B3'
    skuCapacity: 1
  }
}
module existing_plan './modules/app-service-plan.bicep' = if (false) {
  name: 'never'
  params: {
    location: location
    skuName: 'S3'
  }
}
"""

BICEP_PLAN = """param name string
param location string
param skuName string = 'B2'
param skuCapacity int = 1
param zoneRedundant bool = false
resource plan 'Microsoft.Web/serverfarms@2025-05-01' = {
  name: name
  location: location
  sku: {
    name: skuName
    capacity: skuCapacity
  }
  properties: {
    reserved: true
    zoneRedundant: zoneRedundant
  }
}
"""

AVM_MAIN = """param location string
param enableScalability bool = false
param enableRedundancy bool = false
module webServerFarm './modules/app-service-plan.bicep' = {
  name: 'plan'
  params: {
    location: location
    skuName: enableScalability || enableRedundancy ? 'P1v4' : 'B3'
    skuCapacity: enableScalability ? 3 : 1
    zoneRedundant: enableRedundancy
  }
}
"""

AVM_PLAN = """param location string
param skuName string = 'B2'
param skuCapacity int = 1
param zoneRedundant bool = false
module appServicePlan 'br/public:avm/res/web/serverfarm:0.7.0' = {
  name: 'avm.res.web.serverfarm'
  params: {
    name: 'asp'
    location: location
    skuName: skuName
    skuCapacity: skuCapacity
    zoneRedundant: zoneRedundant
  }
}
"""

PARAMS = {
    "parameters": {
        "deploymentFlavor": {"value": "${AZURE_ENV_DEPLOYMENT_FLAVOR=bicep}"},
        "location": {"value": "${AZURE_LOCATION}"},
        "azureAiServiceLocation": {"value": "${AZURE_ENV_AI_SERVICE_LOCATION}"},
    }
}


def _repo(tmp_path):
    files = {
        "azure.yaml": "name: macae-demo\n",
        "infra/main.bicep": MAIN,
        "infra/main.parameters.json": json.dumps(PARAMS),
        "infra/bicep/main.bicep": BICEP_MAIN,
        "infra/bicep/modules/app-service-plan.bicep": BICEP_PLAN,
        "infra/avm/main.bicep": AVM_MAIN,
        "infra/avm/modules/app-service-plan.bicep": AVM_PLAN,
        "README.md": "# MACAE demo\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


def _plans(repo, **values):
    vals = {k: (v, "test") for k, v in values.items()}
    return find_app_service_plans(repo, "infra/main.bicep", vals, ("location", "azureAiServiceLocation"))["plans"]


def _line(repo, rel, needle):
    lines = (repo / rel).read_text(encoding="utf-8").splitlines()
    return next(i for i, line in enumerate(lines, 1) if needle in line)


# --------------------------------------------------------------------------- SKU extraction


def test_bicep_flavor_gives_b3_from_the_module_param_line(tmp_path):
    repo = _repo(tmp_path)
    [plan] = _plans(repo, deploymentFlavor="bicep")
    assert (plan["sku"], plan["capacity"], plan["zone_redundant"]) == ("B3", 1, False)
    assert plan["where"] == f"infra/bicep/main.bicep:{_line(repo, 'infra/bicep/main.bicep', chr(39) + 'B3' + chr(39))}"
    assert plan["location_param"] == "location"  # the primary location, through a var and two modules
    assert not plan["conditional"] and plan["unresolved"] is None  # the `if (false)` plan is not counted


def test_avm_flavor_defaults_to_b3_and_scalability_or_redundancy_switch_to_p1v4(tmp_path):
    repo = _repo(tmp_path)
    where = f"infra/avm/main.bicep:{_line(repo, 'infra/avm/main.bicep', 'P1v4')}"
    [plan] = _plans(repo, deploymentFlavor="avm")
    assert (plan["sku"], plan["capacity"], plan["zone_redundant"], plan["where"]) == ("B3", 1, False, where)
    [waf] = _plans(repo, deploymentFlavor="avm-waf")  # flavor default turns both switches on
    assert (waf["sku"], waf["capacity"], waf["zone_redundant"]) == ("P1v4", 3, True)
    [scale] = _plans(repo, deploymentFlavor="avm", enableScalability="true")
    assert (scale["sku"], scale["capacity"], scale["zone_redundant"]) == ("P1v4", 3, False)
    [red] = _plans(repo, deploymentFlavor="avm", enableRedundancy="true")
    assert (red["sku"], red["capacity"], red["zone_redundant"]) == ("P1v4", 1, True)


def test_an_unknown_flavor_condition_still_finds_the_plans_and_marks_them_conditional(tmp_path):
    repo = _repo(tmp_path)
    plans = find_app_service_plans(repo, "infra/main.bicep", {}, ("location",))["plans"]
    by_sku = {p["sku"]: p for p in plans}
    assert set(by_sku) == {"B3", None} and all(p["conditional"] for p in plans)  # both flavors may be deployed
    assert "cannot be decided" in by_sku[None]["unresolved"]  # the AVM ternary depends on the unknown flavor


def test_a_sku_that_cannot_be_read_is_reported_not_guessed(tmp_path):
    repo = _repo(tmp_path)
    text = (repo / "infra/bicep/main.bicep").read_text(encoding="utf-8").replace("skuName: 'B3'", "skuName: skus[0]")
    (repo / "infra/bicep/main.bicep").write_text(text, encoding="utf-8")
    [plan] = _plans(repo, deploymentFlavor="bicep")
    assert plan["sku"] is None and plan["unresolved"]


# --------------------------------------------------------------------------- reading the quota


def test_assess_region_per_sku_zone_regional_total_and_unreported():
    needs = {("B3", False): 1}
    eastus2 = {"limits": {"B3": {"limit": 0, "applicable": True}}, "usages": {"B3": -1}}
    r = assess_region(eastus2, needs)
    assert r["status"] == "short" and r["rows"][0]["limit"] == 0 and r["rows"][0]["used"] is None
    assert "current use not reported" in r["rows"][0]["basis"]
    ok = assess_region({"limits": {"B3": {"limit": 27, "applicable": True}}, "usages": {"B3": 2}}, needs)
    assert ok["status"] == "ok" and ok["rows"][0]["free"] == 25
    total = assess_region({"limits": {"*": {"limit": 11, "applicable": False}}, "usages": {"*": 0}}, needs)
    assert total["status"] == "ok" and total["rows"][0]["basis"].startswith("regional total")
    zone = assess_region(
        {"limits": {"P1v4_AZ": {"limit": 2, "applicable": True}, "P1v4": {"limit": 60, "applicable": True}}},
        {("P1v4", True): 3},
    )
    assert zone["status"] == "short" and zone["rows"][0]["sku"] == "P1v4_AZ"  # zone-redundant quota is separate
    assert assess_region({"limits": {}, "usages": {}}, needs)["status"] == "unknown"
    assert assess_region({"error": "AuthorizationFailed"}, needs)["status"] == "unknown"


def test_only_the_two_quota_gets_are_allowed():
    url = f"{WEB}/eastus2/providers/Microsoft.Quota"
    ok = ["rest", "--method", "get", "--url", f"{url}/quotas", "--uri-parameters", "api-version=2023-02-01"]
    assert is_read_only(ok) and is_read_only([*ok[:4], f"{url}/usages", *ok[5:]])
    assert not is_read_only(["rest", "--method", "put", "--url", f"{url}/quotas/B3"])
    assert not is_read_only(["rest", "--method", "get", "--url", f"{url}/quotas", "--body", "{}"])
    assert not is_read_only(
        ["rest", "--method", "get", "--url", url.replace("Microsoft.Web", "Microsoft.Compute") + "/quotas"]
    )


# --------------------------------------------------------------------------- the check, on recorded replies


def _quota_fx(limits):
    """limits: {region: limit for B3 | None (API error)}."""
    cmds = {
        "account show": {"exit_code": 0, "stdout": {"id": SUB, "name": "demo-subscription", "user": {"type": "user"}}}
    }
    for region, limit in limits.items():
        base = f"{WEB}/{region}/providers/Microsoft.Quota"
        q = f"rest --method get --url {base}/quotas --uri-parameters api-version=2023-02-01"
        u = f"rest --method get --url {base}/usages --uri-parameters api-version=2023-02-01"
        if limit is None:
            cmds[q] = {"exit_code": 1, "stderr": "(AuthorizationFailed) no access"}
            continue
        cmds[q] = {
            "exit_code": 0,
            "stdout": {"value": [{"name": "B3", "properties": {"isQuotaApplicable": True, "limit": {"value": limit}}}]},
        }
        cmds[u] = {"exit_code": 0, "stdout": {"value": [{"name": "B3", "properties": {"usages": {"value": 0}}}]}}
    return {"commands": cmds}


def _run(repo, limits, location="eastus2"):
    ts = Troubleshooter(repo, ReplayRunner(_quota_fx(limits)))
    o = {"all_regions": True}
    if location:
        o["location"] = location
    d = ts.diagnose(
        "pre-deployment preflight check before azd up",
        env_file="none",
        log=False,
        overrides=o,
        only_checks=["repo.config_drift", "az.context", "quota.compute"],
    )
    return d, next(c for c in d.checks if c.check_id == "quota.compute")


def test_b3_limit_zero_in_eastus2_fails_and_westus3_is_recommended(tmp_path):
    d, c = _run(_repo(tmp_path), {"eastus2": 0, "westus3": 5})
    assert c.status == "fail" and "B3 needs 1, limit 0" in c.summary and "westus3" in c.summary
    assert c.evidence["regions_with_quota"] == ["westus3"]
    prim = recommend_primary(d.to_dict())
    assert prim["state"] == "short" and prim["location"] == "westus3"
    h = d.hypotheses[0]
    assert h.id == "compute-quota-insufficient" and h.status == "confirmed" and "eastus2" in h.title
    assert d.actions["set-primary-location"].commands == ["azd env set AZURE_LOCATION westus3"]
    assert "request-app-service-quota" in d.actions and "change-app-service-sku" in d.actions


def test_the_chosen_location_with_quota_passes(tmp_path):
    d, c = _run(_repo(tmp_path), {"westus3": 5, "eastus2": 0}, location="westus3")
    assert c.status == "pass" and c.summary.startswith("B3 x1 fits in westus3")
    assert recommend_primary(d.to_dict())["location"] == "westus3"


def test_no_location_chosen_recommends_only_a_location_that_passed(tmp_path):
    d, c = _run(_repo(tmp_path), {"eastus2": 0, "westus3": 5}, location=None)
    assert c.status == "pass" and c.evidence["regions_with_quota"] == ["westus3"]
    prim = recommend_primary(d.to_dict())
    assert prim["state"] == "ok" and prim["location"] == "westus3" and prim["why"] == "most free App Service quota"


def test_an_unreadable_quota_is_skip_never_pass(tmp_path):
    d, c = _run(_repo(tmp_path), {"eastus2": None, "westus3": None})
    assert c.status == "skip" and "App Service plan quota (B3 x1) in eastus2 was not checked" in c.summary


def test_no_app_service_plan_means_not_applicable(tmp_path):
    repo = _repo(tmp_path)
    (repo / "infra/bicep/main.bicep").write_text("param location string\nparam aiLocation string\n", encoding="utf-8")
    _, c = _run(repo, {"eastus2": 0})
    assert c.status == "skip" and c.evidence["not_applicable"] and "nothing to check" in c.summary


def test_profile_compute_skus_are_used_when_the_bicep_has_no_parseable_plan(tmp_path):
    repo = _repo(tmp_path)
    (repo / "infra/bicep/main.bicep").write_text("param location string\nparam aiLocation string\n", encoding="utf-8")
    ts = Troubleshooter(repo, ReplayRunner(_quota_fx({"eastus2": 0, "westus3": 5})))
    pid = next(k for k, p in ts.profiles.items() if p.get("auto_derived"))
    ts.profiles[pid]["compute_skus"] = [{"kind": "appservice", "sku": "B3", "capacity": 1, "where": "declared"}]
    d = ts.diagnose(
        "preflight",
        env_file="none",
        log=False,
        overrides={"location": "eastus2", "all_regions": True},
        only_checks=["repo.config_drift", "az.context", "quota.compute"],
    )
    c = next(c for c in d.checks if c.check_id == "quota.compute")
    assert c.status == "fail" and c.evidence["source"] == "profile"


def test_quota_compute_never_runs_in_a_health_check(synthetic_fixture, synthetic_env_file):
    from conftest import REPO_ROOT

    d = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture)).diagnose(
        "check my deployment health", env_file=synthetic_env_file, log=False, full=True
    )
    assert "quota.compute" not in {c.check_id for c in d.checks}  # deployed plans count as used: only before deploy


# --------------------------------------------------------------------------- render_preflight


def _diag(compute=None, models_ok=True):
    checks = [
        {
            "check_id": "az.context",
            "status": "pass",
            "title": "",
            "summary": "",
            "evidence": {"subscription_name": "demo"},
        },
        {
            "check_id": "quota.models",
            "status": "pass" if models_ok else "fail",
            "title": "",
            "summary": "Sufficient quota",
            "evidence": {
                "regions": {
                    "westus3": {
                        "models": [{"model": "gpt-4o", "capacity": 10, "available": 50, "ok": True, "scope": "Global"}]
                    }
                },
                "regions_with_sufficient_quota": ["westus3"],
                "ai_location_env": "AZURE_ENV_AI_SERVICE_LOCATION",
            },
        },
        {
            "check_id": "repo.azd_env",
            "status": "pass",
            "title": "",
            "summary": "",
            "evidence": {"env_name": "demo", "ai_location": "westus3", "location": "eastus2"},
        },
    ]
    if compute is not None:
        checks.append({"check_id": "quota.compute", "title": "App Service plan quota", **compute})
    return {
        "accelerator": {"id": "macae"},
        "mode": "live",
        "duration_ms": 0,
        "checks": checks,
        "hypotheses": [],
        "actions": {},
    }


PLAN = [
    {
        "sku": "B3",
        "capacity": 1,
        "zone_redundant": False,
        "location_role": "primary",
        "where": "infra/bicep/main.bicep:795",
    }
]
ROWS_SHORT = {
    "status": "short",
    "rows": [{"sku": "B3", "need": 1, "limit": 0, "used": 0, "free": 0, "ok": False, "basis": "per SKU"}],
}
ROWS_OK = {
    "status": "ok",
    "rows": [{"sku": "B3", "need": 1, "limit": 5, "used": 0, "free": 5, "ok": True, "basis": "per SKU"}],
}


def test_render_pass_shows_both_regions_and_a_plain_go():
    ev = {
        "plans": PLAN,
        "target_location": "eastus2",
        "regions": {"eastus2": ROWS_OK},
        "regions_with_quota": ["eastus2"],
        "location_env": "AZURE_LOCATION",
    }
    text = render_preflight(_diag({"status": "pass", "summary": "B3 x1 fits in eastus2", "evidence": ev}))
    assert text.startswith("PREFLIGHT: ✅ GO ·") and "models only" not in text
    assert (
        "AI region (models): westus3" in text and "Primary location (apps, data): eastus2 — your azd env choice" in text
    )
    assert "Compute quota (App Service, live): B3 x1 · eastus2 limit 5, free 5 ✅" in text


def test_render_fail_is_no_go_and_points_at_a_location_with_quota():
    ev = {
        "plans": PLAN,
        "target_location": "eastus2",
        "regions": {"eastus2": ROWS_SHORT, "westus3": ROWS_OK},
        "regions_with_quota": ["westus3"],
        "location_env": "AZURE_LOCATION",
    }
    d = _diag(
        {"status": "fail", "summary": "Not enough App Service quota in eastus2: B3 needs 1, limit 0.", "evidence": ev}
    )
    d["hypotheses"] = [
        {"id": "compute-quota-insufficient", "status": "confirmed", "remediation_ids": ["set-primary-location"]}
    ]
    d["actions"] = {
        "set-primary-location": {
            "id": "set-primary-location",
            "category": "approval_required",
            "title": "x",
            "commands": ["azd env set AZURE_LOCATION westus3"],
            "params": {},
        }
    }
    text = render_preflight(d)
    assert text.startswith("PREFLIGHT: 🛑 NO-GO") and "✅ GO" not in text.splitlines()[0]
    assert "eastus2 (your choice) has no App Service quota for B3 x1; westus3 has it" in text
    assert "eastus2 limit 0, free 0 ❌" in text and "▶ Next: azd env set AZURE_LOCATION westus3" in text


def test_render_skip_never_says_a_plain_go():
    ev = {
        "plans": PLAN,
        "target_location": "eastus2",
        "regions": {"eastus2": {"status": "unknown", "rows": [], "reason": "AuthorizationFailed"}},
    }
    summary = "App Service plan quota (B3 x1) in eastus2 was not checked: AuthorizationFailed."
    text = render_preflight(_diag({"status": "skip", "summary": summary, "evidence": ev}))
    first = text.splitlines()[0]
    assert first.startswith("PREFLIGHT: ✅ GO (models only; App Service quota not checked)")
    assert "App Service quota NOT checked" in text


def test_render_without_a_plan_in_the_template_is_a_plain_go():
    ev = {"plans": [], "not_applicable": True}
    text = render_preflight(_diag({"status": "skip", "summary": "No App Service plan", "evidence": ev}))
    assert text.splitlines()[0].startswith("PREFLIGHT: ✅ GO ·")


# --------------------------------------------------------------------------- the reported error


def test_east_us_2_is_eastus2_not_eastus():
    assert extract("Location: East US 2. Current Limit (B3 VMs): 0").regions == ["eastus2"]
    assert extract("deploy to East US then").regions == ["eastus"]


def test_regression_the_reported_error_is_an_app_service_quota_cause_with_the_numbers(tmp_path):
    repo = _repo(tmp_path)
    from accel_troubleshooter.azcli import OfflineRunner

    d = Troubleshooter(repo, OfflineRunner()).diagnose(REPORTED_ERROR, env_file="none", log=False)
    h = d.hypotheses[0]
    assert h.id == "compute-quota-insufficient" and h.status == "confirmed" and h.confidence == "high"
    assert h.title == "App Service plan quota: B3 is not available in eastus2 (limit 0, needs 1)"
    assert any("B3 limit 0, in use 0, required 1" in r for r in h.rationale)
    assert any("infra/bicep/main.bicep:" in r and "primary location" in r for r in h.rationale)
    assert not any(x.id.startswith("model-") or x.id == "quota-insufficient" for x in d.hypotheses)  # not model quota
    assert (
        list(d.actions)[0] == "find-app-service-location"
        and d.actions["find-app-service-location"].category == "read_only"
    )
    assert d.actions["request-app-service-quota"].category == "restricted"
    assert "antquotahelp" in d.actions["request-app-service-quota"].description


def test_cli_preflight_full_report_has_the_compute_table_and_honest_verdict(tmp_path, capsys, monkeypatch):
    import accel_troubleshooter.cli as cli
    from accel_troubleshooter.cli import main

    repo = _repo(tmp_path)
    fx = copy.deepcopy(_quota_fx({"eastus2": 0, "westus3": 5}))
    monkeypatch.setattr(cli, "_runner", lambda args: ReplayRunner(fx))
    rc = main(
        [
            "--workspace",
            str(repo),
            "preflight",
            "--location",
            "eastus2",
            "--format",
            "full",
            "--no-prompt",
            "--env-file",
            "none",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1 and "## Preflight verdict: 🛑 NO-GO" in out
    assert "## Compute quota (App Service plans, primary location)" in out
    assert "| eastus2 | B3 | 1 | 0 |" in out and "| westus3 | B3 | 1 | 5 |" in out
    assert "Recommended primary location (AZURE_LOCATION): **westus3**" in out
