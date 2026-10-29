import json
import shutil
import sys
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_ROOT.parent
sys.path.insert(0, str(TOOL_ROOT))

FIXTURES = TOOL_ROOT / "evals" / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_run_log(tmp_path, monkeypatch):
    from accel_troubleshooter import telemetry

    monkeypatch.setattr(telemetry, "RUNS_FILE", tmp_path / "runs.jsonl")


@pytest.fixture
def synthetic_fixture():
    return json.loads((FIXTURES / "synthetic-base.json").read_text(encoding="utf-8"))


@pytest.fixture
def synthetic_env_file():
    return str(FIXTURES / "synthetic-env.txt")


@pytest.fixture
def chatbot_copy(tmp_path):
    """Minimal non-git copy of the chatbot repo (azure.yaml, infra entry points, docs, markers)."""
    dst = tmp_path / "ccsa"
    for rel in [
        "azure.yaml",
        "infra/main.bicep",
        "infra/main.parameters.json",
        "chat-app/backend/app/foundry_client.py",
        "scenarios/scenario_loader.py",
        "infra/scripts/pre-provision/checkquota.sh",
        "infra/scripts/pre-provision/quota_check_params.sh",
        "infra/scripts/post-provision/data_scripts/03_write_products_to_cosmos.py",
        "infra/scripts/post-provision/data_scripts/run_upload_data_scripts.ps1",
    ]:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dst / rel)
    shutil.copytree(REPO_ROOT / "documents", dst / "documents", ignore=shutil.ignore_patterns("Images"))
    return dst
