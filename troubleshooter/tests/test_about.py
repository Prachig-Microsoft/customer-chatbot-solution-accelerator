from accel_troubleshooter.about import readme_summary
from accel_troubleshooter.cli import main

from conftest import REPO_ROOT


def test_readme_summary_takes_the_overview_paragraph_with_its_line(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Title\n\nBadge text here.\n\n![img](x.png)\n\n```mermaid\nflowchart LR A --> B a long diagram line that "
        "must never be read as prose\n```\n\n## Solution overview\n\n"
        "This accelerator does a useful thing with **Azure AI** and [Cosmos DB](http://x). It has a second sentence. "
        "A third one follows here. A fourth is dropped when too long " + "x" * 400 + ".\n",
        encoding="utf-8",
    )
    text, ref = readme_summary(tmp_path)
    assert text.startswith("This accelerator does a useful thing with Azure AI and Cosmos DB.")
    assert "flowchart" not in text and "**" not in text and "](" not in text
    assert ref == "README.md:13" and text.count(".") >= 2


def test_about_names_the_accelerator_and_cites_the_readme(capsys):
    rc = main(["--workspace", str(REPO_ROOT), "about", "--plain"])
    out = capsys.readouterr().out
    assert rc == 0 and "ABOUT | Customer Chatbot Solution Accelerator (customer-chatbot)" in out
    assert "What it is:" in out and "README.md:" in out and "Azure services:" in out
    assert "TroubleShootingSteps.md" in out and "Ask next:" in out


def test_unmatched_error_points_to_wider_sources_and_labels_them_external(synthetic_fixture, synthetic_env_file):
    from accel_troubleshooter.compact import render_compact
    from accel_troubleshooter.engine import Troubleshooter
    from accel_troubleshooter.azcli import ReplayRunner

    ts = Troubleshooter(REPO_ROOT, ReplayRunner(synthetic_fixture))
    d = ts.diagnose(
        "azd up fails with ZxQuuxWidgetError during the frobnicate step", env_file=synthetic_env_file, log=False
    )
    text = render_compact(d.to_dict(), "diagnose", plain=True)
    if d.hypotheses and d.hypotheses[0].status in ("confirmed", "likely"):
        assert "NOT IN THE ACCELERATOR DOCS?" not in text  # a documented cause was found: nothing to look up elsewhere
    else:
        assert "NOT IN THE ACCELERATOR DOCS?" in text and "(external, please verify)" in text
        assert "github.com/microsoft/customer-chatbot-solution-accelerator/issues?q=" in text
        assert "learn.microsoft.com/search/?terms=" in text
