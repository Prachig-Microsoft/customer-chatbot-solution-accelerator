"""The document gaps file: everything the documentation is missing or gets wrong, saved locally as Markdown.

Combines the offline drift findings (docs and scripts that disagree with the Bicep and parameter files) with
documentation
coverage gaps (no troubleshooting guide, empty guides, broken links, missing sections) and tool-version disagreements
between documents. The file is written to ``.runs/document-gaps.md`` (git-ignored, per machine) every time ``drift`` or
``gaps`` runs, so there is always a current list to turn into pull requests.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

from .toolscan import FRIENDLY, parse_version


def gaps_path() -> Path:
    from .telemetry import RUNS_FILE

    return Path(RUNS_FILE).parent / "document-gaps.md"


def tool_conflicts(requirements: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tools whose documents ask for different minimum versions (for example Python 3.9+ in one, 3.11 in another)."""
    out = []
    for tool, r in sorted(requirements.get("tools", {}).items()):
        mins = r.get("mins") or {}
        if len(mins) > 1:
            out.append(
                {
                    "tool": FRIENDLY.get(tool, tool),
                    "versions": {v: srcs[:2] for v, srcs in sorted(mins.items(), key=lambda kv: parse_version(kv[0]))},
                }
            )
    return out


def build_gaps(drift_report: Dict[str, Any], cov: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
    missing = [{"kind": kind, "path": p} for kind, v in cov.get("categories", {}).items() for p in v.get("missing", [])]
    docs: List[Dict[str, str]] = []
    if not cov.get("troubleshooting_docs"):
        docs.append(
            {
                "what": "No troubleshooting guide was found",
                "fix": "Add a guide with an error/resolution table or question headings, so errors can be matched.",
            }
        )
    for g in cov.get("troubleshooting_docs", []):
        if g["entries"] == 0:
            docs.append(
                {
                    "what": f"{g['path']} has no recognisable entries ({g['format']})",
                    "fix": "Use a table with the error in the first column, or one heading per problem.",
                }
            )
    readme = cov.get("readme") or {}
    if readme and not readme.get("prerequisites_section"):
        docs.append(
            {"what": "README.md has no prerequisites section", "fix": "Add one that lists tools, versions and quota."}
        )
    if readme and not readme.get("has_troubleshooting_section") and not cov.get("troubleshooting_docs"):
        docs.append({"what": "README.md has no troubleshooting section", "fix": "Add one or link a guide."})
    return {
        "drift": drift_report.get("findings", []),
        "missing_docs": missing,
        "broken_links": cov.get("broken_local_links", []),
        "doc_gaps": docs,
        "tool_conflicts": tool_conflicts(requirements),
        "accelerator": drift_report.get("accelerator"),
        "commit": drift_report.get("commit"),
        "branch": drift_report.get("branch"),
    }


def render_gaps(g: Dict[str, Any]) -> str:
    total = sum(len(g[k]) for k in ("drift", "missing_docs", "broken_links", "doc_gaps", "tool_conflicts"))
    out = [
        "# Document gaps",
        "",
        f"Accelerator: **{g['accelerator']}** · branch `{g['branch'] or '-'}` · commit `{g['commit'] or '-'}` · "
        f"generated {time.strftime('%Y-%m-%d %H:%M')} by the accelerator troubleshooter (offline; each item is a "
        "suggestion to review). This file is local and git-ignored.",
        "",
        f"**{total} gap(s).**",
        "",
    ]
    if g["drift"]:
        out += ["## Docs and scripts that disagree with the Bicep", ""]
        for i in g["drift"]:
            out.append(f"- [ ] **{i['impact']}** `{i['where'] or i['file']}` — {i['detail']}")
            if i["fix"]:
                out.append(f"  - Fix: {i['fix']}")
        out.append("")
    if g["tool_conflicts"]:
        out += ["## Documents that ask for different tool versions", ""]
        for c in g["tool_conflicts"]:
            parts = "; ".join(f"{v} in {', '.join(s)}" for v, s in c["versions"].items())
            out.append(f"- [ ] **{c['tool']}**: {parts}")
            out.append("  - Fix: state one minimum version everywhere.")
        out.append("")
    if g["doc_gaps"]:
        out += ["## Missing or unusable documentation", ""]
        for i in g["doc_gaps"]:
            out += [f"- [ ] {i['what']}", f"  - Fix: {i['fix']}"]
        out.append("")
    if g["missing_docs"]:
        out += ["## Documents the profile expects but the repository does not have", ""]
        out += [f"- [ ] `{m['path']}` ({m['kind']})" for m in g["missing_docs"]]
        out.append("")
    if g["broken_links"]:
        out += ["## Broken links between documents", ""]
        out += [f"- [ ] `{b['file']}:{b['line']}` → `{b['target']}`" for b in g["broken_links"]]
        out.append("")
    if not total:
        out.append("No gaps found.")
    return "\n".join(out)


def save_gaps(text: str) -> Path:
    path = gaps_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
