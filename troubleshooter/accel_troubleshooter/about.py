"""'What is this accelerator?': name, a short description taken from its own README, services, docs and repo info."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .repo import git_info, parse_bicep_params

_PROSE_SKIP = ("#", "[", "!", "<", "|", "-", "*", ">", "`", "=")
_OVERVIEW = re.compile(r"overview|about|introduction|what is", re.I)


def _clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]{1,3}", "", text)
    return " ".join(text.split())


def _paragraph_after(lines: List[str], start: int) -> Optional[Tuple[int, str]]:
    para: List[str] = []
    first = 0
    for i in range(start, len(lines)):
        s = lines[i].strip()
        if not s:
            if para:
                break
            continue
        if s.startswith("#") and para:
            break
        if s.startswith(_PROSE_SKIP) or s.startswith("---"):
            if para:
                break
            continue
        if not para:
            first = i + 1
        para.append(s)
    text = _clean(" ".join(para))
    return (first, text) if len(text) >= 60 else None


def _blank_code_fences(lines: List[str]) -> List[str]:
    """Same line count, but everything inside ``` fences is blanked so diagrams/code never read as prose."""
    out, fenced = [], False
    for line in lines:
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
            out.append("")
        else:
            out.append("" if fenced else line)
    return out


def readme_summary(repo: Path, sentences: int = 3, limit: int = 480) -> Tuple[str, str]:
    """First sentences of the README's overview paragraph, plus the 'README.md:line' they came from."""
    path = repo / "README.md"
    if not path.exists():
        return "", ""
    lines = _blank_code_fences(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    found = None
    for i, line in enumerate(lines):
        if re.match(r"^#{2,3}\s", line) and _OVERVIEW.search(line):
            found = _paragraph_after(lines, i + 1)
            if found:
                break
    found = found or _paragraph_after(lines, 0)
    if not found:
        return "", ""
    line_no, text = found
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    out = ""
    for part in parts[:sentences]:
        if out and len(out) + len(part) > limit:
            break
        out = f"{out} {part}".strip()
    if out.endswith(":"):
        out = out[:-1] + "."
    return out, f"README.md:{line_no}"


def build_about(ts, accel: str, repo: Optional[Path]) -> Dict[str, Any]:
    prof = ts.profiles[accel]
    info: Dict[str, Any] = {
        "id": accel,
        "name": prof["name"],
        "upstream": prof.get("upstream_repo"),
        "services": prof.get("services", []),
        "description": "",
        "description_source": "",
        "flavors": [],
        "docs": [],
        "troubleshooting_entries": 0,
        "git": {},
        "repo_found": repo is not None,
    }
    if repo is None:
        return info
    info["description"], info["description_source"] = readme_summary(repo)
    info["git"] = git_info(repo)
    iac = prof.get("iac", {})
    params = parse_bicep_params(repo / iac.get("main", "infra/main.bicep"))
    flavor = params.get("deploymentFlavor")
    if flavor and flavor.allowed:
        info["flavors"] = list(flavor.allowed)
    key_docs = []
    for kind in ("troubleshooting", "deployment", "quota"):
        for rel in prof.get("docs", {}).get(kind, [])[:1]:
            if (repo / rel).exists():
                key_docs.append(rel)
    info["docs"] = key_docs
    idx = ts.index_for(accel)
    if idx is not None:
        rows = {
            (p.path, p.line_start)
            for p in idx.passages
            if p.doc_type == "troubleshooting-row" and p.category != "summary"
        }
        info["troubleshooting_entries"] = len(rows)
    return info


def render_about(info: Dict[str, Any], plain: bool = False) -> str:
    dot, arrow, mid = ("*", "->", "|") if plain else ("•", "↳", "·")
    git = info.get("git") or {}
    where = f" @ {git.get('branch')} {str(git.get('commit', ''))[:7]}".rstrip() if git else ""
    out = [f"{'#' if plain else '▌'}ABOUT {mid} {info['name']} ({info['id']}){where}"]
    if not info["repo_found"]:
        out.append(f"  {dot} Repository not found locally. Clone {info['upstream']} next to this workspace.")
        return "\n".join(out)
    if info["description"]:
        out.append(f"  {dot} What it is: {info['description']}")
        out.append(f"      {arrow} {info['description_source']}")
    if info["services"]:
        out.append(f"  {dot} Azure services: {', '.join(info['services'])}")
    if info["flavors"]:
        out.append(f"  {dot} Deployment flavors: {', '.join(info['flavors'])}")
    docs = []
    for rel in info["docs"]:
        name = rel.rsplit("/", 1)[-1]
        if "roubleshoot" in name and info["troubleshooting_entries"]:
            name += f" ({info['troubleshooting_entries']} error entries)"
        docs.append(name)
    if docs:
        out.append(f"  {dot} Docs I search first: {f' {mid} '.join(docs)}")
    out.append(f"  {dot} Source repo: {info['upstream']}")
    out.append(
        f"  {'>' if plain else '▸'} Ask next: health check {mid} preflight (before azd up) {mid} why is this failing"
    )
    return "\n".join(out)
