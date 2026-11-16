"""Local tool and version check (preflight): what the accelerator's documents ask for versus what is installed."""

from __future__ import annotations

from typing import Any, Dict, List

from ..azcli import LOCAL_TOOLS, CommandNotAllowed
from ..models import FAIL, INFO, PASS, SKIP, WARN, CheckResult
from ..prereqscan import scan_components, setup_route
from ..toolscan import FRIENDLY, scan_requirements, version_at_least
from .base import DiagContext, check, parallel_map


def _need_text(req: Dict[str, Any]) -> str:
    return f">= {req['min']}" if req.get("min") else "installed"


@check(
    "repo.tools",
    "Tools and versions the documents ask for",
    stages=["pre-deployment", "deployment"],
    needs_azure=False,
    order=3,
    opt_in=True,  # only meaningful before deploying: preflight asks for it, a health check does not
)
def tools_and_versions(ctx: DiagContext) -> CheckResult:
    title = "Tools and versions the documents ask for"
    req = scan_requirements(ctx.repo)
    probes = parallel_map(ctx, lambda t: (t, ctx.runner.local_version(t)), list(LOCAL_TOOLS), workers=6)
    installed = {t: (state, ver) for t, (state, ver, _) in probes}
    if all(state == "unknown" for state, _ in installed.values()):
        return CheckResult(
            "repo.tools", title, SKIP, "Local tool versions could not be read (offline or not recorded)."
        )
    rows: List[Dict[str, Any]] = []
    for tool, r in sorted(req["tools"].items()):
        state, ver = installed.get(tool, ("unknown", None))
        if state == "missing":
            status = "missing"
        elif state == "unknown":
            status = "unknown"
        else:
            status = "ok" if version_at_least(ver, r.get("min")) else "old"
        rows.append(
            {
                "tool": tool,
                "name": FRIENDLY.get(tool, tool),
                "required": _need_text(r),
                "min": r.get("min"),
                "installed": ver,
                "status": status,
                "hard": bool(r.get("hard")),
                "optional": bool(r.get("optional")),
                "sources": r.get("sources", []),
            }
        )
    others = {t: v for t, (s, v) in installed.items() if s == "found" and t not in req["tools"]}
    route = setup_route()
    for comp, item in sorted(scan_components(ctx.repo).items()):
        try:
            state, ver, _ = ctx.runner.system_component(comp)
        except CommandNotAllowed:
            state, ver = "unknown", None
        if state == "found":
            status = "ok" if version_at_least(ver, item.get("min")) else "old"
        else:
            status = state  # missing / unknown
        rows.append(
            {
                "tool": comp,
                "kind": "component",
                "name": item["name"],
                "required": _need_text(item),
                "min": item.get("min"),
                "installed": ver,
                "status": status,
                "hard": item["level"] == "required",
                "optional": False,
                "level": item["level"],
                "why": item["why"],
                "devcontainer": item.get("devcontainer"),
                "sources": item["sources"],
            }
        )
    ext_rows: List[Dict[str, Any]] = []
    if req["extensions"] and ctx.runner.mode != "offline":
        ok, data, _ = ctx.az("extension", "list")
        have = {e.get("name"): e.get("version") for e in (data if ok and isinstance(data, list) else [])}
        for name, sources in sorted(req["extensions"].items()):
            ext_rows.append({"name": name, "installed": have.get(name), "ok": name in have, "sources": sources})
    problems = [r for r in rows if r["status"] in ("missing", "old")]
    hard = [r for r in problems if r["hard"] or (r["tool"] in ("azd", "az") and r["status"] == "missing")]
    soft = [r for r in problems if r not in hard and not r["optional"]]
    optional = [r for r in problems if r not in hard and r["optional"]]
    missing_ext = [e for e in ext_rows if not e["ok"]]
    unverified = [
        r["name"] for r in rows if r["status"] == "unknown" and not r["optional"] and r.get("level") != "conditional"
    ]
    ev = {
        "tools": rows,
        "other_tools": others,
        "extensions": ext_rows,
        "route": route,
        "unverified_required": unverified,
    }

    def words(rs: List[Dict[str, Any]]) -> str:
        out = []
        for r in rs:
            have = "not installed" if r["status"] == "missing" else f"{r['installed']} is older"
            text = f"{r['name']} {have} ({'needed' if r.get('kind') == 'component' else 'docs'}: {r['required']}"
            if r.get("kind") == "component":
                text += f"; {r['why']}; {'azd up runs it' if r['hard'] else 'needed when that script runs'}"
                if r.get("devcontainer") and route == "local":
                    text += "; the Dev Container installs it for you"
            out.append(text + ")")
        return "; ".join(out)

    tail = f" Not verified: {', '.join(unverified)}." if unverified else ""
    if hard:
        return CheckResult("repo.tools", title, FAIL, words(hard) + "." + tail, evidence=ev)
    if soft or missing_ext:
        parts = [words(soft)] if soft else []
        if missing_ext:
            parts.append("Azure CLI extension(s) not installed: " + ", ".join(e["name"] for e in missing_ext))
        return CheckResult("repo.tools", title, WARN, "; ".join(parts) + "." + tail, evidence=ev)
    if unverified:
        return CheckResult(
            "repo.tools",
            title,
            WARN,
            f"Could not verify required prerequisite(s): {', '.join(unverified)}.",
            evidence=ev,
        )
    if optional:
        return CheckResult(
            "repo.tools",
            title,
            INFO,
            words(optional) + " (the docs mark this as optional, e.g. for the Dev Container route).",
            evidence=ev,
        )
    checked = [r for r in rows if r["status"] == "ok"]
    return CheckResult(
        "repo.tools",
        title,
        PASS,
        f"{len(checked)} tool(s) and prerequisite(s) match what the documents and scripts ask for ({route} setup).",
        evidence=ev,
    )
