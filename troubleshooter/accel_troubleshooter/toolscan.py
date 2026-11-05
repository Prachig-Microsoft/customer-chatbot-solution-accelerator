"""Which tools (and versions) the accelerator's own documents say you need, read from the repository.

Sources, in order of authority: ``azure.yaml`` ``requiredVersions`` (azd refuses to run below it), prerequisite
sections and version mentions in the README and the deployment/local-development guides, and ``az extension add``
lines in docs and scripts. Nothing is guessed: a tool is reported as required only where a document names it, and the
source file and line are kept so every claim can be checked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .docscan import scan_docs

ALIASES: Dict[str, str] = {
    "azd": r"azure developer cli|\bazd\b",
    "az": r"azure cli|\baz cli\b",
    "python": r"\bpython\b",
    "node": r"\bnode(?:\.js)?\b",
    "pwsh": r"\bpowershell\b|\bpwsh\b",
    "docker": r"\bdocker\b",
    "git": r"\bgit\b",
    "dotnet": r"\.net(?: sdk)?\b|\bdotnet\b",
    "terraform": r"\bterraform\b",
    "kubectl": r"\bkubectl\b",
}
FRIENDLY = {
    "azd": "Azure Developer CLI (azd)",
    "az": "Azure CLI (az)",
    "python": "Python",
    "node": "Node.js",
    "pwsh": "PowerShell 7+ (pwsh)",
    "docker": "Docker",
    "git": "Git",
    "dotnet": ".NET SDK",
    "terraform": "Terraform",
    "kubectl": "kubectl",
}
_PREREQ_HEADING = re.compile(
    r"prerequisite|requirement|before you|what you need|tools|install|set.?up|getting started|local", re.I
)
_OPTIONAL = re.compile(
    r"\boption(?:al|\b)|alternativ|dev ?container|codespace|only if|if you (?:use|prefer|want)", re.I
)
_HEADING = re.compile(r"^#{1,6}\s+(.*)")
_EXT_ADD = re.compile(r"az\s+extension\s+add\s+(?:--name|-n)\s+([\w-]+)")
_DOC_KINDS = ("deployment", "local_dev", "readme")


def parse_version(text: Optional[str]) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:4])


def version_at_least(installed: Optional[str], minimum: Optional[str]) -> bool:
    if not minimum:
        return True
    a, b = parse_version(installed), parse_version(minimum)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


def _mention_pattern(alias: str) -> "re.Pattern[str]":
    # the tool name, a little punctuation (link syntax, a colon, '>='), then a version such as 3.9 / 7 / 1.23.11
    return re.compile(rf"(?:{alias})[^0-9\n]{{0,12}}?(?:>=|≥|version\s*)?\s*(\d+(?:\.\d+){{0,2}})(?!\d)", re.I)


def _azure_yaml_requirements(repo: Path) -> Dict[str, Dict[str, Any]]:
    path = repo / "azure.yaml"
    if not path.exists():
        return {}
    out: Dict[str, Dict[str, Any]] = {"azd": {"min": None, "hard": False, "optional": False, "sources": ["azure.yaml"]}}
    in_block = False
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        if re.match(r"^requiredVersions\s*:", line):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^\s+(\w+)\s*:\s*[\"']?\s*(?:>=|>|=|~|\^)?\s*(\d+(?:\.\d+)*)", line)
            if m and m.group(1) == "azd":
                out["azd"] = {"min": m.group(2), "hard": True, "optional": False, "sources": [f"azure.yaml:{n}"]}
            elif line.strip() and not line.startswith((" ", "\t")):
                in_block = False
    return out


def _doc_files(repo: Path) -> List[str]:
    docs = scan_docs(repo)["docs"]
    files: List[str] = []
    for kind in _DOC_KINDS:
        for rel in docs.get(kind, []):
            if kind == "readme" and rel != "README.md":
                continue  # sub-project READMEs describe samples, not what you need to deploy
            files.append(rel)
    return files


def scan_requirements(repo: Path) -> Dict[str, Any]:
    """{"tools": {name: {min, hard, optional, sources}}, "extensions": {name: [sources]}}."""
    tools = _azure_yaml_requirements(repo)
    patterns = {t: _mention_pattern(a) for t, a in ALIASES.items()}
    bare = {t: re.compile(a, re.I) for t, a in ALIASES.items()}
    extensions: Dict[str, List[str]] = {}
    mentions: Dict[str, List[Dict[str, Any]]] = {}
    for rel in _doc_files(repo):
        lines = (repo / rel).read_text(encoding="utf-8", errors="ignore").splitlines()
        heading = ""
        for n, line in enumerate(lines, start=1):
            h = _HEADING.match(line)
            if h:
                heading = h.group(1)
                continue
            context = " ".join(lines[max(0, n - 4) : n])
            for tool in ALIASES:
                m = patterns[tool].search(line)
                named = bare[tool].search(line) is not None
                if not named:
                    continue
                if not m and not _PREREQ_HEADING.search(heading):
                    continue  # a passing mention outside a prerequisites section is not a requirement
                mentions.setdefault(tool, []).append(
                    {
                        "min": m.group(1) if m else None,
                        "optional": bool(_OPTIONAL.search(context)),
                        "source": f"{rel}:{n}",
                    }
                )
    for tool, found in mentions.items():
        mins = [f["min"] for f in found if f["min"]]
        best = max(mins, key=parse_version) if mins else None
        by_min: Dict[str, List[str]] = {}
        for f in found:
            if f["min"]:
                by_min.setdefault(f["min"], []).append(f["source"])
        cur = tools.get(tool)
        if cur and cur.get("hard"):
            cur["sources"] += [f["source"] for f in found][:2]
            cur["mins"] = by_min
            continue
        tools[tool] = {
            "min": best,
            "hard": False,
            "optional": all(f["optional"] for f in found),
            "sources": [f["source"] for f in found][:3],
            "mins": by_min,
        }
    tools.setdefault("az", {"min": None, "hard": False, "optional": False, "sources": ["needed by azd and this tool"]})
    scripts = [p for pat in ("infra/scripts/**/*", "scripts/**/*") for p in sorted(repo.glob(pat)) if p.is_file()]
    for rel_path in [repo / r for r in _doc_files(repo)] + scripts:
        if rel_path.suffix.lower() not in (".md", ".sh", ".ps1", ".py", ".mdx"):
            continue
        for n, line in enumerate(rel_path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for name in _EXT_ADD.findall(line):
                extensions.setdefault(name, []).append(f"{rel_path.relative_to(repo).as_posix()}:{n}")
    return {"tools": tools, "extensions": {k: v[:2] for k, v in extensions.items()}}
