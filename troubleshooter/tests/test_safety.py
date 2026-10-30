import json

import pytest

from accel_troubleshooter import actions as actions_mod
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


class _FakeAzure:
    """Stateful stand-in for az: tags and the Cosmos update become visible only after a number of polls."""

    SUB = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-x"
    COSMOS = SUB + "/providers/Microsoft.DocumentDB/databaseAccounts/cosmos-x"

    def __init__(self, tag_polls=2, busy_polls=2, enable_conflicts=0, revert=False, never_visible=False):
        self.tag_polls, self.busy_polls = tag_polls, busy_polls
        self.enable_conflicts, self.revert, self.never_visible = enable_conflicts, revert, never_visible
        self.log, self.tagged, self.polls, self.busy, self.enabled, self.shows_after_enable = [], set(), {}, 0, False, 0

    def run(self, argv, **kw):
        a = argv[1:]
        self.log.append(" ".join(a))
        out, code = "", 0
        if a[:2] == ["tag", "update"]:
            self.tagged.add(a[a.index("--resource-id") + 1])
            self.busy = self.busy_polls  # a tag write puts the Cosmos account into Updating
        elif a[:2] == ["tag", "list"]:
            rid = a[a.index("--resource-id") + 1]
            self.polls[rid] = self.polls.get(rid, 0) + 1
            visible = rid in self.tagged and self.polls[rid] > self.tag_polls and not self.never_visible
            out = json.dumps({"properties": {"tags": {"SecurityControl": "Ignore"} if visible else {}}})
        elif a[:2] == ["cosmosdb", "show"]:
            if self.busy > 0:
                self.busy -= 1
                state = "Updating"
            else:
                state = "Succeeded"
            pna = "Enabled" if self.enabled else "Disabled"
            if self.enabled:
                self.shows_after_enable += 1
                if self.revert and self.shows_after_enable > 1:
                    pna = "Disabled"
            out = json.dumps({"provisioningState": state, "publicNetworkAccess": pna})
        elif a[:2] == ["cosmosdb", "update"]:
            if self.busy > 0 or self.enable_conflicts > 0:
                self.enable_conflicts = max(0, self.enable_conflicts - 1)
                code, out = 1, "ERROR: Conflict: another operation is in progress"
            else:
                self.enabled = True
        proc = type(
            "P", (), {"returncode": code, "stdout": out if code == 0 else "", "stderr": "" if code == 0 else out}
        )
        return proc()

    def first(self, prefix):
        return next(i for i, line in enumerate(self.log) if line.startswith(prefix))


def _tag_action():
    return build_action(
        "apply-exemption-tag",
        load_profiles()["customer-chatbot"],
        tag="SecurityControl",
        value="Ignore",
        resource_group="rg-x",
        rg_id=_FakeAzure.SUB,
        resources={"cosmos-x": _FakeAzure.COSMOS},
    )


def _patch_az(monkeypatch, fake):
    monkeypatch.setattr(actions_mod.shutil, "which", lambda exe: "C:/fake/" + exe)
    monkeypatch.setattr(actions_mod.subprocess, "run", fake.run)
    sleeps = []
    monkeypatch.setattr(actions_mod, "_sleep", sleeps.append)
    return sleeps


def test_exemption_tag_waits_for_tags_and_idle_account_before_enabling(monkeypatch):
    a = _tag_action()
    assert a.category == "approval_required"
    assert [c.split()[1] for c in a.commands] == ["tag", "tag", "cosmosdb"]
    assert execute(a, REPO_ROOT, approved=False)["executed"] is False
    fake = _FakeAzure(tag_polls=3, busy_polls=3)
    sleeps = _patch_az(monkeypatch, fake)
    said = []
    res = execute(a, REPO_ROOT, approved=True, progress=said.append)
    assert res["executed"] and res["success"], res
    # every tag write, then tags confirmed visible, then the account idle, and only then the enable command
    last_tag_write = max(i for i, line in enumerate(fake.log) if line.startswith("tag update"))
    last_tag_poll = max(i for i, line in enumerate(fake.log) if line.startswith("tag list"))
    assert last_tag_write < last_tag_poll < fake.first("cosmosdb update")
    assert fake.log[fake.first("cosmosdb update") - 1].startswith("cosmosdb show")
    assert actions_mod.SETTLE_S in sleeps and actions_mod.STABLE_S in sleeps  # waited, never a blind rush
    assert any("tags visible" in m for m in said) and any("Step 4/4" in m for m in said)


def test_exemption_tag_does_not_enable_when_tags_never_show_up(monkeypatch):
    fake = _FakeAzure(never_visible=True)
    _patch_az(monkeypatch, fake)
    res = execute(_tag_action(), REPO_ROOT, approved=True)
    assert res["success"] is False and res["stopped_at"] == "tags not visible yet"
    assert not any(line.startswith("cosmosdb update") for line in fake.log)  # never attempted
    assert len(res["guidance"]) == 3 and "run the same fix again" in res["guidance"][0]


def test_exemption_tag_retries_a_busy_account_then_succeeds(monkeypatch):
    fake = _FakeAzure(tag_polls=0, busy_polls=0, enable_conflicts=2)
    sleeps = _patch_az(monkeypatch, fake)
    res = execute(_tag_action(), REPO_ROOT, approved=True)
    assert res["success"] is True and sum(1 for ln in fake.log if ln.startswith("cosmosdb update")) == 3
    assert sleeps.count(60) == 2


def test_exemption_tag_reports_a_policy_that_changes_it_back(monkeypatch):
    fake = _FakeAzure(tag_polls=0, busy_polls=0, revert=True)
    _patch_az(monkeypatch, fake)
    res = execute(_tag_action(), REPO_ROOT, approved=True)
    assert res["success"] is False and res["stopped_at"] == "policy changed it back"


def test_exemption_tag_stops_when_the_first_tag_write_fails(monkeypatch):
    monkeypatch.setattr(actions_mod.shutil, "which", lambda exe: "C:/fake/" + exe)
    calls = []

    class _Fail:
        returncode, stdout, stderr = 1, "", "AuthorizationFailed"

    monkeypatch.setattr(actions_mod.subprocess, "run", lambda argv, **kw: calls.append(argv) or _Fail())
    res = execute(_tag_action(), REPO_ROOT, approved=True)
    assert res["success"] is False and len(calls) == 1  # nothing after the failed tag command ran


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
