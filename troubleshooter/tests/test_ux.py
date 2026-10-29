import copy
import io
import json
import sys

import pytest

from accel_troubleshooter import runcmd
from accel_troubleshooter.azcli import CommandNotAllowed, ReplayRunner
from accel_troubleshooter.cli import main
from accel_troubleshooter.classify import extract
from accel_troubleshooter.compact import render_compact
from accel_troubleshooter.engine import Troubleshooter
from accel_troubleshooter.textutil import build_issue, clean_terminal_text, condense_log, read_multiline

from conftest import FIXTURES, REPO_ROOT, TOOL_ROOT

AZD_ERROR = (
    "\x1b[31mERROR\x1b[0m: error executing step command 'provision': deployment failed\n"
    "Deployment Error Details:\n"
    "InsufficientQuota: This operation require 50 new capacity in quota Tokens Per Minute (thousands) - "
    "gpt-5.4-mini, which is bigger than the current available capacity 10.\n"
    "Update available: 1.34.0 -> 1.37.2\n"
)


# ----------------------------------------------------------------------------- resource group resolution


def _env_file(tmp_path, name="demo", rg=None):
    f = tmp_path / f"{name}.env"
    lines = ['AZURE_SUBSCRIPTION_ID="00000000-0000-0000-0000-00000000aaaa"', 'AZURE_LOCATION="australiaeast"']
    if rg:
        lines.append(f'AZURE_RESOURCE_GROUP="{rg}"')
    f.write_text("\n".join(lines), encoding="utf-8")
    return str(f)


def _fixture(synthetic_fixture, **cmds):
    fx = copy.deepcopy(synthetic_fixture)
    fx["commands"].update(cmds)
    return fx


def _rg_target(synthetic_fixture, tmp_path, prompt=None, rg=None, **cmds):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(_fixture(synthetic_fixture, **cmds)))
    d = ts.diagnose(
        "check my deployment",
        env_file=_env_file(tmp_path, rg=rg),
        only_checks=["rg.state"],
        log=False,
        prompt_rg=prompt,
    )
    return d


def test_rg_comes_from_azd_env_without_any_prompt(synthetic_fixture, tmp_path):
    def boom():
        raise AssertionError("must not prompt when the azd env has a resource group")

    d = _rg_target(synthetic_fixture, tmp_path, prompt=boom, rg="rg-demo")
    assert d.targets["resource_group"] == "rg-demo" and "azd env" in d.targets["resource_group_source"]


def test_rg_falls_back_to_azd_naming_convention_when_it_exists(synthetic_fixture, tmp_path):
    d = _rg_target(synthetic_fixture, tmp_path, **{"group exists --name rg-demo": {"exit_code": 0, "stdout": True}})
    assert d.targets["resource_group"] == "rg-demo" and "convention" in d.targets["resource_group_source"]


def test_rg_falls_back_to_azd_env_tag(synthetic_fixture, tmp_path):
    base = synthetic_fixture["commands"]["group show --name rg-demo"]
    d = _rg_target(
        synthetic_fixture,
        tmp_path,
        **{
            "group exists --name rg-demo": {"exit_code": 0, "stdout": False},
            "group list": {"exit_code": 0, "stdout": [{"name": "rg-custom", "tags": {"azd-env-name": "demo"}}]},
            "group show --name rg-custom": base,
        },
    )
    assert d.targets["resource_group"] == "rg-custom" and "azd-env-name" in d.targets["resource_group_source"]


def test_rg_prompts_only_when_unresolved_and_uses_the_answer(synthetic_fixture, tmp_path):
    asked = []

    def prompt():
        asked.append(1)
        return "rg-demo"

    d = _rg_target(
        synthetic_fixture,
        tmp_path,
        prompt=prompt,
        **{
            "group exists --name rg-demo": {"exit_code": 0, "stdout": False},
            "group list": {"exit_code": 0, "stdout": []},
        },
    )
    assert asked == [1]
    assert d.targets["resource_group"] == "rg-demo" and d.targets["resource_group_source"] == "entered by you"


def test_rg_unresolved_without_prompt_is_reported_not_guessed(synthetic_fixture, tmp_path):
    d = _rg_target(
        synthetic_fixture,
        tmp_path,
        **{
            "group exists --name rg-demo": {"exit_code": 0, "stdout": False},
            "group list": {"exit_code": 0, "stdout": []},
        },
    )
    assert d.targets["resource_group"] is None
    assert next(c for c in d.checks if c.check_id == "rg.state").status == "skip"
    assert any("Resource group" in m for m in d.missing_evidence)


def test_empty_prompt_answer_skips_the_resource_checks(synthetic_fixture, tmp_path):
    d = _rg_target(
        synthetic_fixture,
        tmp_path,
        prompt=lambda: "  ",
        **{
            "group exists --name rg-demo": {"exit_code": 0, "stdout": False},
            "group list": {"exit_code": 0, "stdout": []},
        },
    )
    assert d.targets["resource_group"] is None


def _now_iso(minutes_ago: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _partial_fixture(synthetic_fixture, state, minutes_ago):
    return _fixture(
        synthetic_fixture,
        **{
            "resource list --resource-group rg-demo": {
                "exit_code": 0,
                "stdout": [
                    {
                        "name": "aif-demo",
                        "type": "Microsoft.CognitiveServices/accounts",
                        "provisioningState": "Succeeded",
                    }
                ],
            },
            "deployment group list --resource-group rg-demo": {
                "exit_code": 0,
                "stdout": [
                    {"name": "module.x", "properties": {"provisioningState": state, "timestamp": _now_iso(minutes_ago)}}
                ],
            },
        },
    )


def test_running_or_just_finished_deployment_is_in_progress_not_partial(synthetic_fixture, synthetic_env_file):
    for state, minutes in (("Running", 1), ("Succeeded", 1)):  # second case = gap between ARM phases
        ts = Troubleshooter(REPO_ROOT, ReplayRunner(_partial_fixture(synthetic_fixture, state, minutes)))
        d = ts.diagnose(
            "azd up status",
            env_file=synthetic_env_file,
            only_checks=["rg.state", "deploy.operations", "resources.state"],
            log=False,
        )
        assert d.hypotheses[0].id == "deployment-in-progress", (state, [h.id for h in d.hypotheses])
        assert not any(h.id == "partial-deployment" for h in d.hypotheses)
        assert not any(a.id.startswith("azd-provision") for a in d.actions.values())


def test_old_missing_resources_are_still_reported_as_partial(synthetic_fixture, synthetic_env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(_partial_fixture(synthetic_fixture, "Succeeded", 120)))
    d = ts.diagnose(
        "azd up status",
        env_file=synthetic_env_file,
        only_checks=["rg.state", "deploy.operations", "resources.state"],
        log=False,
    )
    assert d.hypotheses[0].id == "partial-deployment"


def test_arm_quota_failure_uses_the_numbers_in_azures_own_error(synthetic_fixture, synthetic_env_file):
    """Real failure captured live: configured capacity (50K) fits today, but the failed request asked for 5000."""
    msg = (
        "Not enough quota is available to create this deployment. The deployment requires 5000 quota units, but only "
        "338 quota units are available in One Thousand Tokens Per Minute - gpt-5.4-mini - GlobalStandard. "
        "Current usage: 662 / 1000 quota units."
    )
    fx = _fixture(
        synthetic_fixture,
        **{
            "deployment group list --resource-group rg-demo": {
                "exit_code": 0,
                "stdout": [
                    {"name": "ts-quota-test", "properties": {"provisioningState": "Failed", "timestamp": _now_iso(200)}}
                ],
            },
            "deployment operation group list --resource-group rg-demo --name ts-quota-test": {
                "exit_code": 0,
                "stdout": [
                    {
                        "properties": {
                            "provisioningState": "Failed",
                            "timestamp": _now_iso(200),
                            "targetResource": {
                                "resourceType": "Microsoft.CognitiveServices/accounts/deployments",
                                "resourceName": "aif-demo/troubleshooter-quota-test",
                            },
                            "statusMessage": {"error": {"code": "InsufficientQuota", "message": msg}},
                        }
                    }
                ],
            },
        },
    )
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    d = ts.diagnose("my deployment failed", env_file=synthetic_env_file, log=False)
    top = d.hypotheses[0]
    assert top.id == "quota-insufficient" and top.status == "confirmed" and top.confidence == "high"
    text = " ".join(top.rationale)
    assert "requested 5000, available 338" in text and "differs from the configured capacity (50K)" in text
    assert not top.contradicting
    assert not any("Exact error code" in m for m in d.missing_evidence)
    assert not any("Approximate time" in m for m in d.missing_evidence)
    assert "RESULT" not in render_compact(d.to_dict()) and "LIKELY CAUSE" in render_compact(d.to_dict())


def test_policy_enforced_public_access_is_confirmed_on_non_waf_and_waf_is_only_an_alternative(
    synthetic_fixture, synthetic_env_file
):
    """Real case captured live: Cosmos forced to public-access Disabled by an MCAPS 'modify' policy."""
    cosmos = {
        "name": "cosmos-demo",
        "id": "/subscriptions/00000000-0000-0000-0000-00000000aaaa/resourceGroups/rg-demo/providers/"
        "Microsoft.DocumentDB/databaseAccounts/cosmos-demo",
        "publicNetworkAccess": "Disabled",
    }
    fx = _fixture(
        synthetic_fixture,
        **{
            "cosmosdb show --resource-group rg-demo --name cosmos-demo": {"exit_code": 0, "stdout": cosmos},
            "policy state list --resource *": {
                "exit_code": 0,
                "stdout": [
                    {
                        "policyDefinitionAction": "audit",
                        "policyAssignmentName": "Baseline",
                        "policyDefinitionName": "d1",
                    },
                    {
                        "policyDefinitionAction": "modify",
                        "policyAssignmentName": "GovDeploy",
                        "policyDefinitionName": "CosmosDB_PublicNetwork_Modify",
                        "policyAssignmentScope": "/providers/Microsoft.Management/managementGroups/mg1",
                    },
                ],
            },
        },
    )
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    d = ts.diagnose("the chat does not work", env_file=synthetic_env_file, full=True, log=False)
    h = next(x for x in d.hypotheses if x.id == "public-access-disabled")
    assert h.status == "confirmed" and h.confidence == "high" and "non-WAF" in h.title
    text = " ".join(h.rationale)
    assert "CosmosDB_PublicNetwork_Modify" in text and "management group" in text
    assert "deployment flavor is 'bicep' (non-WAF)" in text and "reverted by the policy" in text
    ids = [k.split("#")[0] for k in d.actions]
    assert "request-policy-exemption" in ids and "use-waf-deployment" in ids
    assert "enable-public-network-access" not in ids  # re-enabling is pointless when a modify policy reverts it
    assert d.actions["use-waf-deployment"].category == "manual_review"
    assert d.actions["request-policy-exemption"].category == "restricted"


def test_public_access_off_without_a_found_policy_stays_likely_not_confirmed(synthetic_fixture, synthetic_env_file):
    fx = _fixture(
        synthetic_fixture,
        **{
            "cosmosdb show --resource-group rg-demo --name cosmos-demo": {
                "exit_code": 0,
                "stdout": {"name": "cosmos-demo", "id": "/x/cosmos-demo", "publicNetworkAccess": "Disabled"},
            }
        },
    )
    d = Troubleshooter(REPO_ROOT, ReplayRunner(fx)).diagnose(
        "chat fails", env_file=synthetic_env_file, full=True, log=False
    )
    h = next(x for x in d.hypotheses if x.id == "public-access-disabled")
    assert h.status == "likely" and h.contradicting


def test_stopped_app_is_confirmed_and_start_is_an_approved_executable_action(synthetic_fixture, synthetic_env_file):
    fx = _fixture(synthetic_fixture)
    fx["commands"]["webapp show --resource-group rg-demo --name api-chat-demo"]["stdout"]["state"] = "Stopped"
    fx["commands"]["HTTP GET https://api-chat-demo.azurewebsites.net/health"] = {
        "exit_code": 403,
        "stdout": {"status": 403, "body_excerpt": "Site Disabled", "error": ""},
    }
    d = Troubleshooter(REPO_ROOT, ReplayRunner(fx)).diagnose(
        "chat is not responding", env_file=synthetic_env_file, full=True, log=False
    )
    top = d.hypotheses[0]
    assert top.id == "app-stopped" and top.status == "confirmed"
    act = d.actions["start-webapp"]
    from accel_troubleshooter.actions import EXECUTABLE_ACTIONS

    assert act.category == "approval_required" and "start-webapp" in EXECUTABLE_ACTIONS
    assert act.commands == ["az webapp start --resource-group rg-demo --name api-chat-demo"]


def test_empty_resource_group_is_not_called_a_failure(synthetic_fixture, synthetic_env_file):
    fx = _fixture(
        synthetic_fixture,
        **{
            "resource list --resource-group rg-demo": {"exit_code": 0, "stdout": []},
            "deployment group list --resource-group rg-demo": {"exit_code": 0, "stdout": []},
        },
    )
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    d = ts.diagnose(
        "azd up in progress",
        env_file=synthetic_env_file,
        only_checks=["rg.state", "deploy.operations", "resources.state"],
        log=False,
    )
    top = d.hypotheses[0]
    assert top.id == "resource-group-empty" and top.confidence == "medium" and top.contradicting


# ----------------------------------------------------------------------------- compact output


def _diag(synthetic_fixture, synthetic_env_file, text, **kw):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    return ts.diagnose(text, env_file=synthetic_env_file, log=False, **kw)


PREFLIGHT_CHECKS = ["repo.azd_env", "repo.config_drift", "az.context", "quota.models", "models.availability"]


def _preflight(fixture, env_file, **kw):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fixture))
    d = ts.diagnose(
        "pre-deployment preflight check before azd up",
        env_file=env_file,
        log=False,
        overrides={"all_regions": True},
        only_checks=PREFLIGHT_CHECKS,
        **kw,
    )
    return render_compact(d.to_dict(), "preflight"), d


def test_preflight_go_is_a_short_answer_with_a_recommended_region(synthetic_fixture, synthetic_env_file):
    text, _ = _preflight(synthetic_fixture, synthetic_env_file)
    lines = text.splitlines()
    assert len(lines) <= 14, text
    assert lines[0].startswith("PREFLIGHT: ✅ GO")
    assert "AI region         eastus2   (your azd env choice, quota OK)" in text
    assert "Quota (K TPM)" in text and "need 50/free 900" in text
    assert "Blocking          none" in text and "Other OK regions" in text
    assert "run -- azd up" in text and "azd env set" not in text  # region already chosen: nothing to set


def test_preflight_recommends_best_region_when_none_chosen(synthetic_fixture, tmp_path):
    env = tmp_path / "demo.env"
    env.write_text('AZURE_SUBSCRIPTION_ID="00000000-0000-0000-0000-00000000aaaa"\n', encoding="utf-8")
    text, _ = _preflight(synthetic_fixture, str(env))
    assert "(best free quota" in text
    assert "azd env set AZURE_ENV_AI_SERVICE_LOCATION " in text and "run -- azd up" in text


def test_preflight_no_go_lists_blockers_and_does_not_tell_you_to_deploy(synthetic_fixture, synthetic_env_file):
    fx = _fixture(
        synthetic_fixture,
        **{
            "cognitiveservices usage list --location *": {
                "exit_code": 0,
                "stdout": [
                    {
                        "name": {"value": "OpenAI.GlobalStandard.gpt-5.4-mini"},
                        "currentValue": 990.0,
                        "limit": 1000.0,
                        "scopeType": "Global",
                    },
                    {
                        "name": {"value": "OpenAI.GlobalStandard.text-embedding-3-small"},
                        "currentValue": 0.0,
                        "limit": 3000.0,
                        "scopeType": "Global",
                    },
                    {
                        "name": {"value": "OpenAI.GlobalStandard.gpt-realtime-mini"},
                        "currentValue": 0.0,
                        "limit": 20.0,
                        "scopeType": "Classic",
                    },
                ],
            }
        },
    )
    text, _ = _preflight(fx, synthetic_env_file)
    assert text.splitlines()[0].startswith("PREFLIGHT: 🛑 NO-GO")
    assert "Blocking\n  ✗ model quota" in text and "need 50, available 10" in text
    assert "run -- azd up" not in text and "fix the blockers" in text


def test_preflight_plain_mode_has_no_emoji(synthetic_fixture, synthetic_env_file):
    d = _preflight(synthetic_fixture, synthetic_env_file)[1]
    text = render_compact(d.to_dict(), "preflight", plain=True)
    assert text.startswith("PREFLIGHT: GO") and "✅" not in text and "[ OK ]" in text


def test_compact_diagnose_leads_with_cause_evidence_and_next_step(synthetic_fixture, synthetic_env_file):
    d = _diag(
        synthetic_fixture,
        synthetic_env_file,
        "quota check",
        overrides={"capacity_overrides": {"gptDeploymentCapacity": 950}},
    )
    text = render_compact(d.to_dict())
    assert text.splitlines()[3].startswith("LIKELY CAUSE") or "LIKELY CAUSE" in text
    assert "FINDINGS" in text and "NEXT" in text and "set-model-capacity" in text
    assert (
        len(text.splitlines())
        < len(__import__("accel_troubleshooter.report", fromlist=["x"]).render_markdown(d.to_dict()).splitlines()) / 2
    )


def test_compact_plain_mode_has_no_emoji(synthetic_fixture, synthetic_env_file):
    d = _diag(synthetic_fixture, synthetic_env_file, "chat returns 500 errors")
    text = render_compact(d.to_dict(), plain=True)
    assert "[ OK ]" in text and "✅" not in text


def test_cli_default_is_compact_and_out_saves_full_report(capsys, tmp_path, synthetic_env_file):
    out = tmp_path / "r.md"
    rc = main(
        [
            "diagnose",
            "chat returns 500 errors",
            "--replay",
            str(FIXTURES / "synthetic-base.json"),
            "--env-file",
            synthetic_env_file,
            "--out",
            str(out),
        ]
    )
    shown = capsys.readouterr().out
    assert rc == 0 and "RESULT" in shown and "Full report saved" in shown
    assert "## 4. Diagnostic checks performed" in out.read_text(encoding="utf-8")
    assert "## 4. Diagnostic checks performed" not in shown


# ----------------------------------------------------------------------------- pasted errors


def test_clean_terminal_text_strips_ansi_and_banner():
    out = clean_terminal_text(AZD_ERROR)
    assert "\x1b" not in out and "Update available" not in out and "InsufficientQuota" in out


def test_condense_log_keeps_error_lines_of_a_long_log():
    noise = "\n".join(f"step {i}: working" for i in range(500))
    out = condense_log(noise + "\nERROR: (ResourceGroupNotFound) Resource group 'rg-x' could not be found.\n" + noise)
    assert "ResourceGroupNotFound" in out and len(out.splitlines()) <= 41


def test_extract_codes_from_raw_azd_and_az_output():
    assert "InsufficientQuota" in extract(clean_terminal_text(AZD_ERROR)).error_codes
    assert (
        "ResourceGroupNotFound"
        in extract("ERROR: (ResourceGroupNotFound) Resource group 'rg-x' could not be found.").error_codes
    )
    assert "InvalidTemplate" in extract('{"error": {"code": "InvalidTemplate", "message": "x"}}').error_codes
    assert "Global" not in extract("quota (Global) is shared").error_codes


def test_read_multiline_stops_after_two_blank_lines_and_keeps_inner_blank():
    lines = iter(["line one\n", "\n", "line two\n", "\n", "\n", "ignored\n"])
    text = read_multiline(reader=lambda: next(lines, ""), out=io.StringIO())
    assert text == "line one\n\nline two"


def test_build_issue_reads_stdin_log_and_last(tmp_path):
    log = tmp_path / "x.log"
    log.write_text(AZD_ERROR, encoding="utf-8")
    assert "InsufficientQuota" in build_issue(None, log_file=str(log))
    assert "InsufficientQuota" in build_issue("-", stdin_text=AZD_ERROR)
    assert "InsufficientQuota" in build_issue(None, last_log=log)


def test_cli_diagnose_reads_error_from_stdin(monkeypatch, capsys, synthetic_env_file):
    monkeypatch.setattr(sys, "stdin", io.StringIO(AZD_ERROR))
    rc = main(
        [
            "diagnose",
            "-",
            "--replay",
            str(FIXTURES / "synthetic-base.json"),
            "--env-file",
            synthetic_env_file,
            "--format",
            "json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert "InsufficientQuota" in out["issue"]["error_codes"] and rc in (0, 1)


def test_cli_diagnose_without_text_and_without_terminal_explains(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["diagnose", "--offline"]) == 3
    assert "--from-clipboard" in capsys.readouterr().err


# ----------------------------------------------------------------------------- run wrapper


def test_run_only_allows_known_clis_and_scripts_inside_the_repo(tmp_path):
    for bad in (["python", "-c", "1"], ["rm", "-rf", "/"], ["curl", "x"], []):
        with pytest.raises(CommandNotAllowed):
            runcmd.resolve_command(bad, REPO_ROOT)
    with pytest.raises(CommandNotAllowed):
        runcmd.resolve_command([str(tmp_path / "evil.ps1")], REPO_ROOT)
    ok = runcmd.resolve_command([r"infra\scripts\post-provision\build_push_images.ps1", "-ShowLogs"], REPO_ROOT)
    assert "-File" in ok and ok[-1] == "-ShowLogs"
    assert runcmd.resolve_command(["azd", "up"], REPO_ROOT)[1:] == ["up"]


def test_azd_interactive_commands_keep_the_real_terminal():
    assert runcmd.wants_passthrough(["azd", "up"]) and runcmd.wants_passthrough(["azd", "provision"])
    assert not runcmd.wants_passthrough(["azd", "up", "--no-prompt"])
    assert not runcmd.wants_passthrough([r"infra\scripts\post-provision\build_push_images.ps1"])
    assert runcmd.wants_passthrough(["azd", "env", "get-values"])


def test_run_command_survives_characters_the_console_cannot_print(tmp_path, monkeypatch):
    class Cp1252Console(io.StringIO):
        encoding = "cp1252"

        def write(self, s):
            s.encode("cp1252")  # raises UnicodeEncodeError like a real legacy console
            return super().write(s)

    monkeypatch.setattr(sys, "stdout", Cp1252Console())
    log = tmp_path / "last.log"
    code, out = runcmd.run_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write('ok \\u2713 done\\n'.encode('utf-8'))"],
        tmp_path,
        True,
        log,
        allowed=frozenset({"python"}),
    )
    assert code == 0 and "done" in out and "done" in log.read_text(encoding="utf-8")


def test_run_command_captures_output_exit_code_and_log(tmp_path, capsys):
    log = tmp_path / "last.log"
    code, out = runcmd.run_command(
        [sys.executable, "-c", "print('hello'); import sys; sys.exit(3)"],
        tmp_path,
        True,
        log,
        allowed=frozenset({"python"}),
    )
    assert code == 3 and "hello" in out and "hello" in capsys.readouterr().out
    assert "(exit 3)" in log.read_text(encoding="utf-8")


def test_next_steps_follow_the_profile_post_provision_chain():
    from accel_troubleshooter.repo import load_profiles

    prof = load_profiles()["customer-chatbot"]
    assert "build_push_images.ps1" in runcmd.next_steps(["azd", "up"], prof)[0]
    assert (
        "postprovision_data_agents.ps1"
        in runcmd.next_steps([r"infra\scripts\post-provision\build_push_images.ps1"], prof)[0]
    )
    assert "web app URL" in runcmd.next_steps([r"infra\scripts\post-provision\postprovision_data_agents.ps1"], prof)[0]
    assert runcmd.next_steps(["az", "account", "show"], prof) == []


def test_cli_run_failure_diagnoses_automatically_from_captured_output(monkeypatch, capsys, synthetic_env_file):
    monkeypatch.setattr(runcmd, "run_command", lambda *a, **k: (1, AZD_ERROR))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = main(
        [
            "run",
            "--replay",
            str(FIXTURES / "synthetic-base.json"),
            "--env-file",
            synthetic_env_file,
            "--format",
            "json",
            "--",
            "azd",
            "up",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    body = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert "InsufficientQuota" in body["issue"]["error_codes"] and body["issue"]["stages"][0] in ("deployment", "quota")


def test_cli_run_success_runs_health_check_and_points_to_next_script(monkeypatch, capsys, synthetic_env_file):
    monkeypatch.setattr(runcmd, "run_command", lambda *a, **k: (0, ""))
    rc = main(
        ["run", "--replay", str(FIXTURES / "synthetic-base.json"), "--env-file", synthetic_env_file, "--", "azd", "up"]
    )
    shown = capsys.readouterr().out
    assert rc == 0 and "NEXT STEP" in shown and "build_push_images.ps1" in shown and "DIAGNOSIS" in shown


def test_cli_run_refuses_disallowed_commands(capsys):
    assert main(["run", "--offline", "--", "python", "-c", "print(1)"]) == 3
    assert "not allowed" in capsys.readouterr().err


def test_mcp_never_prompts_or_runs_commands():
    src = (TOOL_ROOT / "accel_troubleshooter" / "mcp_server.py").read_text(encoding="utf-8")
    assert "prompt_rg" not in src and "runcmd" not in src
