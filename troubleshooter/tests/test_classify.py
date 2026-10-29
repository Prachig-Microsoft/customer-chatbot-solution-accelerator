from pathlib import Path

from accel_troubleshooter.classify import classify, extract, identify_accelerator
from accel_troubleshooter.repo import load_profiles

from conftest import REPO_ROOT, TOOL_ROOT


def test_extracts_codes_regions_models_status_and_resources():
    ctx = extract(
        "Deployment 'pgbyocc4-1761' in rg-pgbyocc4 (East US 2) failed: Code: InsufficientQuota for gpt-5.4-mini "
        "at 2026-10-28T11:02:00Z; app api-chat-pgbyocc4y4hkj returned HTTP 503",
        ["InsufficientQuota"],
    )
    assert "InsufficientQuota" in ctx.error_codes
    assert "eastus2" in ctx.regions
    assert "gpt-5.4-mini" in ctx.models
    assert 503 in ctx.http_statuses
    assert "rg-pgbyocc4" in ctx.resource_names and "api-chat-pgbyocc4y4hkj" in ctx.resource_names
    assert ctx.timestamps == ["2026-10-28T11:02:00Z"]
    assert "pgbyocc4-1761" in ctx.deployment_names


def test_capacity_numbers_are_not_http_statuses():
    assert extract("I need 500K TPM capacity for gpt-5.4-mini").http_statuses == []


def test_tokens_per_minute_is_not_authentication():
    ctx = classify(extract("InsufficientQuota: require 800 capacity in quota Tokens Per Minute (thousands)"))
    assert "authentication" not in ctx.stages
    assert ctx.stages[0] == "quota"


def test_classification_of_reference_phrases():
    cases = {
        "my deployment failed with a quota exceeded error": "quota",
        "Chat with your data is deployed, but the application cannot access Azure OpenAI": "authentication",
        "accelerator is failing while processing documents": "document-processing",
        "application works locally but fails after Azure deployment": "local-vs-cloud",
        "the model is deployed but requests are being throttled": "throttling",
        "infrastructure deployment is stuck": "deployment",
        "post-deployment scripts are throwing an error": "post-deployment-script",
        "I changed a configuration value and the application stopped working": "configuration",
        "My deployment succeeded, but the endpoint is returning errors": "runtime",
    }
    for text, stage in cases.items():
        assert stage in classify(extract(text)).stages[:2], (text, classify(extract(text)).stages)


def test_unseen_phrasing_is_still_classified():
    ctx = classify(extract("getting 401 unauthorized from the API right after I rotated the managed identity"))
    assert ctx.stages[0] == "authentication"


def test_error_code_category_mapping_adds_deployment_stage():
    ctx = classify(
        extract("SkuNotAvailable", ["SkuNotAvailable"]), {"skunotavailable": "quota-and-capacity-limitations"}
    )
    assert "deployment" in ctx.stages and "quota-and-capacity-limitations" in ctx.categories


def test_identify_from_workspace():
    r = identify_accelerator("deployment failed", load_profiles(), REPO_ROOT)
    assert r["accelerator"] == "customer-chatbot" and r["confident"]


def test_text_mention_overrides_workspace_but_is_explained():
    r = identify_accelerator("chat with your data cannot reach openai", load_profiles(), REPO_ROOT)
    assert r["accelerator"] == "chat-with-your-data"
    assert any("mentions" in x for x in r["reasons"]["chat-with-your-data"])


def test_ambiguous_identification_asks_question():
    ws = TOOL_ROOT / "evals"
    r = identify_accelerator("it is broken", load_profiles(), ws)
    assert r["accelerator"] is None and "Which accelerator" in r["question"]
    r = identify_accelerator("customer chatbot and content processing both fail", load_profiles(), ws)
    assert r["accelerator"] is None


def test_resource_names_identify_accelerator_outside_workspace():
    ws = TOOL_ROOT / "evals"
    ctx = extract("api-chat-xyz12 returns 500 and aif-xyz12 shows errors")
    r = identify_accelerator(ctx.raw_text, load_profiles(), ws, resource_names=ctx.resource_names)
    assert r["scores"]["customer-chatbot"] == 2 and r["accelerator"] is None  # weak evidence -> still asks


def test_explicit_selection():
    r = identify_accelerator("x", load_profiles(), Path("."), explicit="content-processing")
    assert r["accelerator"] == "content-processing"
    assert identify_accelerator("x", load_profiles(), Path("."), explicit="nope")["accelerator"] is None
