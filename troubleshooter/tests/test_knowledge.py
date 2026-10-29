from pathlib import Path

from accel_troubleshooter.knowledge import build_index, coverage, freshness, tokenize
from accel_troubleshooter.repo import load_profiles

from conftest import REPO_ROOT


def _chatbot_index(repo=REPO_ROOT):
    prof = load_profiles()["customer-chatbot"]
    return prof, build_index(prof, Path(repo))


def test_troubleshooting_rows_are_parsed_with_codes_and_line_numbers():
    _, idx = _chatbot_index()
    rows = [p for p in idx.passages if p.doc_type == "troubleshooting-row"]
    assert len(rows) > 40
    iq = [p for p in rows if "InsufficientQuota" in p.error_codes and p.category != "summary"]
    assert iq, "InsufficientQuota detail row must be indexed"
    row = iq[0]
    line = (REPO_ROOT / row.path).read_text(encoding="utf-8").splitlines()[row.line_start - 1]
    assert "**InsufficientQuota**" in line
    assert row.section.startswith("Quota")
    assert row.commit and row.repo.endswith(".git")


def test_multi_code_rows_are_split():
    _, idx = _chatbot_index()
    codes = idx.known_error_codes
    for c in (
        "DeploymentModelNotSupported",
        "ServiceModelDeprecated",
        "FlagMustBeSetForRestore",
        "ResourceGroupNotFound",
    ):
        assert c in codes


def test_error_code_search_prefers_detailed_row_over_summary():
    _, idx = _chatbot_index()
    hits = idx.search("deployment failed quota", k=3, error_codes=["InsufficientQuota"])
    assert hits[0].category != "summary"
    assert "InsufficientQuota" in hits[0].error_codes


def test_natural_language_retrieval_without_codes():
    _, idx = _chatbot_index()
    hits = idx.search("how do I check model quota before deployment", k=5)
    assert any("QuotaCheck.md" in h.path for h in hits)


def test_tokenize_splits_camel_case_and_model_names():
    toks = tokenize("InsufficientQuota gpt-5.4-mini")
    assert {"insufficientquota", "insufficient", "quota", "gpt-5.4-mini", "gpt", "mini"} <= set(toks)


def test_freshness_detects_changed_doc(chatbot_copy):
    prof, idx = _chatbot_index(chatbot_copy)
    assert freshness(idx, chatbot_copy)["stale"] == []
    doc = chatbot_copy / "documents" / "QuotaCheck.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\nnew line\n", encoding="utf-8")
    assert "documents/QuotaCheck.md" in freshness(idx, chatbot_copy)["changed_files"]


def test_coverage_reports_missing_and_unindexed_docs(chatbot_copy):
    prof, _ = _chatbot_index(chatbot_copy)
    (chatbot_copy / "documents" / "AppAuthentication.md").unlink()
    (chatbot_copy / "documents" / "NewGuide.md").write_text("# New\n", encoding="utf-8")
    idx = build_index(prof, chatbot_copy)
    cov = coverage(prof, idx, chatbot_copy)
    assert "documents/AppAuthentication.md" in cov["categories"]["auth"]["missing"]
    assert "documents/NewGuide.md" in cov["unindexed_docs"]
