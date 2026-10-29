"""Markdown incident report rendering."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .actions import EXECUTABLE_ACTIONS
from .models import APPROVAL_REQUIRED, MANUAL, READ_ONLY, RESTRICTED

_ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️", "skip": "⏭️", "error": "💥", "info": "ℹ️"}
_CAT = {
    READ_ONLY: "🟢 read-only",
    APPROVAL_REQUIRED: "🟡 approval required",
    MANUAL: "📘 documented steps — review and run manually (never auto-run)",
    RESTRICTED: "🔴 restricted (manual only)",
}
_CAT_ORDER = {READ_ONLY: 0, APPROVAL_REQUIRED: 1, MANUAL: 2, RESTRICTED: 3}


def _cell(text: Any, limit: int = 160) -> str:
    s = str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_markdown(d: Dict[str, Any], verbose: bool = False) -> str:
    out: List[str] = []
    a = d.get("accelerator", {})
    issue = d["issue"]
    out.append(f"# Troubleshooting report `{d['run_id']}`")
    out.append(f"_Generated {d['started_at']} · mode **{d['mode']}** · {d['duration_ms']} ms_\n")

    if d.get("clarification"):
        out.append("## ❓ Clarification needed")
        out.append(d["clarification"])
        scores = a.get("identification", {}).get("scores", {})
        if scores:
            out.append("\nIdentification scores: " + ", ".join(f"{k}={v}" for k, v in scores.items()))
        return "\n".join(out)

    top = d["hypotheses"][0] if d["hypotheses"] else None
    out.append("## 1. Problem summary")
    out.append(f"- **Reported:** {_cell(issue['raw_text'], 400)}")
    reasons = "; ".join(a.get("identification", {}).get("reasons", {}).get(a.get("id"), []))
    out.append(f"- **Accelerator:** {a.get('name')} (`{a.get('id')}`) — {reasons}")
    out.append(
        f"- **Repository:** `{a.get('repo_path')}` · branch `{a.get('git_branch')}` · commit `{a.get('git_commit')}`"
    )
    stages = ", ".join(issue["stages"][:3]) or "unknown"
    cats = ", ".join(issue["categories"][:3]) or "-"
    out.append(f"- **Stage:** {stages} · **Categories:** {cats}")
    extracted = {
        k: issue[k]
        for k in ("error_codes", "http_statuses", "regions", "resource_names", "models", "timestamps")
        if issue.get(k)
    }
    if extracted:
        out.append("- **Extracted:** " + "; ".join(f"{k}={v}" for k, v in extracted.items()))
    t = d.get("targets", {})
    out.append(
        f"- **Targets:** RG `{t.get('resource_group')}` (from {t.get('resource_group_source')}), AI region "
        f"`{t.get('ai_region')}`, azd env `{t.get('azd_env')}`, subscription `{t.get('subscription')}`"
    )
    if top:
        out.append(
            f"- **Most likely root cause:** **{top['title']}** — {top['status']}, confidence {top['confidence']}"
            f" (component: {top['component']})"
        )
    else:
        out.append("- **Most likely root cause:** not determined — see missing evidence.")

    out.append("\n## 2. Root-cause analysis")
    if not d["hypotheses"]:
        out.append("_No hypothesis is supported by the collected evidence._")
    for i, h in enumerate(d["hypotheses"], 1):
        label = {
            "confirmed": "Confirmed finding",
            "likely": "Likely root cause",
            "possible": "Alternative / possible cause",
        }
        out.append(f"\n### 2.{i} {h['title']}")
        status_label = label.get(h["status"], h["status"])
        out.append(f"**{status_label}** · confidence **{h['confidence']}** · component: {h['component']}")
        out.append("\nEvidence / confidence rationale:")
        out.extend(f"- {r}" for r in h["rationale"])
        if h["contradicting"]:
            out.append("\nContradicting evidence:")
            out.extend(f"- {r}" for r in h["contradicting"])
        if h["missing_evidence"]:
            out.append("\nMissing evidence:")
            out.extend(f"- {r}" for r in h["missing_evidence"])
        refs = [r for r in h["doc_refs"] + h["source_refs"] if r]
        if refs:
            out.append("\nReferences: " + ", ".join(f"`{r}`" for r in dict.fromkeys(refs)))
        if h["supporting_checks"]:
            out.append("Checks: " + ", ".join(f"`{c}`" for c in h["supporting_checks"]))
        if h["remediation_ids"]:
            out.append("Remediation: " + ", ".join(f"`{r}`" for r in h["remediation_ids"]))

    out.append("\n## 3. Recommended remediation (smallest safe step first)")
    acts = d.get("actions", {})
    ordered = sorted(acts.values(), key=lambda x: _CAT_ORDER[x["category"]])
    if not ordered:
        out.append("_No remediation proposed until missing evidence is collected._")
    for act in ordered:
        out.append(f"\n**`{act['id']}`** — {act['title']}  \n{_CAT[act['category']]}")
        out.append(f"- {act['description']}")
        if act["commands"]:
            out.append("- Commands:\n" + "\n".join(f"  ```\n  {c}\n  ```" for c in act["commands"]))
        if act["risk"]:
            out.append(f"- Risk: {act['risk']}")
        if act["prerequisites"]:
            out.append(f"- Prerequisites: {', '.join(act['prerequisites'])}")
        if act["verify_checks"]:
            out.append(
                f'- Verify with: `python -m accel_troubleshooter diagnose "..." --only '
                f"{','.join(act['verify_checks'])}`"
            )
        if (
            act["category"] == APPROVAL_REQUIRED
            and not act["params"].get("manual")
            and act["id"].split("#")[0] in EXECUTABLE_ACTIONS
        ):
            out.append(
                f"- Execute after approval: `python -m accel_troubleshooter fix {act['id']} --approve "
                f'--issue "<same issue text>" <same options as diagnose>`'
            )
        if act["doc_refs"]:
            out.append("- Docs: " + ", ".join(f"`{r}`" for r in act["doc_refs"] if r))

    out.append("\n## 4. Diagnostic checks performed")
    out.append("| | Check | Result | Duration |")
    out.append("|---|---|---|---|")
    for c in d["checks"]:
        out.append(
            f"| {_ICON.get(c['status'], c['status'])} | `{c['check_id']}` {c['title']} | {_cell(c['summary'], 220)} | "
            f"{c['duration_ms']} ms |"
        )
    cmd_rows = [(c["check_id"], cmd) for c in d["checks"] for cmd in c["commands"]]
    if cmd_rows:
        out.append("\n<details><summary>Commands executed (sanitized)</summary>\n")
        out.append("| Check | Command | Exit | Source | Timestamp |")
        out.append("|---|---|---|---|---|")
        for cid, cmd in cmd_rows:
            command = _cell(cmd["command"], 120)
            out.append(f"| {cid} | `{command}` | {cmd['exit_code']} | {cmd['source']} | {cmd['timestamp']} |")
        out.append("\n</details>")
    if verbose:
        out.append("\n<details><summary>Check evidence (JSON)</summary>\n\n```json")
        out.append(json.dumps({c["check_id"]: c["evidence"] for c in d["checks"]}, indent=2, default=str)[:20000])
        out.append("```\n</details>")

    out.append("\n## 5. Documentation consulted")
    for doc in d["docs"]:
        out.append(f"- `{doc['ref']}` — {doc['title']} › {doc['section']} ({doc['doc_type']}, score {doc['score']})")
    if d.get("sources"):
        out.append("\nRelevant source:")
        for s in d["sources"]:
            out.append(f"- `{s['ref']}` ({s['title']})")
    idx = a.get("index", {})
    out.append(
        f"\n_Index: {idx.get('passages')} passages built {idx.get('built_at')} at commit "
        f"`{idx.get('indexed_commit')}`; stale files: {idx.get('stale_files') or 'none'}._"
    )

    out.append("\n## 6. Missing evidence")
    out.extend(f"- {m}" for m in d["missing_evidence"]) if d["missing_evidence"] else out.append("- none")
    if d.get("limitations"):
        out.append("\n## 7. Limitations")
        out.extend(f"- {m}" for m in d["limitations"])
    if d.get("verification"):
        v = d["verification"]
        out.append("\n## 8. Verification after fix")
        out.append(f"Action `{v['action']}` at {v['timestamp']} — resolved: **{v['resolved']}**")
        for r in v["checks"]:
            out.append(f"- `{r['check']}`: {r['before']} → {r['after']} — {r['summary']}")
    out.append("\n## Escalation (if unresolved)")
    out.append(
        f"1. Collect this report (JSON: `--format json`) plus the full `azd up` output.\n"
        f"2. Search/open an issue: {a.get('upstream')}/issues\n"
        "3. For quota/capacity: Azure support request (Service and subscription limits)."
    )
    return "\n".join(out)
