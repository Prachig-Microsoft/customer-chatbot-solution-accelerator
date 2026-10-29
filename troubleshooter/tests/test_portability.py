"""Portability: the troubleshooter folder can be dropped into any supported accelerator repo."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from accel_troubleshooter.compact import render_compact
from accel_troubleshooter.engine import Troubleshooter
from accel_troubleshooter.azcli import OfflineRunner, ReplayRunner

from conftest import REPO_ROOT, TOOL_ROOT

SIBLINGS = {
    "content-processing": Path(REPO_ROOT).parent / "content-processing-solution-accelerator",
    "chat-with-your-data": Path(REPO_ROOT).parent / "CWYD" / "chat-with-your-data-solution-accelerator",
}


def _copy_tool_into(repo_copy: Path) -> Path:
    dst = repo_copy / "troubleshooter"
    shutil.copytree(
        TOOL_ROOT,
        dst,
        ignore=shutil.ignore_patterns(".venv", ".cache", ".runs", "demo-output", "__pycache__", "knowledge"),
    )
    return dst


def _light_copy(src: Path, dst: Path) -> None:
    """Copy docs/infra/azure.yaml only (enough for fingerprinting + indexing), under a different folder name."""
    for rel in ("azure.yaml", "infra", "docs", "README.md", "src"):
        s = src / rel
        if s.is_file():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dst / rel)
        elif s.is_dir() and rel != "src":
            shutil.copytree(s, dst / rel, ignore=shutil.ignore_patterns("*.png", "*.jpg", "node_modules"))
    (dst / "src").mkdir(exist_ok=True)


@pytest.mark.parametrize("accel", list(SIBLINGS))
def test_tool_copied_into_another_accelerator_finds_that_repo(accel, tmp_path):
    src = SIBLINGS[accel]
    if not src.exists():
        pytest.skip(f"{accel} clone not available")
    repo_copy = tmp_path / "my-renamed-clone"
    _light_copy(src, repo_copy)
    tool = _copy_tool_into(repo_copy)
    ts = Troubleshooter(repo_copy, OfflineRunner())
    d = ts.diagnose("deployment failed with InsufficientQuota", env_file="none", log=False)
    assert d.accelerator["id"] == accel
    assert d.accelerator["repo_path"] == str(repo_copy.resolve()), d.limitations
    assert d.docs and all(not p.ref.startswith("..") for p in d.docs)
    assert tool.is_dir()


def test_copied_tool_runs_as_a_cli_from_inside_the_other_repo(tmp_path):
    src = SIBLINGS["content-processing"]
    if not src.exists():
        pytest.skip("content-processing clone not available")
    repo_copy = tmp_path / "cps"
    _light_copy(src, repo_copy)
    tool = _copy_tool_into(repo_copy)
    out = subprocess.run(
        [sys.executable, "-m", "accel_troubleshooter", "search", "InsufficientQuota", "-k", "1", "--format", "json"],
        cwd=tool,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    hit = json.loads(out.stdout)[0]
    assert hit["path"] == "docs/TroubleShootingSteps.md" and "InsufficientQuota" in hit["error_codes"]


def test_preflight_never_says_go_when_nothing_was_checked(synthetic_fixture, tmp_path):
    ts = Troubleshooter(REPO_ROOT, OfflineRunner())
    d = ts.diagnose("preflight before azd up", env_file="none", log=False, only_checks=["az.context", "quota.models"])
    text = render_compact(d.to_dict(), "preflight")
    assert "NOT CHECKED" in text and "GO" not in text.splitlines()[0]
    assert "quota.models" not in text and len(text.splitlines()) <= 6


def test_repo_not_found_is_reported_not_called_go(tmp_path):
    ts = Troubleshooter(tmp_path, ReplayRunner({"commands": {}}))
    d = ts.diagnose("deployment failed", accelerator="content-processing", env_file="none", log=False)
    text = render_compact(d.to_dict(), "preflight")
    assert "NOT CHECKED" in text and "not found locally" in text
