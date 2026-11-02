"""Gaps found by pasting the troubleshooting guide's own error codes into the agent."""

import pytest

from accel_troubleshooter.azcli import OfflineRunner
from accel_troubleshooter.compact import render_compact
from accel_troubleshooter.engine import Troubleshooter

from conftest import REPO_ROOT


@pytest.fixture(scope="module")
def ts():
    return Troubleshooter(REPO_ROOT, OfflineRunner())


def _diagnose(ts, text):
    return ts.diagnose(text, accelerator="customer-chatbot", log=False, only_checks=["repo.azd_env"], env_file="none")


def test_resource_group_not_found_is_explained_from_the_guide_even_when_offline(ts):
    d = _diagnose(ts, "ERROR: ResourceGroupNotFound: Resource group 'rg-old' could not be found.")
    top = d.hypotheses[0]
    assert top.id == "resource-group-missing" and top.status == "likely"
    assert any("TroubleShootingSteps.md" in r for r in top.doc_refs)
    assert any("az group show" in m for m in top.missing_evidence)  # says what a live check would add


def test_generic_code_picks_the_row_that_matches_the_pasted_text(ts):
    d = _diagnose(
        ts, "BadRequest: DatabaseAccount is in a failed provisioning state because the previous attempt failed"
    )
    assert "DatabaseAccount is in a failed provisioning state" in d.hypotheses[0].title  # not another BadRequest row


def test_plain_language_row_title_is_matched_without_an_error_code(ts):
    d = _diagnose(
        ts, "Unauthorized - Operation cannot be completed without additional quota. Current Limit (Basic VMs): 0"
    )
    ids = [h.id for h in d.hypotheses]
    assert "quota-insufficient" not in ids  # VM quota is not Azure OpenAI model quota
    assert "Operation cannot be completed without additional quota" in d.hypotheses[0].title


def test_value_outside_the_bicep_allowed_list_is_confirmed_with_the_source_line(ts):
    msg = (
        "InvalidTemplate: The provided value 'westus' for the template parameter 'azureAiServiceLocation' at line '30' "
        "and column '9' is not valid. The parameter value is not part of the allowed value(s): 'eastus2'"
    )
    d = _diagnose(ts, msg)
    top = d.hypotheses[0]
    assert top.id == "parameter-not-allowed" and top.status == "confirmed" and top.confidence == "high"
    text = " ".join(top.rationale)
    assert "infra/main.bicep:" in text and "AZURE_ENV_AI_SERVICE_LOCATION" in text and "eastus2" in text
    assert any("azd env set AZURE_ENV_AI_SERVICE_LOCATION" in c for a in d.actions.values() for c in a.commands)


def test_allowed_value_is_not_blamed_when_the_value_is_valid(ts):
    d = _diagnose(
        ts,
        "The provided value 'eastus2' for the template parameter 'azureAiServiceLocation' is not valid (other reason)",
    )
    assert not any(h.id == "parameter-not-allowed" for h in d.hypotheses)


def test_disallowed_region_names_the_blocker_not_just_a_count(ts, tmp_path):
    env = tmp_path / "bad.env"
    env.write_text('AZURE_ENV_NAME="x"\nAZURE_ENV_AI_SERVICE_LOCATION="westus"\n', encoding="utf-8")
    d = ts.diagnose(
        "preflight", accelerator="customer-chatbot", log=False, only_checks=["repo.config_drift"], env_file=str(env)
    )
    drift = next(c for c in d.checks if c.check_id == "repo.config_drift")
    assert drift.status == "fail" and "'westus' is not in the template's allowed list" in drift.summary


def test_free_text_without_a_cause_lists_closest_docs_labelled_as_keyword_matches(ts):
    d = _diagnose(ts, "azd up says the location 'westus3' is not available for the Azure AI Search resource type")
    text = render_compact(d.to_dict(), "diagnose", plain=True)
    if d.hypotheses and d.hypotheses[0].status in ("confirmed", "likely"):
        pytest.skip("a documented cause was found")
    assert "CLOSEST DOCS (keyword match, not a diagnosis)" in text and "TroubleShootingSteps.md:" in text
    assert "NOT IN THE ACCELERATOR DOCS?" in text  # the wider-search pointer is still given, labelled external
