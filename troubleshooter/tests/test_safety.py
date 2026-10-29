import json

import pytest

from accel_troubleshooter.actions import build_action, execute
from accel_troubleshooter.azcli import AzRunner, CommandNotAllowed, OfflineRunner, ReplayRunner, anonymize, is_read_only
from accel_troubleshooter.models import APPROVAL_REQUIRED, Action
from accel_troubleshooter.redact import redact_obj, redact_text
from accel_troubleshooter.repo import load_profiles

from conftest import REPO_ROOT


@pytest.mark.parametrize(
    "args",
    [
        ["webapp", "restart", "-g", "x", "-n", "y"],
        ["group", "delete", "-n", "x"],
        ["role", "assignment", "create"],
        ["cognitiveservices", "account", "purge"],
        ["deployment", "group", "cancel"],
    ],
)
def test_state_changing_az_commands_are_blocked(args):
    assert not is_read_only(args)
    with pytest.raises(CommandNotAllowed):
        AzRunner().run(args)


def test_shell_metacharacters_are_rejected():
    with pytest.raises(CommandNotAllowed):
        AzRunner().run(["group", "show", "--name", "x&calc"])
    with pytest.raises(CommandNotAllowed):
        ReplayRunner({}).run(["group", "show", "--name", "x|y"])


def test_http_probe_host_allowlist():
    with pytest.raises(CommandNotAllowed):
        AzRunner().http_get("https://evil.example.com/")


def test_replay_exact_then_glob_and_missing():
    r = ReplayRunner(
        {
            "commands": {
                "group show --name a": {"exit_code": 0, "stdout": {"n": 1}},
                "group show --name *": {"exit_code": 3, "stderr": "(ResourceGroupNotFound) x"},
            }
        }
    )
    assert r.run(["group", "show", "--name", "a"])[1] == {"n": 1}
    ok, data, rec = r.run(["group", "show", "--name", "b"])
    assert not ok and "ResourceGroupNotFound" in data["error"]
    ok, _, rec = r.run(["account", "show"])
    assert not ok and rec.source == "replay-missing"


def test_offline_runner_executes_nothing():
    ok, _, rec = OfflineRunner().run(["account", "show"])
    assert not ok and rec.source == "skipped"


def test_redaction():
    assert "abc123" not in redact_text("DefaultEndpointsProtocol=https;AccountKey=abc123;EndpointSuffix=x")
    assert "<redacted-jwt>" in redact_text("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk")
    out = redact_obj({"primaryKey": "s3cr3t", "nested": [{"connectionString": "Server=x;Password=p"}], "name": "ok"})
    assert out["primaryKey"] == "<redacted>" and out["nested"][0]["connectionString"] == "<redacted>"
    assert out["name"] == "ok"


def test_fixture_anonymization():
    rec = {
        "account show": {"stdout": {"id": "12345678-aaaa-bbbb-cccc-1234567890ab", "tenantId": "t-1", "name": "MySub"}}
    }
    text = json.dumps(
        {
            "x": "/subscriptions/12345678-aaaa-bbbb-cccc-1234567890ab/rg",
            "u": "a.b@corp.onmicrosoft.com",
            "n": "MySub",
            "tenantDisplayName": "Corp",
        }
    )
    out = anonymize(text, rec)
    for secret in ("12345678-aaaa", "a.b@", "MySub", "Corp"):
        assert secret not in out


def test_restricted_and_unapproved_actions_never_execute():
    prof = load_profiles()["customer-chatbot"]
    restricted = build_action("delete-resource-group", prof)
    assert execute(restricted, REPO_ROOT, approved=True)["executed"] is False
    a = build_action("restart-webapp", prof, resource_group="rg", app="app")
    res = execute(a, REPO_ROOT, approved=False)
    assert res["executed"] is False and "--approve" in res["reason"]
    manual = build_action("azd-provision", prof)
    assert execute(manual, REPO_ROOT, approved=True)["executed"] is False


def _record_subprocess(monkeypatch):
    from accel_troubleshooter import actions as actions_mod

    calls = []

    class _P:
        returncode, stdout, stderr = 0, "ok", ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return _P()

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    return calls


def test_approved_allowlisted_action_executes(monkeypatch):
    calls = _record_subprocess(monkeypatch)
    a = build_action("restart-webapp", load_profiles()["customer-chatbot"], resource_group="rg-x", app="api-x")
    res = execute(a, REPO_ROOT, approved=True)
    assert res["executed"] and res["success"]
    assert calls and calls[0][1:] == ["webapp", "restart", "--resource-group", "rg-x", "--name", "api-x"]


def test_documented_steps_are_never_executed_even_when_approved(monkeypatch):
    calls = _record_subprocess(monkeypatch)
    prof = load_profiles()["customer-chatbot"]
    doc = build_action(
        "follow-documented-steps", prof, code="InvalidResourceLocation", commands=["azd down --force --purge", "azd up"]
    )
    assert doc.category == "manual_review"
    assert execute(doc, REPO_ROOT, approved=True)["executed"] is False
    logs = build_action("collect-app-logs", prof, resource_group="rg", app="app")
    assert execute(logs, REPO_ROOT, approved=True)["executed"] is False
    arbitrary = Action("probe", "probe", APPROVAL_REQUIRED, "x", commands=["git --version"])
    assert execute(arbitrary, REPO_ROOT, approved=True)["executed"] is False
    assert calls == []


def test_cli_fix_never_runs_documented_commands(monkeypatch, capsys):
    from accel_troubleshooter.cli import main

    calls = _record_subprocess(monkeypatch)
    rc = main(
        [
            "fix",
            "follow-documented-steps",
            "--issue",
            "azd up failed with InvalidResourceLocation",
            "--accelerator",
            "customer-chatbot",
            "--offline",
            "--approve",
            "--yes",
        ]
    )
    assert rc == 1 and all(c[0] == "git" for c in calls)  # only the read-only git status of diagnose
    assert "Not agent-executable" in capsys.readouterr().out


def test_placeholders_are_checked_before_any_command_runs(monkeypatch):
    calls = _record_subprocess(monkeypatch)
    a = build_action("restart-webapp", load_profiles()["customer-chatbot"], resource_group="rg", app="app")
    a.commands = ["az webapp restart --resource-group rg --name app", "az webapp restart --name <app>"]
    assert execute(a, REPO_ROOT, approved=True)["executed"] is False
    assert calls == []


def test_app_setting_values_never_reach_excerpts_or_data():
    secret = "plainsecretvalue123"
    r = ReplayRunner(
        {
            "commands": {
                "webapp config appsettings list --resource-group g --name a": {
                    "exit_code": 0,
                    "stdout": [
                        {"name": "AZURE_OPENAI_API_KEY", "value": secret},
                        {"name": "EMPTY", "value": ""},
                        {"name": "FOUNDRY_CHAT_AGENT", "value": "<populate manually after running x>"},
                    ],
                }
            }
        }
    )
    ok, data, rec = r.run(["webapp", "config", "appsettings", "list", "--resource-group", "g", "--name", "a"])
    assert secret not in json.dumps(data) and secret not in rec.output_excerpt
    assert [d["value"] for d in data] == ["<redacted>", "", "<populate manually after running x>"]


def test_diff_lines_hide_secret_values():
    from accel_troubleshooter.diagnostics.repo_checks import mask_diff_line

    assert "abc123" not in mask_diff_line("infra/vscode_web/.env", "+AZURE_OPENAI_ENDPOINT=abc123")
    assert "xyz" not in mask_diff_line("chat-app/backend/app/config.py", '+    azure_openai_api_key: str = "xyz"')
    assert "s3" not in mask_diff_line("infra/main.parameters.json", '+  "apiKey": "s3"')
    assert mask_diff_line("infra/main.bicep", "+param gptDeploymentCapacity int = 22").endswith("= 22")


def test_executor_rejects_unsafe_arguments_and_placeholders():
    prof = load_profiles()["customer-chatbot"]
    a = build_action("restart-webapp", prof, resource_group="rg;calc", app="app")
    res = execute(a, REPO_ROOT, approved=True)
    assert res["executed"] is False and "unsafe" in res["reason"]
    b = build_action("new-azd-env", prof)
    assert execute(b, REPO_ROOT, approved=True)["executed"] is False
