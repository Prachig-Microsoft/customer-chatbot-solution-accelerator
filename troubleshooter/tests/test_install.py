import json
import subprocess
import sys

import pytest

from accel_troubleshooter import install as install_mod
from conftest import TOOL_ROOT


def _fake_accelerator(tmp_path):
    repo = tmp_path / "other-accelerator"
    repo.mkdir()
    (repo / "azure.yaml").write_text("name: content-processing-solution-accelerator\n", encoding="utf-8")
    return repo


def test_install_copies_the_tool_agent_skills_and_mcp_entry(tmp_path):
    repo = _fake_accelerator(tmp_path)
    res = install_mod.install(repo)
    assert (repo / "troubleshooter" / "accel_troubleshooter" / "cli.py").is_file()
    assert (repo / "troubleshooter" / "mcp_launcher.py").is_file()
    assert not (repo / "troubleshooter" / "tests").exists() and not (repo / "troubleshooter" / ".venv").exists()
    assert not list((repo / "troubleshooter").rglob("__pycache__"))
    assert (repo / ".github" / "agents" / "accelerator-troubleshooter.agent.md").is_file()
    assert {p.parent.name for p in (repo / ".github" / "skills").glob("*/SKILL.md")} == set(install_mod.SKILL_NAMES)
    cfg = json.loads((repo / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert cfg["servers"]["accelerator-troubleshooter"]["args"] == ["troubleshooter/mcp_launcher.py"]
    assert res["manual_mcp"] is False


def test_install_merges_into_an_existing_mcp_file_and_keeps_other_servers(tmp_path):
    repo = _fake_accelerator(tmp_path)
    (repo / ".vscode").mkdir()
    (repo / ".vscode" / "mcp.json").write_text(
        json.dumps({"servers": {"other": {"command": "x"}}, "inputs": []}), encoding="utf-8"
    )
    install_mod.install(repo)
    cfg = json.loads((repo / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert set(cfg["servers"]) == {"other", "accelerator-troubleshooter"} and cfg["inputs"] == []


def test_install_never_overwrites_without_force_and_handles_commented_json(tmp_path):
    repo = _fake_accelerator(tmp_path)
    install_mod.install(repo)
    agent = repo / ".github" / "agents" / "accelerator-troubleshooter.agent.md"
    agent.write_text("my own edits", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        install_mod.install(repo)
    res = install_mod.install(repo, force=True)
    assert agent.read_text(encoding="utf-8") != "my own edits" and res["steps"]
    (repo / ".vscode" / "mcp.json").write_text("// comment\n{}", encoding="utf-8")
    assert install_mod.install(repo, force=True)["manual_mcp"] is True


def test_install_dry_run_writes_nothing_and_refuses_bad_targets(tmp_path):
    repo = _fake_accelerator(tmp_path)
    res = install_mod.install(repo, dry_run=True)
    assert not (repo / "troubleshooter").exists() and not (repo / ".github").exists() and res["dry_run"]
    with pytest.raises(ValueError):
        install_mod.install(tmp_path / "missing")
    with pytest.raises(ValueError, match="already lives"):
        install_mod.install(TOOL_ROOT.parent)


def test_installed_copy_runs_on_its_own_and_recognises_the_accelerator(tmp_path):
    repo = _fake_accelerator(tmp_path)
    install_mod.install(repo)
    out = subprocess.run(
        [sys.executable, "-m", "accel_troubleshooter", "identify", "azd up failed"],
        cwd=repo / "troubleshooter",
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert out.returncode in (0, 2), out.stderr  # 2 = asks which accelerator; never a crash
    assert json.loads(out.stdout)["accelerator"] in ("content-processing", None)
