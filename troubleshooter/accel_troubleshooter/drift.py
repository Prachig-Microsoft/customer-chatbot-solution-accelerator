"""Documentation drift: where the docs and scripts disagree with the Bicep and parameter files (offline, read-only).

Findings share the format of ``repo.config_drift`` (severity / id / message / refs) plus ``scope`` ("docs"), an
``impact`` used only to order the drift report, and a ``fix`` sentence naming the file and line to edit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from .repo import parse_bicep_params, parse_parameters_env_map

_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*(.*?)\s*\|?\s*$")
_MODEL = re.compile(r"\b(gpt-\d[\w.\-]*|text-embedding-[\w.\-]+)")
_LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)\s#]+)(?:#[^)]*)?\)")
_SCRIPT_PATH = re.compile(r"\b(infra/scripts/[\w./-]+\.(?:ps1|sh|py))\b")
_BUILTIN_ENV = {"AZURE_PRINCIPAL_TYPE", "AZURE_PRINCIPAL_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID"}
_SOURCE_SUFFIXES = {".json", ".bicep", ".ps1", ".sh", ".py", ".yaml", ".yml", ".md"}


def parse_param_table(text: str) -> List[Dict[str, Any]]:
    """Rows of a docs table whose first cell is an environment variable: name, type, example value, purpose, line."""
    rows = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _ROW.match(line)
        if not m:
            continue
        name, typ, example, purpose = m.groups()
        ex = re.fullmatch(r"\s*`([^`]*)`\s*", example)
        value = ex.group(1).strip() if ex else None
        if value is not None and (value.startswith("<") or not value):
            value = None
        rows.append({"name": name, "type": typ.strip(), "example": value, "purpose": purpose, "line": i})
    return rows


def _finding(
    severity: str, fid: str, message: str, refs: List[str], fix: str, impact: Optional[str] = None, short: str = ""
) -> Dict[str, Any]:
    out = {
        "severity": severity,
        "impact": impact or severity,
        "scope": "docs",
        "id": fid,
        "message": message,
        "refs": [r for r in refs if r],
        "fix": fix,
    }
    if short:
        out["short"] = short
    return out


def _referenced_names(repo: Path, names: set) -> set:
    """Which of the names appear anywhere in infra/ or azure.yaml (outside the docs table itself)."""
    found: set = set()
    roots = [repo / "infra", repo / "azure.yaml"]
    for root in roots:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []
        for p in files:
            if p.suffix not in _SOURCE_SUFFIXES or p.stat().st_size > 2_000_000 or p.name == "main.json":
                continue  # main.json is the compiled ARM copy of the Bicep
            text = p.read_text(encoding="utf-8", errors="ignore")
            found |= {n for n in names if n in text}
            if found >= names:
                return found
    return found


def _param_findings(ctx, doc_rel: str) -> List[Dict[str, Any]]:
    iac = ctx.profile.get("iac", {})
    path = ctx.repo / doc_rel
    if not path.is_file():
        return []
    rows = parse_param_table(path.read_text(encoding="utf-8", errors="ignore"))
    params = parse_bicep_params(ctx.repo / iac.get("main", "infra/main.bicep"))
    env_map = parse_parameters_env_map(ctx.repo / iac.get("parameters", "infra/main.parameters.json"))
    by_env = {e["env"]: (p, e) for p, e in env_map.items() if e.get("env")}
    main_rel = iac.get("main", "infra/main.bicep")
    out: List[Dict[str, Any]] = []
    unmatched = []
    for row in rows:
        hit = by_env.get(row["name"])
        if not hit:
            unmatched.append(row)
            continue
        pname, entry = hit
        bicep = params.get(pname)
        if entry.get("default"):
            default, src = entry["default"], f"{iac.get('parameters')} default"
        elif entry.get("literal") not in (None, ""):
            default, src = entry["literal"], f"{iac.get('parameters')} value"
        elif bicep is not None and bicep.default is not None:
            default, src = bicep.default, f"{main_rel}:{bicep.line}"
        else:
            default, src = None, ""
        ref_doc = f"{doc_rel}:{row['line']}"
        if row["example"] is not None and default is not None:
            if str(row["example"]).strip().lower() != str(default).strip().lower():
                numeric = str(default).isdigit()
                out.append(
                    _finding(
                        "low",
                        "doc-param-default-mismatch",
                        f"{doc_rel} shows {row['name']} = `{row['example']}` but the template default is "
                        f"`{default}` ({src}).",
                        [ref_doc, src.split(" ")[0] if ":" in src else ""],
                        f"Edit {ref_doc}: change `{row['example']}` to `{default}` (or change the template default).",
                        impact="medium" if numeric else "low",
                        short=f"{row['name']}: docs say `{row['example']}`, template default is `{default}`",
                    )
                )
        allowed_doc = re.search(r"allowed:\s*([^)]*)\)", row["purpose"], re.I)
        if allowed_doc and bicep is not None and bicep.allowed:
            doc_vals = set(re.findall(r"`([^`]+)`", allowed_doc.group(1)))
            if doc_vals and doc_vals != set(bicep.allowed):
                out.append(
                    _finding(
                        "low",
                        "doc-param-allowed-mismatch",
                        f"{doc_rel} lists allowed values {sorted(doc_vals)} for {row['name']} but {main_rel}:"
                        f"{bicep.line} allows {bicep.allowed}.",
                        [ref_doc, f"{main_rel}:{bicep.line}"],
                        f"Edit {ref_doc}: list {', '.join(bicep.allowed)}.",
                    )
                )
        minimum = re.search(r"minimum:\s*(\d+)", row["purpose"], re.I)
        if minimum and bicep is not None and bicep.min_value is not None and int(minimum.group(1)) != bicep.min_value:
            out.append(
                _finding(
                    "low",
                    "doc-param-minimum-mismatch",
                    f"{doc_rel} says the minimum for {row['name']} is {minimum.group(1)} but {main_rel}:{bicep.line} "
                    f"sets @minValue({bicep.min_value}).",
                    [ref_doc, f"{main_rel}:{bicep.line}"],
                    f"Edit {ref_doc}: change the minimum to {bicep.min_value}.",
                )
            )
    used = _referenced_names(ctx.repo, {r["name"] for r in unmatched})
    for row in unmatched:
        if row["name"] not in used:
            out.append(
                _finding(
                    "low",
                    "doc-param-unused",
                    f"{doc_rel} documents {row['name']} but nothing under infra/ or azure.yaml reads it.",
                    [f"{doc_rel}:{row['line']}"],
                    f"Remove the row at {doc_rel}:{row['line']}, or wire the variable into "
                    f"{iac.get('parameters', 'main.parameters.json')}.",
                )
            )
    documented = {r["name"] for r in rows}
    for pname, entry in env_map.items():
        env = entry.get("env")
        if env and env not in documented and env not in _BUILTIN_ENV:
            out.append(
                _finding(
                    "low",
                    "doc-param-undocumented",
                    f"{iac.get('parameters')} reads {env} (parameter {pname}) but {doc_rel} does not document it.",
                    [ctx.doc_ref(iac.get("parameters", ""), re.escape(env)), doc_rel],
                    f"Add a row for {env} to the table in {doc_rel}.",
                )
            )
    scripts = _script_env_vars(ctx.repo)
    for name, (rel, line) in sorted(scripts.items()):
        if name not in documented and name not in by_env:
            out.append(
                _finding(
                    "low",
                    "doc-param-undocumented",
                    f"{rel}:{line} reads {name} but {doc_rel} does not document it.",
                    [f"{rel}:{line}", doc_rel],
                    f"Add a row for {name} to the table in {doc_rel}, or document where it is set.",
                    short=f"{name} is used by {rel.rsplit('/', 1)[-1]} but not documented",
                )
            )
    return out


def _script_env_vars(repo: Path) -> Dict[str, tuple]:
    """AZURE_ENV_* names used in the deployment scripts: name -> (file, first line)."""
    found: Dict[str, tuple] = {}
    root = repo / "infra" / "scripts"
    for p in sorted(root.rglob("*")) if root.exists() else []:
        if p.suffix not in (".ps1", ".sh", ".py") or not p.is_file():
            continue
        rel = p.relative_to(repo).as_posix()
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for name in re.findall(r"\b(AZURE_ENV_[A-Z0-9_]+)\b", line):
                found.setdefault(name, (rel, i))
    return found


def _model_findings(ctx, reqs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Models named in the quota docs that the template does not deploy (their examples may mislead)."""
    out = []
    deployed = {str(r["model"]) for r in reqs if r.get("model")}
    for rel in ctx.profile.get("docs", {}).get("quota", []):
        path = ctx.repo / rel
        if not path.is_file():
            continue
        seen: Dict[str, List[int]] = {}
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for m in _MODEL.findall(line):
                name = m.rstrip(".-")
                if name not in deployed:
                    seen.setdefault(name, []).append(i)
        if seen:
            first = min(min(v) for v in seen.values())
            names = ", ".join(sorted(seen))
            out.append(
                _finding(
                    "low",
                    "doc-models-not-deployed",
                    f"{rel} mentions {names}, which this template does not deploy (it deploys "
                    f"{', '.join(sorted(deployed))}).",
                    [f"{rel}:{first}"],
                    f"Use {', '.join(sorted(deployed))} in the examples at {rel} (lines "
                    f"{', '.join(str(n) for n in sorted({n for v in seen.values() for n in v})[:6])}).",
                    short=f"{rel.rsplit('/', 1)[-1]} examples use {names}, not the deployed models",
                )
            )
    return out


def _doc_files(ctx) -> List[str]:
    seen, out = set(), []
    for rels in ctx.profile.get("docs", {}).values():
        for rel in rels:
            if rel not in seen and rel.endswith(".md") and (ctx.repo / rel).is_file():
                seen.add(rel)
                out.append(rel)
    return out


def _reference_findings(ctx) -> List[Dict[str, Any]]:
    """Broken relative links and scripts that the docs tell you to run but the repo does not contain."""
    out: List[Dict[str, Any]] = []
    for rel in _doc_files(ctx):
        base = (ctx.repo / rel).parent
        broken, missing = [], []
        for i, line in enumerate((ctx.repo / rel).read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for target in _LINK.findall(line):
                # GitHub resolves "/docs/x.md" from the repository root and "x.md" from the document's folder
                resolved = ctx.repo / unquote(target).lstrip("/") if target.startswith("/") else base / unquote(target)
                if not resolved.exists():
                    broken.append((i, target))
            for script in _SCRIPT_PATH.findall(line):
                if not (ctx.repo / script).is_file() and (i, script) not in missing:
                    missing.append((i, script))
        if missing:
            line, script = missing[0]
            out.append(
                _finding(
                    "low",
                    "doc-script-missing",
                    f"{rel} tells you to use {script} but that file does not exist"
                    + (f" (+{len(missing) - 1} more)." if len(missing) > 1 else "."),
                    [f"{rel}:{line}"],
                    f"Fix the path at {rel}:{line}, or restore the script.",
                    impact="high",
                )
            )
        if broken:
            line, target = broken[0]
            out.append(
                _finding(
                    "low",
                    "doc-broken-link",
                    f"{rel} has {len(broken)} relative link(s) to files that do not exist, e.g. {target}.",
                    [f"{rel}:{line}"],
                    f"Fix or remove the link at {rel}:{line}"
                    + (f" (also lines {', '.join(str(n) for n, _ in broken[1:5])})." if len(broken) > 1 else "."),
                    short=f"{rel.rsplit('/', 1)[-1]}: {len(broken)} broken link(s), e.g. {target}",
                )
            )
    return out


def collect_doc_drift(ctx, reqs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    doc = ctx.profile.get("iac", {}).get("param_doc")
    if doc:
        findings += _param_findings(ctx, doc)
    findings += _model_findings(ctx, reqs)
    findings += _reference_findings(ctx)
    return findings


_IMPACT_RANK = {"high": 0, "medium": 1, "low": 2}


def build_drift_report(diag: Dict[str, Any]) -> Dict[str, Any]:
    """Findings of ``repo.config_drift`` as an ordered, fix-oriented list (env-specific findings are left out)."""
    chk = next((c for c in diag.get("checks", []) if c["check_id"] == "repo.config_drift"), None)
    raw = [f for f in ((chk or {}).get("evidence") or {}).get("findings", []) if not f["id"].startswith("env-")]
    items = []
    for f in raw:
        where = (f.get("refs") or [""])[0]
        items.append(
            {
                "id": f["id"],
                "impact": f.get("impact") or f["severity"],
                "where": where,
                "file": where.split(":")[0],
                "what": f.get("short") or f["message"],
                "detail": f["message"],
                "fix": f.get("fix") or FIX_HINTS.get(f["id"], ""),
                "refs": f.get("refs", []),
            }
        )
    items.sort(key=lambda i: (_IMPACT_RANK.get(i["impact"], 3), i["where"]))
    acc = diag.get("accelerator", {})
    return {
        "accelerator": acc.get("name") or acc.get("id"),
        "accelerator_id": acc.get("id"),
        "commit": (acc.get("git_commit") or "")[:7],
        "branch": acc.get("git_branch") or "",
        "findings": items,
        "files": sorted({i["file"] for i in items if i["file"]}),
    }


def render_drift_compact(report: Dict[str, Any], plain: bool = False) -> str:
    dot, arrow, bar = ("*", "->", "#") if plain else ("•", "↳", "▌")
    items = report["findings"]
    head = f"{bar}DOC DRIFT {'|' if plain else '·'} {report['accelerator']} @ {report['commit'] or 'no-git'}"
    if not items:
        return f"{head}\n  {dot} No drift found: docs and scripts match the Bicep and parameter files."
    out = [
        head + f" {'|' if plain else '·'} {len(items)} finding(s) in {len(report['files'])} file(s)",
        f"  {'>' if plain else '▸'} Docs and scripts compared with the Bicep and parameter files (offline, no Azure)",
    ]
    for level in ("high", "medium", "low"):
        group = [i for i in items if i["impact"] == level]
        if not group:
            continue
        out += ["", f"{level.upper()} ({len(group)})"]
        for i in group:
            out.append(f"  {dot} {i['where'] or i['file']}  {i['what'][:150]}")
            if i["fix"]:
                out.append(f"      {arrow} {i['fix'][:170]}")
    out += ["", f"  {'>' if plain else '▸'} PR-ready checklist: drift --format full --out drift.md"]
    return "\n".join(out)


def render_drift_markdown(report: Dict[str, Any]) -> str:
    out = [
        "# Documentation drift report",
        "",
        f"Accelerator: **{report['accelerator']}** · branch `{report['branch'] or '-'}` · "
        f"commit `{report['commit'] or '-'}`",
        "",
        "Docs and scripts were compared with `infra/main.bicep` and `infra/main.parameters.json` "
        "(generated offline by the accelerator troubleshooter; each item is a suggestion to review).",
        "",
    ]
    if not report["findings"]:
        return "\n".join(out + ["No drift found."])
    for path in report["files"] or [""]:
        group = [i for i in report["findings"] if i["file"] == path]
        out += [f"## `{path}`", ""]
        for i in group:
            out.append(f"- [ ] **{i['impact']}** `{i['where']}` — {i['detail']}")
            if i["fix"]:
                out.append(f"  - Fix: {i['fix']}")
        out.append("")
    return "\n".join(out)


# Fix sentences for the older repo.config_drift findings (they are built elsewhere, so the hints live here).
FIX_HINTS = {
    "capacity-exceeds-azd-usage-metadata": "Raise the usageName capacity in the @metadata of the AI location "
    "parameter to match the default capacity, so azd only offers regions with enough quota.",
    "sku-differs-from-usage-metadata": "Make the usageName deployment type match the deployment type parameter.",
    "capacity-below-min": "Raise the capacity to at least the @minValue.",
    "doc-capacity-mismatch": "Update the 'Default Models' list in the quota doc to the template's defaults.",
    "doc-regions-not-allowed": "Remove regions the template cannot deploy from the doc, or add them to @allowed in "
    "main.bicep.",
    "script-capacity-mismatch": "Change the script's default capacity to the template's default.",
    "script-regions-not-allowed": "Remove regions the template cannot deploy from the script's DEFAULT_REGIONS, or add "
    "them to @allowed in main.bicep.",
    "env-ai-region-not-allowed": "Choose an allowed region: azd env set AZURE_ENV_AI_SERVICE_LOCATION <region>.",
}
