"""Tool versions, Fabric capacity state, resume advice, guided cleanup and the document gaps file."""

import copy
import json
import re
from pathlib import Path

import pytest

from accel_troubleshooter import cleanup as cleanup_mod
from accel_troubleshooter import resume as resume_mod
from accel_troubleshooter.azcli import CommandNotAllowed, OfflineRunner, ReplayRunner
from accel_troubleshooter.cleanup import build_cleanup_plan, render_cleanup
from accel_troubleshooter.cli import main
from accel_troubleshooter.compact import render_compact
from accel_troubleshooter.engine import Troubleshooter
from accel_troubleshooter.gaps import build_gaps, gaps_path, render_gaps, save_gaps, tool_conflicts
from accel_troubleshooter.resume import build_resume_plan, render_resume
from accel_troubleshooter.toolscan import scan_requirements, version_at_least

from conftest import REPO_ROOT

README = """# Acme

## Prerequisites
- [Azure Developer CLI](https://aka.ms/azd) (`azd`)
- [Python 3.9+](https://www.python.org/downloads/)
- [PowerShell 7+](https://learn.microsoft.com/powershell/) required by the hooks
- **Option 3 - Dev Container**: [Docker Desktop](https://www.docker.com/products/docker-desktop/)

Run `az extension add --name microsoft-fabric` once.
"""
BICEP = "param location string = resourceGroup().location\n"


def _accelerator(tmp_path, extra_docs=None):
    (tmp_path / "infra").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "azure.yaml").write_text(
        'name: acme\nrequiredVersions:\n  azd: ">=1.23.11"\nhooks:\n  postprovision:\n    posix:\n'
        "      run: ./infra/scripts/install_acme.py\n  predown:\n    posix:\n"
        "      run: ./infra/scripts/remove_acme.py\n",
        encoding="utf-8",
    )
    (tmp_path / "infra" / "main.bicep").write_text(BICEP, encoding="utf-8")
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / "docs" / "DeploymentGuide.md").write_text(
        "# Deploy\n\n## Prerequisites\n- Python 3.11 or later\n\n## Environment Cleanup\nRun `azd down --purge`.\n",
        encoding="utf-8",
    )
    (tmp_path / "infra" / "scripts").mkdir()
    (tmp_path / "infra" / "scripts" / "remove_acme.py").write_text("print('removing')\n", encoding="utf-8")
    for name, text in (extra_docs or {}).items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def _tools_fixture(**versions):
    """versions: tool -> "1.2.3" (installed), None (not installed)."""
    cmds = {}
    for tool in ("azd", "az", "python", "node", "pwsh", "docker", "git", "dotnet", "terraform", "kubectl"):
        v = versions.get(tool)
        cmds[f"LOCAL {tool}"] = {"exit_code": 0, "stdout": v} if v else {"exit_code": 1, "stdout": None}
    cmds["extension list"] = {"exit_code": 0, "stdout": versions.get("extensions", [])}
    return {"commands": cmds}


# --------------------------------------------------------------------------- tools and versions


def test_requirements_come_from_azure_yaml_and_the_documents_with_their_source(tmp_path):
    req = scan_requirements(_accelerator(tmp_path))
    tools = req["tools"]
    assert tools["azd"]["min"] == "1.23.11" and tools["azd"]["hard"] and tools["azd"]["sources"][0] == "azure.yaml:3"
    assert tools["python"]["min"] == "3.11" and set(tools["python"]["mins"]) == {"3.9", "3.11"}  # documents disagree
    assert tools["pwsh"]["min"] == "7" and not tools["pwsh"]["optional"]
    assert tools["docker"]["optional"]  # the Dev Container route only
    assert req["extensions"]["microsoft-fabric"][0] == "README.md:9"


def test_version_comparison_handles_different_lengths():
    assert (
        version_at_least("3.11.9", "3.9") and version_at_least("7.4.1", "7") and not version_at_least("1.20", "1.23.11")
    )
    assert version_at_least("1.23.11", "1.23.11") and version_at_least("2.88.0", None)


def _tools_diag(tmp_path, **versions):
    repo = _accelerator(tmp_path)
    ts = Troubleshooter(repo, ReplayRunner(_tools_fixture(**versions)))
    return ts.diagnose("preflight", log=False, only_checks=["repo.tools"])


def _tools_check(d):
    return next(c for c in d.checks if c.check_id == "repo.tools")


def test_everything_installed_and_new_enough_passes(tmp_path):
    d = _tools_diag(
        tmp_path,
        azd="1.34.0",
        az="2.88.0",
        python="3.11.9",
        pwsh="7.6.0",
        docker="29.0.0",
        extensions=[{"name": "microsoft-fabric", "version": "1.0"}],
    )
    c = _tools_check(d)
    assert c.status == "pass" and "match what the documents ask for" in c.summary


def test_azd_below_the_azure_yaml_minimum_is_a_blocker(tmp_path):
    c = _tools_check(_tools_diag(tmp_path, azd="1.20.0", az="2.88.0", python="3.11.9", pwsh="7.6.0"))
    assert c.status == "fail" and "Azure Developer CLI (azd) 1.20.0 is older (docs: >= 1.23.11)" in c.summary


def test_a_missing_documented_tool_or_extension_is_a_warning_not_a_blocker(tmp_path):
    c = _tools_check(_tools_diag(tmp_path, azd="1.34.0", az="2.88.0", python="3.11.9"))  # no pwsh, no extension
    assert c.status == "warn" and "PowerShell 7+ (pwsh) not installed" in c.summary and "microsoft-fabric" in c.summary


def test_a_tool_the_docs_mark_optional_is_only_info(tmp_path):
    ok = {
        "azd": "1.34.0",
        "az": "2.88.0",
        "python": "3.11.9",
        "pwsh": "7.6.0",
        "extensions": [{"name": "microsoft-fabric"}],
    }
    c = _tools_check(_tools_diag(tmp_path, **ok))  # docker missing, but only the Dev Container route needs it
    assert c.status == "info" and "optional" in c.summary


def test_missing_azd_or_az_is_always_a_blocker_even_if_the_docs_say_nothing(tmp_path):
    c = _tools_check(_tools_diag(tmp_path, az="2.88.0", python="3.11.9", pwsh="7.6.0"))
    assert c.status == "fail" and "not installed" in c.summary


def test_unrecorded_or_offline_tool_versions_are_skipped_not_reported_as_missing(tmp_path):
    repo = _accelerator(tmp_path)
    d = Troubleshooter(repo, ReplayRunner({"commands": {}})).diagnose("x", log=False, only_checks=["repo.tools"])
    assert _tools_check(d).status == "skip"
    d = Troubleshooter(repo, OfflineRunner()).diagnose("x", log=False, only_checks=["repo.tools"])
    assert _tools_check(d).status == "skip"


def test_only_known_local_tools_can_be_probed():
    with pytest.raises(CommandNotAllowed):
        ReplayRunner({"commands": {}}).local_version("rm")


def test_preflight_lists_the_tools_as_the_docs_ask_and_the_others_that_are_installed(tmp_path):
    repo = _accelerator(tmp_path)
    fx = _tools_fixture(azd="1.34.0", az="2.88.0", python="3.11.9", pwsh="7.6.0", git="2.55.0", node="24.0.0")
    d = Troubleshooter(repo, ReplayRunner(fx)).diagnose(
        "preflight", log=False, only_checks=["repo.tools", "quota.models"]
    )
    text = render_compact(d.to_dict(), "preflight")
    assert "Tools (as the docs ask):" in text and "azd 1.34.0 (needs >= 1.23.11)" in text
    assert "Also installed: git 2.55.0, node 24.0.0" in text
    assert "extension 'microsoft-fabric' is not installed (docs: README.md:9)" in text


# --------------------------------------------------------------------------- fabric capacity


def _fabric_fixture(synthetic_fixture, state=None, with_capacity=True):
    fx = copy.deepcopy(synthetic_fixture)
    if with_capacity:
        fx["commands"]["resource list --resource-group rg-demo"]["stdout"].append(
            {"name": "fc-demo", "type": "Microsoft.Fabric/capacities", "provisioningState": "Succeeded"}
        )
        fx["commands"][
            "resource show --resource-group rg-demo --name fc-demo --resource-type Microsoft.Fabric/capacities"
        ] = {
            "exit_code": 0,
            "stdout": {
                "name": "fc-demo",
                "location": "westus3",
                "sku": {"name": "F2"},
                "properties": {
                    "state": state,
                    "provisioningState": "Succeeded",
                    "administration": {"members": ["a@b.c"]},
                },
            },
        }
    return fx


def _fabric(fx, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    return ts.diagnose("fabric is not working", env_file=synthetic_env_file, log=False, only_checks=["azure.fabric"])


def test_an_active_fabric_capacity_passes(synthetic_fixture, synthetic_env_file):
    d = _fabric(_fabric_fixture(synthetic_fixture, "Active"), synthetic_env_file)
    c = next(c for c in d.checks if c.check_id == "azure.fabric")
    assert c.status == "pass" and "fc-demo (F2): Active" in c.summary


def test_a_paused_capacity_is_a_confirmed_cause_with_a_resume_command_that_is_never_executed(
    synthetic_fixture, synthetic_env_file
):
    from accel_troubleshooter.actions import EXECUTABLE_ACTIONS

    d = _fabric(_fabric_fixture(synthetic_fixture, "Paused"), synthetic_env_file)
    c = next(c for c in d.checks if c.check_id == "azure.fabric")
    assert c.status == "warn" and "paused" in c.summary and "unavailable until it is resumed" in c.summary
    top = d.hypotheses[0]
    assert top.id == "fabric-capacity-paused" and top.status == "confirmed"
    act = d.actions["resume-fabric-capacity"]
    assert act.category == "approval_required" and "billing" in act.risk.lower()
    assert "az fabric capacity resume --resource-group rg-demo --capacity-name fc-demo" in act.commands[0]
    assert "resume-fabric-capacity" not in EXECUTABLE_ACTIONS  # the agent shows it, you run it


def test_a_suspended_capacity_fails_and_a_changing_one_is_info(synthetic_fixture, synthetic_env_file):
    for state, status in (("Suspended", "fail"), ("Resuming", "info")):
        d = _fabric(_fabric_fixture(synthetic_fixture, state), synthetic_env_file)
        assert next(c for c in d.checks if c.check_id == "azure.fabric").status == status


def test_no_fabric_capacity_means_the_check_is_skipped_quietly(synthetic_fixture, synthetic_env_file):
    d = _fabric(_fabric_fixture(synthetic_fixture, with_capacity=False), synthetic_env_file)
    c = next(c for c in d.checks if c.check_id == "azure.fabric")
    assert c.status == "skip" and "No Fabric capacity" in c.summary


# --------------------------------------------------------------------------- resume


def _diag(checks=None, hyps=None, actions=None, rg="rg-x"):
    return {
        "accelerator": {"name": "Acme"},
        "targets": {"resource_group": rg, "azd_env": "dev"},
        "checks": [{"check_id": k, **v} for k, v in (checks or {}).items()],
        "hypotheses": hyps or [],
        "actions": actions or {},
    }


OK_RG = {"status": "pass", "summary": "exists", "evidence": {}}
OK_RES = {
    "status": "pass",
    "summary": "ok",
    "evidence": {"counts_by_type": {"a": 3}, "not_succeeded": [], "missing_types": []},
}


def _plan(tmp_path=None, **checks):
    repo = _accelerator(tmp_path) if tmp_path else None
    return build_resume_plan(_diag({"rg.state": OK_RG, "resources.state": OK_RES, **checks}), repo)


def test_a_running_deployment_means_wait(tmp_path):
    running = {
        "status": "warn",
        "summary": "running",
        "evidence": {"recent_deployments": [{"name": "main", "state": "Running", "timestamp": "2099-01-01T00:00:00Z"}]},
    }
    plan = _plan(tmp_path, **{"deploy.operations": running})
    assert plan["verdict"] == "wait" and "DeploymentActive" in " ".join(plan["why"])


def test_a_stuck_deployment_gets_a_cancel_command_shown_not_run(tmp_path):
    old = {
        "status": "fail",
        "summary": "stuck",
        "evidence": {"recent_deployments": [{"name": "main", "state": "Running", "timestamp": "2020-01-01T00:00:00Z"}]},
    }
    plan = _plan(tmp_path, **{"deploy.operations": old})
    assert plan["verdict"] == "wait" and any("az deployment group cancel" in n for n in plan["next"])


FAILED_OPS = {
    "status": "fail",
    "summary": "failed",
    "evidence": {
        "recent_deployments": [{"name": "main", "state": "Failed"}],
        "failed_operations": [{"resource_name": "gpt", "code": "InsufficientQuota"}],
    },
}


def test_a_repeatable_cause_means_fix_first_and_names_the_fix(tmp_path):
    hyp = {
        "id": "quota-insufficient",
        "title": "Insufficient quota",
        "status": "confirmed",
        "remediation_ids": ["set-model-capacity"],
    }
    act = {
        "set-model-capacity": {
            "id": "set-model-capacity",
            "title": "Set capacity",
            "category": "approval_required",
            "commands": ["azd env set X 20"],
        }
    }
    plan = build_resume_plan(
        _diag({"rg.state": OK_RG, "resources.state": OK_RES, "deploy.operations": FAILED_OPS}, [hyp], act),
        _accelerator(tmp_path),
    )
    assert plan["verdict"] == "fix-first" and plan["fixes"][0]["commands"] == ["azd env set X 20"]
    assert "same place" in " ".join(plan["why"]) and "azd provision" in plan["next"][-1]


def test_a_failure_without_a_repeatable_cause_is_safe_to_resume_with_azd_provision(tmp_path):
    plan = _plan(tmp_path, **{"deploy.operations": FAILED_OPS})
    assert plan["verdict"] == "resume-infra" and plan["next"][0].startswith("azd provision")
    assert "keeps what exists and creates what is missing" in " ".join(plan["why"])


def test_a_resource_in_failed_state_must_be_deleted_first(tmp_path):
    bad = {
        "status": "fail",
        "summary": "x",
        "evidence": {
            "counts_by_type": {"a": 1},
            "not_succeeded": [{"name": "cosmos", "state": "Failed"}],
            "missing_types": [],
        },
    }
    plan = _plan(tmp_path, **{"resources.state": bad})
    assert plan["verdict"] == "fix-first" and "cosmos is in a Failed state" in " ".join(plan["why"])


def test_missing_rg_or_an_empty_group_has_nothing_to_resume(tmp_path):
    gone = {"status": "fail", "summary": "Resource group 'rg-x' does not exist", "evidence": {}}
    empty = {
        "status": "pass",
        "summary": "",
        "evidence": {"counts_by_type": {}, "not_succeeded": [], "missing_types": []},
    }
    plan = build_resume_plan(_diag({"rg.state": gone, "resources.state": empty}), _accelerator(tmp_path))
    assert plan["verdict"] == "nothing" and "azd up" in plan["next"][0]


def test_unfinished_post_provision_is_safe_to_resume_with_the_documented_hook(tmp_path):
    web = {
        "status": "fail",
        "summary": "x",
        "evidence": {"apps": [], "post_provision_missing": {"api": ["FOUNDRY_CHAT_AGENT"]}},
    }
    plan = _plan(tmp_path, **{"webapp.state": web})
    assert plan["verdict"] == "resume-last" and "azd hooks run postprovision" in " ".join(plan["next"])


def test_a_paused_fabric_capacity_comes_first(tmp_path):
    fab = {
        "status": "warn",
        "summary": "paused",
        "evidence": {"capacities": [{"name": "fc", "resource_group": "rg-x", "state": "Paused"}]},
    }
    plan = _plan(tmp_path, **{"azure.fabric": fab})
    assert plan["verdict"] == "fix-first" and "az fabric capacity resume" in plan["next"][0]


def test_an_unverifiable_post_provision_step_is_never_claimed_finished(tmp_path):
    plan = _plan(tmp_path)  # no app-level checks for this accelerator
    assert "cannot be verified" in " ".join(plan["why"]) and "finished" not in " ".join(plan["why"])
    assert "NOTHING HALTED" in plan["label"] and any("azd hooks run postprovision" in n for n in plan["next"])
    text = render_resume(plan)
    assert "Nothing was run" in text and "install_acme.py" in text


def test_resume_and_cleanup_modules_cannot_run_anything():
    for mod in (resume_mod, cleanup_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "subprocess" not in src and "runner" not in src and "os.system" not in src


# --------------------------------------------------------------------------- cleanup


INV = {
    "status": "warn",
    "summary": "x",
    "evidence": {
        "resources": [
            {
                "name": "aisa-1",
                "type": "Microsoft.CognitiveServices/accounts",
                "location": "westus3",
                "azd_env_tag": "dev",
            },
            {"name": "fc-1", "type": "Microsoft.Fabric/capacities", "location": "westus3", "azd_env_tag": "dev"},
            {"name": "shared-kv", "type": "Microsoft.KeyVault/vaults", "location": "westus3", "azd_env_tag": None},
        ],
        "foreign": [{"name": "shared-kv", "type": "Microsoft.KeyVault/vaults"}],
        "soft_delete": [
            {
                "name": "aisa-1",
                "type": "Microsoft.CognitiveServices/accounts",
                "purge": "az cognitiveservices account purge --location westus3 --resource-group rg-x --name aisa-1",
            }
        ],
        "locks": [{"name": "do-not-delete", "level": "CanNotDelete", "notes": ""}],
    },
}


def test_cleanup_plan_uses_the_accelerators_own_teardown_and_only_shows_commands(tmp_path):
    repo = _accelerator(tmp_path)
    plan = build_cleanup_plan(_diag({"cleanup.inventory": INV}), repo)
    text = render_cleanup(plan)
    assert plan["predown"].endswith("remove_acme.py") and "infra/scripts/remove_acme.py" in plan["scripts"]
    assert any(r["where"].startswith("docs/DeploymentGuide.md") and "azd down" in r["text"] for r in plan["doc_refs"])
    assert "azd down --purge --force --environment dev" in text
    assert "az group delete --name rg-x --yes --no-wait" in text and "az cognitiveservices account purge" in text
    assert "Nothing is deleted by this tool" in text and "cannot be undone" in text
    assert "skips the predown hook" in text  # option B warns what it leaves behind


def test_cleanup_warns_about_locks_foreign_resources_and_fabric_billing(tmp_path):
    plan = build_cleanup_plan(_diag({"cleanup.inventory": INV}), _accelerator(tmp_path))
    joined = " ".join(plan["warnings"])
    assert (
        "do-not-delete (CanNotDelete)" in joined
        and "azd-env-name=dev" in joined
        and "bills while it is Active" in joined
    )
    assert plan["steps"][0]["commands"] == ["az lock delete --name do-not-delete --resource-group rg-x"]


def test_inventory_check_reads_locks_and_does_not_flag_untagged_groups(synthetic_fixture, synthetic_env_file):
    fx = copy.deepcopy(synthetic_fixture)
    fx["commands"]["resource list --resource-group rg-demo"]["stdout"] = [
        {"name": "aisa-1", "type": "Microsoft.CognitiveServices/accounts", "location": "swedencentral", "tags": None},
        {"name": "kv-1", "type": "Microsoft.KeyVault/vaults", "location": "swedencentral", "tags": {}},
    ]
    fx["commands"]["lock list --resource-group rg-demo"] = {"exit_code": 0, "stdout": []}
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    d = ts.diagnose("clean up", env_file=synthetic_env_file, log=False, only_checks=["cleanup.inventory"])
    c = next(c for c in d.checks if c.check_id == "cleanup.inventory")
    assert c.status == "pass" and c.evidence["foreign"] == []  # no resource uses azd-env-name, so no mismatch to report
    assert [s["name"] for s in c.evidence["soft_delete"]] == ["aisa-1", "kv-1"]
    assert "--name aisa-1" in c.evidence["soft_delete"][0]["purge"]
    fx["commands"]["lock list --resource-group rg-demo"]["stdout"] = [{"name": "l", "level": "ReadOnly"}]
    d = Troubleshooter(REPO_ROOT, ReplayRunner(fx)).diagnose(
        "x", env_file=synthetic_env_file, log=False, only_checks=["cleanup.inventory"]
    )
    assert next(c for c in d.checks if c.check_id == "cleanup.inventory").status == "warn"


def test_cleanup_and_inventory_checks_never_run_in_a_health_check(synthetic_fixture, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    d = ts.diagnose("check my deployment health", env_file=synthetic_env_file, log=False, full=True)
    ids = {c.check_id for c in d.checks}
    assert "cleanup.inventory" not in ids and "repo.tools" not in ids


def test_resume_and_cleanup_commands_run_end_to_end_on_recorded_azure_replies(
    capsys, synthetic_env_file, synthetic_fixture, tmp_path
):
    fx = copy.deepcopy(synthetic_fixture)
    fx["commands"]["lock list --resource-group rg-demo"] = {"exit_code": 0, "stdout": []}
    path = tmp_path / "fx.json"
    path.write_text(json.dumps(fx), encoding="utf-8")
    base = ["--workspace", str(REPO_ROOT)]
    tail = ["--replay", str(path), "--env-file", synthetic_env_file, "--no-prompt"]
    assert main(base + ["cleanup"] + tail) == 0
    out = capsys.readouterr().out
    assert "CLEANUP PLAN" in out and "azd down --purge --force" in out and "Nothing is deleted by this tool" in out
    main(base + ["resume"] + tail)
    out = capsys.readouterr().out
    assert "RESUME" in out and re.search(r"VERDICT\s+\S* ?(NOTHING|SAFE|FIX|WAIT)", out)


# --------------------------------------------------------------------------- document gaps file


def test_gaps_are_collected_from_drift_coverage_and_conflicting_tool_versions(tmp_path):
    req = scan_requirements(_accelerator(tmp_path))
    drift = {
        "findings": [
            {"impact": "high", "where": "docs/a.md:3", "file": "docs/a.md", "detail": "wrong", "fix": "fix it"}
        ],
        "accelerator": "Acme",
        "commit": "abc1234",
        "branch": "main",
    }
    cov = {
        "categories": {"auth": {"present": [], "missing": ["docs/auth.md"]}},
        "troubleshooting_docs": [],
        "readme": {"prerequisites_section": None},
        "broken_local_links": [{"file": "README.md", "line": 4, "target": "x.md"}],
    }
    g = build_gaps(drift, cov, req)
    text = render_gaps(g)
    assert tool_conflicts(req)[0]["tool"] == "Python"
    for needle in (
        "docs/a.md:3",
        "different tool versions",
        "3.9 in",
        "No troubleshooting guide was found",
        "`docs/auth.md` (auth)",
        "`README.md:4` → `x.md`",
        "README.md has no prerequisites section",
    ):
        assert needle in text, needle


def test_drift_saves_the_gaps_file_locally_and_keeps_json_output_valid(capsys, tmp_path):
    out = tmp_path / "runs" / "runs.jsonl"
    import accel_troubleshooter.telemetry as telemetry

    old = telemetry.RUNS_FILE
    telemetry.RUNS_FILE = out
    try:
        assert gaps_path() == tmp_path / "runs" / "document-gaps.md"
        main(["--workspace", str(REPO_ROOT), "drift", "--format", "json"])
        shown = capsys.readouterr()
        json.loads(shown.out)  # still valid JSON: the 'saved to' notice went to stderr
        assert "document gaps" in shown.err and gaps_path().exists()
        assert gaps_path().read_text(encoding="utf-8").startswith("# Document gaps")
        gaps_path().unlink()
        main(["--workspace", str(REPO_ROOT), "drift", "--no-save"])
        assert not gaps_path().exists()
        assert save_gaps("# x") == gaps_path()
    finally:
        telemetry.RUNS_FILE = old


# --------------------------------------------------------------------------- chat layer


def test_the_new_skills_only_show_commands_and_the_agent_forbids_running_destructive_ones():
    skills = REPO_ROOT / ".github" / "skills"
    cleanup = (skills / "cleanup-resources" / "SKILL.md").read_text(encoding="utf-8")
    assert "Never run `azd down`" in cleanup and "only **show** commands" in cleanup and "`cleanup_plan`" in cleanup
    resume = (skills / "resume-deployment" / "SKILL.md").read_text(encoding="utf-8")
    assert "Never run `azd`" in resume and "`resume_plan`" in resume
    agent = (REPO_ROOT / ".github" / "agents" / "accelerator-troubleshooter.agent.md").read_text(encoding="utf-8")
    assert "never run `azd down`" in agent and "document-gaps.md" in agent
    for t in ("**[Repo docs]**", "**[Live Azure]**"):
        assert t in cleanup and t in resume


def test_install_copies_every_skill_including_the_new_ones():
    from accel_troubleshooter.install import SKILL_NAMES

    on_disk = {p.parent.name for p in (REPO_ROOT / ".github" / "skills").glob("*/SKILL.md")}
    assert set(SKILL_NAMES) == on_disk and {"resume-deployment", "cleanup-resources"} <= on_disk


def test_cleanup_for_a_group_that_is_already_gone_only_checks_for_leftovers(tmp_path):
    skipped = {"status": "skip", "summary": "Resource group does not exist.", "evidence": {}}
    plan = build_cleanup_plan(_diag({"cleanup.inventory": skipped}), _accelerator(tmp_path))
    cmds = [c for s in plan["steps"] for c in s["commands"]]
    assert "nothing to clean up" in plan["warnings"][0]
    assert not any("azd down" in c or "az group delete" in c for c in cmds)
    assert "az cognitiveservices account list-deleted" in cmds
