"""Repository, profile, azd-environment and IaC parsing helpers (all read-only)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PACKAGE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = PACKAGE_DIR / "profiles"
TOOL_ROOT = PACKAGE_DIR.parent
DEFAULT_WORKSPACE = TOOL_ROOT.parent


# --------------------------------------------------------------------------- profiles


def load_profiles() -> Dict[str, Dict[str, Any]]:
    profiles = {}
    for path in sorted(PROFILES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_profile_file"] = str(path.relative_to(TOOL_ROOT)).replace("\\", "/")
        profiles[data["id"]] = data
    return profiles


def resolve_repo(profile: Dict[str, Any], workspace: Path, explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the repository for a profile. Never guesses: returns None if not found."""
    if explicit:
        p = Path(explicit).resolve()
        return p if p.exists() else None
    for cand in profile.get("repo_candidates", []):
        p = (workspace / cand).resolve()
        if p.exists() and fingerprint_score(profile, p)[0] >= 2:
            return p
    return None


def fingerprint_score(profile: Dict[str, Any], repo: Path) -> tuple:
    """Score how well a directory matches a profile's fingerprint. Returns (score, reasons)."""
    fp = profile.get("fingerprint", {})
    score, reasons = 0, []
    azure_yaml = repo / "azure.yaml"
    if azure_yaml.exists():
        m = re.search(r"^name:\s*(\S+)", azure_yaml.read_text(encoding="utf-8", errors="ignore"), re.M)
        if m and m.group(1) in fp.get("azure_yaml_names", []):
            score += 2
            reasons.append(f"azure.yaml name '{m.group(1)}'")
    remote = git_info(repo).get("remote", "")
    if remote and any(pat in remote for pat in fp.get("remote_patterns", [])):
        score += 2
        reasons.append(f"git remote '{remote}'")
    markers = [m for m in fp.get("marker_files", []) if (repo / m).exists()]
    if markers:
        score += len(markers)
        reasons.append(f"marker files {markers}")
    return score, reasons


# --------------------------------------------------------------------------- git

_GIT_CACHE: Dict[str, Dict[str, str]] = {}


def git_info(repo: Path) -> Dict[str, str]:
    key = str(repo)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    info = {"remote": "", "branch": "", "commit": ""}
    if (repo / ".git").exists():
        for k, args in (
            ("remote", ["remote", "get-url", "origin"]),
            ("branch", ["rev-parse", "--abbrev-ref", "HEAD"]),
            ("commit", ["rev-parse", "--short", "HEAD"]),
        ):
            try:
                out = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    stdin=subprocess.DEVNULL,
                )
                info[k] = out.stdout.strip() if out.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                pass
    _GIT_CACHE[key] = info
    return info


def git_changed_files(repo: Path) -> List[Dict[str, str]]:
    """Uncommitted changes (porcelain). Read-only."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    files = []
    for line in out.stdout.splitlines():
        if len(line) > 3:
            files.append({"status": line[:2].strip(), "path": line[3:].strip()})
    return files


def git_diff_lines(repo: Path, path: str) -> List[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "diff", "-U0", "--", path],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        ln
        for ln in out.stdout.splitlines()
        if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---"))
    ]


# --------------------------------------------------------------------------- azd env


@dataclass
class AzdEnv:
    name: str = ""
    path: str = ""
    values: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)  # .azure/<env>/config.json infra.parameters

    @property
    def found(self) -> bool:
        return bool(self.values) or bool(self.params)

    def get(self, key: Optional[str], default: str = "") -> str:
        if not key:
            return default
        return self.values.get(key, default)


def _parse_dotenv(text: str) -> Dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        values[k.strip()] = v
    return values


def load_env_file(path: Path) -> AzdEnv:
    return AzdEnv(
        name=Path(path).stem,
        path=str(path),
        values=_parse_dotenv(Path(path).read_text(encoding="utf-8", errors="ignore")),
    )


def load_azd_env(repo: Path, env_name: Optional[str] = None) -> AzdEnv:
    azure_dir = repo / ".azure"
    if not azure_dir.exists():
        return AzdEnv()
    if not env_name:
        cfg = azure_dir / "config.json"
        if cfg.exists():
            try:
                env_name = json.loads(cfg.read_text(encoding="utf-8")).get("defaultEnvironment")
            except json.JSONDecodeError:
                env_name = None
    if not env_name:
        envs = [p.name for p in azure_dir.iterdir() if (p / ".env").exists()]
        env_name = envs[0] if len(envs) == 1 else None
    if not env_name:
        return AzdEnv()
    params: Dict[str, Any] = {}
    env_cfg = azure_dir / env_name / "config.json"
    if env_cfg.exists():
        try:
            params = (json.loads(env_cfg.read_text(encoding="utf-8")).get("infra") or {}).get("parameters") or {}
        except json.JSONDecodeError:
            params = {}
    env_file = azure_dir / env_name / ".env"
    if not env_file.exists():
        return AzdEnv(name=env_name, params=params)
    return AzdEnv(
        name=env_name,
        path=str(env_file),
        params=params,
        values=_parse_dotenv(env_file.read_text(encoding="utf-8", errors="ignore")),
    )


# --------------------------------------------------------------------------- bicep


@dataclass
class BicepParam:
    name: str
    type: str
    default: Any
    line: int
    allowed: List[str] = field(default_factory=list)
    min_value: Optional[int] = None
    usage_names: List[str] = field(default_factory=list)
    description: str = ""


def _parse_default(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    raw = raw.strip()
    if re.fullmatch(r"'[^']*'", raw):
        return raw[1:-1]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw in ("true", "false"):
        return raw == "true"
    return raw  # expression


def parse_bicep_params(path: Path) -> Dict[str, BicepParam]:
    params: Dict[str, BicepParam] = {}
    if not path.exists():
        return params
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    decorators: List[str] = []
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        m = re.match(r"^param\s+(\w+)\s+([\w?]+)(?:\s*=\s*(.+))?$", stripped)
        if m:
            deco = "\n".join(decorators)
            allowed_block = re.search(r"@allowed\(\[(.*?)\]\)", deco, re.S)
            allowed = re.findall(r"'([^']+)'", allowed_block.group(1)) if allowed_block else []
            usage_block = re.search(r"usageName:\s*\[(.*?)\]", deco, re.S)
            usage = re.findall(r"'([^']+)'", usage_block.group(1)) if usage_block else []
            minv = re.search(r"@minValue\((\d+)\)", deco)
            desc = re.search(r"@description\('([^']*)'\)", deco)
            params[m.group(1)] = BicepParam(
                name=m.group(1),
                type=m.group(2),
                default=_parse_default(m.group(3)),
                line=idx,
                allowed=allowed,
                min_value=int(minv.group(1)) if minv else None,
                usage_names=usage,
                description=desc.group(1) if desc else "",
            )
            decorators = []
        elif re.match(r"^(var|module|resource|output|targetScope|import|type|func|metadata)\s", line):
            decorators = []
        else:
            # Everything between two top-level statements belongs to the next param's decorators.
            decorators.append(line)
    return params


def parse_usage_names(entries: List[str]) -> List[Dict[str, Any]]:
    """'OpenAI.GlobalStandard.gpt-5.4-mini,50' -> {usage_name, sku, model, capacity}."""
    out = []
    for e in entries:
        name, _, cap = e.partition(",")
        parts = name.split(".", 2)
        out.append(
            {
                "usage_name": name.strip(),
                "sku": parts[1] if len(parts) > 2 else "",
                "model": parts[2] if len(parts) > 2 else name,
                "capacity": int(cap) if cap.strip().isdigit() else None,
            }
        )
    return out


def parse_parameters_env_map(path: Path) -> Dict[str, Dict[str, Optional[str]]]:
    """main.parameters.json -> {param: {env: VAR, default: X}} for ${VAR=default} values."""
    result: Dict[str, Dict[str, Optional[str]]] = {}
    if not path.exists():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return result
    for pname, pval in data.get("parameters", {}).items():
        v = pval.get("value") if isinstance(pval, dict) else None
        if isinstance(v, str):
            m = re.fullmatch(r"\$\{(\w+)(?:=([^}]*))?\}", v.strip())
            if m:
                result[pname] = {"env": m.group(1), "default": m.group(2)}
                continue
        result[pname] = {"env": None, "default": None, "literal": v}
    return result


def find_line(path: Path, pattern: str) -> Optional[int]:
    if not path.exists():
        return None
    rx = re.compile(pattern)
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        if rx.search(line):
            return i
    return None
