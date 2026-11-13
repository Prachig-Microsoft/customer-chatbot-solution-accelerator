import json

import pytest

from accel_troubleshooter.azcli import CommandNotAllowed, ReplayRunner, is_read_only
from accel_troubleshooter.diagnostics.insights_checks import classify_message, normalise, summarise
from accel_troubleshooter.engine import Troubleshooter

from conftest import REPO_ROOT

SUB = "00000000-0000-0000-0000-00000000aaaa"
APP_ID = "11111111-1111-1111-1111-111111111111"
ARM = (
    f"rest --method get --url https://management.azure.com/subscriptions/{SUB}/resourceGroups/rg-demo/providers/"
    "microsoft.insights/components/appi-demo --uri-parameters api-version=2020-02-02"
)


def _query(table, window="PT1H"):
    return (
        f"rest --method get --url https://api.applicationinsights.io/v1/apps/{APP_ID}/query "
        f"--uri-parameters query={table} timespan={window} --resource https://api.applicationinsights.io"
    )


def _table(columns, rows):
    return {
        "exit_code": 0,
        "stdout": {"tables": [{"name": "PrimaryResult", "columns": [{"name": c} for c in columns], "rows": rows}]},
    }


def _fixture(base, exceptions=(), traces=(), requests=(), events=(), window="PT1H", component=True):
    fx = json.loads(json.dumps(base))
    cmds = fx["commands"]
    if component:
        cmds["resource list --resource-group rg-demo"]["stdout"].append(
            {"name": "appi-demo", "type": "Microsoft.Insights/components"}
        )
        cmds[ARM] = {
            "exit_code": 0,
            "stdout": {"properties": {"AppId": APP_ID, "ConnectionString": "InstrumentationKey=abc"}},
        }
    cmds[_query("exceptions", window)] = _table(
        ["timestamp", "type", "outerMessage", "cloud_RoleName", "itemCount"], list(exceptions)
    )
    cmds[_query("traces", window)] = _table(["timestamp", "message", "severityLevel", "cloud_RoleName"], list(traces))
    cmds[_query("requests", window)] = _table(
        ["timestamp", "name", "resultCode", "cloud_RoleName", "itemCount"], list(requests)
    )
    cmds[_query("customEvents", window)] = _table(
        ["timestamp", "name", "customDimensions", "cloud_RoleName"], list(events)
    )
    return fx


def _run(fx, env_file, issue="chat returns 500 errors", **overrides):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    return ts.diagnose(issue, env_file=env_file, log=False, only_checks=["appinsights.errors"], overrides=overrides)


def _check(d):
    return next(c for c in d.checks if c.check_id == "appinsights.errors")


COSMOS = (
    "(Forbidden) Request originated from IP 10.1.2.3 through public internet. This is blocked by your Cosmos DB "
    "account firewall settings."
)


def test_cosmos_firewall_errors_in_the_apps_own_logs_are_named_and_counted(synthetic_fixture, synthetic_env_file):
    rows = [[f"2026-11-03T06:{m:02d}:00Z", "CosmosHttpResponseError", COSMOS, "api-chat-demo", 1] for m in range(14)]
    d = _run(_fixture(synthetic_fixture, exceptions=rows), synthetic_env_file)
    c = _check(d)
    assert c.status == "fail" and "cosmos-firewall x14" in c.summary
    assert c.evidence["by_category"] == {"cosmos-firewall": 14}
    assert c.evidence["top_errors"][0]["count"] == 14 and c.evidence["top_errors"][0]["role"] == "api-chat-demo"
    top = d.hypotheses[0]
    assert top.id == "observed-cosmos-firewall" and top.status == "confirmed" and top.confidence == "high"
    assert any("14 logged error(s)" in r for r in top.rationale) and "appinsights.errors" in top.supporting_checks
    assert not any("Application logs (az webapp log tail" in m for m in d.missing_evidence)  # now answered


def test_logs_back_up_an_existing_finding_instead_of_duplicating_it(synthetic_fixture, synthetic_env_file):
    from test_ux import _policy_fixture

    base = _policy_fixture(synthetic_fixture)
    rows = [["2026-11-03T06:00:00Z", "CosmosHttpResponseError", COSMOS, "api-chat-demo", 3]]
    fx = _fixture(base, exceptions=rows)
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    d = ts.diagnose(
        "chat returns 500",
        env_file=synthetic_env_file,
        log=False,
        only_checks=["appinsights.errors", "network.public_access"],
    )
    ids = [h.id for h in d.hypotheses]
    assert "public-access-disabled" in ids and "observed-cosmos-firewall" not in ids
    h = next(x for x in d.hypotheses if x.id == "public-access-disabled")
    assert any("Application Insights" in r and "3 logged" in r for r in h.rationale)
    assert "appinsights.errors" in h.supporting_checks


def test_no_errors_is_a_pass_and_no_traffic_is_not(synthetic_fixture, synthetic_env_file):
    ok = _run(
        _fixture(synthetic_fixture, requests=[["2026-11-03T06:00:00Z", "GET /health", "200", "api-chat-demo", 20]]),
        synthetic_env_file,
    )
    assert _check(ok).status == "pass" and "20 request(s)" in _check(ok).summary
    quiet = _run(_fixture(synthetic_fixture), synthetic_env_file)
    assert _check(quiet).status == "info" and "no traffic" in _check(quiet).summary  # nothing to judge


def test_unknown_errors_are_possible_causes_with_a_stack_trace_request(synthetic_fixture, synthetic_env_file):
    rows = [["2026-11-03T06:00:00Z", "ZeroDivisionError", "division by zero in pricing", "api-chat-demo", 2]]
    d = _run(_fixture(synthetic_fixture, exceptions=rows), synthetic_env_file)
    assert _check(d).status == "warn"
    h = next(x for x in d.hypotheses if x.id == "observed-other")
    assert h.status == "possible" and any("stack trace" in m for m in h.missing_evidence)


def test_failed_requests_count_as_the_symptom_not_a_second_cause(synthetic_fixture, synthetic_env_file):
    fx = _fixture(
        synthetic_fixture,
        exceptions=[
            ["2026-11-03T06:00:00Z", "RateLimit", "429 Too Many Requests, retry after 4 seconds", "api-chat-demo", 5]
        ],
        requests=[["2026-11-03T06:00:01Z", "POST /api/chat/message", "500", "api-chat-demo", 5]],
    )
    c = _check(_run(fx, synthetic_env_file))
    assert c.evidence["requests"] == {"total": 5, "failed_5xx": 5} and c.evidence["by_category"] == {"throttled": 5}


def test_error_events_and_log_levels(synthetic_fixture, synthetic_env_file):
    fx = _fixture(
        synthetic_fixture,
        traces=[
            ["2026-11-03T06:00:00Z", "all good", 1, "api-chat-demo"],
            ["2026-11-03T06:00:01Z", "Error running AI agent: DeploymentNotFound for gpt-x", 3, "api-chat-demo"],
        ],
        events=[
            ["2026-11-03T06:00:02Z", "Chat_Message_Sent", "{}", "api-chat-demo"],
            ["2026-11-03T06:00:03Z", "Error_Config_Missing", '{"setting":"azure_foundry_endpoint"}', "api-chat-demo"],
        ],
    )
    c = _check(_run(fx, synthetic_env_file))
    assert c.evidence["by_category"] == {"model-not-found": 1, "config-missing": 1}  # info logs/events are ignored


def test_window_is_validated_and_passed_to_the_query(synthetic_fixture, synthetic_env_file):
    assert _check(_run(_fixture(synthetic_fixture), synthetic_env_file, window="rm -rf")).status == "error"
    d = _run(_fixture(synthetic_fixture, window="PT15M"), synthetic_env_file, window="PT15M")
    assert _check(d).status == "info" and _check(d).evidence["window"] == "PT15M"


def test_missing_component_and_unreadable_component_are_explained(synthetic_fixture, synthetic_env_file):
    none = _check(_run(_fixture(synthetic_fixture, component=False), synthetic_env_file))
    assert none.status == "info" and "No Application Insights" in none.summary
    fx = _fixture(synthetic_fixture)
    fx["commands"][ARM] = {"exit_code": 1, "stderr": "AuthorizationFailed: no Reader on the component"}
    denied = _check(_run(fx, synthetic_env_file))
    assert denied.status == "warn" and "Reader" in denied.summary


def test_report_never_contains_the_instrumentation_key(synthetic_fixture, synthetic_env_file):
    from accel_troubleshooter.report import render_markdown

    d = _run(_fixture(synthetic_fixture), synthetic_env_file)
    text = json.dumps(d.to_dict()) + render_markdown(d.to_dict(), verbose=True)
    assert "InstrumentationKey=abc" not in text and "abc" not in "".join(c.summary for c in d.checks)


def test_classification_order_and_normalisation():
    assert classify_message(COSMOS) == "cosmos-firewall"
    assert classify_message("(Forbidden) principal does not have the required RBAC permissions") == "rbac-denied"
    assert classify_message("Error code: 429 - Rate limit exceeded, retry after 4") == "throttled"
    assert classify_message("HTTP 401 Authentication required") == "auth-token"
    assert classify_message("Azure AI Foundry is not configured") == "config-missing"
    assert classify_message("The API deployment for this resource does not exist") == "model-not-found"
    assert classify_message("something odd") == "other"
    a = normalise("Request 11111111-1111-1111-1111-111111111111 from 10.1.2.3 failed after 5021 ms at https://x.y/z")
    assert a == "Request <id> from <ip> failed after <n> ms at <url>"
    rows = {
        "exceptions": [{"timestamp": "t", "type": "E", "outerMessage": "id 123456 failed", "cloud_RoleName": "a"}] * 2
        + [{"timestamp": "t", "type": "E", "outerMessage": "id 999999 failed", "cloud_RoleName": "a"}]
    }
    assert summarise(rows)["groups"][0]["count"] == 3  # the same error with different ids is one group


def test_az_rest_is_get_only_and_limited_to_two_apis():
    ok = ARM.split()
    assert is_read_only(ok) and is_read_only(_query("exceptions").split())
    bad = [
        ok[:2] + ["post"] + ok[3:],
        ok + ["--body", "x"],
        ok + ["--headers", "a=b"],
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v",
        ],
        ["rest", "--method", "get", "--url", "https://evil.example.com/x"],
        _query("exceptions").split()[:-1] + ["https://evil.example.com"],
    ]
    assert not any(is_read_only(b) for b in bad)
    with pytest.raises(CommandNotAllowed):
        ReplayRunner({}).run(bad[0])


# --------------------------------------------------------------------------- 401s once sign-in is configured

CHAT_SETTINGS = "webapp config appsettings list --resource-group rg-demo --name api-chat-demo"
SCENARIO_SETTINGS = "webapp config appsettings list --resource-group rg-demo --name api-scenario-demo"
SIGN_IN = [{"name": "ENTRA_AUTH_CLIENT_ID", "value": "x"}, {"name": "ENTRA_AUTH_TENANT_ID", "value": "y"}]
NO_SIGN_IN = "HTTP 401 error at https://api-scenario-demo.azurewebsites.net/api/auth/me Authentication required"


def _signed_in(fx, on=True):
    if on:
        fx["commands"][CHAT_SETTINGS]["stdout"] = fx["commands"][CHAT_SETTINGS]["stdout"] + SIGN_IN
        fx["commands"][SCENARIO_SETTINGS] = {"exit_code": 0, "stdout": SIGN_IN}
    return fx


def _rejections(n, text=NO_SIGN_IN):
    return [[f"2026-11-03T06:{m:02d}:00Z", text, 3, "api-scenario-demo"] for m in range(n)]


def _requests(n):
    return [["2026-11-03T06:00:00Z", "GET /api/products", "200", "api-scenario-demo", n]]


def _health(fx, env_file):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(fx))
    return ts.diagnose("check my deployment health", env_file=env_file, log=False, full=True)


def test_rejected_requests_without_sign_in_are_expected_once_sign_in_is_configured(
    synthetic_fixture, synthetic_env_file
):
    fx = _signed_in(_fixture(synthetic_fixture, traces=_rejections(2), requests=_requests(40)))
    d = _health(fx, synthetic_env_file)
    c = _check(d)
    assert c.status == "pass" and "Sign-in is enforced: 2 request(s) without a sign-in were rejected" in c.summary
    assert c.evidence["expected_rejections"]["count"] == 2 and not c.evidence["expected_rejections"]["mostly_rejected"]
    assert not any(h.id == "observed-auth-token" for h in d.hypotheses)  # no "not healthy" from expected 401s


def test_the_same_401s_are_a_finding_when_sign_in_is_not_configured(synthetic_fixture, synthetic_env_file):
    fx = _signed_in(_fixture(synthetic_fixture, traces=_rejections(2), requests=_requests(40)), on=False)
    d = _health(fx, synthetic_env_file)
    assert _check(d).status == "fail" and _check(d).evidence["expected_rejections"] is None
    assert any(h.id == "observed-auth-token" for h in d.hypotheses)


def test_token_faults_are_still_findings_with_sign_in_configured(synthetic_fixture, synthetic_env_file):
    fault = "401 Unauthorized: invalid audience in bearer token"
    fx = _signed_in(_fixture(synthetic_fixture, traces=_rejections(2) + _rejections(3, fault), requests=_requests(40)))
    d = _health(fx, synthetic_env_file)
    c = _check(d)
    assert c.status == "fail" and c.evidence["by_category"] == {"auth-token": 3}  # the 2 expected ones were set aside
    assert "Sign-in is enforced: 2 request(s)" in c.summary
    h = next(x for x in d.hypotheses if x.id == "observed-auth-token")
    assert any("invalid audience" in r for r in h.rationale)


def test_most_requests_rejected_means_signed_in_users_are_affected(synthetic_fixture, synthetic_env_file):
    fx = _signed_in(_fixture(synthetic_fixture, traces=_rejections(30), requests=_requests(40)))
    d = _health(fx, synthetic_env_file)
    c = _check(d)
    assert c.status == "fail" and "more than half" in c.summary and c.evidence["expected_rejections"]["mostly_rejected"]
    assert any(h.id == "observed-auth-token" for h in d.hypotheses)


def test_health_check_skill_reports_expected_rejections_as_info():
    skill = (REPO_ROOT / ".github" / "skills" / "health-check" / "SKILL.md").read_text(encoding="utf-8")
    assert "Sign-in is enforced" in skill and "never as ❌" in skill


def test_the_telemetry_sdks_own_upload_errors_are_not_app_errors(synthetic_fixture, synthetic_env_file):
    noise = [
        [
            "2026-11-03T06:00:00Z",
            "ServiceResponseTimeoutError: Envelopes could not be exported and are not retryable",
            3,
            "api-chat-demo",
        ],
        [
            "2026-11-03T06:01:00Z",
            "ServiceResponseTimeoutError: Exception occurred while pinging live metrics.",
            3,
            "api-chat-demo",
        ],
    ]
    d = _run(_fixture(synthetic_fixture, traces=noise, requests=_requests(10)), synthetic_env_file)
    c = _check(d)
    assert c.status == "pass" and c.evidence["telemetry_noise"] == 2 and c.evidence["by_category"] == {}
    assert not any(h.id == "observed-timeout-network" for h in d.hypotheses)
