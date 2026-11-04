"""Every document is read, guides and READMEs get extra attention, and coverage says what was skipped."""

import json

from accel_troubleshooter.cli import main
from accel_troubleshooter.docscan import kind_of, scan_docs
from accel_troubleshooter.knowledge import build_index, coverage

TABLE_GUIDE = """# Known problems

| Symptom | Cause | Resolution |
|---|---|---|
| `AuthorizationFailed` on deploy | Missing role | Ask for Contributor on the subscription |
| Container never starts | Bad image tag | Rebuild the image and redeploy |
"""
FAQ_GUIDE = """# FAQs

## Why does the deployment ask for a region?
Models are only offered in some regions, so pick one from the list in the deployment guide.

## Why is chat slow after deployment?
The first call warms the model deployment; wait a minute and retry.
"""
README = """# Acme Insights

Turns data into answers.

## Prerequisites and costs
You need an Azure subscription and quota in swedencentral or eastus2 for gpt-4o.

## Troubleshooting
If `azd up` fails with a QuotaExceeded error, lower the capacity and run `azd up` again.
"""


def _repo(tmp_path):
    for d in ("docs", "src/api", "infra", "troubleshooter", ".github/agents", "node_modules/x"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "azure.yaml").write_text("name: acme\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / "src/api/README.md").write_text("# API\n\nRun the API locally with python app.py.\n", "utf-8")
    (tmp_path / "docs/Problems.md").write_text(TABLE_GUIDE, encoding="utf-8")
    (tmp_path / "docs/FAQs.md").write_text(FAQ_GUIDE, encoding="utf-8")
    (tmp_path / "docs/TransparencyNotes.md").write_text(
        "# Notes\n\n## Frequently asked questions\nAbout AI use.\n", "utf-8"
    )
    (tmp_path / "LICENSE.md").write_text("MIT license text " * 5, encoding="utf-8")
    (tmp_path / "troubleshooter" / "README.md").write_text("# the tool itself\n", encoding="utf-8")
    (tmp_path / ".github/agents/a.agent.md").write_text("# agent\n", encoding="utf-8")
    (tmp_path / "node_modules/x/README.md").write_text("# vendored\n", encoding="utf-8")
    (tmp_path / "notes.ipynb").write_text("{}", encoding="utf-8")
    return tmp_path


def test_every_readme_is_kept_and_the_root_one_comes_first(tmp_path):
    docs = scan_docs(_repo(tmp_path))["docs"]
    assert docs["readme"] == ["README.md", "src/api/README.md"]


def test_a_guide_is_recognised_by_its_content_not_only_its_name(tmp_path):
    docs = scan_docs(_repo(tmp_path))["docs"]
    assert "docs/Problems.md" in docs["troubleshooting"]  # an error/resolution table under a vague name
    assert "docs/FAQs.md" in docs["troubleshooting"]  # recognised by name
    assert "docs/TransparencyNotes.md" not in docs["troubleshooting"]  # a generic FAQ heading is not a guide
    assert kind_of("docs/Transparency_FAQ.md") != "troubleshooting"


def test_noise_and_the_tools_own_files_are_not_read_and_the_skips_are_reported(tmp_path):
    scan = scan_docs(_repo(tmp_path))
    read = {p for paths in scan["docs"].values() for p in paths}
    assert not read & {
        "LICENSE.md",
        "troubleshooter/README.md",
        ".github/agents/a.agent.md",
        "node_modules/x/README.md",
    }
    reasons = {s["path"]: s["reason"] for s in scan["skipped"]}
    assert "boilerplate" in reasons["LICENSE.md"] and "notebook" in reasons["notes.ipynb"]


def test_guides_parse_as_table_rows_or_one_entry_per_heading(tmp_path):
    repo = _repo(tmp_path)
    prof = {"id": "acme", "docs": {}, "source_globs": []}
    idx = build_index(prof, repo)
    rows = [p for p in idx.passages if p.doc_type == "troubleshooting-row"]
    by_path = {}
    for p in rows:
        by_path.setdefault(p.path, []).append(p)
    assert {r.title for r in by_path["docs/Problems.md"]} == {"AuthorizationFailed on deploy", "Container never starts"}
    assert any("AuthorizationFailed" in r.error_codes for r in by_path["docs/Problems.md"])
    assert [r.title for r in by_path["docs/FAQs.md"]] == [
        "Why does the deployment ask for a region?",
        "Why is chat slow after deployment?",
    ]


def test_a_readme_troubleshooting_section_outranks_a_plain_mention(tmp_path):
    repo = _repo(tmp_path)
    (repo / "docs" / "Overview.md").write_text(
        "# Overview\n\nWe once saw a QuotaExceeded message in a demo.\n", "utf-8"
    )
    idx = build_index({"id": "acme", "docs": {}, "source_globs": []}, repo)
    top = idx.search("azd up QuotaExceeded error", k=2)
    assert top[0].path == "README.md" and top[0].category == "troubleshooting"


def test_coverage_says_what_was_read_how_guides_parsed_and_what_the_readme_promises(tmp_path):
    repo = _repo(tmp_path)
    prof = {"id": "acme", "docs": {}, "source_globs": []}
    cov = coverage(prof, build_index(prof, repo), repo)
    formats = {g["path"]: g["format"] for g in cov["troubleshooting_docs"]}
    assert formats["docs/Problems.md"] == "table rows" and "FAQ" in formats["docs/FAQs.md"]
    assert cov["readme"]["has_troubleshooting_section"] and cov["readme"]["prerequisites_section"]
    assert {"swedencentral", "eastus2"} <= set(cov["readme"]["regions_mentioned"])
    assert (
        cov["docs_read"] == len([f for f in build_index(prof, repo).meta["files"] if f.endswith(".md")])
        and cov["docs_skipped"]
    )


def test_docs_command_prints_a_readable_summary(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert main(["--workspace", str(repo), "docs", "--rebuild"]) == 0
    out = capsys.readouterr().out
    assert "Documents read:" in out and "Troubleshooting guide: docs/Problems.md" in out and "Not read:" in out
    assert "README:" in out and json.dumps(out)  # printed, not JSON
