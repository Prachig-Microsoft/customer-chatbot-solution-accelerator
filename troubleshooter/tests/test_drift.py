import json
import re

from accel_troubleshooter.azcli import OfflineRunner, ReplayRunner
from accel_troubleshooter.cli import main
from accel_troubleshooter.drift import (
    build_drift_report,
    parse_param_table,
    render_drift_compact,
    render_drift_markdown,
)
from accel_troubleshooter.engine import Troubleshooter

from conftest import REPO_ROOT

PARAM_DOC = "documents/CustomizingAzdParameters.md"
GPT_CAP_ROW = "`10`                         | Sets the GPT model capacity"


def _report(repo, mutate=None):
    ts = Troubleshooter(repo, OfflineRunner())
    if mutate:
        mutate(ts.profiles["customer-chatbot"])
    d = ts.diagnose(
        "doc drift", accelerator="customer-chatbot", only_checks=["repo.config_drift"], env_file="none", log=False
    )
    return build_drift_report(d.to_dict())


def _ids(report, fid):
    return [f for f in report["findings"] if f["id"] == fid]


def _edit(repo, rel, old, new):
    p = repo / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, (rel, old)
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def _gpt_capacity(report):
    return [f for f in report["findings"] if "GPT_MODEL_CAPACITY" in f["detail"]]


def test_param_table_parser_skips_placeholders_and_keeps_line_numbers():
    rows = parse_param_table(
        "intro\n| `AZURE_LOCATION` | string | `<User selects>` | where |\n"
        "| `AZURE_ENV_X` | integer | `10` | minimum: 5 |\n| not a row |\n"
    )
    assert [(r["name"], r["example"], r["line"]) for r in rows] == [
        ("AZURE_LOCATION", None, 2),
        ("AZURE_ENV_X", "10", 3),
    ]


def test_documented_default_that_differs_from_the_template_is_found_with_file_and_line(chatbot_copy):
    cap = _gpt_capacity(_report(chatbot_copy))
    assert len(cap) == 1 and cap[0]["id"] == "doc-param-default-mismatch" and cap[0]["impact"] == "medium"
    assert cap[0]["where"].startswith(f"{PARAM_DOC}:") and "template default is `50`" in cap[0]["detail"]
    assert "change `10` to `50`" in cap[0]["fix"] and "infra/main.bicep:" in cap[0]["detail"]
    _edit(chatbot_copy, PARAM_DOC, GPT_CAP_ROW, GPT_CAP_ROW.replace("`10`", "`50`"))  # fixing the doc clears it
    assert not _gpt_capacity(_report(chatbot_copy))


def test_edits_to_the_bicep_or_the_parameter_file_are_picked_up_immediately(chatbot_copy):
    bicep = "param gptDeploymentCapacity int = 50"
    _edit(chatbot_copy, "infra/main.bicep", bicep, "param gptDeploymentCapacity int = 75")
    assert "template default is `75`" in _gpt_capacity(_report(chatbot_copy))[0]["detail"]
    # a ${VAR=90} default in the parameters file beats the Bicep default, exactly as azd resolves it
    var = "${AZURE_ENV_GPT_MODEL_CAPACITY}"
    _edit(chatbot_copy, "infra/main.parameters.json", var, "${AZURE_ENV_GPT_MODEL_CAPACITY=90}")
    cap = _gpt_capacity(_report(chatbot_copy))[0]["detail"]
    assert "template default is `90`" in cap and "main.parameters.json default" in cap


def test_replacing_a_variable_with_a_literal_means_the_documented_variable_is_no_longer_read(chatbot_copy):
    params = chatbot_copy / "infra" / "main.parameters.json"
    data = json.loads(params.read_text(encoding="utf-8"))
    data["parameters"]["gptDeploymentCapacity"]["value"] = 90
    params.write_text(json.dumps(data), encoding="utf-8")
    assert any("AZURE_ENV_GPT_MODEL_CAPACITY" in f["detail"] for f in _ids(_report(chatbot_copy), "doc-param-unused"))


def test_documented_variable_nothing_reads_and_variable_nothing_documents(chatbot_copy):
    import shutil

    script = "infra/scripts/post-provision/configure_auth.ps1"
    shutil.copy2(REPO_ROOT / script, chatbot_copy / script)
    row = "| `AZURE_ENV_GHOST_SETTING` | string | `x` | Nothing reads this. |\n"
    _edit(chatbot_copy, PARAM_DOC, "| `AZURE_ENV_VM_SIZE`", row + "| `AZURE_ENV_VM_SIZE`")
    params = chatbot_copy / "infra" / "main.parameters.json"
    data = json.loads(params.read_text(encoding="utf-8"))
    data["parameters"]["newThing"] = {"value": "${AZURE_ENV_NEW_THING}"}
    data["parameters"]["principal"] = {"value": "${AZURE_PRINCIPAL_ID}"}  # azd built-in: never reported
    params.write_text(json.dumps(data), encoding="utf-8")
    r = _report(chatbot_copy)
    assert "AZURE_ENV_GHOST_SETTING" in " ".join(f["detail"] for f in _ids(r, "doc-param-unused"))
    undocumented = " ".join(f["detail"] for f in _ids(r, "doc-param-undocumented"))
    assert "AZURE_ENV_NEW_THING" in undocumented and "AZURE_PRINCIPAL_ID" not in undocumented
    assert "AZURE_ENV_ENTRA_CLIENT_ID" in undocumented  # set by a post-provision script, not in the table


def test_variables_the_real_repo_does_read_are_not_reported_as_unused():
    assert not _ids(_report(REPO_ROOT), "doc-param-unused")  # IMAGETAG, VM size and others are read somewhere


def test_allowed_values_and_minimums_are_compared_with_the_bicep(chatbot_copy):
    _edit(chatbot_copy, PARAM_DOC, "allowed: `Standard`, `GlobalStandard`", "allowed: `Standard`, `Basic`")
    _edit(chatbot_copy, PARAM_DOC, "(minimum: 10)", "(minimum: 99)")
    r = _report(chatbot_copy)
    assert "allows ['Standard', 'GlobalStandard']" in _ids(r, "doc-param-allowed-mismatch")[0]["detail"]
    assert (
        "minimum for AZURE_ENV_EMBEDDING_DEPLOYMENT_CAPACITY is 99"
        in _ids(r, "doc-param-minimum-mismatch")[0]["detail"]
    )


def test_missing_scripts_and_broken_links_in_the_docs_are_found_and_ranked_first(chatbot_copy):
    _edit(
        chatbot_copy,
        "documents/DeploymentGuide.md",
        "# Deployment Guide",
        "# Deployment Guide\n\nRun infra/scripts/post-provision/does_not_exist.ps1 now. See [x](./Nope.md).\n",
    )
    r = _report(chatbot_copy)
    script = next(f for f in _ids(r, "doc-script-missing") if f["file"] == "documents/DeploymentGuide.md")
    assert script["where"] == "documents/DeploymentGuide.md:3" and script["impact"] == "high"
    assert "does_not_exist.ps1" in script["detail"]
    assert any("Nope.md" in f["detail"] for f in _ids(r, "doc-broken-link"))
    assert r["findings"][0]["impact"] == "high"  # most important first


def test_root_relative_links_resolve_from_the_repository_root_like_github(chatbot_copy):
    doc = chatbot_copy / "documents" / "QuotaCheck.md"
    doc.write_text("See [guide](/documents/DeploymentGuide.md) and [gone](/documents/Gone.md).\n", encoding="utf-8")
    broken = [f for f in _ids(_report(chatbot_copy), "doc-broken-link") if f["file"] == "documents/QuotaCheck.md"]
    assert (
        len(broken) == 1 and "1 relative link(s)" in broken[0]["detail"] and "/documents/Gone.md" in broken[0]["detail"]
    )


def test_models_in_the_quota_docs_that_the_template_does_not_deploy(chatbot_copy):
    f = _ids(_report(chatbot_copy), "doc-models-not-deployed")[0]
    assert (
        "gpt-4o" in f["detail"] and "gpt-5.4-mini" in f["detail"] and f["where"].startswith("documents/QuotaCheck.md:")
    )


def test_doc_findings_never_become_causes_and_never_turn_the_check_into_a_warning(
    synthetic_fixture, synthetic_env_file
):
    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    d = ts.diagnose("check my deployment health", env_file=synthetic_env_file, full=True, log=False)
    assert not any(h.id == "config-drift" for h in d.hypotheses)
    drift = next(c for c in d.checks if c.check_id == "repo.config_drift")
    docs = [f for f in drift.evidence["findings"] if f.get("scope") == "docs"]
    assert drift.status in ("info", "pass") and docs and all(f["severity"] == "low" for f in docs)


def test_profile_without_a_parameter_doc_is_not_an_error(chatbot_copy):
    r = _report(chatbot_copy, mutate=lambda p: p["iac"].pop("param_doc"))
    assert not _ids(r, "doc-param-default-mismatch") and _ids(r, "doc-models-not-deployed")


def test_renderers_and_cli_exit_codes(chatbot_copy, tmp_path, capsys):
    r = _report(chatbot_copy)
    compact = render_drift_compact(r, plain=True)
    assert (
        compact.startswith("#DOC DRIFT | Customer Chatbot")
        and "MEDIUM (1)" in compact
        and "-> Edit documents/" in compact
    )
    md = render_drift_markdown(r)
    assert md.startswith("# Documentation drift report")
    assert "- [ ] **medium** `documents/CustomizingAzdParameters.md:" in md and not re.search(r"\x1b", compact + md)
    out = tmp_path / "drift.md"
    assert main(["--workspace", str(chatbot_copy), "drift", "--plain", "--out", str(out)]) == 1
    assert out.read_text(encoding="utf-8").startswith("# Documentation drift report")
    assert "Report saved to" in capsys.readouterr().out
    main(["--workspace", str(chatbot_copy), "drift", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["findings"]


def test_a_report_without_findings_says_so():
    empty = {"accelerator": "X", "accelerator_id": "x", "commit": "", "branch": "", "findings": [], "files": []}
    assert "No drift found" in render_drift_compact(empty, plain=True)
    assert render_drift_markdown(empty).endswith("No drift found.")
