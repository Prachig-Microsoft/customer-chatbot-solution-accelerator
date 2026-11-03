"""Draft a new troubleshooting-guide row from a diagnosed incident. Nothing is written to the guide itself.

The row follows the guide's own table format (``| **Issue/Error Code** | Description | <ul><li>steps</li></ul> |``),
is generalised (no resource names, ids, addresses or secrets), says which section it belongs to and after which line it
would go, and warns when the guide already documents the same problem.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .redact import redact_text

# Hypothesis id -> the guide section it belongs in (headings of documents/TroubleShootingSteps.md).
SECTION_FOR = {
    "quota-insufficient": "Quota & Capacity Limitations",
    "model-throttling": "Quota & Capacity Limitations",
    "observed-throttled": "Quota & Capacity Limitations",
    "model-region-unsupported": "Regional & Location Issues",
    "parameter-not-allowed": "Regional & Location Issues",
    "provider-unregistered": "Subscription & Access Issues",
    "subscription-mismatch": "Subscription & Access Issues",
    "rbac-missing": "Subscription & Access Issues",
    "deployer-cosmos-role-missing": "Subscription & Access Issues",
    "observed-rbac-denied": "Subscription & Access Issues",
    "observed-auth-token": "Subscription & Access Issues",
    "resource-group-missing": "Resource Group & Deployment Management",
    "resource-group-empty": "Resource Group & Deployment Management",
    "deployment-in-progress": "Resource Group & Deployment Management",
    "partial-deployment": "Resource State & Provisioning",
    "public-access-disabled": "Network & Infrastructure Configuration",
    "observed-cosmos-firewall": "Network & Infrastructure Configuration",
    "app-settings-missing": "Configuration & Property Errors",
    "agents-not-created": "Configuration & Property Errors",
    "images-not-built": "Configuration & Property Errors",
    "local-vs-cloud-config": "Configuration & Property Errors",
    "config-drift": "Configuration & Property Errors",
    "recent-config-change": "Configuration & Property Errors",
    "observed-config-missing": "Configuration & Property Errors",
    "observed-model-not-found": "Resource Identification & References",
}
DEFAULT_SECTION = "Miscellaneous"
SKIP_PREFIX = ("documented-",)  # the guide already has these rows
_GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


class Generaliser:
    """Replace everything specific to one deployment with a placeholder."""

    def __init__(self, literals: Dict[str, str], profile: Optional[Dict[str, Any]] = None):
        self.literals = {k: v for k, v in sorted(literals.items(), key=lambda kv: -len(kv[0])) if k}
        prefixes = [p.strip("^") for p in (profile or {}).get("resource_name_patterns", [])]
        self.names = [re.compile(rf"\b{re.escape(p)}[a-z0-9]+\b") for p in prefixes if p]

    def __call__(self, text: str) -> str:
        out = redact_text(str(text or ""))
        for literal, placeholder in self.literals.items():
            out = out.replace(literal, placeholder)
        out = re.sub(r"https://[\w-]+\.azurewebsites\.net", "https://<app>.azurewebsites.net", out)
        for rx in self.names:
            out = rx.sub("<resource-name>", out)
        out = _GUID.sub("<guid>", out)
        out = re.sub(r"\brg-[\w-]+", "<resource-group>", out)
        out = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<user>", out)
        out = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", out)
        return re.sub(r"\s+", " ", out).strip()


def _pick(diag) -> Tuple[Optional[Any], str]:
    """The finding worth documenting, or why there is none."""
    firm = [h for h in diag.hypotheses if h.status in ("confirmed", "likely")]
    if not firm:
        return None, (
            "Nothing to draft: the diagnosis has no confirmed or likely cause. Run it with the real error text or "
            "resource group, then draft again."
        )
    own = [h for h in firm if not h.id.startswith(SKIP_PREFIX)]
    if own:
        return own[0], ""
    return None, "documented"


def _section_line(guide: Path, section: str) -> Tuple[Optional[int], int]:
    """(line number after which a new row goes, rows already in that section's table)."""
    if not guide.is_file():
        return None, 0
    lines = guide.read_text(encoding="utf-8", errors="ignore").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == f"## {section}"), None)
    if start is None:
        return None, 0
    last, rows = None, 0
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            break
        if lines[i].startswith("|"):
            last = i + 1
            rows += 1
    return last, max(0, rows - 2)


def _confirm_commands(diag, hyp, gen: Generaliser) -> List[str]:
    """Read-only commands the checks ran to confirm this finding, generalised and de-duplicated."""
    out: List[str] = []
    wanted = set(hyp.supporting_checks)
    for c in diag.checks:
        if c.check_id in wanted:
            for rec in c.commands:
                cmd = gen(rec.command)
                if rec.source in ("live", "replay") and cmd.startswith("az ") and cmd not in out:
                    if "get-access-token" not in cmd:
                        out.append(cmd)
    return out[:3]


GENERIC_TITLES = {
    "parameter-not-allowed": (
        "InvalidTemplate (parameter value not allowed)",
        "A template parameter was set to a value outside its @allowed list, for example an AI region the template "
        "cannot deploy to.",
    ),
    "public-access-disabled": (
        "Cosmos DB public network access turned off by Azure Policy",
        "A tenant Azure Policy forces public network access off, so the app and the post-deployment scripts cannot "
        "reach Cosmos DB.",
    ),
    "observed-cosmos-firewall": (
        "Cosmos DB firewall blocks the API",
        "The app logs show Cosmos DB rejecting requests because of its firewall settings.",
    ),
    "images-not-built": (
        "App Services still run the placeholder image",
        "The container images were not built and pushed after provisioning.",
    ),
    "agents-not-created": (
        "Foundry agent settings are not populated",
        "The post-provision script that creates the Foundry agents has not run (or failed).",
    ),
    "app-stopped": ("App Service is stopped", "The API or web app is not running, so requests fail."),
    "rbac-missing": (
        "Managed identity is missing a role assignment",
        "The app's identity lacks a role it needs, so calls to the AI, Search or Cosmos DB service are denied.",
    ),
    "deployer-cosmos-role-missing": (
        "Post-provision data script gets Unauthorized from Cosmos DB",
        "The signed-in user has no Cosmos DB data-plane role, which the data upload script needs.",
    ),
}


def _documented_row(diag):
    """A guide row for the same error code the user reported, if the guide has one."""
    codes = {c.lower() for c in diag.issue.error_codes}
    for d in getattr(diag, "docs", []) or []:
        if d.doc_type == "troubleshooting-row" and d.category != "summary":
            if any(c in x.lower() for x in d.error_codes for c in codes):
                return d
    return None


def _targets_generaliser(diag, profile: Optional[Dict[str, Any]]) -> Generaliser:
    t = diag.targets or {}
    literals = {
        str(t.get("resource_group") or ""): "<resource-group>",
        str(t.get("azd_env") or ""): "<env-name>",
        str(t.get("subscription") or ""): "<subscription-id>",
    }
    return Generaliser({k: v for k, v in literals.items() if len(k) > 3}, profile)


def _draft_from_note(diag, guide_path: Path, note: str, profile, existing) -> Dict[str, Any]:
    """No cause was found, but the user knows what fixed it: draft from their words, clearly marked as theirs."""
    gen = _targets_generaliser(diag, profile)
    codes = list(dict.fromkeys(diag.issue.error_codes))
    symptom = gen(diag.issue.raw_text)[:90]
    title_cell = " / ".join(f"**{c}**" for c in codes[:3]) if codes else f"**{symptom}**"
    steps = [f"<b>Verified fix:</b> {gen(note)}", "<b>Cause:</b> (add one sentence: why it happens)"]
    row = (
        f"| {title_cell} | (describe the symptom in one line) | <ul>"
        + "".join(f"<li>{s}</li>" for s in steps)
        + "</ul> |"
    )
    after, rows = _section_line(guide_path, DEFAULT_SECTION)
    return {
        "drafted": True,
        "row": row,
        "finding": {"id": "user-reported", "status": "reported by you", "confidence": "not verified by the tool"},
        "section": DEFAULT_SECTION,
        "insert_after_line": after,
        "rows_in_section": rows,
        "guide": guide_path.name,
        "similar_entry": f"{existing.ref} ({existing.title[:60]})" if existing else "",
        "incident": gen(diag.issue.raw_text)[:200],
        "run_id": diag.run_id,
        "note": "No cause was found by the checks, so this row only contains your note. Pick the right section.",
    }


def draft_entry(
    diag, guide_path: Path, note: str = "", force: bool = False, profile: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Everything a person needs to review and paste one guide row. Never writes to the guide."""
    hyp, why = _pick(diag)
    existing = _documented_row(diag)
    if (why == "documented" or existing) and not force:
        at = existing.ref if existing else next(h for h in diag.hypotheses if h.doc_refs).doc_refs[0]
        return {
            "drafted": False,
            "documented_at": at,
            "reason": f"The guide already documents this ({at}). No new entry is needed; use --force to draft one "
            "anyway (for example to extend that entry).",
        }
    if hyp is None:
        if why != "documented":
            if not note.strip():
                return {"drafted": False, "reason": why + " Or pass --note with what fixed it to draft from that."}
            return _draft_from_note(diag, guide_path, note, profile, existing)
        hyp = next(h for h in diag.hypotheses if h.status in ("confirmed", "likely"))
    gen = _targets_generaliser(diag, profile)
    codes = list(dict.fromkeys(diag.issue.error_codes))
    generic = GENERIC_TITLES.get(hyp.id)
    title = generic[0] if generic else gen(hyp.title)[:90]
    title_cell = " / ".join(f"**{c}**" for c in codes[:3]) if codes else f"**{title}**"
    description = generic[1] if generic else gen(hyp.title)
    steps: List[str] = []
    cmds = _confirm_commands(diag, hyp, gen)
    if cmds:
        steps.append("<b>How to confirm</b> (read-only):<br>" + "<br>".join(f"`{c}`" for c in cmds))
    cause = [gen(r) for r in hyp.rationale[:3] if r]
    if cause:
        steps.append("<b>Why it happens:</b> " + "; ".join(cause))
    for aid in hyp.remediation_ids:
        action = diag.actions.get(aid)
        if action is None or action.id.startswith("collect-"):
            continue
        documented_steps = action.id.startswith("follow-documented-steps")
        body = "Fix" if documented_steps else gen(action.title)
        real = [gen(c) for c in action.commands][:3]
        if real:
            body += ":<br>" + "<br>".join(f"`{c}`" for c in real)
        if action.risk and not documented_steps:
            body += f"<br>⚠️ {gen(action.risk)}"
        steps.append(body)
    if note.strip():
        steps.append("<b>Verified fix:</b> " + gen(note))
    refs = [r for r in dict.fromkeys(hyp.doc_refs + hyp.source_refs) if r][:3]
    if refs:
        steps.append("<b>See also:</b> " + ", ".join(f"`{r}`" for r in refs))
    row = f"| {title_cell} | {description} | <ul>" + "".join(f"<li>{s}</li>" for s in steps) + "</ul> |"
    section = SECTION_FOR.get(hyp.id, DEFAULT_SECTION)
    after, rows = _section_line(guide_path, section)
    return {
        "drafted": True,
        "row": row,
        "finding": {"id": hyp.id, "status": hyp.status, "confidence": hyp.confidence},
        "section": section,
        "insert_after_line": after,
        "rows_in_section": rows,
        "guide": guide_path.name,
        "similar_entry": f"{existing.ref} ({existing.title[:60]})" if existing else "",
        "incident": gen(diag.issue.raw_text)[:200],
        "run_id": diag.run_id,
    }


def render_draft(d: Dict[str, Any], guide_rel: str, plain: bool = False) -> str:
    if not d.get("drafted"):
        return d["reason"]
    mark = ("*", "->") if plain else ("•", "↳")
    where = (
        f"after line {d['insert_after_line']} of {guide_rel} (end of the '{d['section']}' table, "
        f"{d['rows_in_section']} rows)"
        if d["insert_after_line"]
        else f"the '{d['section']}' section of {guide_rel} (add the section if it does not exist)"
    )
    status = f"{d['finding']['status']}, {d['finding']['confidence']} confidence"
    if d["finding"]["id"] == "user-reported":
        status = "reported by you, not verified by the tool"
    out = [
        f"DRAFT GUIDE ENTRY ({status}): review it, then add it",
        f"  {mark[0]} Where: {where}",
        f"  {mark[0]} From incident: {d['incident']}",
    ]
    if d.get("note"):
        out.append(f"  {mark[0]} {d['note']}")
    if d.get("similar_entry"):
        out.append(f"  {mark[0]} Similar entry already exists: {d['similar_entry']}. Consider extending it instead.")
    out += [
        "",
        "| Issue/Error Code | Description | Steps to Resolve |",
        "|---|---|---|",
        d["row"],
        "",
        f"  {mark[1]} Nothing was written. Check the steps and remove anything specific to your tenant, then paste it.",
    ]
    return "\n".join(out)
