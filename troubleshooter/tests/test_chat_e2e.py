import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from accel_troubleshooter.azcli import AzRunner, CommandNotAllowed, ReplayRunner
from accel_troubleshooter.cli import main
from accel_troubleshooter.engine import Troubleshooter
from accel_troubleshooter.report import render_markdown

from conftest import REPO_ROOT

CID = "22222222-2222-2222-2222-222222222222"
BASE = "https://api-chat-demo.azurewebsites.net"
POST = f"HTTP POST {BASE}/api/chat/message"
DELETE = f"HTTP DELETE {BASE}/api/chat/sessions/*"
SECRET = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
CHECKS = ["rg.state", "resources.state", "webapp.state", "chat.e2e"]
COSMOS = (
    "Error sending message: (Forbidden) Request originated from IP 10.1.2.3 through public internet. "
    "This is blocked by your Cosmos DB account firewall settings."
)


def _fx(base, post=None, delete=None, token=None):
    fx = json.loads(json.dumps(base))
    cmds = fx["commands"]
    cmds[f"TOKEN api://{CID}/user_impersonation"] = token or {"exit_code": 0, "stdout": SECRET}
    cmds[POST] = {
        "exit_code": 0,
        "stdout": post or {"status": 200, "json": {"content": "Hi! I am working."}, "latency_ms": 3200},
    }
    cmds[DELETE] = {"exit_code": 0, "stdout": delete or {"status": 200}}
    return fx


def _run(fx, env_file, approved=True, only=None, **overrides):
    runner = ReplayRunner(fx)
    ts = Troubleshooter(REPO_ROOT, runner)
    ov = {"client_id": CID, **overrides}
    if approved:
        ov["chat_test_approved"] = True
    d = ts.diagnose("check chat", env_file=env_file, log=False, only_checks=only or CHECKS, overrides=ov)
    return d, runner


def _e2e(d):
    return next(c for c in d.checks if c.check_id == "chat.e2e")


def _calls(runner, prefix):
    return [r.command for r in runner.history if r.command.startswith(prefix)]


def test_a_real_reply_passes_cleans_up_and_never_exposes_the_token(synthetic_fixture, synthetic_env_file):
    d, runner = _run(_fx(synthetic_fixture), synthetic_env_file)
    c = _e2e(d)
    assert c.status == "pass" and "model reply in 3.2 s" in c.summary
    assert c.evidence["reply_preview"] == "Hi! I am working." and c.evidence["test_session_deleted"] is True
    assert len(_calls(runner, "HTTP POST")) == 1 and len(_calls(runner, "HTTP DELETE")) == 1  # one message, one cleanup
    everything = json.dumps(d.to_dict()) + render_markdown(d.to_dict(), verbose=True)
    everything += " ".join(r.command + r.output_excerpt for r in runner.history)
    assert SECRET not in everything and "eyJhbGci" not in everything and "Bearer" not in everything


def test_nothing_is_sent_unless_approved_and_the_check_is_not_part_of_a_normal_health_check(
    synthetic_fixture, synthetic_env_file
):
    d, runner = _run(_fx(synthetic_fixture), synthetic_env_file, approved=False)
    assert _e2e(d).status == "skip" and "Not approved" in _e2e(d).summary
    assert not _calls(runner, "HTTP POST") and not any("get-access-token" in r.command for r in runner.history)
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(_fx(synthetic_fixture)))
    from accel_troubleshooter.engine import CHECKS as ALL
    from accel_troubleshooter.classify import extract

    assert "chat.e2e" not in ts.plan_checks(extract("x"), full=True)  # health never sends a real message
    planned = ts.plan_checks(extract("x"), full=False, only=["chat.e2e"])
    assert {"rg.state", "resources.state", "webapp.state", "chat.e2e"} <= set(planned) and ALL["chat.e2e"].opt_in


@pytest.mark.parametrize(
    "post,category,deleted",
    [
        ({"status": 401, "json": {"detail": "Invalid bearer token"}}, "auth-token", None),
        (
            {
                "status": 503,
                "json": {"detail": "Azure AI Foundry agents are not fully configured. Missing or empty settings: x"},
            },
            "config-missing",
            True,
        ),
        ({"status": 429, "json": {"detail": "Service temporarily unavailable due to high demand."}}, "throttled", True),
        (
            {
                "status": 500,
                "json": {
                    "detail": "AI agent error: DeploymentNotFound: The API deployment for this resource does not exist"
                },
            },
            "model-not-found",
            True,
        ),
        ({"status": 500, "json": {"detail": "Error sending message: timed out"}}, "timeout-network", True),
        ({"status": 500, "json": {"detail": "Error sending message: boom"}}, "other", True),
        ({"status": None, "error": "timed out"}, "timeout-network", True),  # a slow model: the session may exist
    ],
)
def test_each_failure_is_classified_and_only_cleaned_up_when_a_session_could_exist(
    synthetic_fixture, synthetic_env_file, post, category, deleted
):
    d, runner = _run(_fx(synthetic_fixture, post=post), synthetic_env_file)
    c = _e2e(d)
    assert c.evidence["category"] == category
    assert c.status == ("warn" if category == "throttled" else "fail")
    assert c.evidence["test_session_deleted"] is deleted
    assert bool(_calls(runner, "HTTP DELETE")) is bool(deleted)
    assert any(h.status != "possible" or category in ("other", "timeout-network") for h in d.hypotheses)


def test_failure_that_matches_an_existing_cause_is_added_to_it_not_duplicated(synthetic_fixture, synthetic_env_file):
    from test_ux import _policy_fixture

    fx = _fx(_policy_fixture(synthetic_fixture), post={"status": 500, "json": {"detail": COSMOS}})
    d, _ = _run(fx, synthetic_env_file, only=CHECKS + ["network.public_access"])
    ids = [h.id for h in d.hypotheses]
    assert ids[0] == "public-access-disabled" and "observed-cosmos-firewall" not in ids
    h = d.hypotheses[0]
    assert any("a real signed-in chat message failed (HTTP 500)" in r for r in h.rationale)
    assert "chat.e2e" in h.supporting_checks


def test_without_a_known_cause_the_failed_message_is_its_own_confirmed_finding(synthetic_fixture, synthetic_env_file):
    d, _ = _run(
        _fx(synthetic_fixture, post={"status": 401, "json": {"detail": "Invalid bearer token"}}), synthetic_env_file
    )
    h = next(x for x in d.hypotheses if x.id == "observed-auth-token")
    assert (
        h.status == "confirmed"
        and "chat.e2e" in h.supporting_checks
        and any("AppAuthentication.md" in r for r in h.doc_refs)
    )


def test_token_problems_explain_how_to_fix_and_send_nothing(synthetic_fixture, synthetic_env_file):
    token = {
        "exit_code": 1,
        "stderr": "AADSTS65001: The user or administrator has not consented to use the application",
    }
    d, runner = _run(_fx(synthetic_fixture, token=token), synthetic_env_file)
    c = _e2e(d)
    assert c.status == "warn" and "AADSTS65001" in c.summary and "did not run" in c.summary
    assert any("04b07795" in s for s in c.evidence["how_to_fix"]) and any(
        "--token-env" in s for s in c.evidence["how_to_fix"]
    )
    assert not _calls(runner, "HTTP POST")


def test_missing_client_id_stopped_app_and_token_from_environment(synthetic_fixture, synthetic_env_file, monkeypatch):
    runner = ReplayRunner(_fx(synthetic_fixture))
    d = Troubleshooter(REPO_ROOT, runner).diagnose(
        "x", env_file=synthetic_env_file, log=False, only_checks=CHECKS, overrides={"chat_test_approved": True}
    )
    assert _e2e(d).status == "skip" and "AZURE_ENV_ENTRA_CLIENT_ID" in _e2e(d).summary

    stopped = _fx(synthetic_fixture)
    stopped["commands"]["webapp show --resource-group rg-demo --name api-chat-demo"]["stdout"]["state"] = "Stopped"
    assert "stopped" in _e2e(_run(stopped, synthetic_env_file)[0]).summary

    monkeypatch.setenv("TS_TEST_TOKEN", "Bearer " + SECRET)
    d, runner = _run(_fx(synthetic_fixture), synthetic_env_file, token_env="TS_TEST_TOKEN")
    assert _e2e(d).status == "pass" and not any("get-access-token" in r.command for r in runner.history)
    monkeypatch.setenv("TS_TEST_TOKEN", "")
    assert _e2e(_run(_fx(synthetic_fixture), synthetic_env_file, token_env="TS_TEST_TOKEN")[0]).status == "skip"


def test_cli_chat_test_needs_approval_and_prints_the_result(synthetic_fixture, synthetic_env_file, tmp_path, capsys):
    path = tmp_path / "fx.json"
    path.write_text(json.dumps(_fx(synthetic_fixture)), encoding="utf-8")
    base = [
        "chat-test",
        "--replay",
        str(path),
        "--env-file",
        synthetic_env_file,
        "--client-id",
        CID,
        "--plain",
        "--no-prompt",
    ]
    assert main(base) == 3 and "Re-run with --yes" in capsys.readouterr().err  # never sends without approval
    rc = main(base + ["--yes"])
    out = capsys.readouterr().out
    assert rc in (0, 1) and "real chat message" in out and "model reply" in out


class _Handler(BaseHTTPRequestHandler):
    seen = []

    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append(("POST", self.path, self.headers.get("Authorization"), payload))
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            return self._reply(401, {"detail": "Invalid bearer token"})
        if "fail" in payload["content"]:
            return self._reply(500, {"detail": COSMOS})
        self._reply(200, {"id": payload["session_id"], "content": "pong"})

    def do_DELETE(self):
        _Handler.seen.append(("DELETE", self.path, self.headers.get("Authorization"), None))
        self._reply(200, {"message": "deleted"})

    def log_message(self, *a):
        pass


def test_real_http_path_sends_the_bearer_token_but_never_records_it():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        runner = AzRunner()
        ok, res, rec = runner.http_request(
            "POST",
            base + "/api/chat/message",
            {"Authorization": f"Bearer {SECRET}"},
            {"content": "hi", "session_id": "s1"},
        )
        assert ok and res["status"] == 200 and res["json"]["content"] == "pong"
        assert _Handler.seen[-1][2] == f"Bearer {SECRET}" and _Handler.seen[-1][3]["session_id"] == "s1"
        assert SECRET not in rec.command + rec.output_excerpt and "Bearer" not in rec.command
        ok, res, _ = runner.http_request(
            "POST",
            base + "/api/chat/message",
            {"Authorization": f"Bearer {SECRET}"},
            {"content": "fail", "session_id": "s2"},
        )
        assert not ok and res["status"] == 500 and "Cosmos DB account firewall" in res["json"]["detail"]
        ok, res, _ = runner.http_request(
            "POST", base + "/api/chat/message", {"Authorization": "Bearer wrong"}, {"content": "x", "session_id": "s3"}
        )
        assert res["status"] == 401
        assert runner.http_request("DELETE", base + "/api/chat/sessions/s1", {"Authorization": "x"})[1]["status"] == 200
    finally:
        server.shutdown()
    gone = AzRunner().http_request("POST", base + "/api/chat/message", None, {"content": "x"}, timeout=2)[1]
    assert gone["status"] is None and gone["error"]  # server down: a clear error, not a crash


def test_http_requests_and_tokens_are_restricted():
    runner = AzRunner()
    with pytest.raises(CommandNotAllowed):
        runner.http_request("POST", "https://example.com/api", None, {})  # host not on the allow-list
    with pytest.raises(CommandNotAllowed):
        runner.http_request("PUT", "https://x.azurewebsites.net/api", None, {})  # only GET/POST/DELETE
    with pytest.raises(CommandNotAllowed):
        runner.get_token("https://management.azure.com/.default")  # only an app's own API scope
    with pytest.raises(CommandNotAllowed):
        ReplayRunner({}).get_token("api://not-a-guid/x")
