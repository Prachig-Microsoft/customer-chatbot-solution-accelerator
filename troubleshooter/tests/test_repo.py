import json
from pathlib import Path

from accel_troubleshooter.repo import load_azd_env, parse_bicep_params, parse_parameters_env_map, parse_usage_names

from conftest import REPO_ROOT


def test_bicep_param_parsing_on_real_template():
    p = parse_bicep_params(REPO_ROOT / "infra" / "main.bicep")
    assert "eastus2" in p["azureAiServiceLocation"].allowed
    assert any(u.startswith("OpenAI.GlobalStandard.gpt-5.4-mini") for u in p["azureAiServiceLocation"].usage_names)
    assert p["gptDeploymentCapacity"].min_value == 10 and isinstance(p["gptDeploymentCapacity"].default, int)
    assert p["deploymentType"].allowed == ["Standard", "GlobalStandard"]
    assert "australiaeast" in p["location"].allowed


def test_bicep_parser_handles_metadata_blocks(tmp_path):
    f = tmp_path / "m.bicep"
    f.write_text(
        """
@allowed([
  'a'
  'b'
])
@metadata({
  azd: {
    type: 'location'
    usageName: [
      'OpenAI.Standard.text-embedding-3-small,100'
    ]
  }
})
param loc string

var x = 1
@minValue(5)
param cap int = 7
""",
        encoding="utf-8",
    )
    p = parse_bicep_params(f)
    assert p["loc"].allowed == ["a", "b"]
    assert parse_usage_names(p["loc"].usage_names) == [
        {
            "usage_name": "OpenAI.Standard.text-embedding-3-small",
            "sku": "Standard",
            "model": "text-embedding-3-small",
            "capacity": 100,
        }
    ]
    assert p["cap"].default == 7 and p["cap"].min_value == 5 and p["cap"].allowed == []


def test_parameters_env_mapping():
    m = parse_parameters_env_map(REPO_ROOT / "infra" / "main.parameters.json")
    assert m["gptDeploymentCapacity"]["env"] == "AZURE_ENV_GPT_MODEL_CAPACITY"
    assert m["deploymentFlavor"] == {"env": "AZURE_ENV_DEPLOYMENT_FLAVOR", "default": "bicep"}


def test_azd_env_reads_dotenv_and_infra_parameters(tmp_path):
    env = tmp_path / ".azure" / "dev"
    env.mkdir(parents=True)
    (tmp_path / ".azure" / "config.json").write_text(json.dumps({"defaultEnvironment": "dev"}), encoding="utf-8")
    (env / ".env").write_text('AZURE_RESOURCE_GROUP="rg-dev"\nFOO=bar\n', encoding="utf-8")
    (env / "config.json").write_text(
        json.dumps({"infra": {"parameters": {"azureAiServiceLocation": "swedencentral"}}}), encoding="utf-8"
    )
    e = load_azd_env(Path(tmp_path))
    assert e.name == "dev" and e.get("AZURE_RESOURCE_GROUP") == "rg-dev"
    assert e.params["azureAiServiceLocation"] == "swedencentral"


def test_missing_azd_env_is_handled(tmp_path):
    assert not load_azd_env(tmp_path).found
