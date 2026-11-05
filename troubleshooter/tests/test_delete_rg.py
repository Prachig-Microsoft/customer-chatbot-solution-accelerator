"""Deleting one resource group: two typed confirmations, a fresh re-check, exactly one delete command."""

import json

import pytest

from accel_troubleshooter import delete_rg
from accel_troubleshooter.actions import EXECUTABLE_ACTIONS
from accel_troubleshooter.azcli import ReplayRunner
from accel_troubleshooter.cli import main

from conftest import REPO_ROOT

SUB = "00000000-0000-0000-0000-00000000aaaa"
RG = "rg-demo1"
DELETED = f"/subscriptions/{SUB}/providers/Microsoft.CognitiveServices/locations/eastus2"


def _fx(resources=None, locks=None, exists=True, sub=SUB, gone_after=1):
    res = (
        resources
        if resources is not None
        else [
            {
                "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Search/searchServices/srch",
                "name": "srch",
                "type": "Microsoft.Search/searchServices",
                "tags": {"azd-env-name": "demo1"},
            },
            {
                "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.CognitiveServices/accounts/aif",
                "name": "aif",
                "type": "Microsoft.CognitiveServices/accounts",
                "tags": {"azd-env-name": "demo1"},
            },
        ]
    )
    cmds = {
        "account show": {"exit_code": 0, "stdout": {"id": sub, "name": "demo-subscription"}},
        f"resource list --resource-group {RG}": {"exit_code": 0, "stdout": res},
        f"lock list --resource-group {RG}": {"exit_code": 0, "stdout": locks or []},
        "cognitiveservices account list-deleted": {
            "exit_code": 0,
            "stdout": [
                {
                    "id": f"{DELETED}/resourceGroups/{RG}/deletedAccounts/aif",
                    "name": "aif",
                    "location": "eastus2",
                },
                {
                    "id": f"{DELETED}/resourceGroups/rg-other/deletedAccounts/x",
                    "name": "x",
                    "location": "eastus2",
                },
            ],
        },
    }
    if exists:
        cmds[f"group show --name {RG}"] = {
            "exit_code": 0,
            "stdout": {
                "id": f"/subscriptions/{SUB}/resourceGroups/{RG}",
                "location": "eastus2",
                "tags": {"azd-env-name": "demo1"},
                "properties": {"provisioningState": "Succeeded"},
            },
        }
    else:
        cmds[f"group show --name {RG}"] = {"exit_code": 3, "stderr": "ResourceGroupNotFound"}
    return cmds


class _Azure(ReplayRunner):
    """Replays reads; 'group exists' says True until gone_after polls, then False."""

    def __init__(self, cmds, gone_after=1):
        super().__init__({"commands": cmds})
        self.polls, self.gone_after = 0, gone_after

    def run(self, args, timeout=None):
        if args[:2] == ["group", "exists"]:
            self.polls += 1
            self.fixtures["group exists --name " + RG] = {"exit_code": 0, "stdout": self.polls <= self.gone_after}
        return super().run(args, timeout)


@pytest.fixture
def executed(monkeypatch):
    ran = []

    def fake_run_one(cmd, repo, timeout):
        ran.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(delete_rg, "_run_one", fake_run_one)
    monkeypatch.setattr(delete_rg, "_sleep", lambda s: None)
    return ran


def _plan(cmds, **kw):
    snap = delete_rg.snapshot(_Azure(cmds), RG)
    return snap, delete_rg.assess(snap, **kw)


def _delete(cmds, name=RG, phrase=f"delete {RG}", fp=None, runner=None, tmp_path=None, **kw):
    if fp is None:
        fp = _plan(cmds)[1]["fingerprint"]
    return delete_rg.delete_resource_group(runner or _Azure(cmds), RG, name, phrase, fp, tmp_path or REPO_ROOT, **kw)


def test_deletes_only_that_group_with_exactly_one_command_and_reports_leftovers(executed, tmp_path, monkeypatch):
    import accel_troubleshooter.telemetry as telemetry

    monkeypatch.setattr(telemetry, "RUNS_FILE", tmp_path / "runs.jsonl")
    res = _delete(_fx())
    assert res["deleted"] and executed == [f"az group delete --name {RG} --yes --no-wait"]
    assert [s["name"] for s in res["soft_deleted"]] == ["aif"]  # only the one that came from this group
    assert "purge" in res["soft_deleted"][0]["purge"]
    log = [json.loads(x) for x in (tmp_path / "deletions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["resource_group"] == RG and log[-1]["deleted"] and len(log[-1]["resources"]) == 2
    text = delete_rg.render_result(res)
    assert "Only this resource group was deleted" in text and "you run it" in text


@pytest.mark.parametrize(
    "name,phrase,why",
    [
        ("rg-demo", f"delete {RG}", "not exactly"),  # a different, similar group
        ("RG-DEMO1", f"delete {RG}", "not exactly"),  # case matters
        (" rg-demo1", f"delete {RG}", "not exactly"),
        (RG, "yes", "second confirmation"),
        (RG, f"delete {RG} ", "second confirmation"),
        (RG, "delete rg-demo", "second confirmation"),
    ],
)
def test_any_mistyped_confirmation_deletes_nothing(executed, name, phrase, why):
    res = _delete(_fx(), name=name, phrase=phrase)
    assert not res["deleted"] and why in res["refused"] and executed == []


def test_nothing_is_deleted_if_the_group_changed_after_the_user_saw_it(executed):
    fp = _plan(_fx())[1]["fingerprint"]
    more = _fx()[f"resource list --resource-group {RG}"]["stdout"] + [
        {
            "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.KeyVault/vaults/kv",
            "name": "kv",
            "type": "Microsoft.KeyVault/vaults",
        }
    ]
    res = _delete(_fx(resources=more), fp=fp)
    assert not res["deleted"] and "changed since you confirmed" in res["refused"] and executed == []
    res = _delete(_fx(sub="11111111-1111-1111-1111-111111111111"), fp=fp)  # another subscription now
    assert not res["deleted"] and executed == []


def test_locks_missing_group_and_wrong_subscription_block_it(executed):
    locked = _fx(locks=[{"name": "keep", "level": "CanNotDelete"}])
    snap, v = _plan(locked)
    assert any("lock" in b for b in v["blockers"]) and not _delete(locked, fp="x")["deleted"]
    snap, v = _plan(_fx(exists=False))
    assert any("does not exist" in b for b in v["blockers"]) and v["fingerprint"] == ""
    snap, v = _plan(_fx(), env_rg=RG, env_subscription="22222222-2222-2222-2222-222222222222")
    assert any("az account set" in b for b in v["blockers"])
    assert executed == []


def test_no_fingerprint_means_no_delete(executed):
    res = delete_rg.delete_resource_group(_Azure(_fx()), RG, RG, f"delete {RG}", "", REPO_ROOT)
    assert not res["deleted"] and "cleanup plan first" in res["refused"] and executed == []


def test_warnings_for_another_environment_and_a_skipped_predown_hook():
    _, v = _plan(_fx(), azd_env="other-env", env_rg="rg-other", predown="remove_x.py")
    joined = " ".join(v["warnings"])
    assert "not the resource group of the current azd environment" in joined
    assert "azd-env-name=demo1" in joined and "predown hook (remove_x.py) does NOT run" in joined
    assert not v["blockers"]


def test_a_slow_delete_is_reported_as_still_deleting_not_as_done(executed, monkeypatch):
    monkeypatch.setattr(delete_rg, "WAIT_LIMIT_S", 60)
    res = _delete(_fx(), runner=_Azure(_fx(), gone_after=99))
    assert not res["deleted"] and res["still_deleting"] and executed  # started, not claimed finished
    assert "still deleting" in delete_rg.render_result(res)


def test_azure_refusing_the_delete_is_reported(monkeypatch):
    monkeypatch.setattr(delete_rg, "_run_one", lambda cmd, repo, t: (1, "AuthorizationFailed: no permission", ""))
    res = _delete(_fx())
    assert not res["deleted"] and "AuthorizationFailed" in res["refused"]


def test_invalid_names_never_reach_azure():
    snap = delete_rg.snapshot(_Azure({}), "rg-x; rm -rf /")
    assert snap["errors"] and not snap["exists"]


def test_delete_is_not_reachable_through_the_approved_fix_path():
    assert not any("delete" in a for a in EXECUTABLE_ACTIONS)


def test_cli_without_confirmations_only_prints_what_to_type(monkeypatch, capsys):
    import accel_troubleshooter.cli as cli

    class LiveLike(_Azure):
        mode = "live"

    monkeypatch.setattr(cli, "_runner", lambda args: LiveLike(_fx()))
    called = []
    monkeypatch.setattr(delete_rg, "_run_one", lambda *a: called.append(a) or (0, "", ""))
    rc = main(
        [
            "--workspace",
            str(REPO_ROOT),
            "cleanup",
            "--delete",
            "--resource-group",
            RG,
            "--no-prompt",
            "--env-file",
            "none",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1 and called == []
    assert "DELETE CHECKS" in out and f"type exactly:        delete {RG}" in out and "fingerprint" in out


def test_cli_refuses_to_delete_in_replay_or_offline_mode(capsys):
    rc = main(["--workspace", str(REPO_ROOT), "cleanup", "--delete", "--resource-group", RG, "--offline"])
    assert rc == 3 and "live Azure access" in capsys.readouterr().err


def test_mcp_tool_passes_only_what_it_was_given():
    pytest.importorskip("mcp")
    from accel_troubleshooter import mcp_server

    seen = []
    orig = mcp_server._cli
    mcp_server._cli = lambda args, timeout=0: seen.append(args) or "ok"
    try:
        mcp_server.delete_resource_group(RG)
        assert "--confirm-name" not in seen[-1] and "--delete" in seen[-1]  # step 1: checks only
        mcp_server.delete_resource_group(RG, confirm_name=RG, confirm_phrase=f"delete {RG}", fingerprint="abc")
        assert seen[-1][-6:] == ["--confirm-name", RG, "--confirm", f"delete {RG}", "--fingerprint", "abc"]
    finally:
        mcp_server._cli = orig


def test_agent_and_skill_require_two_typed_confirmations():
    agent = (REPO_ROOT / ".github" / "agents" / "accelerator-troubleshooter.agent.md").read_text(encoding="utf-8")
    skill = (REPO_ROOT / ".github" / "skills" / "cleanup-resources" / "SKILL.md").read_text(encoding="utf-8")
    assert "type the exact name" in agent and "Type `delete <name>`" in agent
    assert "exactly what the" in agent and "never do steps 1 and 4 in the same turn" in agent
    assert "Never delete more than that one group" in agent
    assert "two-confirmation procedure" in skill and "One resource group only" in skill
