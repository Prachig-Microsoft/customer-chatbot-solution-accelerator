"""ai.deployments must read the account that holds the models, not another Cognitive Services account (Speech, ...).

Regression: on Chat With Your Data the resource group has a Speech account (spch-...) and a Foundry account (aif-...).
The check took the first Cognitive Services account Azure listed, the Speech one, found 0 deployments there and said
the models were missing although they were deployed on the Foundry account.
"""

from types import SimpleNamespace

from accel_troubleshooter.diagnostics.azure_checks import _ai_account, ai_deployments, pick_ai_account
from accel_troubleshooter.models import FAIL, PASS, SKIP

SPEECH = {
    "name": "spch-demo",
    "type": "Microsoft.CognitiveServices/accounts",
    "kind": "SpeechServices",
    "location": "japaneast",
}
FOUNDRY = {
    "name": "aif-demo",
    "type": "Microsoft.CognitiveServices/accounts",
    "kind": "AIServices",
    "location": "japaneast",
}
OPENAI = {"name": "oai-demo", "type": "Microsoft.CognitiveServices/accounts", "kind": "OpenAI", "location": "eastus2"}
DOCINT = {"name": "di-demo", "type": "Microsoft.CognitiveServices/accounts", "kind": "FormRecognizer", "location": "x"}


class _Env(dict):
    def get(self, key, default=None):
        return super().get(key, default) if key else default


def test_the_speech_account_is_never_picked_even_when_azure_lists_it_first():
    picked = pick_ai_account([SPEECH, FOUNDRY])
    assert picked["name"] == "aif-demo" and picked["kind"] == "AIServices"
    assert picked["why"] == "the AIServices account in the resource group"


def test_the_account_an_azd_endpoint_names_wins():
    env = _Env(AZURE_AI_PROJECT_ENDPOINT="https://aif-demo.services.ai.azure.com/api/projects/proj-demo")
    assert pick_ai_account([SPEECH, OPENAI, FOUNDRY], env)["why"] == "host of azd env AZURE_AI_PROJECT_ENDPOINT"
    env = _Env(AZURE_OPENAI_ENDPOINT="https://oai-demo.openai.azure.com/")
    assert pick_ai_account([FOUNDRY, OPENAI], env)["name"] == "oai-demo"


def test_an_explicit_azd_account_name_wins_and_aiservices_beats_openai():
    env = _Env(AI_SERVICE_NAME="aif-demo")
    assert pick_ai_account([SPEECH, OPENAI], env, "AI_SERVICE_NAME")["why"] == "azd env AI_SERVICE_NAME"
    assert pick_ai_account([OPENAI, FOUNDRY])["name"] == "aif-demo"
    assert pick_ai_account([SPEECH, DOCINT]) == {}  # nothing that can hold model deployments


class _Ctx:
    """Just enough of DiagContext for the account choice and the deployment check."""

    def __init__(self, resources, deployments, env=None, ai_key=None):
        self.facts = {"resources": resources, "model_requirements": [{"model": "gpt-5-mini"}]}
        self.env = _Env(env or {})
        self.resource_group = "rg-demo"
        self.calls = []
        self._deployments = deployments
        self._ai_key = ai_key

    def env_key(self, logical):
        return self._ai_key if logical == "ai_account" else None

    def rg_known_missing(self):
        return False

    def az(self, *args):
        self.calls.append(" ".join(args))
        if args[:3] == ("cognitiveservices", "account", "deployment"):
            name = args[args.index("--name") + 1]
            return True, self._deployments.get(name, []), None
        return False, None, None


GPT = {
    "name": "gpt-5-mini",
    "sku": {"name": "GlobalStandard", "capacity": 50},
    "properties": {"provisioningState": "Succeeded", "model": {"name": "gpt-5-mini", "version": "1"}},
}


def _check(ctx):
    import accel_troubleshooter.diagnostics.azure_checks as ac

    orig = ac.effective_model_requirements
    ac.effective_model_requirements = lambda c: c.facts["model_requirements"]
    try:
        return ai_deployments(ctx)
    finally:
        ac.effective_model_requirements = orig


def test_regression_models_on_the_foundry_account_are_found_not_reported_missing():
    ctx = _Ctx([SPEECH, FOUNDRY], {"aif-demo": [GPT], "spch-demo": []})
    res = _check(ctx)
    assert res.status == PASS and res.evidence["account"] == "aif-demo"
    assert res.evidence["account_kind"] == "AIServices" and "AIServices account" in res.summary
    assert not any("spch-demo" in c for c in ctx.calls)


def test_missing_models_are_still_reported_on_the_right_account():
    res = _check(_Ctx([SPEECH, FOUNDRY], {"aif-demo": []}))
    assert res.status == FAIL and "Missing: ['gpt-5-mini']" in res.summary and res.evidence["account"] == "aif-demo"


def test_only_a_speech_account_is_skip_with_the_reason_not_missing_models():
    res = _check(_Ctx([SPEECH], {"spch-demo": []}))
    assert res.status == SKIP and "SpeechServices" in res.summary and "hold no model deployments" in res.summary


def test_resources_listed_without_a_kind_are_looked_up_once():
    ctx = _Ctx([{"name": "spch-demo", "type": "Microsoft.CognitiveServices/accounts"}], {})
    ctx.az = lambda *a: (True, [SPEECH, FOUNDRY], None)  # account list returns the kinds
    assert _ai_account(ctx) == "aif-demo" and _ai_account(ctx) == "aif-demo"
    assert SimpleNamespace(**ctx.facts["ai_account"]).why.startswith("the AIServices account")
