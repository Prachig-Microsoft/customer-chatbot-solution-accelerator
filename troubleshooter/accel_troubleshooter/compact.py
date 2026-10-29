"""Compact, terminal-friendly rendering (the default output). The full Markdown report lives in report.py."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .actions import EXECUTABLE_ACTIONS
from .models import APPROVAL_REQUIRED, MANUAL, READ_ONLY, RESTRICTED

_ICONS = {"pass": "✅", "fail": "❌", "warn": "⚠️ ", "skip": "⏭️ ", "error": "💥", "info": "ℹ️ "}
_PLAIN = {"pass": "[ OK ]", "fail": "[FAIL]", "warn": "[WARN]", "skip": "[skip]", "error": "[ERR ]", "info": "[info]"}
_CAT_ICON = {READ_ONLY: "🟢", APPROVAL_REQUIRED: "🟡", MANUAL: "📘", RESTRICTED: "🔴"}
_CAT_PLAIN = {
    READ_ONLY: "(read-only)",
    APPROVAL_REQUIRED: "(needs approval)",
    MANUAL: "(manual)",
    RESTRICTED: "(restricted)",
}
_CAT_ORDER = {READ_ONLY: 0, APPROVAL_REQUIRED: 1, MANUAL: 2, RESTRICTED: 3}

SHORT = {
    "repo.azd_env": "azd environment",
    "repo.config_drift": "config consistency",
    "repo.git_changes": "uncommitted config",
    "az.context": "Azure login / subscription",
    "providers.registration": "resource providers",
    "rg.state": "resource group",
    "deploy.operations": "ARM deployments",
    "resources.state": "resources",
    "activity.failures": "activity log (72h)",
    "quota.models": "model quota",
    "models.availability": "model availability",
    "ai.deployments": "AI model deployments",
    "network.public_access": "network access",
    "webapp.state": "app services",
    "webapp.health": "health endpoints",
    "rbac.app_identity": "app identity roles",
    "rbac.deployer_data": "your Cosmos DB role",
    "config.compare": "local vs Azure settings",
}


def _t(text: Any, n: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _check(d: Dict[str, Any], cid: str) -> Optional[Dict[str, Any]]:
    return next((c for c in d["checks"] if c["check_id"] == cid), None)


def _quota_lines(qc: Dict[str, Any], plain: bool) -> List[str]:
    """One line per model. Global-scope quota is one subscription-wide pool, so it is shown once, not per region."""
    ev = qc.get("evidence") or {}
    regions = ev.get("regions") or {}
    out: List[str] = []
    ok_i, bad_i = ("OK", "!!") if plain else ("✅", "❌")
    for r in ev.get("requirements", []):
        rows = [(region, m) for region, v in regions.items() for m in v.get("models", []) if m["model"] == r["model"]]
        if not rows:
            continue
        need = r["capacity"]
        scopes = {m["scope"] for _, m in rows}
        avails = {m["available"] for _, m in rows}
        label = f"{r['model']:<24} need {need:>4}K  "
        if scopes == {"Global"} and len(avails) == 1:
            avail = next(iter(avails))
            flag = ok_i if avail is not None and avail >= (need or 0) else bad_i
            out.append(f"  {label}have {avail}K  (Global pool, same in every region)  {flag}")
        else:
            parts = [f"{region} {m['available']}{'' if m['ok'] else ' ' + bad_i}" for region, m in rows]
            out.append(f"  {label}per region: " + " · ".join(parts))
    return out


def render_compact(d: Dict[str, Any], kind: str = "diagnose", plain: bool = False, max_findings: int = 3) -> str:
    icons = _PLAIN if plain else _ICONS
    a = d.get("accelerator", {})
    out: List[str] = []
    if d.get("clarification"):
        return "\n".join(["? Which accelerator?", "  " + d["clarification"]])

    title = {"preflight": "PREFLIGHT", "quota": "QUOTA CHECK", "diagnose": "DIAGNOSIS"}.get(kind, kind.upper())
    commit = (a.get("git_commit") or "")[:7]
    sub = (_check(d, "az.context") or {}).get("evidence", {}).get("subscription_name")
    out.append(f"{title}  {a.get('id', '?')} @ {commit or 'no-git'}  ·  {d['mode']}  ·  {d['duration_ms'] / 1000:.0f}s")
    t = d.get("targets", {})
    rg = f"RG {t['resource_group']} ({t['resource_group_source']})" if t.get("resource_group") else "RG not found"
    out.append(
        f"Target: {rg}  ·  AI region {t.get('ai_region') or '-'}  ·  azd env {t.get('azd_env') or '-'}"
        + (f"  ·  sub {sub}" if sub else "")
    )
    out.append("")

    blocking = [c for c in d["checks"] if c["status"] in ("fail", "error")]
    top = d["hypotheses"][0] if d["hypotheses"] else None
    cands = (_check(d, "quota.models") or {}).get("evidence", {}).get("regions_with_sufficient_quota") or []
    if kind == "preflight":
        out.append(
            "VERDICT  " + ("🛑 NO-GO — fix the items marked below" if blocking else "✅ GO — safe to run azd up")
        )
        if plain:
            out[-1] = out[-1].replace("🛑", "[X]").replace("✅", "[OK]")
    elif top and top["status"] in ("confirmed", "likely"):
        out.append(f"LIKELY CAUSE  {top['title']}  [{top['status']} · {top['confidence']} confidence]")
    elif kind == "quota":
        out.append("VERDICT  " + ("quota is NOT sufficient" if blocking else "quota is sufficient"))
    else:
        out.append("RESULT  No confirmed or likely cause from the evidence collected — see 'Still needed'.")
    out.append("")

    skipped = [c for c in d["checks"] if c["status"] == "skip"]
    for c in d["checks"]:
        if c["status"] == "skip":
            continue
        name = SHORT.get(c["check_id"], c["title"])
        out.append(f"  {icons[c['status']]} {name:<27} {_t(c['summary'], 78)}")
    if skipped:
        reasons = sorted({_t(c["summary"], 50) for c in skipped})[:2]
        out.append(f"  {icons['skip']} {len(skipped)} skipped: " + "; ".join(reasons))

    qc = _check(d, "quota.models")
    if qc and (qc.get("evidence") or {}).get("regions") and kind in ("preflight", "quota", "diagnose"):
        lines = _quota_lines(qc, plain)
        if lines:
            out += ["", "QUOTA (K tokens/min)"] + lines
            if cands:
                out.append(f"  Usable AI regions: {', '.join(cands)}")

    findings = [h for h in d["hypotheses"] if h["status"] in ("confirmed", "likely", "possible")][:max_findings]
    if findings and kind != "preflight":
        out += ["", "FINDINGS"]
        for i, h in enumerate(findings, 1):
            out.append(f"  {i}. {_t(h['title'], 90)}  [{h['status']} · {h['confidence']}]")
            for r in h["rationale"][:2]:
                out.append(f"     • {_t(r, 110)}")
            refs = [x for x in dict.fromkeys(h["doc_refs"] + h["source_refs"]) if x][:3]
            if refs:
                out.append("     see: " + ", ".join(refs))

    drift = (_check(d, "repo.config_drift") or {}).get("evidence", {}).get("findings", [])
    if drift:
        out += ["", "NOTES"]
        for f in drift[:3]:
            out.append(
                f"  • {_t(f.get('short') or f['message'], 130)}" + (f"  ({f['refs'][0]})" if f.get("refs") else "")
            )
        if len(drift) > 3:
            out.append(f"  • … {len(drift) - 3} more (use --format full)")

    acts = sorted(d.get("actions", {}).values(), key=lambda x: _CAT_ORDER[x["category"]])[:3]
    steps: List[str] = []
    if kind == "preflight" and not blocking:
        region = cands[0] if cands else "<region>"
        env_ev = (_check(d, "repo.azd_env") or {}).get("evidence", {})
        ai_key = "AZURE_ENV_AI_SERVICE_LOCATION"
        if not env_ev.get("env_name"):
            steps += ["azd env new <name>", f"azd env set {ai_key} {region}"]
        elif not env_ev.get("ai_region_chosen"):
            steps.append(f"azd env set {ai_key} {region}")
        steps.append("python -m accel_troubleshooter run -- azd up        (or plain: azd up)")
    if steps or acts:
        out += ["", "NEXT"]
        out += [f"  $ {s}" for s in steps]
        for act in acts:
            icon = _CAT_PLAIN[act["category"]] if plain else _CAT_ICON[act["category"]]
            cmd = _t(act["commands"][0], 95) if act["commands"] else _t(act["description"], 95)
            out.append(f"  {icon} {act['id']}: {cmd}")
        if any(a_["category"] == APPROVAL_REQUIRED and a_["id"].split("#")[0] in EXECUTABLE_ACTIONS for a_ in acts):
            out.append(
                "  To apply an approval-required action: fix <action-id> --approve   (you confirm; then it re-checks)"
            )

    missing = d.get("missing_evidence", [])
    if missing and kind != "preflight":
        out += ["", "STILL NEEDED"]
        out += [f"  • {_t(m, 120)}" for m in missing[:4]]

    out += ["", "Details: --format full · --out report.md (full report saved) · --format json"]
    return "\n".join(out)
