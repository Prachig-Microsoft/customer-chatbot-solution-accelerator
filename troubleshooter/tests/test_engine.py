import json

from accel_troubleshooter.azcli import ReplayRunner
from accel_troubleshooter.cli import main
from accel_troubleshooter.engine import Troubleshooter
from accel_troubleshooter.evaluation import run_eval
from accel_troubleshooter.report import render_markdown

from conftest import FIXTURES, REPO_ROOT

SECRET = "fake-key-should-never-be-printed"


def test_eval_suite_passes():
    result = run_eval()
    assert result["passed"] == result["total"], json.dumps(result["failed"], indent=2)


def test_healthy_environment_produces_no_root_cause(synthetic_fixture, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    d = ts.diagnose("the endpoint is returning errors", env_file=synthetic_env_file, log=False)
    assert not [h for h in d.hypotheses if h.status in ("confirmed", "likely")]
    assert any("logs" in m.lower() for m in d.missing_evidence)


def test_secrets_never_leave_the_tool(synthetic_fixture, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    d = ts.diagnose("chat returns 500 errors", env_file=synthetic_env_file, full=True, log=False)
    dumped = json.dumps(d.to_dict(), default=str) + render_markdown(d.to_dict(), verbose=True)
    assert SECRET not in dumped
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" in dumped  # names are fine


def test_every_check_runs_without_error_on_synthetic_env(synthetic_fixture, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    d = ts.diagnose("full health check", env_file=synthetic_env_file, full=True, log=False)
    errors = {c.check_id: c.summary for c in d.checks if c.status == "error"}
    assert not errors, errors
    assert {c.check_id for c in d.checks} >= {"webapp.state", "rbac.app_identity", "quota.models", "deploy.operations"}


def test_what_if_capacity_drives_quota_and_drift(synthetic_fixture, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    d = ts.diagnose(
        "quota check",
        env_file=synthetic_env_file,
        log=False,
        overrides={"capacity_overrides": {"gptDeploymentCapacity": 950}},
    )
    top = d.hypotheses[0]
    assert top.id == "quota-insufficient" and top.status == "likely" and top.confidence == "high"
    assert any("Global pool" in r for r in top.rationale)
    assert any(a.params.get("capacity") == 900 for a in d.actions.values())
    drift = next(c for c in d.checks if c.check_id == "repo.config_drift")
    assert any(f["id"] == "capacity-exceeds-azd-usage-metadata" for f in drift.evidence["findings"])
    # Global-scope shortfall: switching region would not help, so no region change is proposed.
    assert not any(k.startswith("set-ai-region") for k in d.actions)


def test_local_vs_cloud_gap_in_non_git_workspace(chatbot_copy, synthetic_fixture, synthetic_env_file):
    (chatbot_copy / "chat-app" / "backend" / ".env").write_text(
        "AZURE_FOUNDRY_ENDPOINT=x\nCOSMOS_DB_ENDPOINT=y\nFOUNDRY_CHAT_AGENT=a\n", encoding="utf-8"
    )
    synthetic_fixture["commands"]["webapp config appsettings list --resource-group rg-demo --name api-chat-demo"][
        "stdout"
    ] = [
        s
        for s in synthetic_fixture["commands"][
            "webapp config appsettings list --resource-group rg-demo --name api-chat-demo"
        ]["stdout"]
        if s["name"] != "COSMOS_DB_ENDPOINT"
    ]
    ts = Troubleshooter(chatbot_copy, ReplayRunner(synthetic_fixture))
    d = ts.diagnose("works locally but fails after deployment to Azure", env_file=synthetic_env_file, log=False)
    ids = [h.id for h in d.hypotheses]
    assert "local-vs-cloud-config" in ids and "app-settings-missing" in ids
    assert d.accelerator["git_commit"] == ""  # non-git copy still identified via azure.yaml + markers


def test_verify_reports_before_and_after(synthetic_fixture, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    before = ts.diagnose(
        "quota",
        env_file=synthetic_env_file,
        log=False,
        overrides={"capacity_overrides": {"gptDeploymentCapacity": 950}},
    )
    action = next(a for a in before.actions.values() if a.id.startswith("set-model-capacity"))
    after = ts.verify(
        before,
        action,
        env_file=synthetic_env_file,
        overrides={"capacity_overrides": {"gptDeploymentCapacity": action.params["capacity"]}},
    )
    row = next(r for r in after["checks"] if r["check"] == "quota.models")
    assert row["before"] == "fail" and row["after"] == "pass"


def test_partial_profile_reports_limitation():
    ts = Troubleshooter(REPO_ROOT, ReplayRunner({"commands": {}}))
    d = ts.diagnose("content processing deployment failed with InsufficientQuota", log=False, env_file="none")
    assert d.accelerator["id"] == "content-processing"
    assert any("not yet implemented" in x for x in d.limitations)
    assert all("docs/" in r or r.startswith("infra/") for h in d.hypotheses for r in h.doc_refs if r)


def test_cli_diagnose_json_and_exit_codes(capsys, synthetic_env_file):
    rc = main(
        [
            "diagnose",
            "chat returns 500 after deployment",
            "--replay",
            str(FIXTURES / "synthetic-base.json"),
            "--env-file",
            synthetic_env_file,
            "--format",
            "json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["accelerator"]["id"] == "customer-chatbot"
    rc = main(["--workspace", str(FIXTURES), "identify", "it is broken"])
    assert rc == 2


def test_cli_search_cites_exact_row(capsys):
    assert (
        main(["search", "InsufficientQuota", "--accelerator", "customer-chatbot", "-k", "1", "--format", "json"]) == 0
    )
    hit = json.loads(capsys.readouterr().out)[0]
    assert hit["path"] == "documents/TroubleShootingSteps.md" and "InsufficientQuota" in hit["error_codes"]


def test_cli_fix_refuses_restricted_and_unapproved(capsys, synthetic_env_file):
    args = [
        "fix",
        "{}",
        "--issue",
        "quota check",
        "--replay",
        str(FIXTURES / "synthetic-base.json"),
        "--env-file",
        synthetic_env_file,
        "--capacity",
        "gptDeploymentCapacity=950",
    ]
    assert main([a.replace("{}", "request-quota-increase") for a in args]) == 1
    assert "Restricted" in capsys.readouterr().out
    assert main([a.replace("{}", "set-model-capacity") for a in args]) == 1
    assert "Approval required" in capsys.readouterr().out
