"""System prerequisites found in an accelerator's hooks, scripts and Dev Container, and how preflight reports them."""

import json

import pytest

from accel_troubleshooter import prereqscan
from accel_troubleshooter.azcli import CommandNotAllowed, OfflineRunner, ReplayRunner
from accel_troubleshooter.compact import render_compact
from accel_troubleshooter.engine import Troubleshooter
from accel_troubleshooter.prereqscan import scan_components, setup_route

TOOLS_OK = {"azd": "1.34.0", "az": "2.88.0", "python": "3.11.9"}


def _repo(tmp_path, hooked=True, devcontainer=True, app_only=False):
    """A CKM-like accelerator: a post-provision hook pip-installs a requirements file that lists pyodbc."""
    (tmp_path / "infra" / "scripts" / "post").mkdir(parents=True)
    (tmp_path / "src" / "api").mkdir(parents=True)
    hook = "      run: pip install -r ./infra/scripts/post/requirements.txt\n" if hooked else "      run: echo done\n"
    (tmp_path / "azure.yaml").write_text(
        f'name: kmx\nrequiredVersions:\n  azd: ">=1.10.0"\nhooks:\n  postprovision:\n    posix:\n{hook}',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# KMX\n\n## Prerequisites\n- Python 3.11+\n", encoding="utf-8")
    if not app_only:
        (tmp_path / "infra" / "scripts" / "post" / "requirements.txt").write_text(
            "azure-identity==1.19.0\npyodbc==5.2.0  # SQL\n", encoding="utf-8"
        )
    (tmp_path / "src" / "api" / "requirements.txt").write_text("pyodbc\n", encoding="utf-8")
    if devcontainer:
        (tmp_path / ".devcontainer").mkdir()
        (tmp_path / ".devcontainer" / "devcontainer.json").write_text(
            json.dumps(
                {
                    "image": "mcr.microsoft.com/devcontainers/python:3.11",
                    "features": {"ghcr.io/devcontainers-extra/features/mssql-odbc-driver:1": {"version": "18"}},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return tmp_path


def _fixture(odbc="18", **tools):
    cmds = {}
    versions = {**TOOLS_OK, **tools}
    for tool in ("azd", "az", "python", "node", "pwsh", "docker", "git", "dotnet", "terraform", "kubectl"):
        v = versions.get(tool)
        cmds[f"LOCAL {tool}"] = {"exit_code": 0, "stdout": v} if v else {"exit_code": 1, "stdout": None}
    if odbc == "missing":
        cmds["SYSTEM odbc-sql"] = {"exit_code": 1, "stdout": None}
    elif odbc is not None:
        cmds["SYSTEM odbc-sql"] = {"exit_code": 0, "stdout": odbc}
    return {"commands": cmds}


def _tools(repo, fx):
    if not (repo / "infra" / "main.bicep").exists():
        (repo / "infra").mkdir(exist_ok=True)
        (repo / "infra" / "main.bicep").write_text(
            "param location string = resourceGroup().location\n", encoding="utf-8"
        )
    checks = ["repo.tools", "quota.models"]  # a GO needs the model quota check too
    d = Troubleshooter(repo, ReplayRunner(fx)).diagnose("preflight", log=False, only_checks=checks)
    return next(c for c in d.checks if c.check_id == "repo.tools"), d


# --------------------------------------------------------------------------- scan


def test_a_package_the_azd_hook_installs_makes_its_driver_required_with_the_devcontainer_version(tmp_path):
    found = scan_components(_repo(tmp_path))
    odbc = found["odbc-sql"]
    assert odbc["level"] == "required" and odbc["min"] == "18"
    assert odbc["sources"][0] == "infra/scripts/post/requirements.txt:2"
    assert odbc["devcontainer"].startswith(".devcontainer/devcontainer.json:")
    assert all(not s.startswith("src/") for s in odbc["sources"])  # app code runs in its own image


def test_the_same_package_outside_the_hooks_is_only_conditional(tmp_path):
    odbc = scan_components(_repo(tmp_path, hooked=False))["odbc-sql"]
    assert odbc["level"] == "conditional"


def test_app_code_alone_needs_nothing_on_the_deploying_machine(tmp_path):
    assert scan_components(_repo(tmp_path, app_only=True)) == {}


def test_without_a_devcontainer_there_is_no_documented_version(tmp_path):
    odbc = scan_components(_repo(tmp_path, devcontainer=False))["odbc-sql"]
    assert odbc["min"] is None and odbc["devcontainer"] is None


def test_the_setup_route_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("CODESPACES", "true")
    assert setup_route() == "codespaces"
    monkeypatch.delenv("CODESPACES")
    monkeypatch.setenv("REMOTE_CONTAINERS", "true")
    assert setup_route() == "devcontainer"


def test_only_known_system_components_can_be_probed():
    with pytest.raises(CommandNotAllowed):
        ReplayRunner({"commands": {}}).system_component("anything")
    assert OfflineRunner().system_component("odbc-sql")[0] == "unknown"


# --------------------------------------------------------------------------- repo.tools and preflight


@pytest.fixture(autouse=True)
def _local_route(monkeypatch):
    monkeypatch.setattr("accel_troubleshooter.diagnostics.tool_checks.setup_route", lambda: "local")
    monkeypatch.setattr(prereqscan, "setup_route", lambda: "local")


def test_an_installed_driver_of_the_documented_version_passes(tmp_path):
    c, _ = _tools(_repo(tmp_path), _fixture(odbc="18"))
    assert c.status == "pass" and "(local setup)" in c.summary
    row = next(r for r in c.evidence["tools"] if r["tool"] == "odbc-sql")
    assert row["status"] == "ok" and row["level"] == "required"


def test_a_missing_required_driver_is_a_blocker_that_says_why_and_how_to_get_it(tmp_path):
    c, d = _tools(_repo(tmp_path), _fixture(odbc="missing"))
    assert c.status == "fail"
    assert "Microsoft ODBC Driver for SQL Server not installed" in c.summary
    assert "azd up runs it" in c.summary and "the Dev Container installs it for you" in c.summary
    assert render_compact(d.to_dict(), "preflight").startswith("PREFLIGHT: 🛑 NO-GO")


def test_an_older_driver_than_the_devcontainer_installs_is_a_blocker(tmp_path):
    c, _ = _tools(_repo(tmp_path), _fixture(odbc="17"))
    assert c.status == "fail" and "17 is older" in c.summary


def test_a_missing_conditional_driver_is_only_a_warning(tmp_path):
    c, _ = _tools(_repo(tmp_path, hooked=False), _fixture(odbc="missing"))
    assert c.status == "warn" and "needed when that script runs" in c.summary


def test_a_required_driver_that_could_not_be_read_is_never_a_plain_go(tmp_path):
    c, d = _tools(_repo(tmp_path), _fixture(odbc=None))  # not recorded = could not verify
    assert c.status == "warn" and "Could not verify required prerequisite(s)" in c.summary
    assert c.evidence["unverified_required"] == ["Microsoft ODBC Driver for SQL Server"]
    text = render_compact(d.to_dict(), "preflight")
    assert "prerequisites not verified: Microsoft ODBC Driver for SQL Server" in text.splitlines()[0]
    assert "▸ Not verified (required): Microsoft ODBC Driver for SQL Server" in text


def test_preflight_shows_the_component_and_the_setup_route(tmp_path):
    _, d = _tools(_repo(tmp_path), _fixture(odbc="18"))
    text = render_compact(d.to_dict(), "preflight")
    assert "Tools (as the docs ask, local setup):" in text and "Microsoft ODBC Driver for SQL Server 18" in text


def test_an_accelerator_without_such_needs_reports_no_components(tmp_path):
    (tmp_path / "azure.yaml").write_text("name: plain\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Plain\n\n## Prerequisites\n- Python 3.11+\n", encoding="utf-8")
    c, _ = _tools(tmp_path, _fixture(odbc="missing"))
    assert all(r.get("kind") != "component" for r in c.evidence["tools"]) and c.status == "pass"


# --------------------------------------------------------------------------- azd env region conflict


def _azd_env(repo, dotenv, ai_param):
    env = repo / ".azure" / "dev"
    env.mkdir(parents=True)
    (repo / ".azure" / "config.json").write_text(json.dumps({"defaultEnvironment": "dev"}), encoding="utf-8")
    (env / ".env").write_text(dotenv, encoding="utf-8")
    (env / "config.json").write_text(
        json.dumps({"infra": {"parameters": {"azureAiServiceLocation": ai_param}}}), encoding="utf-8"
    )
    d = Troubleshooter(repo, OfflineRunner()).diagnose("preflight", log=False, only_checks=["repo.azd_env"])
    return next(c for c in d.checks if c.check_id == "repo.azd_env")


def test_an_ai_region_in_dotenv_that_differs_from_config_json_is_flagged(chatbot_copy):
    dotenv = 'AZURE_SUBSCRIPTION_ID="sub"\nAZURE_LOCATION="australiaeast"\nAZURE_ENV_AI_SERVICE_LOCATION="eastus2"\n'
    c = _azd_env(chatbot_copy, dotenv, "japaneast")
    assert c.status == "warn" and "differs from config.json azureAiServiceLocation=japaneast" in c.summary
    assert c.evidence["ai_location_conflict"]["env"] == "eastus2"
    assert c.evidence["ai_location"] == "eastus2"  # the value azd uses is the one that is checked


def test_an_ai_region_only_in_config_json_is_used_without_a_warning(chatbot_copy):
    c = _azd_env(chatbot_copy, 'AZURE_SUBSCRIPTION_ID="sub"\nAZURE_LOCATION="australiaeast"\n', "japaneast")
    assert c.evidence["ai_location"] == "japaneast" and not c.evidence["ai_location_conflict"]
    assert c.status != "warn"


@pytest.mark.parametrize("pkg", ["aioodbc==0.5.0", "turbodbc"])
def test_other_packages_that_use_the_odbc_driver_need_it_too(tmp_path, pkg):
    repo = _repo(tmp_path)
    (repo / "infra" / "scripts" / "post" / "requirements.txt").write_text(f"azure-identity\n{pkg}\n", encoding="utf-8")
    odbc = scan_components(repo)["odbc-sql"]
    assert odbc["level"] == "required" and odbc["sources"][0] == "infra/scripts/post/requirements.txt:2"
    assert pkg.split("=")[0] in odbc["why"]


def test_packages_with_odbc_in_their_name_only_are_not_mistaken_for_a_driver_need(tmp_path):
    repo = _repo(tmp_path)
    (repo / "infra" / "scripts" / "post" / "requirements.txt").write_text("pyodbc-stubs\nmyodbc\n", encoding="utf-8")
    assert scan_components(repo) == {}
