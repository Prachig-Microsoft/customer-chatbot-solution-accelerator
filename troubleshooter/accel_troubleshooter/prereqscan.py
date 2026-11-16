"""System components an accelerator needs on the machine that runs ``azd up``, found in its own files.

Some prerequisites are never written in a "Prerequisites" list: a post-provision hook installs ``pyodbc`` from a
requirements file, and ``pyodbc`` needs a Microsoft ODBC driver on the machine, which only the Dev Container installs.
This module finds such needs from the repository, independent of any accelerator:

* ``azure.yaml`` hooks: the requirements files they ``pip install`` and the scripts they run make a need **required**
  (``azd up`` runs them);
* other requirements files and scripts under ``infra/`` or ``scripts/``: **conditional** (needed when that script runs);
* ``src/`` and other app code: not counted (it runs in the app's own image in Azure);
* ``.devcontainer/devcontainer.json`` features name the component and its version (e.g. ``mssql-odbc-driver`` 18): the
  documented version, and proof that the Dev Container route installs it for you.

Only packages in ``NATIVE`` are mapped to components; extend the table to cover more.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .docscan import SKIP_DIRS

# Python package -> the system component it needs, and why.
NATIVE: Dict[str, Dict[str, str]] = {
    "pyodbc": {
        "component": "odbc-sql",
        "why": "the Python package pyodbc connects through an ODBC driver installed on the machine",
    },
    "aioodbc": {
        "component": "odbc-sql",
        "why": "the Python package aioodbc (async pyodbc) connects through an ODBC driver installed on the machine",
    },
    "turbodbc": {
        "component": "odbc-sql",
        "why": "the Python package turbodbc connects through an ODBC driver installed on the machine",
    },
}
# Dev Container feature id fragment -> component.
FEATURES = {"mssql-odbc-driver": "odbc-sql", "odbc": "odbc-sql"}
NAMES = {"odbc-sql": "Microsoft ODBC Driver for SQL Server"}
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(?:[=<>!~]=?.*)?$")
_HOOK_FILE = re.compile(r"[\w./\\-]+\.(?:txt|ps1|sh|py)")


def setup_route() -> str:
    """Where this check runs: codespaces, devcontainer or local."""
    if os.environ.get("CODESPACES", "").lower() == "true":
        return "codespaces"
    if os.environ.get("REMOTE_CONTAINERS") or os.environ.get("DEVCONTAINER") or Path("/.dockerenv").exists():
        return "devcontainer"
    return "local"


def _hook_files(repo: Path) -> List[str]:
    """Files the azure.yaml hooks use (requirements files and scripts), as repo-relative posix paths."""
    path = repo / "azure.yaml"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^hooks\s*:\s*\n((?:[ \t].*\n|\s*\n)*)", text, re.M)
    block = m.group(1) if m else ""
    out = []
    for hit in _HOOK_FILE.findall(block):
        rel = hit.replace("\\", "/").lstrip("./")
        if (repo / rel).is_file():
            out.append(rel)
    return list(dict.fromkeys(out))


def _skip(rel: Path) -> bool:
    parts = {p.lower() for p in rel.parts[:-1]}
    return bool(parts & SKIP_DIRS) or (rel.parts and rel.parts[0] == "troubleshooter")


def _locus(rel: str, hooked: List[str]) -> Optional[str]:
    if rel in hooked:
        return "required"
    if rel.startswith(("infra/", "scripts/", "infra\\", "scripts\\")):
        return "conditional"
    return None  # app code: runs in its own image


def _scripts_called(repo: Path, hooked: List[str]) -> List[str]:
    """Scripts a hooked script calls by name (one level), so a need inside them is also required."""
    out = []
    for rel in hooked:
        if not rel.endswith((".ps1", ".sh")):
            continue
        base = (repo / rel).parent
        for name in re.findall(r"[\w.-]+\.(?:ps1|sh|py)", (repo / rel).read_text(encoding="utf-8", errors="ignore")):
            cand = base / name
            if cand.is_file():
                out.append(cand.relative_to(repo).as_posix())
    return list(dict.fromkeys(out))


def scan_components(repo: Path) -> Dict[str, Dict[str, Any]]:
    """{component: {name, level: required|conditional, why, sources, min, devcontainer}}."""
    repo = repo.resolve()
    hooked = _hook_files(repo)
    hooked += [s for s in _scripts_called(repo, hooked) if s not in hooked]
    found: Dict[str, Dict[str, Any]] = {}

    def add(component: str, level: str, why: str, source: str) -> None:
        item = found.setdefault(
            component,
            {
                "name": NAMES.get(component, component),
                "level": level,
                "why": why,
                "sources": [],
                "min": None,
                "devcontainer": None,
            },
        )
        if level == "required":
            item["level"] = "required"
        if source not in item["sources"]:
            item["sources"].append(source)

    for path in sorted(repo.rglob("*requirements*.txt")):
        rel = path.relative_to(repo)
        if _skip(rel):
            continue
        level = _locus(rel.as_posix(), hooked)
        if not level:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            m = _REQ_LINE.match(line.split("#", 1)[0])
            pkg = m.group(1).lower() if m else ""
            if pkg in NATIVE:
                add(NATIVE[pkg]["component"], level, NATIVE[pkg]["why"], f"{rel.as_posix()}:{n}")
    for pattern in ("infra/**/*", "scripts/**/*"):
        for path in sorted(repo.glob(pattern)):
            if not path.is_file() or path.suffix.lower() not in (".ps1", ".sh", ".py"):
                continue
            rel = path.relative_to(repo)
            if _skip(rel):
                continue
            level = _locus(rel.as_posix(), hooked) or "conditional"
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                for pkg, spec in NATIVE.items():
                    if re.search(rf"(?<![\w-]){re.escape(pkg)}(?![\w-])", line):
                        add(spec["component"], level, spec["why"], f"{rel.as_posix()}:{n}")
                        break
    dc = repo / ".devcontainer" / "devcontainer.json"
    if dc.is_file():
        text = dc.read_text(encoding="utf-8", errors="ignore")
        try:
            features = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M)).get("features") or {}
        except ValueError:
            features = {}
        for fid, opts in features.items():
            comp = next((c for frag, c in FEATURES.items() if frag in fid.lower()), None)
            if not comp:
                continue
            line = next((i for i, ln in enumerate(text.splitlines(), 1) if fid in ln), 1)
            version = str((opts or {}).get("version") or "") if isinstance(opts, dict) else ""
            if comp in found:
                found[comp]["devcontainer"] = f".devcontainer/devcontainer.json:{line}"
                if version.isdigit():
                    found[comp]["min"] = version
    for item in found.values():
        item["sources"] = item["sources"][:3]
    return found
