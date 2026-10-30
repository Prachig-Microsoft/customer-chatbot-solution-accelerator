import json
from types import SimpleNamespace

from accel_troubleshooter.diagnostics.repo_checks import effective_model_requirements
from accel_troubleshooter.repo import AzdEnv

PROFILE = {
    "iac": {
        "main": "infra/main.bicep",
        "parameters": "infra/main.parameters.json",
        "ai_location_param": "aiLocation",
        "location_param": "location",
        "models": [
            {
                "role": "chat",
                "name_param": "gptModelName",
                "version_param": "gptModelVersion",
                "capacity_param": "gptCapacity",
                "sku_fixed": "GlobalStandard",
            }
        ],
    }
}

BICEP = """\
param location string
param aiLocation string
param gptModelName string = 'gpt-5.4-mini'
param gptModelVersion string = '2026-03-17'
param gptCapacity int = 50
"""


def _requirement(tmp_path, parameters, env=None, params=None, overrides=None, bicep=BICEP):
    (tmp_path / "infra").mkdir(exist_ok=True)
    (tmp_path / "infra" / "main.bicep").write_text(bicep, encoding="utf-8")
    (tmp_path / "infra" / "main.parameters.json").write_text(json.dumps({"parameters": parameters}), encoding="utf-8")
    ctx = SimpleNamespace(
        profile=PROFILE,
        repo=tmp_path,
        overrides={"capacity_overrides": overrides or {}},
        env=AzdEnv(values=env or {}, params=params or {}),
        facts={},
    )
    return effective_model_requirements(ctx)[0]


def test_required_capacity_follows_what_azd_will_really_deploy(tmp_path):
    cap = {"gptCapacity": {"value": "${AZURE_ENV_GPT_MODEL_CAPACITY}"}}
    # 1. nothing set anywhere: the Bicep default
    r = _requirement(tmp_path, cap)
    assert r["capacity"] == 50 and "main.bicep" in r["capacity_source"]
    # 2. a ${VAR=default} written in the parameters file beats the Bicep default
    r = _requirement(tmp_path, {"gptCapacity": {"value": "${AZURE_ENV_GPT_MODEL_CAPACITY=80}"}})
    assert r["capacity"] == 80 and "main.parameters.json" in r["capacity_source"]
    # 3. a literal edited into the parameters file beats the Bicep default too
    r = _requirement(tmp_path, {"gptCapacity": {"value": 120}})
    assert r["capacity"] == 120 and "main.parameters.json" in r["capacity_source"]
    # 4. azd config.json parameters beat the parameters file
    r = _requirement(tmp_path, {"gptCapacity": {"value": 120}}, params={"gptCapacity": 30})
    assert r["capacity"] == 30 and "config.json" in r["capacity_source"]
    # 5. an azd env variable beats everything stored in files
    r = _requirement(tmp_path, cap, env={"AZURE_ENV_GPT_MODEL_CAPACITY": "22"}, params={"gptCapacity": 30})
    assert r["capacity"] == 22 and r["capacity_source"] == "azd env AZURE_ENV_GPT_MODEL_CAPACITY"
    # 6. a what-if override (used to test capacities before changing anything) beats all of it
    r = _requirement(tmp_path, cap, env={"AZURE_ENV_GPT_MODEL_CAPACITY": "22"}, overrides={"gptCapacity": 5})
    assert r["capacity"] == 5 and r["capacity_source"] == "what-if override"


def test_edits_are_picked_up_on_the_next_run(tmp_path):
    cap = {"gptCapacity": {"value": "${AZURE_ENV_GPT_MODEL_CAPACITY}"}}
    assert _requirement(tmp_path, cap)["capacity"] == 50
    edited = BICEP.replace("= 50", "= 75")
    assert _requirement(tmp_path, cap, bicep=edited)["capacity"] == 75  # nothing is cached between runs
