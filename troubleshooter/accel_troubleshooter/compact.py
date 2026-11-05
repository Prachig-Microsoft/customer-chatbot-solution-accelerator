"""Compact, bullet-style, optionally colourful terminal rendering (the default output).

Rendering returns plain text; ``colorize`` adds ANSI colour afterwards (only on a real terminal, never with --plain or
NO_COLOR), so tests and redirected output stay clean. The full Markdown report lives in report.py.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

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
_ASCII = {"•": "*", "◦": "-", "▸": ">", "▶": ">", "↳": "->", "▌": "#", "·": "|", "—": "-", "…": "..."}

SHORT = {
    "repo.azd_env": "azd environment",
    "repo.config_drift": "config consistency",
    "repo.git_changes": "uncommitted config",
    "repo.exemption_tag": "policy tag in template",
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
    "azure.fabric": "Fabric capacity state",
    "repo.tools": "tools and versions",
    "cleanup.inventory": "what a cleanup deletes",
    "webapp.state": "app services",
    "webapp.health": "health endpoints",
    "auth.setup": "sign-in (Entra, optional)",
    "rbac.app_identity": "app identity roles",
    "rbac.deployer_data": "your Cosmos DB role",
    "config.compare": "local vs Azure settings",
    "appinsights.errors": "app error logs (1h)",
    "chat.e2e": "real chat message",
}


# ----------------------------------------------------------------------------- colour

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = (f"\x1b[{n}m" for n in (31, 32, 33, 34, 35, 36))


def color_enabled(stream=None, plain: bool = False, force: Optional[bool] = None) -> bool:
    """Colour only on an interactive terminal; NO_COLOR and --plain always win; FORCE_COLOR forces it on."""
    if plain or os.environ.get("NO_COLOR") or force is False:
        return False
    if force or os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def enable_windows_vt() -> None:
    """Make legacy Windows consoles interpret ANSI escapes (a no-op elsewhere and in modern terminals)."""
    if os.name == "nt":
        os.system("")  # noqa: S605 - documented trick that switches on VIRTUAL_TERMINAL_PROCESSING


def _p(text: str, *styles: str) -> str:
    return "".join(styles) + text + RESET


_STATUS_COLOR = {"confirmed": RED, "likely": YELLOW, "possible": CYAN}
_SECTIONS = ("CHECKS", "QUOTA", "FINDINGS", "NOTES", "NEXT", "STILL NEEDED")


def colorize(text: str) -> str:
    """Add ANSI colour to rendered compact output, line by line (purely cosmetic)."""
    out: List[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith(("PREFLIGHT:", "▌", "# ")):
            if "NO-GO" in s:
                line = _p(line, BOLD, RED)
            elif "NOT CHECKED" in s:
                line = _p(line, BOLD, YELLOW)
            elif "GO" in s.split("·")[0].split("|")[0] and "PREFLIGHT" in s:
                line = _p(line, BOLD, GREEN)
            else:
                line = _p(line, BOLD, CYAN)
        elif s.startswith(("CAUSE", "RESULT", "VERDICT")):
            m = re.search(r"\[(confirmed|likely|possible)", s)
            ok = s.startswith("RESULT") or "is sufficient" in s
            line = _p(line, BOLD, GREEN if ok else _STATUS_COLOR.get(m.group(1), YELLOW) if m else YELLOW)
        elif s in _SECTIONS or s.split(" (")[0] in _SECTIONS:
            line = _p(line, BOLD, MAGENTA)
        elif s.startswith(("✅", "[ OK ]")):
            line = line.replace(s[:1] if s[0] == "✅" else "[ OK ]", _p(s[:1] if s[0] == "✅" else "[ OK ]", GREEN), 1)
        elif s.startswith(("❌", "[FAIL]", "💥", "[ERR ]")):
            line = _p(line, RED)
        elif s.startswith(("⚠️", "[WARN]")):
            line = _p(line, YELLOW)
        elif s.startswith(("ℹ️", "[info]", "⏭️", "[skip]")):
            line = _p(line, DIM)
        elif s.startswith(("↳", "->", "◦", "- ")):
            line = _p(line, DIM)
        elif s.startswith(("▶", "$ ", "> ")):
            line = _p(line, CYAN)
        elif s.startswith(("🔴", "(restricted)")):
            line = _p(line, RED)
        elif s.startswith(("🟡", "(needs approval)")):
            line = _p(line, YELLOW)
        elif s.startswith(("📘", "(manual)")):
            line = _p(line, BLUE)
        elif s.startswith(("🟢", "(read-only)")):
            line = _p(line, GREEN)
        elif s.startswith(("•", "* ")):
            m = re.search(r"\[(confirmed|likely|possible)", s)
            if m and "·" in s or m and "|" in s:
                line = _p(line, BOLD, _STATUS_COLOR[m.group(1)])
        out.append(line)
    return "\n".join(out)


def _finish(text: str, plain: bool) -> str:
    if plain:
        for k, v in _ASCII.items():
            text = text.replace(k, v)
    return text


# ----------------------------------------------------------------------------- helpers


def _t(text: Any, n: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _check(d: Dict[str, Any], cid: str) -> Optional[Dict[str, Any]]:
    return next((c for c in d["checks"] if c["check_id"] == cid), None)


_NOT_GUIDE_WORTHY = {"deployment-in-progress", "resource-group-empty", "az-cli-unavailable", "subscription-mismatch"}


def _guide_gap(d: Dict[str, Any], top: Dict[str, Any]) -> bool:
    """True when a real cause was found but the guide has no row for the error code the user reported."""
    if top["id"].startswith("documented-") or top["id"] in _NOT_GUIDE_WORTHY:
        return False
    codes = {c.lower() for c in (d.get("issue") or {}).get("error_codes", [])}
    for doc in d.get("docs", []):
        if doc.get("doc_type") == "troubleshooting-row" and doc.get("category") != "summary":
            if any(c in x.lower() for x in doc.get("error_codes", []) for c in codes):
                return False
    return True


def _quota_lines(qc: Dict[str, Any], plain: bool) -> List[str]:
    """One bullet per model. Global-scope quota is one subscription-wide pool, so it is shown once, not per region."""
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
        label = f"{r['model']:<24} need {need:>4}K"
        if scopes == {"Global"} and len(avails) == 1:
            avail = next(iter(avails))
            flag = ok_i if avail is not None and avail >= (need or 0) else bad_i
            out.append(f"  • {label} · have {avail}K (one pool for every region) {flag}")
        else:
            parts = [f"{region} {m['available']}{'' if m['ok'] else ' ' + bad_i}" for region, m in rows]
            out.append(f"  • {label} · per region: " + " · ".join(parts))
    return out


def _recommend_region(d: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the AI region: keep the user's choice if it has quota, else the best region with quota for every model."""
    env_ev = (_check(d, "repo.azd_env") or {}).get("evidence", {})
    qev = (_check(d, "quota.models") or {}).get("evidence", {})
    regions = qev.get("regions") or {}
    cands = qev.get("regions_with_sufficient_quota") or []
    locs = (_check(d, "repo.config_drift") or {}).get("evidence", {}).get("allowed_locations") or []
    chosen = env_ev.get("ai_location")

    def headroom(region: str) -> int:  # per-region (Classic) quota is what differs between regions
        return sum(m["available"] or 0 for m in regions[region]["models"] if m["scope"] != "Global")

    ranked = sorted(cands, key=lambda r: (headroom(r), r in locs), reverse=True)
    if chosen and chosen in cands:
        region, why = chosen, "your azd env choice, quota OK"
    elif chosen and chosen in regions:
        if ranked:
            region, why = ranked[0], f"{chosen} (your choice) is short on quota; {ranked[0]} has enough"
        else:
            region, why = chosen, "short on quota here and in every allowed region (Global quota is shared)"
    elif ranked:
        region, why = ranked[0], "best free quota" + (", also a valid primary location" if ranked[0] in locs else "")
    else:
        region, why = None, "no allowed region has enough quota"
    others = [r for r in ranked if r != region]
    return {"region": region, "why": why, "others": others, "chosen": chosen, "locations": locs, "env": env_ev}


def render_preflight(d: Dict[str, Any], plain: bool = False) -> str:
    """The pre-deployment answer in about 12 bullets: verdict, where to deploy, quota there, blockers, next commands."""
    icons = _PLAIN if plain else _ICONS
    a = d.get("accelerator", {})
    sub = (_check(d, "az.context") or {}).get("evidence", {}).get("subscription_name") or "?"
    blocking = [c for c in d["checks"] if c["status"] in ("fail", "error")]
    rec = _recommend_region(d)
    quota = _check(d, "quota.models")
    checked = quota is not None and quota["status"] in ("pass", "fail", "warn", "info")
    if not blocking and not checked:
        why_not = (quota or {}).get("summary") if quota and quota["status"] == "skip" else None
        why_not = why_not or (d.get("limitations") or [None])[0] or "no live checks ran"
        verdict = "NOT CHECKED" if plain else "❔ NOT CHECKED"
        out = [f"PREFLIGHT: {verdict} · {a.get('id', '?')} · {d['mode']} mode"]
        out += [
            f"  • Why: {_t(why_not, 110)}",
            "  ▶ Fix that, then run preflight again (a GO needs the live quota check)",
        ]
        return _finish("\n".join(out), plain)
    verdict = ("NO-GO" if plain else "🛑 NO-GO") if blocking else ("GO" if plain else "✅ GO")
    out = [f"PREFLIGHT: {verdict} · {a.get('id', '?')} · subscription {sub} · {d['duration_ms'] / 1000:.0f}s"]

    qev = (_check(d, "quota.models") or {}).get("evidence", {})
    n_a = bool(qev.get("not_applicable"))
    region = rec["region"]
    if n_a:
        out.append("  ▸ Model quota: not applicable — this accelerator's Bicep deploys no Azure OpenAI models")
    else:
        out.append(f"  ▸ AI region: {region or 'none usable'} — {rec['why']}")
    env_ev = rec["env"]
    loc = env_ev.get("location")
    if loc:
        out.append(f"  ▸ Primary location: {loc} — your azd env")
    elif rec["locations"]:
        out.append(f"  ▸ Primary location: any of {', '.join(rec['locations'])}")
    rows = (qev.get("regions") or {}).get(region or "", {}).get("models", []) if region else []
    if rows:
        parts = [f"{r['model']} need {r['capacity']} / free {r['available']}{'' if r['ok'] else ' ❌'}" for r in rows]
        out.append("  ▸ Quota (K TPM, live from Azure): " + " · ".join(parts))
    if rec["others"]:
        out.append(f"  ▸ Other OK regions: {', '.join(rec['others'])}")

    names = {
        "az.context": "login",
        "providers.registration": "providers",
        "quota.models": "quota",
        "models.availability": "models/SKUs",
        "repo.tools": "tools",
        "repo.git_changes": "no local edits",
    }
    marks = []
    for cid, label in names.items():
        c = _check(d, cid)
        if cid == "quota.models" and n_a:
            continue  # nothing was checked, so no tick
        if c and c["status"] != "skip":
            marks.append(f"{icons[c['status']].strip()} {label}")
    out.append("  ▸ Checks: " + "   ".join(marks))
    tools = _check(d, "repo.tools")
    if tools and tools["status"] != "skip":
        ev = tools.get("evidence") or {}
        sym = {"ok": "OK" if plain else "✅", "old": "!!" if plain else "⚠️", "missing": "!!" if plain else "⚠️"}
        parts = []
        for r in ev.get("tools", []):
            if r["status"] == "unknown":
                continue
            have = r["installed"] or "not installed"
            need = f" (needs {r['required']})" if r.get("min") else ""
            parts.append(f"{sym.get(r['status'], '')} {r['tool']} {have}{need}".strip())
        if parts:
            out.append("  ▸ Tools (as the docs ask): " + " · ".join(parts))
        for e in ev.get("extensions", []):
            if not e["ok"]:
                out.append(f"  ▸ Azure CLI extension '{e['name']}' is not installed (docs: {e['sources'][0]})")
        extra = ev.get("other_tools") or {}
        if extra:
            out.append("  ▸ Also installed: " + ", ".join(f"{k} {v}" for k, v in sorted(extra.items())))
    if blocking:
        out.append("  • Blocking:")
        for c in blocking[:4]:
            out.append(f"      ❌ {SHORT.get(c['check_id'], c['title'])}: {_t(c['summary'], 100)}")
    else:
        out.append("  • Blocking: none")
    notes = (_check(d, "repo.config_drift") or {}).get("evidence", {}).get("findings", [])
    if notes:
        first = _t(notes[0].get("short") or notes[0]["message"], 80)
        out.append(f"  • Heads-up (not blocking): {len(notes)} doc/script note(s), e.g. {first}")
    tagc = _check(d, "repo.exemption_tag")
    if tagc and tagc["status"] == "info":
        ev = tagc.get("evidence") or {}
        out.append(
            f"  • Reminder: tag {ev.get('tag')} is not in {ev.get('template_ref')}. If your tenant policy turns Cosmos "
            "DB public access off, add it before azd up:"
        )
        out.append(f"      {ev.get('snippet')}")

    steps: List[str] = []
    if not blocking and n_a:
        steps.append("python -m accel_troubleshooter run -- azd up")
    elif not blocking and region:
        if not env_ev.get("env_name"):
            steps += ["azd env new <name>", f"azd env set AZURE_ENV_AI_SERVICE_LOCATION {region}"]
        elif env_ev.get("ai_location") != region:
            steps.append(f"azd env set AZURE_ENV_AI_SERVICE_LOCATION {region}")
        steps.append("python -m accel_troubleshooter run -- azd up")
    else:
        for act in sorted(d.get("actions", {}).values(), key=lambda x: _CAT_ORDER[x["category"]])[:2]:
            runnable = act["category"] in (READ_ONLY, APPROVAL_REQUIRED) and act["commands"]
            cmd = act["commands"][0] if runnable else ""
            if act["id"].split("#")[0] == "set-ai-region" and region and act["params"].get("env"):
                cmd = f"azd env set {act['params']['env']} {region}"  # same region as the recommendation above
            steps.append(_t(cmd, 100) if runnable else f"{act['title']} (manual; see --format full)")
        steps.append("fix the blockers above, then run preflight again")
    out += [f"  ▶ Next: {steps[0]}"] + [f"      ▶ {s}" for s in steps[1:]]
    return _finish("\n".join(out), plain)


def render_compact(d: Dict[str, Any], kind: str = "diagnose", plain: bool = False, max_findings: int = 3) -> str:
    if kind == "preflight" and not d.get("clarification"):
        return render_preflight(d, plain)
    icons = _PLAIN if plain else _ICONS
    a = d.get("accelerator", {})
    out: List[str] = []
    if d.get("clarification"):
        return _finish("\n".join(["▌ Which accelerator?", "  • " + d["clarification"]]), plain)

    title = {"preflight": "PREFLIGHT", "quota": "QUOTA CHECK", "diagnose": "DIAGNOSIS"}.get(kind, kind.upper())
    commit = (a.get("git_commit") or "")[:7]
    sub = (_check(d, "az.context") or {}).get("evidence", {}).get("subscription_name")
    out.append(f"▌{title} · {a.get('id', '?')} @ {commit or 'no-git'} · {d['mode']} · {d['duration_ms'] / 1000:.0f}s")
    t = d.get("targets", {})
    rg = f"RG {t['resource_group']} ({t['resource_group_source']})" if t.get("resource_group") else "RG not found"
    out.append(
        f"  ▸ Target: {rg} · AI region {t.get('ai_region') or '-'} · azd env {t.get('azd_env') or '-'}"
        + (f" · sub {sub}" if sub else "")
    )

    blocking = [c for c in d["checks"] if c["status"] in ("fail", "error")]
    top = d["hypotheses"][0] if d["hypotheses"] else None
    cands = (_check(d, "quota.models") or {}).get("evidence", {}).get("regions_with_sufficient_quota") or []
    if top and top["status"] in ("confirmed", "likely"):
        out.append(f"CAUSE  {top['title']}  [{top['status']} · {top['confidence']} confidence]")
    elif kind == "quota":
        out.append("VERDICT  " + ("quota is NOT sufficient" if blocking else "quota is sufficient"))
    else:
        out.append("RESULT  No confirmed or likely cause from the evidence collected (see STILL NEEDED).")

    out += ["", "CHECKS"]
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
            n_regions = len((qc["evidence"] or {}).get("regions") or {})
            out += ["", "QUOTA (K tokens/min, live from Azure)"] + lines
            if cands:
                out.append(f"  ▸ Usable AI regions: {', '.join(cands)}")
            srcs = sorted({r["capacity_source"] for r in qc["evidence"].get("requirements", [])})
            out.append(
                f"  ▸ Source: live 'az cognitiveservices usage list' in {n_regions} region(s)"
                + (f", subscription {sub}" if sub else "")
                + f"; need from {', '.join(srcs[:3])}"
            )

    findings = [h for h in d["hypotheses"] if h["status"] in ("confirmed", "likely", "possible")][:max_findings]
    if findings and kind != "preflight":
        out += ["", "FINDINGS"]
        for h in findings:
            out.append(f"  • {_t(h['title'], 96)}  [{h['status']} · {h['confidence']}]")
            for r in h["rationale"][:3]:
                out.append(f"      ◦ {_t(r, 150)}")
            refs = [x for x in dict.fromkeys(h["doc_refs"] + h["source_refs"]) if x][:3]
            if refs:
                out.append("      ↳ see " + ", ".join(refs))

    drift = (_check(d, "repo.config_drift") or {}).get("evidence", {}).get("findings", [])
    if drift:
        out += ["", "NOTES"]
        for f in drift[:3]:
            out.append(
                f"  • {_t(f.get('short') or f['message'], 130)}" + (f"  ({f['refs'][0]})" if f.get("refs") else "")
            )
        if len(drift) > 3:
            out.append(f"  • … {len(drift) - 3} more (use --format full)")

    acts = sorted(d.get("actions", {}).values(), key=lambda x: _CAT_ORDER[x["category"]])[:4]
    if acts:
        out += ["", "NEXT"]
        for act in acts:
            icon = _CAT_PLAIN[act["category"]] if plain else _CAT_ICON[act["category"]]
            cmd = _t(act["commands"][0], 95) if act["commands"] else _t(act["description"], 95)
            out.append(f"  {icon} {act['id']}: {cmd}")
        if any(a_["category"] == APPROVAL_REQUIRED and a_["id"].split("#")[0] in EXECUTABLE_ACTIONS for a_ in acts):
            out.append("  ▸ To apply a 🟡 action: fix <action-id> --approve   (you confirm; then it re-checks)")

    missing = d.get("missing_evidence", [])
    if kind == "diagnose" and top and top["status"] in ("confirmed", "likely") and _guide_gap(d, top):
        out += [
            "",
            "  ▸ The troubleshooting guide has no entry for this cause. To draft one (nothing is written):",
            '    python -m accel_troubleshooter draft-guide "<same text>" --note "what fixed it"',
        ]
    health = _check(d, "webapp.health")
    auth = _check(d, "auth.setup")
    if auth and auth["status"] in ("info", "warn") and kind == "diagnose":
        ev = auth.get("evidence") or {}
        scripts = " or ".join(x for x in (ev.get("script_ps1"), ev.get("script_sh")) if x)
        out += [
            "",
            "OPTIONAL NEXT STEP  sign-in with Microsoft Entra (" + (ev.get("guide_step") or "see the guide") + ")",
        ]
        if ev.get("post_provision_pending"):
            out.append("  ▸ First finish the post-provision steps above (images, data, agents); then:")
        else:
            out.append("  ▸ Post-provision is done, so this is the step left in the guide:")
        out += [
            f"  ▸ Run: {scripts}   (📘 manual, from the repository root; creates/updates an app registration)",
            "  ▸ Needs permission to create app registrations; if blocked, ask an admin for a client ID and run "
            f"`azd env set {ev.get('client_id_env') or 'AZURE_ENV_ENTRA_CLIENT_ID'} <client-id>` first.",
            "  ▸ Then verify it as described in "
            + ", ".join(auth.get("doc_refs", [])[:1] or ["the authentication guide"])
            + f" ('{ev.get('verify_heading') or 'Verify'}'), and re-run the health check.",
        ]
    if (
        a.get("chat_test")
        and health
        and health["status"] == "pass"
        and not _check(d, "chat.e2e")
        and kind == "diagnose"
    ):
        out += [
            "",
            "  ▸ Not proven yet: a real chat reply (health probes only say the apps are alive).",
            "    Run: python -m accel_troubleshooter chat-test   (one message, asks first, cleans up)",
        ]
    if kind == "diagnose" and not (top and top["status"] in ("confirmed", "likely")):
        close = [
            x
            for x in d.get("docs", [])
            if x.get("doc_type") == "troubleshooting-row" and x.get("category") != "summary" and x.get("score", 0) >= 15
        ][:3]
        if close:
            out += ["", "CLOSEST DOCS (keyword match, not a diagnosis)"]
            out += [f"  • {_t(x['title'], 70)}  ↳ {x['ref']}" for x in close]
        issue = d.get("issue") or {}
        raw = str(issue.get("raw_text") or "")
        terms = (issue.get("error_codes") or [None])[0] or (
            " ".join(raw.split()[:8]) if len(raw.split()) > 3 and not raw.lower().startswith("check my") else ""
        )
        if terms and a.get("upstream"):
            q = quote_plus(terms)
            out += [
                "",
                "NOT IN THE ACCELERATOR DOCS?",
                "  • No confirmed cause from the docs or the live checks. Wider search (external, please verify):",
                f"      {a['upstream']}/issues?q={q}",
                f"      https://learn.microsoft.com/search/?terms={q}",
            ]
    if missing and kind != "preflight":
        out += ["", "STILL NEEDED"]
        out += [f"  • {_t(m, 120)}" for m in missing[:4]]

    out += ["", "  ▸ More: --format full · --out report.md · --format json"]
    return _finish("\n".join(out), plain)
