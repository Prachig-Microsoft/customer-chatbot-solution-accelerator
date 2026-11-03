"""Regression tests for defects found by an independent review of the token / HTTP / chat-test code."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from accel_troubleshooter.azcli import AzRunner, CommandNotAllowed, ReplayRunner, _host_allowed
from accel_troubleshooter.diagnostics.base import CHECKS, CheckSpec, DiagContext, run_check
from accel_troubleshooter.engine import Troubleshooter

from conftest import REPO_ROOT
from test_chat_e2e import CHECKS as E2E_CHECKS
from test_chat_e2e import CID, DELETE, POST, SECRET, _calls, _e2e, _fx, _run


def _serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_a_redirect_never_forwards_the_bearer_token_to_another_host():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    target, target_url = _serve(Target)

    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", target_url + "/stolen")
            self.end_headers()

        def log_message(self, *a):
            pass

    redirector, url = _serve(Redirector)
    try:
        ok, res, rec = AzRunner().http_request(
            "POST", url + "/api/chat/message", {"Authorization": f"Bearer {SECRET}"}, {"x": 1}
        )
    finally:
        redirector.shutdown()
        target.shutdown()
    assert seen == [] and not ok and res["status"] == 302  # the redirect is reported, not followed


def test_look_alike_hosts_are_refused():
    assert _host_allowed("localhost") and _host_allowed("127.0.0.1") and _host_allowed("api-x.azurewebsites.net")
    for bad in (
        "evillocalhost",
        "notlocalhost",
        "azurewebsites.net.evil.com",
        "evil-azurewebsites.net",
        "x.example.com",
    ):
        assert not _host_allowed(bad), bad
    with pytest.raises(CommandNotAllowed):
        AzRunner().http_request("GET", "http://evillocalhost/x")


def test_a_token_with_line_breaks_is_refused_without_echoing_it():
    runner = AzRunner()
    with pytest.raises(CommandNotAllowed) as exc:
        runner.http_request("POST", "http://localhost:9/p", {"Authorization": f"Bearer {SECRET}\nX-Evil: 1"}, {})
    assert SECRET not in str(exc.value)


def test_a_malformed_token_in_the_environment_is_skipped_with_a_message_that_has_no_token(
    synthetic_fixture, synthetic_env_file, monkeypatch
):
    monkeypatch.setenv("TS_BAD_TOKEN", SECRET + "\nsecond line")
    d, runner = _run(_fx(synthetic_fixture), synthetic_env_file, token_env="TS_BAD_TOKEN")
    c = _e2e(d)
    assert c.status == "skip" and "single-line token" in c.summary and SECRET not in json.dumps(d.to_dict())
    assert not _calls(runner, "HTTP POST")


def test_exception_text_from_a_failing_check_is_redacted(synthetic_fixture, synthetic_env_file):
    def boom(ctx):
        raise ValueError(f"Invalid header value b'Bearer {SECRET}'")

    spec = CheckSpec("test.boom", "boom", boom, [], False, False, 99)
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    ctx = DiagContext(
        profile=ts.profiles["customer-chatbot"], repo=REPO_ROOT, issue=None, env=None, runner=ts.runner, overrides={}
    )
    res = run_check(spec, ctx)
    assert res.status == "error" and SECRET not in res.summary and "<redacted" in res.summary
    assert "test.boom" not in CHECKS  # the helper spec was never registered


def test_the_token_goes_only_to_the_host_azure_reports_never_to_a_url_from_a_local_file(synthetic_fixture, tmp_path):
    env = (REPO_ROOT / "troubleshooter" / "evals" / "fixtures" / "synthetic-env.txt").read_text(encoding="utf-8")
    tampered = tmp_path / "tampered.env"
    tampered.write_text(
        env.replace("https://api-chat-demo.azurewebsites.net", "https://evil.azurewebsites.net"), encoding="utf-8"
    )
    d, runner = _run(_fx(synthetic_fixture), str(tampered))
    posts = _calls(runner, "HTTP POST")
    assert len(posts) == 1 and "api-chat-demo.azurewebsites.net" in posts[0] and "evil" not in posts[0]
    assert _e2e(d).status == "pass"


def test_a_platform_503_is_not_reported_as_missing_configuration(synthetic_fixture, synthetic_env_file):
    platform = {"status": 503, "json": None, "body_excerpt": "<html>Service Unavailable</html>"}
    d, _ = _run(_fx(synthetic_fixture, post=platform), synthetic_env_file)
    assert _e2e(d).evidence["category"] == "other"
    assert not any(h.id == "observed-config-missing" for h in d.hypotheses)
    configured = {
        "status": 503,
        "json": {"detail": "Azure AI Foundry is not configured: 'azure_foundry_endpoint' missing"},
    }
    d, _ = _run(_fx(synthetic_fixture, post=configured), synthetic_env_file)
    assert _e2e(d).evidence["category"] == "config-missing"  # the backend's own 503 names the setting


def test_cleanup_is_attempted_after_a_timeout_and_a_failed_delete_is_reported(synthetic_fixture, synthetic_env_file):
    fx = _fx(synthetic_fixture, post={"status": None, "error": "timed out"}, delete={"status": 500})
    d, runner = _run(fx, synthetic_env_file)
    ev = _e2e(d).evidence
    assert len(_calls(runner, "HTTP DELETE")) == 1 and ev["test_session_deleted"] is False
    assert "may still exist" in ev["cleanup_note"] and "troubleshooter-probe-" in ev["cleanup_note"]


def test_a_404_on_delete_means_there_was_nothing_to_clean(synthetic_fixture, synthetic_env_file):
    d, _ = _run(
        _fx(synthetic_fixture, post={"status": 500, "json": {"detail": "boom"}}, delete={"status": 404}),
        synthetic_env_file,
    )
    assert _e2e(d).evidence["test_session_deleted"] is True and "cleanup_note" not in _e2e(d).evidence


def test_no_cleanup_when_the_request_was_rejected_before_the_handler(synthetic_fixture, synthetic_env_file):
    for status in (401, 403):
        d, runner = _run(_fx(synthetic_fixture, post={"status": status, "json": {"detail": "no"}}), synthetic_env_file)
        assert not _calls(runner, "HTTP DELETE") and _e2e(d).evidence["test_session_deleted"] is None
    assert POST and DELETE and CID and E2E_CHECKS  # shared constants stay in use
