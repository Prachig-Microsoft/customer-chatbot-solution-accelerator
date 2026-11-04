"""Find and classify every document in an accelerator repository, and say what was and was not read.

Used to derive a profile for an accelerator that has none, and to report documentation coverage. Troubleshooting
guides and READMEs get special attention: a guide is recognised by its name *or* its content (so a badly named
``Known problems.md`` is still found), and every README in the repository is kept, not just the root one.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DOC_EXTENSIONS = {".md", ".mdx", ".rst"}
TEXT_IN_DOCS_DIRS = {".txt"}
DOC_DIRS = {"docs", "documents", "doc", "documentation", "wiki"}
SKIP_DIRS = {
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "site-packages",
    "__pycache__",
    ".azure",
    ".cache",
    ".runs",
    "bin",
    "obj",
    "dist",
    "build",
    "target",
    ".next",
    "coverage",
    "images",
    "img",
    "assets",
}
BOILERPLATE = re.compile(
    r"^(license|licence|code_of_conduct|security|support|contributing|pull_request_template|issue_template|"
    r"notice|third_party_notices?|codeowners)(\.|$)",
    re.I,
)
MAX_FILE_BYTES = 400_000
MAX_DOCS = 600
README_NAME = re.compile(r"^readme(\.|$)", re.I)

# First match wins, so the more specific kinds come first.
DOC_KINDS = [
    (
        "troubleshooting",
        r"troubleshoot|trouble.?shoot|known.?(issue|problem)|common.?(error|issue|problem)|(?<!transparency_)faq",
    ),
    ("release_notes", r"changelog|release.?notes|whats.?new|migration|upgrade|breaking"),
    ("quota", r"quota|capacity"),
    ("auth", r"auth|entra|app.?registration|identity|sign.?in"),
    ("local_dev", r"local|devcontainer|development"),
    ("reuse", r"re.?use|delete|purge|cleanup|clean.?up"),
    ("deployment", r"deploy|setup|set.?up|provision|azd|getting.?started|install|customi[sz]|parameter|build|acr|post"),
    ("architecture", r"architecture|technical|design|overview|pipeline|how.?it.?works"),
]
_HEADING_SIGNS = re.compile(
    r"^#{1,4}\s.*(troubleshoot|common (issues|errors|problems)|known (issues|problems))", re.I | re.M
)
_TABLE_SIGNS = re.compile(
    r"^\|\s*[^|]*(error|issue|symptom|problem)[^|]*\|.*\|\s*[^|]*(resolution|solution|fix|cause|steps|remedy)",
    re.I | re.M,
)
# The troubleshooter's own files (installed copy, agent, skills) are not the accelerator's documentation.
OWN_DIRS = {"": {"troubleshooter"}, ".github": {"agents", "skills", "prompts"}}


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def has_troubleshooting_section(text: str) -> bool:
    """A heading such as 'Troubleshooting' or 'Known issues' (that section is boosted, the whole file is not)."""
    return bool(_HEADING_SIGNS.search(text))


def looks_like_troubleshooting(text: str) -> bool:
    """Content test for a whole file: a table of errors with their resolutions."""
    return bool(_TABLE_SIGNS.search(text))


def kind_of(rel: str, text: str = "") -> str:
    name = Path(rel).name
    if README_NAME.match(name):
        return "readme"
    stem = Path(rel).stem.lower()
    for kind, pattern in DOC_KINDS:
        if re.search(pattern, stem):
            return kind
    if text and looks_like_troubleshooting(text):
        return "troubleshooting"
    return "other"


def scan_docs(repo: Path, classify: bool = True) -> Dict[str, Any]:
    """Walk the repository and return {docs: {kind: [paths]}, files: [paths], skipped: [{path, reason}], found: n}.

    ``classify=False`` skips reading file contents (the cheap form used to notice newly added documents).
    """
    repo = repo.resolve()
    found: List[str] = []
    skipped: List[Dict[str, str]] = []
    for root, dirs, files in os.walk(repo):
        rel_dir = Path(root).relative_to(repo)
        here = "" if str(rel_dir) == "." else rel_dir.as_posix()
        dirs[:] = sorted(
            d
            for d in dirs
            if d.lower() not in SKIP_DIRS
            and not (d.startswith(".") and d != ".github")
            and d not in OWN_DIRS.get(here, set())
        )
        in_docs_dir = any(part.lower() in DOC_DIRS for part in rel_dir.parts)
        for name in sorted(files):
            ext = Path(name).suffix.lower()
            rel = (rel_dir / name).as_posix() if str(rel_dir) != "." else name
            if ext == ".ipynb":
                skipped.append({"path": rel, "reason": "notebook (not read)"})
                continue
            if ext not in DOC_EXTENSIONS and not (ext in TEXT_IN_DOCS_DIRS and in_docs_dir):
                continue
            if BOILERPLATE.match(name):
                skipped.append({"path": rel, "reason": "boilerplate (license, conduct, security, templates)"})
                continue
            try:
                if (Path(root) / name).stat().st_size > MAX_FILE_BYTES:
                    skipped.append({"path": rel, "reason": f"larger than {MAX_FILE_BYTES // 1000} KB"})
                    continue
            except OSError:
                skipped.append({"path": rel, "reason": "unreadable"})
                continue
            found.append(rel)
    docs: Dict[str, List[str]] = {}
    for rel in found[:MAX_DOCS]:
        docs.setdefault(kind_of(rel, _read(repo / rel) if classify else ""), []).append(rel)
    for rel in found[MAX_DOCS:]:
        skipped.append({"path": rel, "reason": f"over the limit of {MAX_DOCS} documents"})
    # the root README first: it is the entry point everything else hangs off
    if "README.md" in docs.get("readme", []):
        docs["readme"].remove("README.md")
        docs["readme"].insert(0, "README.md")
    return {"docs": docs, "files": found[:MAX_DOCS], "skipped": skipped, "found": len(found)}


_REGIONS = (
    "eastus2|eastus|westus3|westus2|westus|centralus|northcentralus|southcentralus|westcentralus|northeurope|"
    "westeurope|swedencentral|francecentral|germanywestcentral|uksouth|ukwest|southindia|centralindia|japaneast|"
    "japanwest|australiaeast|canadacentral|canadaeast|koreacentral|norwayeast|switzerlandnorth|brazilsouth|"
    "southeastasia|eastasia"
)


def readme_facts(readme: Path) -> Dict[str, Any]:
    """What the root README promises: headings, prerequisites, regions, models and the commands it tells you to run."""
    text = _read(readme)
    if not text:
        return {}
    lines = text.splitlines()
    headings = [ln.lstrip("# ").strip() for ln in lines if re.match(r"^#{1,3}\s", ln)][:30]
    prereq: Optional[str] = next(
        (h for h in headings if re.search(r"prerequisite|requirement|before you", h, re.I)), None
    )
    regions = sorted(set(re.findall(rf"\b({_REGIONS})\b", text, re.I)))
    models = sorted(set(re.findall(r"\b(gpt-[\w.-]+|text-embedding-[\w.-]+|o[134](?:-[a-z]+)?)\b", text)))
    commands = sorted({c.strip() for c in re.findall(r"`((?:azd|az)\s[^`]+)`", text)})[:15]
    return {
        "headings": headings,
        "prerequisites_section": prereq,
        "regions_mentioned": regions,
        "models_mentioned": models[:12],
        "commands": commands,
        "has_troubleshooting_section": any(re.search(r"troubleshoot|known issue|faq", h, re.I) for h in headings),
    }
