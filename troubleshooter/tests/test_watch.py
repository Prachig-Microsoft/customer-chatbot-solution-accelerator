import json
from datetime import datetime, timedelta, timezone

from accel_troubleshooter.cli import main


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _replay(tmp_path, synthetic_fixture, deployments, ops=None, name="main"):
    fx = json.loads(json.dumps(synthetic_fixture))
    fx["commands"]["deployment group list --resource-group rg-demo"] = {"exit_code": 0, "stdout": deployments}
    if ops is not None:
        key = f"deployment operation group list --resource-group rg-demo --name {name}"
        fx["commands"][key] = {"exit_code": 0, "stdout": ops}
    path = tmp_path / "fx.json"
    path.write_text(json.dumps(fx), encoding="utf-8")
    return str(path)


def _watch(replay, env_file, *extra):
    argv = ["watch", "--replay", replay, "--env-file", env_file, "--interval", "0", "--timeout", "1", "--plain"]
    return main(argv + list(extra))


def test_watch_diagnoses_at_the_first_failed_operation_while_still_running(
    tmp_path, synthetic_fixture, synthetic_env_file, capsys
):
    msg = "The deployment requires 5000 quota units, but only 338 quota units are available in gpt-5.4-mini."
    ops = [
        {
            "properties": {
                "provisioningState": "Failed",
                "timestamp": _iso(1),
                "targetResource": {
                    "resourceType": "Microsoft.CognitiveServices/accounts/deployments",
                    "resourceName": "g",
                },
                "statusMessage": {"error": {"code": "InsufficientQuota", "message": msg}},
            }
        }
    ]
    deps = [{"name": "main", "properties": {"provisioningState": "Running", "timestamp": _iso(2)}}]
    rc = _watch(_replay(tmp_path, synthetic_fixture, deps, ops), synthetic_env_file)
    out = capsys.readouterr().out
    assert rc == 1 and "already failed" in out and "InsufficientQuota" in out
    assert "CAUSE" in out and "Insufficient Azure OpenAI model quota" in out
    assert "azd stops at the first failed step" in out


def test_watch_finishes_when_a_recent_deployment_succeeded(tmp_path, synthetic_fixture, synthetic_env_file, capsys):
    deps = [{"name": "main", "properties": {"provisioningState": "Succeeded", "timestamp": _iso(1)}}]
    rc = _watch(_replay(tmp_path, synthetic_fixture, deps), synthetic_env_file)
    out = capsys.readouterr().out
    assert rc == 0 and "Deployment finished" in out and "health" in out


def test_watch_keeps_waiting_when_only_an_old_deployment_exists(
    tmp_path, synthetic_fixture, synthetic_env_file, capsys, monkeypatch
):
    import accel_troubleshooter.cli as cli

    ticks = iter([0.0, 1.0, 2.0, 100.0])  # the clock jumps past --timeout after a few checks
    monkeypatch.setattr(cli.time, "time", lambda: next(ticks, 1000.0))
    deps = [{"name": "main", "properties": {"provisioningState": "Succeeded", "timestamp": _iso(600)}}]
    rc = _watch(_replay(tmp_path, synthetic_fixture, deps), synthetic_env_file)
    out = capsys.readouterr().out
    assert "waiting for `azd up` to start" in out and "Deployment finished" not in out and rc == 1
