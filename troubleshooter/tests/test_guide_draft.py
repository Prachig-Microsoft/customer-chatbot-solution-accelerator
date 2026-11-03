import json

from accel_troubleshooter.azcli import OfflineRunner, ReplayRunner
from accel_troubleshooter.cli import main
from accel_troubleshooter.engine import Troubleshooter
from accel_troubleshooter.guide_draft import Generaliser, _section_line, draft_entry, render_draft

from conftest import REPO_ROOT

GUIDE = REPO_ROOT / "documents" / "TroubleShootingSteps.md"
PARAM_ERR = (
    "InvalidTemplate: The provided value 'westus' for the template parameter 'azureAiServiceLocation' is not valid. "
    "The parameter value is not part of the allowed value(s): 'eastus2'"
)
PUBLIC_CHECKS = ["rg.state", "resources.state", "network.public_access"]


def _diag(text, runner=None, env_file="none", **kw):
    ts = Troubleshooter(REPO_ROOT, runner or OfflineRunner())
    return ts, ts.diagnose(text, accelerator="customer-chatbot", log=False, env_file=env_file, **kw)


def _row_cells(row):
    assert row.startswith("| ") and row.rstrip().endswith("|") and "\n" not in row  # one table row, like the guide's
    return row.strip().strip("|").split(" | ", 2)


def test_a_new_cause_becomes_a_guide_row_in_the_guides_own_format_with_section_and_line():
    ts, d = _diag(PARAM_ERR, only_checks=["repo.azd_env"])
    note = "Set the region to centralus and re-ran azd up."
    out = draft_entry(d, GUIDE, note, profile=ts.profiles["customer-chatbot"])
    assert (
        out["drafted"] and out["finding"]["id"] == "parameter-not-allowed" and out["finding"]["status"] == "confirmed"
    )
    code, desc, steps = (c.strip() for c in _row_cells(out["row"]))
    assert "InvalidTemplate" in code and "outside its @allowed list" in desc
    assert steps.startswith("<ul><li>") and steps.endswith("</ul>")
    assert "azd env set AZURE_ENV_AI_SERVICE_LOCATION" in steps and f"<b>Verified fix:</b> {note}" in steps
    assert out["section"] == "Regional & Location Issues"
    text = GUIDE.read_text(encoding="utf-8").splitlines()
    after = out["insert_after_line"]
    assert text[after - 1].startswith("|") and not text[after].startswith("|")  # right after the last row of that table


def test_section_lookup_handles_missing_sections_and_files(tmp_path):
    assert _section_line(tmp_path / "nope.md", "x") == (None, 0)
    f = tmp_path / "g.md"
    f.write_text("## A\n\n| h | h |\n|---|---|\n| r1 | x |\n| r2 | x |\n\n## B\n\n| h |\n", encoding="utf-8")
    assert _section_line(f, "A") == (6, 2) and _section_line(f, "C") == (None, 0)


def test_a_problem_the_guide_already_documents_is_not_drafted_again_unless_forced():
    ts, d = _diag("ResourceGroupNotFound: Resource group 'rg-old' could not be found.", only_checks=["repo.azd_env"])
    out = draft_entry(d, GUIDE, profile=ts.profiles["customer-chatbot"])
    assert out["drafted"] is False and "TroubleShootingSteps.md:" in out["documented_at"] and "--force" in out["reason"]
    forced = draft_entry(d, GUIDE, force=True, profile=ts.profiles["customer-chatbot"])
    assert forced["drafted"] and "Similar entry" in render_draft(forced, "documents/TroubleShootingSteps.md")


def test_nothing_is_drafted_without_a_confirmed_or_likely_cause_unless_the_user_supplies_the_fix():
    ts, d = _diag("azd up failed: ZxQuuxWidgetError during frobnicate", only_checks=["repo.azd_env"])
    prof = ts.profiles["customer-chatbot"]
    out = draft_entry(d, GUIDE, profile=prof)
    assert out["drafted"] is False and "no confirmed or likely cause" in out["reason"] and "--note" in out["reason"]
    mine = draft_entry(d, GUIDE, "Deleted the stale .azure folder and re-ran azd up in rg-secret.", profile=prof)
    assert mine["drafted"] and mine["finding"]["id"] == "user-reported" and mine["section"] == "Miscellaneous"
    assert "**ZxQuuxWidgetError**" in mine["row"] and "<b>Verified fix:</b> Deleted the stale" in mine["row"]
    assert "rg-secret" not in json.dumps(mine) and "reported by you" in render_draft(mine, "g.md", True)
    assert "Pick the right section" in render_draft(mine, "g.md", True)


def test_the_row_contains_nothing_specific_to_the_incident(synthetic_fixture, synthetic_env_file):
    from test_ux import _policy_fixture

    note = "Fixed by admin jane.doe@contoso.com in rg-secretgroup, ip 10.20.30.40, app api-chat-abc123xyz"
    ts, d = _diag(
        "chat does not work, Cosmos is blocked",
        runner=ReplayRunner(_policy_fixture(synthetic_fixture)),
        env_file=synthetic_env_file,
        only_checks=PUBLIC_CHECKS,
    )
    out = draft_entry(d, GUIDE, note, force=True, profile=ts.profiles["customer-chatbot"])
    assert out["drafted"], out
    text = json.dumps(out)
    for leaked in ("jane.doe", "contoso.com", "rg-secretgroup", "10.20.30.40", "api-chat-abc123xyz", "rg-demo"):
        assert leaked not in text, leaked
    assert "<user>" in out["row"] and "<resource-group>" in out["row"] and "<ip>" in out["row"]


def test_generaliser_replaces_ids_names_and_secrets():
    g = Generaliser(
        {"rg-prod-1": "<resource-group>", "myenv": "<env-name>"}, {"resource_name_patterns": ["^api-chat-", "^aif-"]}
    )
    out = g(
        "az cosmosdb show --resource-group rg-prod-1 --name cosmos-x in myenv sub 11111111-2222-3333-4444-555555555555 "
        "on api-chat-1a2b3c and aif-zz9 at https://api-chat-1a2b3c.azurewebsites.net key AccountKey=abc123 from 1.2.3.4"
    )
    assert "rg-prod-1" not in out and "myenv" not in out and "api-chat-1a2b3c" not in out and "aif-zz9" not in out
    for placeholder in ("<guid>", "<ip>", "AccountKey=<redacted>", "https://<app>.azurewebsites.net"):
        assert placeholder in out, placeholder


def test_live_check_commands_are_included_as_read_only_confirmation_steps(synthetic_fixture, synthetic_env_file):
    from test_ux import _policy_fixture

    ts, d = _diag(
        "chat does not work",
        runner=ReplayRunner(_policy_fixture(synthetic_fixture)),
        env_file=synthetic_env_file,
        only_checks=PUBLIC_CHECKS,
    )
    out = draft_entry(d, GUIDE, force=True, profile=ts.profiles["customer-chatbot"])
    assert (
        out["finding"]["id"] == "public-access-disabled" and out["section"] == "Network & Infrastructure Configuration"
    )
    assert "How to confirm" in out["row"] and "`az cosmosdb show --resource-group <resource-group>" in out["row"]
    assert (
        "SecurityControl" in out["row"] and "Cosmos DB public network access turned off by Azure Policy" in out["row"]
    )


def test_cli_prints_the_draft_saves_it_on_request_and_never_touches_the_guide(tmp_path, capsys):
    before = GUIDE.read_bytes()
    out = tmp_path / "draft.md"
    args = ["draft-guide", PARAM_ERR, "--offline", "--no-prompt", "--plain", "--note", "Use centralus."]
    rc = main(args + ["--out", str(out)])
    text = capsys.readouterr().out
    assert rc == 0 and "DRAFT GUIDE ENTRY (confirmed" in text and "Nothing was written" in text
    assert out.read_text(encoding="utf-8").startswith("DRAFT GUIDE ENTRY") and GUIDE.read_bytes() == before
    assert main(["draft-guide", "ResourceGroupNotFound: rg-x could not be found", "--offline", "--no-prompt"]) == 1
    capsys.readouterr()  # discard the "already documented" message
    main(["draft-guide", PARAM_ERR, "--offline", "--no-prompt", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["drafted"] is True
