"""The health check reports the optional Microsoft Entra sign-in step and says when to run it."""

import copy

from accel_troubleshooter.azcli import ReplayRunner
from accel_troubleshooter.compact import render_compact
from accel_troubleshooter.engine import Troubleshooter

from conftest import REPO_ROOT

CHAT_SETTINGS = "webapp config appsettings list --resource-group rg-demo --name api-chat-demo"
SCENARIO_SETTINGS = "webapp config appsettings list --resource-group rg-demo --name api-scenario-demo"
AUTH = [{"name": "ENTRA_AUTH_CLIENT_ID", "value": "x"}, {"name": "ENTRA_AUTH_TENANT_ID", "value": "y"}]


def _health(fixture, env_file, chat_extra=None, scenario_settings=None, drop=None):
    fx = copy.deepcopy(fixture)
    cmds = fx["commands"]
    cmds[CHAT_SETTINGS]["stdout"] = [s for s in cmds[CHAT_SETTINGS]["stdout"] if s["name"] != drop] + (chat_extra or [])
    if scenario_settings is not None:
        cmds[SCENARIO_SETTINGS] = {"exit_code": 0, "stdout": scenario_settings}
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    d = ts.diagnose("check my deployment health", env_file=env_file, log=False, full=True)
    return d, render_compact(d.to_dict())


def _auth(d):
    return next(c for c in d.checks if c.check_id == "auth.setup")


def test_unconfigured_sign_in_is_an_info_with_the_script_and_doc(synthetic_fixture, synthetic_env_file):
    d, text = _health(synthetic_fixture, synthetic_env_file)
    assert _auth(d).status == "info" and "optional" in _auth(d).summary
    assert "OPTIONAL NEXT STEP  sign-in with Microsoft Entra" in text
    assert "configure_auth.ps1 or infra/scripts/post-provision/configure_auth.sh" in text
    assert "AppAuthentication.md" in text and "DeploymentGuide.md (5.4" in text
    assert "Post-provision is done" in text
    assert not [c for c in d.checks if c.status in ("fail", "error") and c.check_id == "auth.setup"]


def test_sign_in_waits_for_post_provision_when_agents_are_not_created(synthetic_fixture, synthetic_env_file):
    d, text = _health(synthetic_fixture, synthetic_env_file, drop="FOUNDRY_CHAT_AGENT")
    assert _auth(d).evidence["post_provision_pending"] is True
    assert "First finish the post-provision steps" in text and "Post-provision is done" not in text


def test_configured_sign_in_passes_and_prints_no_hint(synthetic_fixture, synthetic_env_file):
    d, text = _health(synthetic_fixture, synthetic_env_file, chat_extra=AUTH, scenario_settings=AUTH)
    assert _auth(d).status == "pass"
    assert "OPTIONAL NEXT STEP" not in text


def test_partly_configured_sign_in_is_a_warning(synthetic_fixture, synthetic_env_file):
    d, text = _health(synthetic_fixture, synthetic_env_file, chat_extra=AUTH[:1], scenario_settings=AUTH)
    assert _auth(d).status == "warn" and "missing ENTRA_AUTH_TENANT_ID" in _auth(d).summary
    assert "OPTIONAL NEXT STEP" in text


def test_sign_in_check_never_reads_setting_values(synthetic_fixture, synthetic_env_file):
    secret = "super-secret-client-id"
    d, text = _health(
        synthetic_fixture, synthetic_env_file, chat_extra=[{"name": "ENTRA_AUTH_CLIENT_ID", "value": secret}]
    )
    assert secret not in text and secret not in str(_auth(d).evidence)
