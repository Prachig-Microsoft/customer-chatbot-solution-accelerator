"""An accelerator with no shipped profile gets one derived from its repository."""

import json

from accel_troubleshooter.autoprofile import derive_profile
from accel_troubleshooter.azcli import OfflineRunner
from accel_troubleshooter.cli import main
from accel_troubleshooter.engine import Troubleshooter

BICEP = """targetScope = 'resourceGroup'
param location string = resourceGroup().location
@allowed([
  'eastus2'
  'swedencentral'
])
@metadata({
  azd: {
    type: 'location'
    usageName: [
      'OpenAI.GlobalStandard.gpt-4o,150'
      'OpenAI.GlobalStandard.text-embedding-3-large,30'
    ]
  }
})
param aiDeploymentsLocation string
param gptModelName string = 'gpt-4o'
param gptModelVersion string = '2024-11-20'
param gptDeploymentCapacity int = 150
param embeddingDeploymentCapacity int = 30
module search 'br/public:avm/res/search/search-service:0.9.0' = { name: 'search' }
resource st 'Microsoft.Storage/storageAccounts@2023-01-01' = { name: 'st1', location: location }
"""


def _accelerator(tmp_path, name="acme-insights-accelerator"):
    (tmp_path / "infra").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "azure.yaml").write_text(f"name: {name}\n", encoding="utf-8")
    (tmp_path / "infra" / "main.bicep").write_text(BICEP, encoding="utf-8")
    (tmp_path / "infra" / "main.parameters.json").write_text(
        json.dumps({"parameters": {"aiDeploymentsLocation": {"value": "${AZURE_AI_LOCATION}"}}}), encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# Acme Insights\n\nTurns data into answers.\n", encoding="utf-8")
    (tmp_path / "docs" / "TroubleShooting.md").write_text("# Troubleshooting\n\n## Quota\nRequest more.\n", "utf-8")
    (tmp_path / "docs" / "DeploymentGuide.md").write_text("# Deploy\n\nRun azd up.\n", encoding="utf-8")
    return tmp_path


def test_profile_is_read_from_azure_yaml_bicep_parameters_and_docs(tmp_path):
    p = derive_profile(_accelerator(tmp_path))
    assert p["id"] == "acme-insights-accelerator" and p["diagnostics"] == "generic" and p["auto_derived"]
    assert (
        p["docs"]["troubleshooting"] == ["docs/TroubleShooting.md"]
        and "docs/DeploymentGuide.md" in p["docs"]["deployment"]
    )
    assert p["iac"]["ai_location_param"] == "aiDeploymentsLocation"
    chat, emb = p["iac"]["models"]
    assert (chat["role"], chat["name_fixed"], chat["capacity_fixed"], chat["capacity_param"]) == (
        "chat",
        "gpt-4o",
        150,
        "gptDeploymentCapacity",
    )
    assert (
        chat["version_param"] == "gptModelVersion" and "version_param" not in emb
    )  # only trusted when it names the model
    assert emb["role"] == "embedding" and emb["capacity_param"] == "embeddingDeploymentCapacity"
    assert p["azd_env_keys"]["ai_location"] == "AZURE_AI_LOCATION"
    assert {"Microsoft.Search", "Microsoft.Storage"} <= set(p["required_providers"])


def test_not_an_accelerator_gives_no_profile(tmp_path):
    (tmp_path / "README.md").write_text("# just notes\n", encoding="utf-8")
    assert derive_profile(tmp_path) is None


def test_unknown_accelerator_is_identified_without_asking(tmp_path):
    repo = _accelerator(tmp_path)
    ts = Troubleshooter(repo, OfflineRunner())
    assert "acme-insights-accelerator" in ts.profiles
    ident = ts.identify("azd up failed", None)
    assert ident["confident"] and ident["accelerator"] == "acme-insights-accelerator"
    d = ts.diagnose("InsufficientQuota while deploying", log=False)
    assert d.accelerator["id"] == "acme-insights-accelerator"
    assert any("derived from its repository" in x for x in d.limitations)


def test_shipped_profiles_win_and_no_profile_is_derived_for_them(synthetic_fixture):
    from conftest import REPO_ROOT

    ts = Troubleshooter(REPO_ROOT, OfflineRunner())
    assert not any(p.get("auto_derived") for p in ts.profiles.values())


def test_init_profile_saves_a_reviewable_file_and_refuses_to_overwrite(tmp_path, capsys):
    repo = _accelerator(tmp_path)
    out = tmp_path / "saved.json"
    assert main(["--workspace", str(repo), "init-profile", "--out", str(out)]) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["id"] == "acme-insights-accelerator" and "auto_derived" not in saved and "_profile_file" not in saved
    assert "gpt-4o GlobalStandard 150K" in capsys.readouterr().out
    assert main(["--workspace", str(repo), "init-profile", "--out", str(out)]) == 3  # exists: needs --force
    assert main(["--workspace", str(repo), "init-profile", "--out", str(out), "--force"]) == 0


def test_an_accelerator_with_no_models_gets_go_not_not_checked(tmp_path):
    from accel_troubleshooter.azcli import ReplayRunner
    from accel_troubleshooter.compact import render_compact

    (tmp_path / "infra").mkdir()
    (tmp_path / "azure.yaml").write_text("name: stream-demo\n", encoding="utf-8")
    (tmp_path / "infra" / "main.bicep").write_text(
        "param location string = resourceGroup().location\n"
        "resource hub 'Microsoft.EventHub/namespaces@2024-01-01' = { name: 'eh', location: location }\n",
        encoding="utf-8",
    )
    profile = derive_profile(tmp_path)
    assert profile["iac"]["models"] == [] and profile["iac"]["no_model_quota"]
    fx = {
        "commands": {"account show": {"exit_code": 0, "stdout": {"name": "sub", "id": "x", "user": {"type": "user"}}}}
    }
    ts = Troubleshooter(tmp_path, ReplayRunner(fx))
    d = ts.diagnose(
        "preflight before azd up", log=False, only_checks=["az.context", "quota.models", "models.availability"]
    )
    text = render_compact(d.to_dict(), "preflight")
    assert "PREFLIGHT: ✅ GO" in text and "Model quota: not applicable" in text and "NOT CHECKED" not in text


def test_models_the_bicep_mentions_but_we_could_not_read_are_not_waved_through(tmp_path):
    (tmp_path / "infra").mkdir()
    (tmp_path / "azure.yaml").write_text("name: llm-demo\n", encoding="utf-8")
    (tmp_path / "infra" / "main.bicep").write_text(
        "resource d 'Microsoft.CognitiveServices/accounts/deployments@2024-01-01' = { name: 'gpt-4o' }\n",
        encoding="utf-8",
    )
    assert "no_model_quota" not in derive_profile(tmp_path)["iac"]  # a model is deployed: quota must be checked
